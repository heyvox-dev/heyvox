"""
End-to-end tests for heyvox voice pipeline.

These tests use BlackHole (virtual audio loopback) to play audio into heyvox's
microphone input and verify the full pipeline: wake word → recording → STT →
text injection.

Requirements:
  - BlackHole 2ch: `brew install blackhole-2ch`
  - Vox running with BlackHole as mic: set mic_priority to ["BlackHole 2ch"] in config
  - macOS (uses afplay, osascript, etc.)

Run with: pytest tests/test_e2e.py -v --timeout=60

The tests are skipped automatically if BlackHole is not installed or heyvox is not running.
"""

import json
import os
import subprocess
import tempfile
import time
import wave
import struct
import math

import numpy as np
import pytest

from tests.conftest import blackhole_installed, vox_running


# --- Audio generation helpers ---

def generate_silence(duration_secs: float, sample_rate: int = 16000) -> np.ndarray:
    """Generate silent audio (zeros)."""
    return np.zeros(int(sample_rate * duration_secs), dtype=np.int16)


def generate_tone(freq: float, duration_secs: float, amplitude: float = 0.5,
                  sample_rate: int = 16000) -> np.ndarray:
    """Generate a sine wave tone."""
    t = np.linspace(0, duration_secs, int(sample_rate * duration_secs), endpoint=False)
    samples = (amplitude * 32767 * np.sin(2 * np.pi * freq * t)).astype(np.int16)
    return samples


