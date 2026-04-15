"""
Automatic training data collection for wake word improvement.

Collects labeled audio clips across all 4 categories during normal operation:
  - **tp/** True Positives  — confirmed wake word triggers (start + stop)
  - **fp/** False Positives — triggers that led to garbled/cancelled recordings
  - **tn/** True Negatives  — high-scoring non-triggers (hard negatives)
  - **fn/** False Negatives — missed wake words (detected via STT strip or retry pattern)

Clips are saved as 2-second 16kHz mono WAVs with naming:
    {category}_{timestamp}_score{score:.2f}.wav

Enable via config:
    wake_words:
      collect_negatives: true  # enables all training data collection

The retrospective FN-start detection works by tracking recent TN saves.
When a successful trigger follows within 5 seconds, the TN clip is
reclassified as FN (moved from tn/ to fn/).
"""

import logging
import os
import shutil
import time
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

_MIN_SPEECH_RMS = 300


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
        fn_reclassify_window_secs: float = 5.0,
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
        self._fn_window = fn_reclassify_window_secs

        # Rolling audio buffer for idle-time collection (TP-start, TN, FN-start)
        self._audio_buffer: list[np.ndarray] = []
        self._buffer_samples = 0

        # Rate limiting for TN saves
        self._last_tn_save = 0.0

        # Recent TN saves for retrospective FN-start reclassification
        # List of (timestamp, filepath) — pruned on each trigger
        self._recent_tn: list[tuple[float, Path]] = []

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
        return self._save("tp", audio, score, suffix="start")

    def save_tp_stop(self, audio_chunks: list, sample_rate: int, score: float = 0.0) -> bool:
        """Save a confirmed stop-trigger positive from recording tail."""
        audio = self._extract_tail(audio_chunks, sample_rate)
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
        if filepath:
            self._recent_tn.append((now, filepath))
            # Prune old entries
            self._recent_tn = [
                (t, p) for t, p in self._recent_tn
                if now - t < self._fn_window * 2
            ]
            return True
        return False

    # ------------------------------------------------------------------
    # FN: False Negatives
    # ------------------------------------------------------------------

    def save_fn_stop(self, audio_chunks: list, sample_rate: int) -> bool:
        """Save a false negative — STT proved wake word was in recording tail.

        Called when strip_wake_words() removes text from the transcription,
        meaning the model failed to detect the wake word during recording.
        """
        audio = self._extract_tail(audio_chunks, sample_rate)
        if audio is None:
            return False
        return self._save("fn", audio, 0.0, suffix="stop")

    def reclassify_fn_start(self) -> int:
        """Reclassify recent TN saves as FN if a trigger follows within the window.

        Called when a successful start trigger fires. Any TN clip saved
        in the last fn_reclassify_window_secs was likely a missed wake word
        (the user retried and succeeded).

        Returns number of clips reclassified.
        """
        now = time.time()
        fn_dir = self._dirs["fn"]
        reclassified = 0
        remaining = []
        for save_time, tn_path in self._recent_tn:
            if now - save_time <= self._fn_window and tn_path.exists():
                # Move from tn/ to fn/, rename prefix
                new_name = tn_path.name.replace("tn_", "fn_start_", 1)
                fn_path = fn_dir / new_name
                try:
                    shutil.move(str(tn_path), str(fn_path))
                    reclassified += 1
                    logger.debug("Reclassified TN → FN: %s", new_name)
                except OSError:
                    pass
            else:
                remaining.append((save_time, tn_path))
        self._recent_tn = remaining
        return reclassified

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

    def _save(self, category: str, audio: np.ndarray, score: float,
              suffix: str = "", return_path: bool = False):
        """Save a clip to the given category directory."""
        cat_dir = self._dirs[category]
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        parts = [category]
        if suffix:
            parts.append(suffix)
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
        """Remove oldest clips in a category if over limit."""
        cat_dir = self._dirs[category]
        clips = sorted(cat_dir.glob("*.wav"))
        if len(clips) <= self._max_clips:
            return
        for clip in clips[: len(clips) - self._max_clips]:
            try:
                clip.unlink()
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
