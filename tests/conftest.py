"""Shared fixtures for heyvox tests."""

import os
import subprocess
import pytest


def _blackhole_available() -> bool:
    """Check if BlackHole virtual audio driver is installed."""
    try:
        r = subprocess.run(
            ["system_profiler", "SPAudioDataType"],
            capture_output=True, text=True, timeout=5,
        )
        return "BlackHole" in r.stdout
    except Exception:
        return False


def _vox_running() -> bool:
    """Check if heyvox process is running."""
    from heyvox.constants import HEYVOX_PID_FILE
    return os.path.exists(HEYVOX_PID_FILE)


# Markers for conditional test skipping
blackhole_installed = pytest.mark.skipif(
    not _blackhole_available(),
    reason="BlackHole virtual audio driver not installed (brew install blackhole-2ch)",
)

vox_running = pytest.mark.skipif(
    not _vox_running(),
    reason="Vox is not running (start with: heyvox start)",
)


def _audio_device_available() -> bool:
    """Return True if a real audio input device is accessible."""
    try:
        import pyaudio
        pa = pyaudio.PyAudio()
        try:
            count = pa.get_device_count()
            for i in range(count):
                info = pa.get_device_info_by_index(i)
                if info.get("maxInputChannels", 0) > 0:
                    return True
            return False
        finally:
            pa.terminate()
    except Exception:
        return False


requires_audio = pytest.mark.skipif(
    not _audio_device_available(),
    reason="No physical audio input device (skip in CI)",
)


@pytest.fixture(autouse=True)
def isolate_flags(tmp_path, monkeypatch):
    """Redirect all flag files to a temp directory so tests never interfere
    with a running HeyVox instance.

    Patches both the constants module AND all modules that import constants
    at module level (e.g. tts.py imports RECORDING_FLAG on load).
    """
    rec_flag = str(tmp_path / "heyvox-recording")
    tts_flag = str(tmp_path / "heyvox-tts-playing")
    cmd_file = str(tmp_path / "heyvox-tts-cmd")
    hud_sock = str(tmp_path / "heyvox-hud.sock")

    # Patch source of truth
    monkeypatch.setattr("heyvox.constants.RECORDING_FLAG", rec_flag)
    monkeypatch.setattr("heyvox.constants.TTS_PLAYING_FLAG", tts_flag)
    monkeypatch.setattr("heyvox.constants.TTS_CMD_FILE", cmd_file)
    monkeypatch.setattr("heyvox.constants.HUD_SOCKET_PATH", hud_sock)

    # Patch consumers that imported at module level
    try:
        monkeypatch.setattr("heyvox.audio.tts.RECORDING_FLAG", rec_flag)
        monkeypatch.setattr("heyvox.audio.tts.TTS_PLAYING_FLAG", tts_flag)
        monkeypatch.setattr("heyvox.audio.tts.TTS_CMD_FILE", cmd_file)
    except AttributeError:
        pass  # Module not imported yet — fine, will pick up patched constants

    try:
        monkeypatch.setattr("heyvox.main.RECORDING_FLAG", rec_flag)
        monkeypatch.setattr("heyvox.main.TTS_PLAYING_FLAG", tts_flag)
        monkeypatch.setattr("heyvox.main.HUD_SOCKET_PATH", hud_sock)
    except AttributeError:
        pass

    try:
        monkeypatch.setattr("heyvox.recording.RECORDING_FLAG", rec_flag)
    except AttributeError:
        pass

    yield {
        "recording_flag": rec_flag,
        "tts_flag": tts_flag,
        "cmd_file": cmd_file,
        "hud_sock": hud_sock,
    }


@pytest.fixture
def mock_config():
    """Return a HeyvoxConfig with test-friendly defaults."""
    from heyvox.config import HeyvoxConfig

    return HeyvoxConfig(
        target_mode="always-focused",
        enter_count=0,
        push_to_talk={"enabled": False, "key": "fn"},
        tts={"enabled": False},
        mic_priority=["BlackHole 2ch", "MacBook Pro Microphone"],
        log_file="/dev/null",
    )
