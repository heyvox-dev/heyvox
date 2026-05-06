"""Tests for heyvox.device_manager — DeviceManager class and mic selection."""
from unittest.mock import MagicMock

import numpy as np
import pytest

from heyvox.audio.mic import (
    add_device_cooldown,
    clear_device_cooldowns,
    find_best_mic,
    is_device_cooled_down,
)


def test_device_manager_import():
    pass


def test_device_manager_constructor_accepts_ctx_config_log_hud():
    from heyvox.device_manager import DeviceManager
    from heyvox.app_context import AppContext
    ctx = AppContext()
    dm = DeviceManager(ctx=ctx, config=None, log_fn=print, hud_send=lambda msg: None)
    assert dm.ctx is ctx


def test_device_manager_has_required_methods():
    from heyvox.device_manager import DeviceManager
    assert callable(getattr(DeviceManager, 'init', None))
    assert callable(getattr(DeviceManager, 'scan', None))
    assert callable(getattr(DeviceManager, 'reinit', None))
    assert callable(getattr(DeviceManager, 'health_check', None))
    assert callable(getattr(DeviceManager, 'cleanup', None))


def test_device_manager_initial_state():
    from heyvox.device_manager import DeviceManager
    from heyvox.app_context import AppContext
    ctx = AppContext()
    dm = DeviceManager(ctx=ctx, config=None, log_fn=print, hud_send=lambda msg: None)
    assert dm.pa is None
    assert dm.stream is None
    assert dm.dev_index is None
    assert dm.headset_mode is False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_pa(devices: list[dict]) -> MagicMock:
    """Create a mock PyAudio with the given device list."""
    pa = MagicMock()
    pa.get_device_count.return_value = len(devices)
    pa.get_device_info_by_index.side_effect = lambda i: devices[i]

    # Mock open() to return a stream mock that can be read
    mock_stream = MagicMock()
    # Return audio data with level > MIN_AUDIO_LEVEL (10) so devices "pass"
    audio_data = (np.ones(1024, dtype=np.int16) * 100).tobytes()
    mock_stream.read.return_value = audio_data
    pa.open.return_value = mock_stream

    # Default input device fallback
    pa.get_default_input_device_info.return_value = {"index": 0}
    return pa


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_cooldowns():
    """Clear device cooldowns before and after each test."""
    clear_device_cooldowns()
    yield
    clear_device_cooldowns()


# ---------------------------------------------------------------------------
# TestFindBestMic
# ---------------------------------------------------------------------------

class TestFindBestMic:
    """Behavioral tests for find_best_mic with mocked PyAudio."""

    def test_priority_ordering(self, monkeypatch):
        """Returns highest-priority matching device when multiple candidates exist."""
        monkeypatch.setattr("heyvox.audio.mic._get_dead_input_device_names", lambda: set())
        devices = [
            {"name": "MacBook Pro Microphone", "maxInputChannels": 1},
            {"name": "BlackHole 2ch", "maxInputChannels": 2},
        ]
        pa = _mock_pa(devices)
        result = find_best_mic(pa, mic_priority=["BlackHole 2ch", "MacBook Pro Microphone"])
        # BlackHole 2ch is rank-0 priority and is at index 1
        assert result == 1

    def test_cooldown_skips_device(self, monkeypatch):
        """Skips devices in cooldown and falls back to next candidate."""
        monkeypatch.setattr("heyvox.audio.mic._get_dead_input_device_names", lambda: set())
        add_device_cooldown("blackhole 2ch")
        devices = [
            {"name": "BlackHole 2ch", "maxInputChannels": 2},
            {"name": "MacBook Pro Microphone", "maxInputChannels": 1},
        ]
        pa = _mock_pa(devices)
        result = find_best_mic(pa, mic_priority=["BlackHole 2ch", "MacBook Pro Microphone"])
        # BlackHole is in cooldown, falls to MacBook Pro at index 1
        assert result == 1

    def test_dead_device_filtered(self, monkeypatch):
        """Skips devices reported dead by CoreAudio."""
        monkeypatch.setattr(
            "heyvox.audio.mic._get_dead_input_device_names",
            lambda: {"jabra link 380"},
        )
        devices = [
            {"name": "Jabra Link 380", "maxInputChannels": 1},
            {"name": "MacBook Pro Microphone", "maxInputChannels": 1},
        ]
        pa = _mock_pa(devices)
        result = find_best_mic(pa, mic_priority=["Jabra Link 380", "MacBook Pro Microphone"])
        # Jabra is dead per CoreAudio, falls to MacBook Pro at index 1
        assert result == 1

    def test_fallback_to_non_priority_device(self, monkeypatch):
        """Falls back to non-priority device when no priority device matches."""
        monkeypatch.setattr("heyvox.audio.mic._get_dead_input_device_names", lambda: set())
        devices = [
            {"name": "Unknown USB Mic", "maxInputChannels": 1},
        ]
        pa = _mock_pa(devices)
        result = find_best_mic(pa, mic_priority=["BlackHole 2ch"])
        # No priority match — falls back to Unknown USB Mic at index 0
        assert result == 0

    def test_returns_default_when_all_fail(self, monkeypatch):
        """Returns system default device index when all candidates fail to open."""
        monkeypatch.setattr("heyvox.audio.mic._get_dead_input_device_names", lambda: set())
        devices = [
            {"name": "Dead Mic", "maxInputChannels": 1},
        ]
        pa = _mock_pa(devices)
        # Simulate device that cannot be opened at all
        pa.open.side_effect = OSError("Device error")
        pa.get_default_input_device_info.return_value = {"index": 0}
        result = find_best_mic(pa, mic_priority=["Dead Mic"])
        # All fail → system default fallback
        assert result == 0

    def test_returns_none_when_no_devices(self, monkeypatch):
        """Returns None when no input devices are available at all."""
        monkeypatch.setattr("heyvox.audio.mic._get_dead_input_device_names", lambda: set())
        pa = _mock_pa([])
        pa.get_default_input_device_info.side_effect = IOError("No devices")
        result = find_best_mic(pa, mic_priority=["anything"])
        assert result is None


# ---------------------------------------------------------------------------
# TestDeviceCooldown
# ---------------------------------------------------------------------------

class TestDeviceCooldown:
    """Tests for device cooldown tracking."""

    def test_add_and_check_cooldown(self):
        """Device added to cooldown is reported as cooled down."""
        add_device_cooldown("Test Mic")
        assert is_device_cooled_down("test mic") is True

    def test_clear_cooldowns(self):
        """Clearing cooldowns releases all devices."""
        add_device_cooldown("Test Mic")
        clear_device_cooldowns()
        assert is_device_cooled_down("test mic") is False

    def test_cooldown_case_insensitive(self):
        """Cooldown lookup is case-insensitive."""
        add_device_cooldown("My Device")
        assert is_device_cooled_down("my device") is True
