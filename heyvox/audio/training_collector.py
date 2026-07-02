"""
Automatic training data collection for wake word improvement.

Collects labeled audio clips across all 4 categories during normal operation:
  - **tp/** True Positives  — confirmed wake word triggers (start + stop)
  - **fp/** False Positives — triggers that led to garbled/cancelled recordings
  - **tn/** True Negatives  — high-scoring non-triggers (hard negatives)
  - **fn/** False Negatives — missed wake words (detected via STT strip or retry pattern)

Clips are saved as 2-second 16kHz mono WAVs with naming:
    {category}_{suffix?}_{mic-tag?}_{timestamp}_score{score:.2f}.wav

The mic tag (sanitized device name, e.g. "jabra-elite-7-pro") is included
when a `get_mic_name` callback is supplied. This lets retraining filter
or balance by recording device to avoid one mic's timbre dominating.

Enable via config:
    wake_words:
      collect_negatives: true  # enables all training data collection
"""

import logging
import re
import time
from pathlib import Path
from typing import Callable

import numpy as np

logger = logging.getLogger(__name__)

_MIN_SPEECH_RMS = 300
_MIC_TAG_MAX_LEN = 40

# Subtype prefix inside a category dir, e.g. "fn_stop_..." / "tp_start_...".
# Used by _prune to balance deletions across subtypes (DEF-157).
_SUBTYPE_RE = re.compile(r"^(?:tp|fp|tn|fn)_(start|stop)_")


def classify_stop_outcome(
    stop_reason: str, end_stripped: bool, has_text: bool
) -> str | None:
    """Decide the training label for a finished recording's audio tail.

    Returns "tp", "fn", or None (don't collect). Centralised so the
    labeling rules are unit-testable away from the recording pipeline:

    - A recording ended BY the stop wake word is a confirmed detection →
      "tp". This includes transcripts that still needed a text-level
      wake-word strip — that is the DEF-091 imperfect-audio-trim case,
      NOT a miss (mislabeling it as FN was DEF-155).
    - A recording ended any other way (silence_timeout, ptt_interrupt,
      max_duration) whose transcript had a wake word stripped from its
      END is a proven miss → "fn": the user audibly spoke the stop word
      (STT heard it) but the detector never fired.
    - A start-only strip proves nothing about the stop detector and a
      stop_wake recording with no usable text is likely a false trigger
      (the FP paths handle it) → None.
    """
    if stop_reason == "stop_wake":
        return "tp" if has_text else None
    return "fn" if end_stripped else None


def _sanitize_mic_tag(name: str) -> str:
    """Turn a device name into a filesystem-safe kebab-case tag."""
    if not name:
        return ""
    tag = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return tag[:_MIC_TAG_MAX_LEN] if tag else ""


