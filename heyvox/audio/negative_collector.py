"""
Passive hard negative mining for wake word training.

Saves audio clips that contain speech but are NOT the wake word.
These clips come from the user's real environment and are the most
valuable negatives for reducing false positives.

Clips are saved as 2-second 16kHz mono WAVs with naming:
    neg_YYYYMMDD_HHMMSS_score0.42.wav

Enable via config:
    wake_words:
      collect_negatives: true
"""

import logging
import os
import time
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# Minimum RMS energy to consider a frame as containing speech (not silence)
_MIN_SPEECH_RMS = 300


class NegativeCollector:
    """Collects hard negative audio clips during normal wake word operation."""

    def __init__(
        self,
        negatives_dir: str,
        max_clips: int = 1000,
        score_range: tuple[float, float] = (0.1, 0.7),
        interval_secs: float = 10.0,
        sample_rate: int = 16000,
        clip_duration_secs: float = 2.0,
    ):
        self._dir = Path(negatives_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._max_clips = max_clips
        self._score_lo, self._score_hi = score_range
        self._interval = interval_secs
        self._sample_rate = sample_rate
        self._clip_samples = int(clip_duration_secs * sample_rate)
        self._last_save = 0.0
        self._audio_buffer: list[np.ndarray] = []
        self._buffer_samples = 0

    def feed(self, audio: np.ndarray) -> None:
        """Feed raw audio frames to build up a rolling buffer."""
        self._audio_buffer.append(audio.copy())
        self._buffer_samples += len(audio)
        # Keep at most 3 seconds of audio
        max_samples = self._clip_samples + self._sample_rate
        while self._buffer_samples > max_samples and len(self._audio_buffer) > 1:
            removed = self._audio_buffer.pop(0)
            self._buffer_samples -= len(removed)

    def maybe_save(self, max_score: float) -> bool:
        """Check score and possibly save the current audio buffer as a negative clip.

        Args:
            max_score: Highest wake word score from the current prediction.

        Returns:
            True if a clip was saved.
        """
        # Score must be in the interesting range (has some speech content,
        # but not actually the wake word)
        if not (self._score_lo <= max_score <= self._score_hi):
            return False

        # Rate limit
        now = time.time()
        if now - self._last_save < self._interval:
            return False

        # Check that there's actual speech energy, not just silence
        if self._buffer_samples < self._clip_samples:
            return False

        audio = np.concatenate(self._audio_buffer)[-self._clip_samples:]
        rms = int(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
        if rms < _MIN_SPEECH_RMS:
            return False

        # Save the clip
        self._last_save = now
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"neg_{timestamp}_score{max_score:.2f}.wav"
        filepath = self._dir / filename

        try:
            import soundfile as sf
            sf.write(str(filepath), audio, self._sample_rate)
            logger.debug("Saved negative clip: %s (rms=%d, score=%.2f)", filename, rms, max_score)
        except Exception:
            logger.warning("Failed to save negative clip", exc_info=True)
            return False

        # Prune oldest clips if over limit
        self._prune()
        return True

    def _prune(self) -> None:
        """Remove oldest clips if over max_clips limit."""
        clips = sorted(self._dir.glob("neg_*.wav"))
        if len(clips) <= self._max_clips:
            return
        to_remove = clips[: len(clips) - self._max_clips]
        for clip in to_remove:
            try:
                clip.unlink()
            except OSError:
                pass
        if to_remove:
            logger.debug("Pruned %d oldest negative clips", len(to_remove))

    @property
    def clip_count(self) -> int:
        """Number of collected negative clips."""
        return len(list(self._dir.glob("neg_*.wav")))
