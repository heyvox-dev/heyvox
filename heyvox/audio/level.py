"""Mic level helpers: relative silence threshold and gain-drop detection (DEF-276).

Two problems share one cause, a mic whose level sags 10-15 dB without any
device event (the G435 state from DEF-101, seen again 2026-11-05 on USB):

1. The hands-free silence watchdog counts a chunk as "quiet" when its peak is
   below a fixed ``silence_threshold``. After the sag, normal speech sits under
   that number, so the watchdog stops the recording ~4 s in, mid-sentence.
   ``effective_silence_threshold`` lowers the threshold for the running
   recording when its own speech peaks are low. It never raises it, so a
   healthy mic behaves exactly as before.

2. Nothing told the user the mic had sagged; the fix (power-cycle the headset)
   is easy once you know. ``MicLevelBaseline`` keeps a per-mic history of the
   speech level of recent dictations and reports a drop against it.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
import time
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

# --- relative silence threshold -------------------------------------------

# Silence = chunk peak below this fraction of the recording's speech peaks
# (0.2 = -14 dB). Measured 2026-11-05 on 27 G435 dictations: healthy speech
# p90 chunk peak 1500-5700, sagged 420-670; pauses in the sagged state 2-50.
RELATIVE_SILENCE_RATIO = 0.2
# Percentile of chunk peaks taken as the speech reference.
SPEECH_PEAK_PERCENTILE = 90
# Percentile of chunk peaks taken as the ambient estimate, and its margin:
# the threshold never drops to where ambient noise stops counting as silence.
NOISE_PEAK_PERCENTILE = 10
NOISE_MARGIN = 2.5
# Absolute floor for the relative threshold.
RELATIVE_SILENCE_FLOOR = 60
# Chunks needed before the recording's own levels are trusted (~1 s at 80 ms).
RELATIVE_MIN_CHUNKS = 12


def chunk_peak(chunk) -> int:
    """Absolute peak of one int16 audio chunk."""
    if chunk is None or len(chunk) == 0:
        return 0
    return int(np.abs(np.asarray(chunk, dtype=np.int32)).max())


def effective_silence_threshold(chunk_peaks, static_threshold: int) -> int:
    """Silence threshold for the running recording.

    Returns ``static_threshold`` unless the recording's own speech peaks are so
    low that the static value would count speech as silence; then a threshold
    relative to those peaks, kept above the recording's ambient level.
    """
    if static_threshold <= 0 or len(chunk_peaks) < RELATIVE_MIN_CHUNKS:
        return static_threshold
    peaks = np.asarray(chunk_peaks, dtype=np.float64)
    speech_ref = float(np.percentile(peaks, SPEECH_PEAK_PERCENTILE))
    noise_ref = float(np.percentile(peaks, NOISE_PEAK_PERCENTILE))
    relative = max(
        RELATIVE_SILENCE_FLOOR,
        int(speech_ref * RELATIVE_SILENCE_RATIO),
        int(noise_ref * NOISE_MARGIN),
    )
    return min(static_threshold, relative)


# --- gain-drop detection ---------------------------------------------------

# A dictation this many dB under the mic's usual speech level counts as a drop.
# The 2026-11-05 sag was 15-17 dB; normal spread between dictations is ~±5 dB.
DROP_DB = 10.0
# Only dictations at least this long feed or trigger the baseline: short
# recordings are often wake-word false positives with no speech in them.
MIN_DICTATION_SECS = 3.0
# Baseline = median of the last N dictations per mic; needs MIN_SAMPLES first.
BASELINE_WINDOW = 20
BASELINE_MIN_SAMPLES = 5

_FILE_NAME = "mic-levels.json"


def _default_cache_dir() -> Path:
    try:
        from platformdirs import user_cache_dir
        return Path(user_cache_dir("heyvox"))
    except ImportError:
        return Path.home() / ".cache" / "heyvox"


class MicLevelBaseline:
    """Per-mic rolling median of dictation speech levels, persisted to the cache dir.

    ``observe`` returns the drop in dB when a dictation sits ``DROP_DB`` or more
    under the mic's baseline, else ``None``. Every dictation is recorded, drops
    included: if a mic's level changes for good, the median follows within
    about half a window and the warning stops on its own.
    """

    def __init__(self, cache_dir: Path | None = None):
        self._path = Path(cache_dir or _default_cache_dir()) / _FILE_NAME
        self._data: dict[str, list[float]] = self._load()

    def _load(self) -> dict[str, list[float]]:
        try:
            raw = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, list[float]] = {}
        for mic, values in raw.items():
            if isinstance(mic, str) and isinstance(values, list):
                out[mic] = [float(v) for v in values if isinstance(v, (int, float))][-BASELINE_WINDOW:]
        return out

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(f".{os.getpid()}.{int(time.time() * 1000)}.tmp")
            tmp.write_text(json.dumps(self._data))
            os.replace(tmp, self._path)
        except OSError as exc:
            log.debug("mic-levels save failed: %s", exc)

    def baseline(self, mic: str) -> float | None:
        values = self._data.get(mic.lower(), [])
        if len(values) < BASELINE_MIN_SAMPLES:
            return None
        return statistics.median(values)

    def observe(self, mic: str, speech_db: float | None, duration: float) -> float | None:
        """Record one dictation; return its drop in dB below baseline, or None."""
        if not mic or speech_db is None or duration < MIN_DICTATION_SECS:
            return None
        key = mic.lower()
        base = self.baseline(key)
        values = self._data.setdefault(key, [])
        values.append(round(float(speech_db), 1))
        del values[:-BASELINE_WINDOW]
        self._save()
        if base is None:
            return None
        drop = base - float(speech_db)
        return drop if drop >= DROP_DB else None