class TrainingCollector:
    """Collects labeled training data during normal wake word operation."""

    def __init__(
        self,
        base_dir: str,
        max_clips_per_category: int = 1000,
        tn_score_range: tuple[float, float] = (0.1, 0.7),
        tn_interval_secs: float = 10.0,
        sample_rate: int = 16000,
        clip_duration_secs: float = 2.0,
        get_mic_name: Callable[[], str] | None = None,
    ):
        self._base = Path(base_dir)
        self._dirs = {}
        for cat in ("tp", "fp", "tn", "fn"):
            d = self._base / cat
            d.mkdir(parents=True, exist_ok=True)
            self._dirs[cat] = d

        self._max_clips = max_clips_per_category
        self._tn_score_lo, self._tn_score_hi = tn_score_range
        self._tn_interval = tn_interval_secs
        self._sample_rate = sample_rate
        self._clip_samples = int(clip_duration_secs * sample_rate)
        self._get_mic_name = get_mic_name

        # Rolling audio buffer for idle-time collection (TP-start, TN, FN-start)
        self._audio_buffer: list[np.ndarray] = []
        self._buffer_samples = 0

        # Rate limiting for TN saves
        self._last_tn_save = 0.0

    # ------------------------------------------------------------------
    # Audio buffer (fed from main loop during idle)
    # ------------------------------------------------------------------

    def feed(self, audio: np.ndarray) -> None:
        """Feed raw audio to build a rolling buffer (call every chunk)."""
        self._audio_buffer.append(audio.copy())
        self._buffer_samples += len(audio)
        max_samples = self._clip_samples + self._sample_rate  # 3s
        while self._buffer_samples > max_samples and len(self._audio_buffer) > 1:
            removed = self._audio_buffer.pop(0)
            self._buffer_samples -= len(removed)

    def _extract_buffer_clip(self) -> np.ndarray | None:
        """Extract the last clip_duration_secs from the rolling buffer."""
        if self._buffer_samples < self._clip_samples:
            return None
        audio = np.concatenate(self._audio_buffer)[-self._clip_samples:]
        rms = int(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
        if rms < _MIN_SPEECH_RMS:
            return None
        return audio

    # ------------------------------------------------------------------
    # TP: True Positives
    # ------------------------------------------------------------------

    def save_tp_start(self, score: float) -> bool:
        """Save a confirmed start-trigger positive from the rolling buffer."""
        audio = self._extract_buffer_clip()
        if audio is None:
            return False
        filepath = self._save("tp", audio, score, suffix="start", return_path=True)
        return filepath is not None

    def save_tp_stop(self, audio_chunks: list, sample_rate: int, score: float = 0.0) -> bool:
        """Save a confirmed stop-trigger positive from recording tail.

        Callers must pass the UN-trimmed tail: the wake-word audio trim
        removes ~0.5s from the end, which is most of the spoken stop word —
        clips cut from trimmed audio contain no wake word at all and are
        worthless as positives (DEF-156). `score` is the stop-trigger /
        observed-max score so clip quality is gradeable from the filename.
        """
        audio = self._extract_speech_tail(audio_chunks, sample_rate)
        if audio is None:
            return False
        return self._save("tp", audio, score, suffix="stop")

    # ------------------------------------------------------------------
    # FP: False Positives
    # ------------------------------------------------------------------

    def save_fp(self, audio_chunks: list, sample_rate: int, reason: str = "") -> bool:
        """Save a false positive — trigger led to garbled/cancelled recording."""
        if not audio_chunks:
            return False
        audio = self._extract_tail(audio_chunks, sample_rate)
        if audio is None:
            # For very short recordings, try to save whatever we have
            try:
                audio = np.concatenate(audio_chunks)
                if len(audio) < sample_rate // 2:  # less than 0.5s
                    return False
            except (ValueError, TypeError):
                return False
        return self._save("fp", audio, 0.0, suffix=reason or "garbled")

    # ------------------------------------------------------------------
    # TN: True Negatives (hard negatives)
    # ------------------------------------------------------------------

    def save_tn(self, max_score: float) -> bool:
        """Save a true negative — high score but correctly didn't trigger."""
        if not (self._tn_score_lo <= max_score <= self._tn_score_hi):
            return False
        now = time.time()
        if now - self._last_tn_save < self._tn_interval:
            return False
        audio = self._extract_buffer_clip()
        if audio is None:
            return False
        self._last_tn_save = now
        filepath = self._save("tn", audio, max_score, return_path=True)
        return filepath is not None

    # ------------------------------------------------------------------
    # FN: False Negatives
    # ------------------------------------------------------------------

    def save_fn_stop(
        self, audio_chunks: list, sample_rate: int, score: float = 0.0
    ) -> bool:
        """Save a false negative — STT proved wake word was in recording tail.

        Call only when classify_stop_outcome() returns "fn" (recording NOT
        ended by the stop-wake path + end-strip proof — DEF-155). Pass the
        UN-trimmed tail; a silence-timeout recording carries ~timeout secs
        of silence after the missed wake word, which _extract_speech_tail
        skips. `score` is the highest stop-score the model produced during
        the recording, so model-blind misses (score≈0) and gate-blocked
        misses (score near threshold) are distinguishable from the filename.
        """
        audio = self._extract_speech_tail(audio_chunks, sample_rate)
        if audio is None:
            return False
        return self._save("fn", audio, score, suffix="stop")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _extract_tail(self, audio_chunks: list, sample_rate: int) -> np.ndarray | None:
        """Extract the last ~2 seconds from a list of audio chunks."""
        if not audio_chunks:
            return None
        try:
            full = np.concatenate(audio_chunks)
        except (ValueError, TypeError):
            return None
        clip_samples = int(2.0 * sample_rate)
        audio = full[-clip_samples:] if len(full) > clip_samples else full
        rms = int(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
        if rms < _MIN_SPEECH_RMS:
            return None
        return audio

    def _extract_speech_tail(
        self, audio_chunks: list, sample_rate: int
    ) -> np.ndarray | None:
        """Extract a ~2s window ending just after the LAST speech activity.

        Unlike _extract_tail (verbatim last 2s), this skips trailing
        silence first. A recording ended by silence-timeout carries
        ~timeout seconds of silence after the missed stop wake word — the
        verbatim tail is pure silence and the RMS gate discards exactly
        the FN clips we most need (DEF-155). Falls back to _extract_tail
        when no speech-level frame is found.
        """
        if not audio_chunks:
            return None
        try:
            full = np.concatenate(audio_chunks)
        except (ValueError, TypeError):
            return None
        frame = max(1, int(0.05 * sample_rate))
        n = (len(full) // frame) * frame
        if n == 0:
            return None
        frames = full[:n].reshape(-1, frame).astype(np.float64)
        rms_per_frame = np.sqrt((frames**2).mean(axis=1))
        speech_idx = np.nonzero(rms_per_frame >= _MIN_SPEECH_RMS)[0]
        if len(speech_idx) == 0:
            return self._extract_tail(audio_chunks, sample_rate)
        # End the window ~0.25s after the last speech frame so the final
        # word's decay isn't clipped.
        end = min(len(full), (int(speech_idx[-1]) + 1) * frame + int(0.25 * sample_rate))
        clip_samples = int(2.0 * sample_rate)
        audio = full[max(0, end - clip_samples):end]
        rms = int(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
        if rms < _MIN_SPEECH_RMS:
            return None
        return audio

    def _save(self, category: str, audio: np.ndarray, score: float,
              suffix: str = "", return_path: bool = False):
        """Save a clip to the given category directory."""
        cat_dir = self._dirs[category]
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        parts = [category]
        if suffix:
            parts.append(suffix)
        if self._get_mic_name is not None:
            try:
                mic_tag = _sanitize_mic_tag(self._get_mic_name() or "")
            except Exception:
                mic_tag = ""
            if mic_tag:
                parts.append(mic_tag)
        parts.append(timestamp)
        parts.append(f"score{score:.2f}")
        filename = "_".join(parts) + ".wav"
        filepath = cat_dir / filename

        try:
            import soundfile as sf
            sf.write(str(filepath), audio, self._sample_rate)
            logger.debug("Saved %s clip: %s", category.upper(), filename)
        except Exception:
            logger.warning("Failed to save %s clip", category.upper(), exc_info=True)
            if return_path:
                return None
            return False

        self._prune(category)
        if return_path:
            return filepath
        return True

    def _prune(self, category: str) -> None:
        """Remove oldest clips (by mtime) when a category exceeds the limit.

        Deletions come from the LARGEST subtype group (fn_stop vs fn_start
        etc.) so a high-volume subtype cannot evict a rarer one. The old
        `sorted(glob)` sorted alphabetically: "tp_start_*" < "tp_stop_*"
        meant every tp_start clip — regardless of age — was deleted before
        any tp_stop, silently erasing the whole subtype (DEF-157).
        """
        cat_dir = self._dirs[category]
        clips = list(cat_dir.glob("*.wav"))
        excess = len(clips) - self._max_clips
        if excess <= 0:
            return
        groups: dict[str, list[Path]] = {}
        for clip in clips:
            m = _SUBTYPE_RE.match(clip.name)
            groups.setdefault(m.group(1) if m else "", []).append(clip)
        for group in groups.values():
            # Newest first so .pop() removes the oldest.
            group.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for _ in range(excess):
            largest = max(groups.values(), key=len)
            if not largest:
                break
            try:
                largest.pop().unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def counts(self) -> dict[str, int]:
        """Return clip counts per category."""
        return {
            cat: len(list(d.glob("*.wav")))
            for cat, d in self._dirs.items()
        }
