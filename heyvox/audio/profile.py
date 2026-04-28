"""
Per-device microphone profile management for heyvox.

Manages per-device audio profiles combining:
1. Config-file overrides (config.yaml ``mic_profiles:`` section) — always wins
2. Auto-calibration cache (~/.cache/heyvox/mic-profiles.json) — fills missing fields

Device lookup uses partial case-insensitive substring matching, consistent with
the mic_priority matching in heyvox/audio/mic.py.

Requirement: AUDIO-01, D-01, D-02, D-03, D-04, D-12
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from heyvox.config import MicProfileEntryConfig

log = logging.getLogger(__name__)

# Cache expiry: 30 days in seconds
_CACHE_EXPIRY_SECS = 30 * 24 * 3600

# Calibration: multiplier applied to median noise floor to compute silence_threshold
_CALIBRATION_MULTIPLIER = 3.5

# Maximum silence_threshold from calibration (prevents absurd values on very noisy mics)
_CALIBRATION_MAX_THRESHOLD = 500

# DEF-097: Minimum silence_threshold floor. Hardware-gated mics (some Jabra,
# certain Bluetooth profiles) report idle audio at level 0, which would
# multiply to silence_threshold = 0 and disable the VAD silent gate
# entirely. Without a working gate the DEF-096 silence-transition reset
# and pre-silence threshold discount can never fire, regressing stop-wake
# reliability. Real speech is consistently > 1000, so a floor of 50 is
# well below speech and well above any plausible quiet-mic noise floor.
_CALIBRATION_MIN_THRESHOLD = 50


@dataclass
class MicProfileEntry:
    """Resolved mic profile for a specific device.

    All fields are Optional — callers fall back to global config defaults for None fields.
    """
    noise_floor: int | None = None
    silence_threshold: int | None = None
    buffer_size: int | None = None
    cooldown_tier: int | None = None
    sample_rate: int | None = None
    chunk_size: int | None = None
    gain: float | None = None
    voice_isolation_mode: bool | None = None
    echo_safe: bool | None = None
    min_audio_dbfs: float | None = None  # DEF-101: per-mic energy-gate floor


class MicProfileManager:
    """Manages per-device microphone profiles.

    Merges config-file overrides with auto-calibration cache.
    Config overrides always win (D-03).

    Args:
        config_profiles: Dict from HeyvoxConfig.mic_profiles (keyed by partial device name).
        cache_dir: Directory for mic-profiles.json cache file
            (typically ~/.cache/heyvox/).
    """

    def __init__(
        self,
        config_profiles: dict[str, "MicProfileEntryConfig"],
        cache_dir: Path,
    ) -> None:
        self._config_profiles = config_profiles
        self._cache_dir = cache_dir
        self._cache_file = cache_dir / "mic-profiles.json"
        self._cache: dict[str, dict] = {}
        self._load_cache()

    # -------------------------------------------------------------------------
    # Cache I/O
    # -------------------------------------------------------------------------

    def _load_cache(self) -> None:
        """Load calibration cache from disk. Silently ignores missing/corrupt files."""
        if not self._cache_file.exists():
            return
        try:
            raw = json.loads(self._cache_file.read_text())
            if isinstance(raw, dict):
                self._cache = raw
        except (json.JSONDecodeError, OSError) as e:
            log.debug("MicProfileManager: failed to load cache: %s", e)

    def _write_cache(self) -> None:
        """Atomically write the in-memory cache to disk.

        Uses tempfile.mkstemp + os.replace for atomic writes (D-03).
        Creates cache_dir if it doesn't exist.
        """
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                dir=self._cache_dir, suffix=".tmp", prefix=".mic-profiles-"
            )
            try:
                os.write(fd, json.dumps(self._cache, indent=2).encode("utf-8"))
            finally:
                os.close(fd)
            os.replace(tmp_path, self._cache_file)
        except OSError as e:
            log.warning("MicProfileManager: failed to write cache: %s", e)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # -------------------------------------------------------------------------
    # Profile lookup
    # -------------------------------------------------------------------------

    def get_profile(self, device_name: str) -> MicProfileEntry:
        """Return the resolved profile for a device.

        Lookup order:
        1. Find matching config_profiles key (partial case-insensitive match)
        2. Find matching cache entry by exact (lowercased) device name
        3. Merge: cache provides base values, config overrides win

        Args:
            device_name: Full device name as reported by PyAudio.

        Returns:
            MicProfileEntry with merged values. All fields are None if no
            profile found (caller falls back to global config defaults).
        """
        device_key = device_name.lower().strip()

        # --- Step 1: Find config profile by partial match ---
        config_entry: "MicProfileEntryConfig | None" = None
        for profile_key, profile in self._config_profiles.items():
            if profile_key.lower() in device_key:
                config_entry = profile
                break

        # --- Step 2: Find cache entry by exact (lowercased) device name ---
        cache_data: dict | None = None
        raw_cache = self._cache.get(device_key)
        if raw_cache is not None:
            calibrated_at = raw_cache.get("calibrated_at", 0)
            if time.time() - calibrated_at <= _CACHE_EXPIRY_SECS:
                cache_data = raw_cache
            else:
                log.debug(
                    "MicProfileManager: cache entry for '%s' expired, ignoring",
                    device_name,
                )

        # --- Step 3: Merge — start from cache, overlay config overrides ---
        entry = MicProfileEntry()

        # Apply cached values first (auto-calibration provides base)
        if cache_data is not None:
            entry.noise_floor = cache_data.get("noise_floor")
            entry.silence_threshold = cache_data.get("silence_threshold")

        # Apply config overrides (always win per D-03)
        if config_entry is not None:
            if config_entry.noise_floor is not None:
                entry.noise_floor = config_entry.noise_floor
            if config_entry.silence_threshold is not None:
                entry.silence_threshold = config_entry.silence_threshold
            if config_entry.buffer_size is not None:
                entry.buffer_size = config_entry.buffer_size
            if config_entry.cooldown_tier is not None:
                entry.cooldown_tier = config_entry.cooldown_tier
            if config_entry.sample_rate is not None:
                entry.sample_rate = config_entry.sample_rate
            if config_entry.chunk_size is not None:
                entry.chunk_size = config_entry.chunk_size
            if config_entry.gain is not None:
                entry.gain = config_entry.gain
            if config_entry.voice_isolation_mode is not None:
                entry.voice_isolation_mode = config_entry.voice_isolation_mode
            if config_entry.echo_safe is not None:
                entry.echo_safe = config_entry.echo_safe
            if config_entry.min_audio_dbfs is not None:
                entry.min_audio_dbfs = config_entry.min_audio_dbfs

        return entry

    # -------------------------------------------------------------------------
    # Calibration
    # -------------------------------------------------------------------------

    def run_calibration(self, chunks: list[np.ndarray]) -> tuple[int, int]:
        """Compute noise floor and silence threshold from audio chunks.

        Algorithm (D-04, D-12):
        - noise_floor = median of per-chunk peak values
        - silence_threshold = clamp(noise_floor * 3.5,
                                    _CALIBRATION_MIN_THRESHOLD,
                                    _CALIBRATION_MAX_THRESHOLD)

        DEF-097: the floor (`_CALIBRATION_MIN_THRESHOLD = 50`) prevents
        hardware-gated mics that report idle level 0 from collapsing the
        silence_threshold to 0. A 0 threshold disables the VAD silent
        gate, which in turn disables every downstream feature that keys
        off VAD silence (DEF-053 grace, DEF-096-A silence reset,
        DEF-096-B pre-silence discount).

        Args:
            chunks: List of int16 numpy arrays (audio frames).

        Returns:
            (noise_floor, silence_threshold) — both as int.
        """
        if not chunks:
            return 0, _CALIBRATION_MIN_THRESHOLD

        peak_levels = [int(np.abs(chunk).max()) for chunk in chunks]
        noise_floor = int(np.median(peak_levels))
        silence_threshold = max(
            _CALIBRATION_MIN_THRESHOLD,
            min(int(noise_floor * _CALIBRATION_MULTIPLIER),
                _CALIBRATION_MAX_THRESHOLD),
        )
        return noise_floor, silence_threshold

    def save_calibration(
        self, device_name: str, noise_floor: int, silence_threshold: int
    ) -> None:
        """Persist calibration data for a device to the cache file.

        Uses the lowercased full device name as the cache key.
        Writes atomically via tempfile + os.replace (D-03).

        Args:
            device_name: Full device name as reported by PyAudio.
            noise_floor: Measured noise floor (median peak level).
            silence_threshold: Computed silence threshold.
        """
        device_key = device_name.lower().strip()
        self._cache[device_key] = {
            "noise_floor": noise_floor,
            "silence_threshold": silence_threshold,
            "calibrated_at": time.time(),
        }
        self._write_cache()
        log.info(
            "MicProfileManager: saved calibration for '%s' "
            "(noise_floor=%d, silence_threshold=%d)",
            device_name, noise_floor, silence_threshold,
        )
