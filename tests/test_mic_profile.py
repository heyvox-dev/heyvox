"""
Unit tests for MicProfileManager and MicProfileEntryConfig.

Phase 13, Plan 01: per-device mic profiles with auto-calibration.
"""
import json
import time
from pathlib import Path

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Task 1 tests: MicProfileEntryConfig config model
# ---------------------------------------------------------------------------

class TestMicProfileEntryConfig:
    def test_all_fields_default_to_none(self):
        from heyvox.config import MicProfileEntryConfig
        p = MicProfileEntryConfig()
        assert p.noise_floor is None
        assert p.silence_threshold is None
        assert p.buffer_size is None
        assert p.cooldown_tier is None
        assert p.sample_rate is None
        assert p.chunk_size is None
        assert p.gain is None
        assert p.voice_isolation_mode is None
        assert p.echo_safe is None

    def test_fields_can_be_set(self):
        from heyvox.config import MicProfileEntryConfig
        p = MicProfileEntryConfig(
            silence_threshold=300,
            echo_safe=True,
            gain=1.5,
            sample_rate=16000,
        )
        assert p.silence_threshold == 300
        assert p.echo_safe is True
        assert p.gain == 1.5
        assert p.sample_rate == 16000

    def test_extra_fields_ignored(self):
        from heyvox.config import MicProfileEntryConfig
        # Should not raise with extra="ignore"
        p = MicProfileEntryConfig(**{"silence_threshold": 300, "unknown_field": "ignored"})
        assert p.silence_threshold == 300

    def test_heyvox_config_has_mic_profiles_field(self):
        from heyvox.config import HeyvoxConfig
        c = HeyvoxConfig()
        assert hasattr(c, "mic_profiles")
        assert isinstance(c.mic_profiles, dict)
        assert c.mic_profiles == {}

    def test_heyvox_config_loads_mic_profiles_from_dict(self):
        from heyvox.config import HeyvoxConfig, MicProfileEntryConfig
        c = HeyvoxConfig(mic_profiles={
            "G435": {"silence_threshold": 300, "echo_safe": True},
            "MacBook": {"silence_threshold": 200},
        })
        assert "G435" in c.mic_profiles
        assert isinstance(c.mic_profiles["G435"], MicProfileEntryConfig)
        assert c.mic_profiles["G435"].silence_threshold == 300
        assert c.mic_profiles["G435"].echo_safe is True
        assert c.mic_profiles["MacBook"].silence_threshold == 200
        assert c.mic_profiles["MacBook"].echo_safe is None

    def test_empty_config_yaml_loads_with_no_mic_profiles(self):
        """Empty HeyvoxConfig should have empty mic_profiles dict."""
        from heyvox.config import HeyvoxConfig
        c = HeyvoxConfig()
        assert c.mic_profiles == {}


# ---------------------------------------------------------------------------
# Task 2 tests: MicProfileManager
# ---------------------------------------------------------------------------