def write_wav(path: str, samples: np.ndarray, sample_rate: int = 16000):
    """Write int16 samples to a WAV file."""
    with wave.open(path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(samples.tobytes())


def play_to_blackhole(wav_path: str):
    """Play a WAV file through BlackHole output device.

    Uses sounddevice (which accepts PyAudio-style device indices) instead of
    afplay (which needs CoreAudio UIDs). The audio appears on BlackHole's
    input, which heyvox reads as its microphone.
    """
    import sounddevice as sd
    import soundfile as sf

    device_idx = _find_blackhole_device_id()
    if device_idx is None:
        pytest.skip("Could not find BlackHole device")

    data, sr = sf.read(wav_path, dtype="int16")
    sd.play(data, samplerate=sr, device=device_idx)
    sd.wait()


def _find_blackhole_device_id() -> int | None:
    """Find the PyAudio/sounddevice device index for BlackHole 2ch output."""
    try:
        import pyaudio
        pa = pyaudio.PyAudio()
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if "BlackHole" in info["name"] and info["maxOutputChannels"] > 0:
                pa.terminate()
                return i
        pa.terminate()
    except Exception:
        pass
    return None


def read_vox_log(since_time: float) -> list[str]:
    """Read heyvox log lines since a given timestamp."""
    # Check both possible log paths (config may override default)
    log_path = "/tmp/vox.log" if os.path.exists("/tmp/vox.log") else "/tmp/heyvox.log"
    if not os.path.exists(log_path):
        return []
    lines = []
    with open(log_path) as f:
        for line in f:
            # Lines are formatted as [HH:MM:SS] ...
            try:
                ts_str = line[1:9]  # "HH:MM:SS"
                h, m, s = ts_str.split(":")
                day_secs = int(h) * 3600 + int(m) * 60 + int(s)
                ref_struct = time.localtime(since_time)
                ref_secs = ref_struct.tm_hour * 3600 + ref_struct.tm_min * 60 + ref_struct.tm_sec
                if day_secs >= ref_secs:
                    lines.append(line.strip())
            except (ValueError, IndexError):
                continue
    return lines


def wait_for_log_entry(pattern: str, timeout: float = 15.0, since: float = None) -> str | None:
    """Wait for a log line matching a pattern to appear."""
    if since is None:
        since = time.time()
    deadline = time.time() + timeout
    while time.time() < deadline:
        for line in read_vox_log(since):
            if pattern in line:
                return line
        time.sleep(0.3)
    return None


# --- Fixtures ---

@pytest.fixture
def test_audio_dir():
    """Temporary directory for test WAV files."""
    with tempfile.TemporaryDirectory(prefix="heyvox-e2e-") as d:
        yield d


@pytest.fixture
def log_start_time():
    """Capture current time to filter log entries."""
    return time.time()


# --- Tests ---

@blackhole_installed
@vox_running
class TestWakeWordDetection:
    """Test that heyvox detects wake words played through BlackHole."""

    def test_wake_word_triggers_recording(self, test_audio_dir, log_start_time):
        """Play a pre-recorded wake word and verify recording starts."""
        # This test requires a WAV file of someone saying the wake word.
        # For automated testing, we'll use the wake word audio from cues/ or
        # a pre-recorded sample. This is a framework — the actual WAV needs
        # to be recorded once and committed to tests/fixtures/.
        wake_word_wav = os.path.join(
            os.path.dirname(__file__), "fixtures", "hey_jarvis.wav"
        )
        if not os.path.exists(wake_word_wav):
            pytest.skip(
                "Wake word audio fixture not found. "
                "Record 'hey jarvis' to tests/fixtures/hey_jarvis.wav"
            )

        play_to_blackhole(wake_word_wav)
        line = wait_for_log_entry("Recording started", timeout=10, since=log_start_time)
        assert line is not None, "Wake word did not trigger recording"


@blackhole_installed
@vox_running
class TestFullPipeline:
    """Test the full pipeline: wake word → STT → text injection."""

    def test_transcription_output(self, test_audio_dir, log_start_time):
        """Play wake word + speech, verify transcription appears in log."""
        fixtures_dir = os.path.join(os.path.dirname(__file__), "fixtures")
        full_wav = os.path.join(fixtures_dir, "hey_jarvis_test_phrase.wav")
        if not os.path.exists(full_wav):
            pytest.skip(
                "Full pipeline audio fixture not found. "
                "Record 'hey jarvis [test phrase] hey jarvis' "
                "to tests/fixtures/hey_jarvis_test_phrase.wav"
            )

        play_to_blackhole(full_wav)

        # Wait for transcription
        line = wait_for_log_entry("Transcription", timeout=20, since=log_start_time)
        assert line is not None, "No transcription produced"

        # Wait for injection
        inject_line = wait_for_log_entry("Injecting via", timeout=5, since=log_start_time)
        assert inject_line is not None, "Text was not injected"


@blackhole_installed
@vox_running
class TestDuplicateDetection:
    """Test that the same message is not spoken twice (dedup)."""

    def test_no_duplicate_tts(self, test_audio_dir, log_start_time):
        """Trigger a response and verify TTS is generated only once."""
        # This test would require:
        # 1. Play wake word + command
        # 2. Wait for the agent to respond with a TTS block
        # 3. Check logs that only one TTS generation occurred
        #
        # For now this is a framework placeholder — the actual test needs
        # the full Claude Code loop running.
        pytest.skip("Requires full Claude Code loop — manual test for now")


@blackhole_installed
@vox_running
class TestMediaPause:
    """Test that media pauses during recording."""

    def test_media_pauses_on_recording(self, log_start_time):
        """Verify media pause is logged when recording starts."""
        fixtures_dir = os.path.join(os.path.dirname(__file__), "fixtures")
        wake_word_wav = os.path.join(fixtures_dir, "hey_jarvis.wav")
        if not os.path.exists(wake_word_wav):
            pytest.skip("Wake word fixture not found")

        play_to_blackhole(wake_word_wav)
        line = wait_for_log_entry("pause_media", timeout=10, since=log_start_time)
        # Media module should at least log its attempt
        assert line is not None, "Media pause was not attempted during recording"


@blackhole_installed
@vox_running
class TestTimingBaseline:
    """Measure timing baselines for regression detection."""

    def test_stt_latency(self, test_audio_dir, log_start_time):
        """Measure STT transcription latency.

        Baseline: whisper-small-mlx should transcribe <20s audio in <2s.
        """
        fixtures_dir = os.path.join(os.path.dirname(__file__), "fixtures")
        full_wav = os.path.join(fixtures_dir, "hey_jarvis_test_phrase.wav")
        if not os.path.exists(full_wav):
            pytest.skip("Full pipeline audio fixture not found")

        play_to_blackhole(full_wav)

        # Parse transcription timing from log: "Transcription (1.2s): ..."
        line = wait_for_log_entry("Transcription (", timeout=20, since=log_start_time)
        assert line is not None, "No transcription found"

        # Extract duration
        import re
        match = re.search(r"Transcription \(([\d.]+)s\)", line)
        assert match, f"Could not parse transcription duration from: {line}"
        duration = float(match.group(1))
        assert duration < 5.0, f"STT too slow: {duration}s (expected <5s)"


# --- Setup instructions ---

class TestE2ESetupInstructions:
    """Not real tests — prints setup instructions when fixtures are missing."""

    def test_setup_info(self):
        """Prints E2E test setup instructions."""
        fixtures_dir = os.path.join(os.path.dirname(__file__), "fixtures")
        has_fixtures = os.path.isdir(fixtures_dir) and any(
            f.endswith(".wav") for f in os.listdir(fixtures_dir)
        ) if os.path.isdir(fixtures_dir) else False

        if has_fixtures:
            return  # Fixtures exist, nothing to report

        # This test always passes — it's informational
        print("\n")
        print("=" * 60)
        print("E2E TEST SETUP")
        print("=" * 60)
        print()
        print("To run full E2E tests, you need:")
        print()
        print("1. Install BlackHole virtual audio:")
        print("   brew install blackhole-2ch")
        print()
        print("2. Record wake word fixtures:")
        print("   mkdir -p tests/fixtures")
        print("   # Record yourself saying 'hey jarvis':")
        print("   rec tests/fixtures/hey_jarvis.wav trim 0 3")
        print("   # Record 'hey jarvis [test phrase] hey jarvis':")
        print("   rec tests/fixtures/hey_jarvis_test_phrase.wav trim 0 10")
        print()
        print("3. Configure heyvox to use BlackHole as mic:")
        print("   # In ~/.config/heyvox/config.yaml:")
        print("   mic_priority:")
        print("     - BlackHole 2ch")
        print()
        print("4. Start heyvox and run E2E tests:")
        print("   heyvox start")
        print("   pytest tests/test_e2e.py -v")
        print("=" * 60)
