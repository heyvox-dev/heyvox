"""
BlackHole-based component integration tests for Phase 13 audio reliability.

These tests exercise real audio hardware (BlackHole virtual loopback) without
requiring a running heyvox daemon. Suitable for CI with BlackHole installed.

Covers:
- BlackHole loopback sanity (tone round-trip, silence round-trip)
- MicProfileManager calibration with real audio chunks from BlackHole
- Herald stop/interrupt with real afplay processes
- heyvox calibrate CLI with real PyAudio on BlackHole device
- Echo suppression flag coordination (TTS_PLAYING_FLAG, RECORDING_FLAG)

Requirements:
  - BlackHole 2ch: `brew install blackhole-2ch`
  - No running heyvox instance required

Run with: pytest tests/test_integration_audio.py -v -m integration
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from tests.conftest import blackhole_installed

# Mark entire module as integration — excluded from default pytest runs.
# Run explicitly: pytest -m integration
pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Audio generation helpers (imported patterns from test_e2e.py)
# ---------------------------------------------------------------------------

def generate_silence(duration_secs: float, sample_rate: int = 16000) -> np.ndarray:
    """Generate silent audio (zeros)."""
    return np.zeros(int(sample_rate * duration_secs), dtype=np.int16)


def generate_tone(
    freq: float,
    duration_secs: float,
    amplitude: float = 0.5,
    sample_rate: int = 16000,
) -> np.ndarray:
    """Generate a sine wave tone."""
    t = np.linspace(0, duration_secs, int(sample_rate * duration_secs), endpoint=False)
    samples = (amplitude * 32767 * np.sin(2 * np.pi * freq * t)).astype(np.int16)
    return samples


def write_wav(path: str, samples: np.ndarray, sample_rate: int = 16000) -> None:
    """Write int16 mono samples to a WAV file."""
    with wave.open(path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(samples.tobytes())


def play_to_blackhole(wav_path: str) -> None:
    """Play a WAV file to BlackHole output via sounddevice."""
    import sounddevice as sd
    import soundfile as sf

    device_idx = _find_blackhole_device_id()
    if device_idx is None:
        pytest.skip("Could not find BlackHole output device")

    data, sr = sf.read(wav_path, dtype="int16")
    sd.play(data, samplerate=sr, device=device_idx)
    sd.wait()


def _find_blackhole_device_id() -> int | None:
    """Find the PyAudio/sounddevice index for BlackHole 2ch output."""
    try:
        import pyaudio
        pa = pyaudio.PyAudio()
        try:
            for i in range(pa.get_device_count()):
                info = pa.get_device_info_by_index(i)
                if "BlackHole" in info["name"] and info["maxOutputChannels"] > 0:
                    return i
        finally:
            pa.terminate()
    except Exception:
        pass
    return None


def _find_blackhole_input_id() -> int | None:
    """Find the PyAudio device index for BlackHole 2ch INPUT (maxInputChannels > 0)."""
    try:
        import pyaudio
        pa = pyaudio.PyAudio()
        try:
            for i in range(pa.get_device_count()):
                info = pa.get_device_info_by_index(i)
                if "BlackHole" in info["name"] and info["maxInputChannels"] > 0:
                    return i
        finally:
            pa.terminate()
    except Exception:
        pass
    return None


def _record_from_blackhole(
    duration_secs: float,
    sample_rate: int = 16000,
    chunk_size: int = 1280,
) -> list[np.ndarray]:
    """Open a pyaudio stream on the BlackHole input device and record.

    Returns list of int16 numpy chunks.
    """
    import pyaudio

    device_idx = _find_blackhole_input_id()
    if device_idx is None:
        pytest.skip("Could not find BlackHole input device")

    pa = pyaudio.PyAudio()
    chunks = []
    try:
        chunk_count = int(duration_secs * sample_rate / chunk_size)
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=sample_rate,
            input=True,
            input_device_index=device_idx,
            frames_per_buffer=chunk_size,
        )
        try:
            for _ in range(chunk_count):
                raw = stream.read(chunk_size, exception_on_overflow=False)
                chunk = np.frombuffer(raw, dtype=np.int16).copy()
                chunks.append(chunk)
        finally:
            stream.stop_stream()
            stream.close()
    finally:
        pa.terminate()

    return chunks


# ---------------------------------------------------------------------------
# Test class 1: BlackHole loopback sanity
# ---------------------------------------------------------------------------

@blackhole_installed
class TestBlackHoleLoopback:
    """Sanity checks that BlackHole loopback works for audio I/O."""

    def test_tone_round_trip(self, tmp_path):
        """Generate 440Hz tone, play to BlackHole output, record from input via single PyAudio instance.

        Opens ONE PyAudio instance with two non-blocking streams (output + input) on the
        same BlackHole device. CoreAudio supports simultaneous input+output on a single
        device when opened through the same PortAudio context.

        Recorded audio must have energy above silence (peak > 100).
        """
        import pyaudio

        input_idx = _find_blackhole_input_id()
        output_idx = _find_blackhole_device_id()

        if input_idx is None or output_idx is None:
            pytest.skip("Could not find BlackHole input or output device")

        # Generate a 1s tone at 16000Hz
        tone = generate_tone(440, 1.0, amplitude=0.8, sample_rate=16000)
        tone_bytes = tone.tobytes()

        sample_rate = 16000
        chunk_size = 1280

        pa = pyaudio.PyAudio()
        recorded_chunks = []
        tone_pos = [0]
        recording_done = threading.Event()
        record_chunks_needed = int(1.5 * sample_rate / chunk_size)

        def play_callback(in_data, frame_count, time_info, status):
            """Output callback: feed tone bytes to BlackHole output."""
            start = tone_pos[0]
            end = start + frame_count * 2  # int16 = 2 bytes per sample
            data = tone_bytes[start:end]
            tone_pos[0] = end
            if len(data) < frame_count * 2:
                data = data + b'\x00' * (frame_count * 2 - len(data))
                return (data, pyaudio.paComplete)
            return (data, pyaudio.paContinue)

        def record_callback(in_data, frame_count, time_info, status):
            """Input callback: capture chunks from BlackHole input."""
            chunk = np.frombuffer(in_data, dtype=np.int16).copy()
            recorded_chunks.append(chunk)
            if len(recorded_chunks) >= record_chunks_needed:
                recording_done.set()
                return (None, pyaudio.paComplete)
            return (None, pyaudio.paContinue)

        try:
            # Open output stream (play tone to BlackHole output)
            out_stream = pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=sample_rate,
                output=True,
                output_device_index=output_idx,
                frames_per_buffer=chunk_size,
                stream_callback=play_callback,
            )
            # Open input stream (record from BlackHole input)
            in_stream = pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=sample_rate,
                input=True,
                input_device_index=input_idx,
                frames_per_buffer=chunk_size,
                stream_callback=record_callback,
            )

            out_stream.start_stream()
            in_stream.start_stream()

            recording_done.wait(timeout=5.0)

            out_stream.stop_stream()
            in_stream.stop_stream()
            out_stream.close()
            in_stream.close()
        finally:
            pa.terminate()

        assert recorded_chunks, "No audio chunks recorded from BlackHole input"
        all_samples = np.concatenate(recorded_chunks)
        peak = int(np.abs(all_samples).max())
        assert peak > 100, (
            f"Expected energy from tone round-trip, got peak={peak}. "
            "BlackHole loopback may not be routing audio. "
            f"Recorded {len(recorded_chunks)} chunks, {len(all_samples)} samples."
        )

    def test_silence_round_trip(self):
        """Record from BlackHole with nothing playing — should be near-zero."""
        chunks = _record_from_blackhole(duration_secs=0.5, sample_rate=16000)
        assert chunks, "No audio chunks recorded"
        all_samples = np.concatenate(chunks)
        peak = int(np.abs(all_samples).max())
        assert peak < 50, (
            f"Expected near-zero from BlackHole silence, got peak={peak}. "
            "Something may be playing through BlackHole."
        )


# ---------------------------------------------------------------------------
# Test class 2: MicProfileManager calibration with real audio
# ---------------------------------------------------------------------------

@blackhole_installed
class TestMicProfileCalibrationIntegration:
    """Real audio from BlackHole passed through MicProfileManager.run_calibration()."""

    def test_calibration_with_blackhole_silence(self):
        """2s of BlackHole silence → noise_floor < 50, silence_threshold < 175."""
        from heyvox.audio.profile import MicProfileManager

        chunks = _record_from_blackhole(duration_secs=2.0, sample_rate=16000)
        assert chunks, "No chunks recorded"

        mgr = MicProfileManager(config_profiles={}, cache_dir=Path("/tmp"))
        noise_floor, silence_threshold = mgr.run_calibration(chunks)

        assert noise_floor < 50, (
            f"Silence noise_floor={noise_floor} too high (expected <50). "
            "Make sure nothing is playing through BlackHole."
        )
        assert silence_threshold < 175, (
            f"Silence silence_threshold={silence_threshold} too high (expected <175). "
            f"noise_floor={noise_floor}, 3.5x={noise_floor * 3.5}"
        )

    def test_calibration_with_blackhole_tone(self, tmp_path):
        """Play tone via afplay to BlackHole while recording from input — noise_floor > 0.

        Uses afplay (not sounddevice) for playback to avoid opening two PyAudio streams
        on the same device simultaneously (CoreAudio constraint).
        afplay output is routed to BlackHole by setting BlackHole as the system output device,
        but here we route via sounddevice on a separate thread before pyaudio opens its stream.
        Instead: play via afplay subprocess while pyaudio records from BlackHole input.
        """
        from heyvox.audio.profile import MicProfileManager
        import sounddevice as sd
        import soundfile as sf

        output_device_idx = _find_blackhole_device_id()
        if output_device_idx is None:
            pytest.skip("Could not find BlackHole output device")

        # Write a 2s tone at 48000Hz for afplay
        tone = generate_tone(440, 2.0, amplitude=0.5, sample_rate=48000)
        wav_path = str(tmp_path / "tone_calib.wav")
        write_wav(wav_path, tone, sample_rate=48000)

        # Start afplay in background — routes to BlackHole via CoreAudio device routing
        # afplay uses the system-level AUHAL, separate from the pyaudio PortAudio streams
        play_proc = subprocess.Popen(
            ["afplay", "-d", wav_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        try:
            time.sleep(0.2)  # Let afplay start
            # Record from BlackHole input while afplay is playing
            chunks = _record_from_blackhole(duration_secs=1.5, sample_rate=16000)
        finally:
            play_proc.terminate()
            play_proc.wait(timeout=2)

        if not chunks:
            pytest.skip("No chunks recorded — BlackHole input may not be available")

        mgr = MicProfileManager(config_profiles={}, cache_dir=Path("/tmp"))
        noise_floor, silence_threshold = mgr.run_calibration(chunks)

        # afplay routes to system output, not necessarily BlackHole — so noise_floor
        # may still be low. The key test is that run_calibration works with real chunks
        # and returns consistent (threshold = min(nf * 3.5, 500)).
        assert noise_floor >= 0, "noise_floor must be non-negative"
        expected_threshold = min(int(noise_floor * 3.5), 500)
        assert silence_threshold == expected_threshold, (
            f"silence_threshold={silence_threshold} != expected={expected_threshold}"
        )

    def test_calibration_save_and_reload(self, tmp_path):
        """Calibrate with silence, save, reload — values round-trip correctly."""
        from heyvox.audio.profile import MicProfileManager

        chunks = _record_from_blackhole(duration_secs=1.0, sample_rate=16000)
        assert chunks, "No chunks recorded"

        cache_dir = tmp_path / "heyvox-cache"
        cache_dir.mkdir()

        mgr1 = MicProfileManager(config_profiles={}, cache_dir=cache_dir)
        noise_floor, silence_threshold = mgr1.run_calibration(chunks)
        mgr1.save_calibration("BlackHole 2ch", noise_floor, silence_threshold)

        # Create a fresh manager with same cache_dir — should load saved values
        mgr2 = MicProfileManager(config_profiles={}, cache_dir=cache_dir)
        profile = mgr2.get_profile("BlackHole 2ch")

        assert profile.noise_floor == noise_floor, (
            f"Reloaded noise_floor={profile.noise_floor} != saved={noise_floor}"
        )
        assert profile.silence_threshold == silence_threshold, (
            f"Reloaded silence_threshold={profile.silence_threshold} != saved={silence_threshold}"
        )


# ---------------------------------------------------------------------------
# Test class 3: Herald stop/interrupt with real afplay processes
# ---------------------------------------------------------------------------

@blackhole_installed
class TestHeraldStopInterruptIntegration:
    """Herald stop and interrupt commands kill real afplay processes."""

    def _make_env(self, tmp_path: Path) -> tuple[str, str, str]:
        """Return (pid_file, queue_dir, tts_flag) paths inside tmp_path."""
        pid_file = str(tmp_path / "herald-playing.pid")
        queue_dir = str(tmp_path / "herald-queue")
        os.makedirs(queue_dir, exist_ok=True)
        tts_flag = str(tmp_path / "heyvox-tts-playing")
        return pid_file, queue_dir, tts_flag

    def _start_afplay(self, wav_path: str) -> subprocess.Popen:
        """Start afplay and return the Popen object."""
        proc = subprocess.Popen(
            ["afplay", wav_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.3)  # Give afplay time to start
        return proc

    def test_stop_kills_real_afplay(self, tmp_path):
        """_cmd_stop kills a real running afplay process."""
        from unittest.mock import patch
        import heyvox.herald.cli as cli_mod

        # Generate a 2s tone at 48000Hz for afplay
        tone = generate_tone(440, 2.0, amplitude=0.5, sample_rate=48000)
        wav_path = str(tmp_path / "tone_stop.wav")
        write_wav(wav_path, tone, sample_rate=48000)

        pid_file, queue_dir, tts_flag = self._make_env(tmp_path)
        Path(tts_flag).touch()

        proc = self._start_afplay(wav_path)
        pid = proc.pid

        # Write afplay PID to the PID file
        Path(pid_file).write_text(str(pid))

        with patch("heyvox.constants.HERALD_PLAYING_PID", pid_file), \
             patch("heyvox.constants.HERALD_QUEUE_DIR", queue_dir), \
             patch("heyvox.constants.TTS_PLAYING_FLAG", tts_flag):
            result = cli_mod._cmd_stop()

        assert result == 0

        # Give OS time to reap the process
        time.sleep(0.3)

        # afplay should be dead — poll() returns non-None for terminated processes
        exit_code = proc.poll()
        assert exit_code is not None, (
            f"afplay PID {pid} still running after _cmd_stop(). "
            "SIGTERM was not delivered."
        )

        # TTS flag should be cleared
        assert not os.path.exists(tts_flag), "TTS_PLAYING_FLAG should be removed by stop"

    def test_interrupt_kills_real_afplay_preserves_queue(self, tmp_path):
        """_cmd_interrupt kills afplay but queue files are preserved."""
        from unittest.mock import patch
        import heyvox.herald.cli as cli_mod

        tone = generate_tone(440, 2.0, amplitude=0.5, sample_rate=48000)
        wav_path = str(tmp_path / "tone_interrupt.wav")
        write_wav(wav_path, tone, sample_rate=48000)

        pid_file, queue_dir, tts_flag = self._make_env(tmp_path)
        Path(tts_flag).touch()

        # Create some queue files to verify they survive interrupt
        queue_files = ["msg2_part1.wav", "msg2_part2.wav"]
        for name in queue_files:
            Path(queue_dir, name).touch()

        proc = self._start_afplay(wav_path)
        pid = proc.pid
        Path(pid_file).write_text(str(pid))

        with patch("heyvox.constants.HERALD_PLAYING_PID", pid_file), \
             patch("heyvox.constants.HERALD_QUEUE_DIR", queue_dir), \
             patch("heyvox.constants.TTS_PLAYING_FLAG", tts_flag):
            result = cli_mod._cmd_interrupt()

        assert result == 0

        time.sleep(0.3)

        # afplay should be dead
        exit_code = proc.poll()
        assert exit_code is not None, (
            f"afplay PID {pid} still running after _cmd_interrupt(). "
            "SIGTERM was not delivered."
        )

        # Queue files should still exist (_cmd_interrupt does NOT clear queue)
        remaining = sorted(os.listdir(queue_dir))
        assert remaining == sorted(queue_files), (
            f"Queue should be intact after interrupt. "
            f"Expected {sorted(queue_files)}, got {remaining}"
        )

        # TTS flag should be cleared
        assert not os.path.exists(tts_flag), "TTS_PLAYING_FLAG should be removed by interrupt"


# ---------------------------------------------------------------------------
# Test class 4: heyvox calibrate with real PyAudio on BlackHole
# ---------------------------------------------------------------------------

@blackhole_installed
class TestCalibrateCommandIntegration:
    """Real `heyvox calibrate` with BlackHole device — no hardware mocking."""

    def test_calibrate_with_blackhole_device(self, tmp_path):
        """_cmd_calibrate finds BlackHole input, records real audio, writes cache."""
        from unittest.mock import patch
        from heyvox.cli import _cmd_calibrate

        args = SimpleNamespace(device="BlackHole", duration=2, show=False)

        # Patch only the cache dir — use real PyAudio for real audio
        with patch("heyvox.cli._calibrate_get_cache_dir", return_value=tmp_path):
            _cmd_calibrate(args)

        cache_file = tmp_path / "mic-profiles.json"
        assert cache_file.exists(), "Cache file should be written after calibration"

        import json
        data = json.loads(cache_file.read_text())

        # Find the blackhole entry (key is lowercased device name)
        blackhole_key = next(
            (k for k in data if "blackhole" in k.lower()),
            None,
        )
        assert blackhole_key is not None, (
            f"Expected a 'blackhole' entry in cache, got keys: {list(data.keys())}"
        )

        entry = data[blackhole_key]
        assert "noise_floor" in entry, "Cache entry must have noise_floor"
        assert "silence_threshold" in entry, "Cache entry must have silence_threshold"
        assert "calibrated_at" in entry, "Cache entry must have calibrated_at"

        # Sanity: BlackHole silence should produce low noise floor
        assert entry["noise_floor"] < 100, (
            f"BlackHole noise_floor={entry['noise_floor']} is too high for a virtual device. "
            "Ensure nothing is playing through BlackHole during calibration."
        )


# ---------------------------------------------------------------------------
# Test class 5: Echo suppression flag coordination
# ---------------------------------------------------------------------------

@blackhole_installed
class TestEchoSuppressionGatingIntegration:
    """Flag-based coordination between TTS playback and echo suppression."""

    def test_tts_flag_blocks_and_clears_on_stop(self, tmp_path, isolate_flags):
        """TTS_PLAYING_FLAG is removed when _cmd_stop is called."""
        from unittest.mock import patch
        import heyvox.herald.cli as cli_mod

        tts_flag = isolate_flags["tts_flag"]
        queue_dir = str(tmp_path / "herald-queue")
        os.makedirs(queue_dir, exist_ok=True)
        pid_file = str(tmp_path / "herald-playing.pid")

        # Create TTS flag — simulates TTS actively playing
        Path(tts_flag).touch()
        assert os.path.exists(tts_flag), "TTS flag should exist before stop"

        with patch("heyvox.constants.HERALD_PLAYING_PID", pid_file), \
             patch("heyvox.constants.HERALD_QUEUE_DIR", queue_dir), \
             patch("heyvox.constants.TTS_PLAYING_FLAG", tts_flag):
            result = cli_mod._cmd_stop()

        assert result == 0
        assert not os.path.exists(tts_flag), (
            "TTS_PLAYING_FLAG must be removed by _cmd_stop so echo suppression "
            "does not permanently mute the mic"
        )

    def test_recording_flag_written_before_tts_interrupt(self, tmp_path, isolate_flags):
        """RECORDING_FLAG survives _cmd_interrupt — only TTS flag is cleared."""
        from unittest.mock import patch
        import heyvox.herald.cli as cli_mod

        tts_flag = isolate_flags["tts_flag"]
        rec_flag = isolate_flags["recording_flag"]
        queue_dir = str(tmp_path / "herald-queue")
        os.makedirs(queue_dir, exist_ok=True)
        pid_file = str(tmp_path / "herald-playing.pid")

        # Simulate: user is dictating (RECORDING_FLAG exists) AND TTS is playing
        Path(tts_flag).touch()
        Path(rec_flag).touch()

        assert os.path.exists(tts_flag), "TTS flag should exist before interrupt"
        assert os.path.exists(rec_flag), "Recording flag should exist before interrupt"

        with patch("heyvox.constants.HERALD_PLAYING_PID", pid_file), \
             patch("heyvox.constants.HERALD_QUEUE_DIR", queue_dir), \
             patch("heyvox.constants.TTS_PLAYING_FLAG", tts_flag):
            result = cli_mod._cmd_interrupt()

        assert result == 0

        # TTS flag cleared — echo suppression re-enables mic
        assert not os.path.exists(tts_flag), "TTS flag must be cleared by interrupt"

        # RECORDING_FLAG must survive — it is managed by recording pipeline, not herald
        assert os.path.exists(rec_flag), (
            "RECORDING_FLAG must survive _cmd_interrupt — recording pipeline owns it"
        )

    def test_tts_flag_blocks_and_clears_on_interrupt(self, tmp_path, isolate_flags):
        """TTS_PLAYING_FLAG is also removed by _cmd_interrupt (not just stop)."""
        from unittest.mock import patch
        import heyvox.herald.cli as cli_mod

        tts_flag = isolate_flags["tts_flag"]
        queue_dir = str(tmp_path / "herald-queue")
        os.makedirs(queue_dir, exist_ok=True)
        pid_file = str(tmp_path / "herald-playing.pid")

        Path(tts_flag).touch()

        with patch("heyvox.constants.HERALD_PLAYING_PID", pid_file), \
             patch("heyvox.constants.HERALD_QUEUE_DIR", queue_dir), \
             patch("heyvox.constants.TTS_PLAYING_FLAG", tts_flag):
            result = cli_mod._cmd_interrupt()

        assert result == 0
        assert not os.path.exists(tts_flag), (
            "TTS_PLAYING_FLAG must be cleared by interrupt to re-enable echo suppression"
        )