class TestMicProfileManager:
    @pytest.fixture
    def cache_dir(self, tmp_path):
        return tmp_path / "heyvox"

    @pytest.fixture
    def empty_manager(self, cache_dir):
        from heyvox.audio.profile import MicProfileManager
        return MicProfileManager(config_profiles={}, cache_dir=cache_dir)

    def test_empty_config_returns_all_none_entry(self, empty_manager):
        from heyvox.audio.profile import MicProfileEntry
        entry = empty_manager.get_profile("MacBook Pro Microphone")
        assert isinstance(entry, MicProfileEntry)
        assert entry.silence_threshold is None
        assert entry.noise_floor is None
        assert entry.echo_safe is None

    def test_partial_name_match(self, cache_dir):
        from heyvox.config import MicProfileEntryConfig
        from heyvox.audio.profile import MicProfileManager
        mgr = MicProfileManager(
            config_profiles={"G435": MicProfileEntryConfig(silence_threshold=300)},
            cache_dir=cache_dir,
        )
        entry = mgr.get_profile("G435 Wireless Gaming Headset")
        assert entry.silence_threshold == 300

    def test_case_insensitive_match(self, cache_dir):
        from heyvox.config import MicProfileEntryConfig
        from heyvox.audio.profile import MicProfileManager
        mgr = MicProfileManager(
            config_profiles={"macbook": MicProfileEntryConfig(silence_threshold=200)},
            cache_dir=cache_dir,
        )
        entry = mgr.get_profile("MacBook Pro Microphone")
        assert entry.silence_threshold == 200

    def test_config_override_wins_over_cache(self, cache_dir):
        """Config profile silence_threshold=300 should override cached 200."""
        from heyvox.config import MicProfileEntryConfig
        from heyvox.audio.profile import MicProfileManager

        # Pre-populate cache with silence_threshold=200
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / "mic-profiles.json"
        cache_data = {
            "g435 wireless gaming headset": {
                "noise_floor": 25,
                "silence_threshold": 200,
                "calibrated_at": time.time(),
            }
        }
        cache_file.write_text(json.dumps(cache_data))

        mgr = MicProfileManager(
            config_profiles={"G435": MicProfileEntryConfig(silence_threshold=300)},
            cache_dir=cache_dir,
        )
        entry = mgr.get_profile("G435 Wireless Gaming Headset")
        assert entry.silence_threshold == 300  # Config wins over cache

    def test_cache_fills_missing_config_fields(self, cache_dir):
        """Cache noise_floor fills in when config only sets silence_threshold."""
        from heyvox.config import MicProfileEntryConfig
        from heyvox.audio.profile import MicProfileManager

        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / "mic-profiles.json"
        cache_data = {
            "g435 wireless gaming headset": {
                "noise_floor": 25,
                "silence_threshold": 200,
                "calibrated_at": time.time(),
            }
        }
        cache_file.write_text(json.dumps(cache_data))

        mgr = MicProfileManager(
            config_profiles={"G435": MicProfileEntryConfig(silence_threshold=300)},
            cache_dir=cache_dir,
        )
        entry = mgr.get_profile("G435 Wireless Gaming Headset")
        # Config wins on silence_threshold, but cache provides noise_floor
        assert entry.silence_threshold == 300
        assert entry.noise_floor == 25

    def test_save_and_load_calibration(self, cache_dir):
        from heyvox.audio.profile import MicProfileManager
        mgr = MicProfileManager(config_profiles={}, cache_dir=cache_dir)
        mgr.save_calibration("MacBook Pro Microphone", noise_floor=30, silence_threshold=105)

        # Load fresh manager to verify persistence
        mgr2 = MicProfileManager(config_profiles={}, cache_dir=cache_dir)
        entry = mgr2.get_profile("MacBook Pro Microphone")
        assert entry.noise_floor == 30
        assert entry.silence_threshold == 105

    def test_cache_expiry_ignores_old_entries(self, cache_dir):
        """Cache entries older than 30 days should be ignored."""
        from heyvox.audio.profile import MicProfileManager

        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / "mic-profiles.json"
        old_time = time.time() - (31 * 24 * 3600)  # 31 days ago
        cache_data = {
            "macbook pro microphone": {
                "noise_floor": 30,
                "silence_threshold": 105,
                "calibrated_at": old_time,
            }
        }
        cache_file.write_text(json.dumps(cache_data))

        mgr = MicProfileManager(config_profiles={}, cache_dir=cache_dir)
        entry = mgr.get_profile("MacBook Pro Microphone")
        # Expired cache should be ignored
        assert entry.noise_floor is None
        assert entry.silence_threshold is None

    def test_run_calibration_algorithm(self):
        """Calibration computes noise_floor=median, silence_threshold=noise_floor*3.5 (capped 500)."""
        from heyvox.audio.profile import MicProfileManager
        mgr = MicProfileManager(config_profiles={}, cache_dir=Path("/tmp"))

        # Create 50 chunks where peak values cluster around 30
        rng = np.random.default_rng(42)
        chunks = []
        for _ in range(50):
            # Base audio with peak around 30 (noise floor)
            data = rng.integers(-30, 30, size=1280, dtype=np.int16)
            chunks.append(data)

        noise_floor, silence_threshold = mgr.run_calibration(chunks)
        # Median of peaks ~30, threshold = 30 * 3.5 = 105 (uncapped)
        assert 10 <= noise_floor <= 50
        assert silence_threshold == min(int(noise_floor * 3.5), 500)

    def test_silence_threshold_capped_at_500(self):
        """Very noisy input should cap silence_threshold at 500."""
        from heyvox.audio.profile import MicProfileManager
        mgr = MicProfileManager(config_profiles={}, cache_dir=Path("/tmp"))

        # Create chunks with very high peak levels
        chunks = [np.full(1280, 2000, dtype=np.int16) for _ in range(50)]
        noise_floor, silence_threshold = mgr.run_calibration(chunks)
        assert noise_floor == 2000
        assert silence_threshold == 500  # Capped

    def test_atomic_write_cache(self, cache_dir):
        """save_calibration should write cache file atomically."""
        from heyvox.audio.profile import MicProfileManager
        mgr = MicProfileManager(config_profiles={}, cache_dir=cache_dir)
        mgr.save_calibration("Test Mic", noise_floor=20, silence_threshold=70)

        cache_file = cache_dir / "mic-profiles.json"
        assert cache_file.exists()
        # Verify JSON is valid
        data = json.loads(cache_file.read_text())
        assert "test mic" in data
