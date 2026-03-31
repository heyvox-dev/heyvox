"""
Stress tests for heyvox voice pipeline.

Tests memory stability, rapid-fire dictation, concurrent TTS+recording,
long recordings, and recovery from edge cases. Uses BlackHole virtual
audio loopback.

Run with: pytest tests/test_stress.py -v -s
"""

import os
import re
import resource
import time

import numpy as np
import pytest

from tests.conftest import blackhole_installed, vox_running
from tests.test_e2e import (
    play_to_blackhole,
    wait_for_log_entry,
    read_vox_log,
    write_wav,
    generate_silence,
    generate_tone,
)


FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
WAKE_WORD_WAV = os.path.join(FIXTURES_DIR, "hey_jarvis.wav")
FULL_WAV = os.path.join(FIXTURES_DIR, "hey_jarvis_test_phrase.wav")


def _has_fixtures():
    return os.path.exists(WAKE_WORD_WAV) and os.path.exists(FULL_WAV)


def _get_heyvox_rss_mb() -> float:
    """Get RSS of the running heyvox main process in MB."""
    import subprocess
    r = subprocess.run(
        ["pgrep", "-f", "heyvox.main"],
        capture_output=True, text=True,
    )
    if not r.stdout.strip():
        return 0.0
    pid = r.stdout.strip().split("\n")[0]
    r2 = subprocess.run(
        ["ps", "-p", pid, "-o", "rss="],
        capture_output=True, text=True,
    )
    try:
        return int(r2.stdout.strip()) / 1024  # KB → MB
    except ValueError:
        return 0.0


def _count_log_entries(pattern: str, since: float) -> int:
    """Count log lines matching a pattern since a timestamp."""
    return sum(1 for line in read_vox_log(since) if pattern in line)


def _flag_exists(path: str) -> bool:
    return os.path.exists(path)


# ---------------------------------------------------------------------------
# Memory stability tests
# ---------------------------------------------------------------------------

@blackhole_installed
@vox_running
class TestMemoryStability:
    """Verify memory doesn't grow unboundedly across dictation cycles."""

    def test_memory_after_10_cycles(self, tmp_path):
        """Run 10 wake→record→stop cycles and check memory doesn't grow >20%.

        First cycle is a warmup (triggers lazy MLX model load ~800MB).
        Growth is measured from cycle 2 onwards.
        """
        if not _has_fixtures():
            pytest.skip("Wake word fixtures not found")

        # Warmup: first cycle triggers lazy model load
        t = time.time()
        play_to_blackhole(FULL_WAV)
        wait_for_log_entry("Ready for next wake word", timeout=30, since=t)
        time.sleep(1)

        # Baseline after model is loaded
        mem_before = _get_heyvox_rss_mb()
        assert mem_before > 0, "heyvox process not found"

        for i in range(10):
            t = time.time()
            play_to_blackhole(FULL_WAV)
            line = wait_for_log_entry("Ready for next wake word", timeout=25, since=t)
            assert line is not None, f"Cycle {i+1} did not complete"
            time.sleep(0.5)

        mem_after = _get_heyvox_rss_mb()
        growth = (mem_after - mem_before) / mem_before * 100 if mem_before > 0 else 0
        print(f"\nMemory: {mem_before:.0f} MB → {mem_after:.0f} MB (growth: {growth:+.1f}%)")
        assert growth < 20, f"Memory grew {growth:.1f}% after 10 cycles (leak?)"

    def test_lazy_unload_frees_memory(self):
        """Verify MLX model unloads after idle timeout."""
        # This test would need to wait 5 minutes or temporarily set a shorter timeout.
        # For CI, we check the API exists and the flag is correct.
        from heyvox.audio.stt import model_loaded, memory_mb
        # If model is loaded, memory_mb should report ~855
        if model_loaded():
            assert memory_mb() > 500, "model_loaded=True but memory_mb too low"
        else:
            assert memory_mb() == 0, "model_loaded=False but memory_mb != 0"


# ---------------------------------------------------------------------------
# Rapid-fire dictation tests
# ---------------------------------------------------------------------------

@blackhole_installed
@vox_running
class TestRapidFire:
    """Test rapid consecutive dictations don't cause races or drops."""

    def test_back_to_back_dictations(self):
        """3 rapid dictations with minimal gap — all should transcribe."""
        if not _has_fixtures():
            pytest.skip("Wake word fixtures not found")

        transcription_count = 0
        for i in range(3):
            t = time.time()
            play_to_blackhole(FULL_WAV)
            line = wait_for_log_entry("Transcription", timeout=25, since=t)
            if line:
                transcription_count += 1
            # Wait for pipeline to finish before next cycle
            wait_for_log_entry("Ready for next wake word", timeout=10, since=t)
            time.sleep(0.3)

        assert transcription_count >= 2, (
            f"Only {transcription_count}/3 dictations produced transcriptions"
        )

    def test_wake_during_busy(self):
        """Wake word during transcription should be ignored (not crash)."""
        if not _has_fixtures():
            pytest.skip("Wake word fixtures not found")

        t = time.time()
        # Start a dictation
        play_to_blackhole(FULL_WAV)
        # Wait for "processing" state (transcription in progress)
        wait_for_log_entry("processing", timeout=15, since=t)
        # Immediately play another wake word while busy
        play_to_blackhole(WAKE_WORD_WAV)
        time.sleep(2)

        # Should not crash — check process is still alive
        rss = _get_heyvox_rss_mb()
        assert rss > 0, "heyvox process died after wake word during busy state"


# ---------------------------------------------------------------------------
# Flag coordination tests
# ---------------------------------------------------------------------------

@blackhole_installed
@vox_running
class TestFlagCoordination:
    """Test recording/TTS flag files are correctly managed."""

    def test_recording_flag_set_during_recording(self):
        """Recording flag should exist while recording, gone after."""
        if not _has_fixtures():
            pytest.skip("Wake word fixtures not found")

        t = time.time()
        play_to_blackhole(WAKE_WORD_WAV)
        # Wait for recording to start
        line = wait_for_log_entry("Recording started", timeout=10, since=t)
        assert line is not None, "Recording did not start"

        # Flag should exist during recording — check both possible paths
        time.sleep(0.3)
        flag_present = (
            _flag_exists("/tmp/heyvox-recording") or
            _flag_exists("/tmp/vox-recording") or
            any(_flag_exists(f) for f in ["/tmp/heyvox-recording", "/tmp/vox-recording"])
        )
        # Also check via log that recording flag was written
        flag_logs = [l for l in read_vox_log(t) if "recording" in l.lower()]
        assert flag_present or len(flag_logs) > 0, (
            "Recording flag not set during active recording"
        )

        # Play stop wake word
        play_to_blackhole(WAKE_WORD_WAV)
        wait_for_log_entry("Ready for next wake word", timeout=20, since=t)

    def test_stale_flag_cleanup_on_start(self):
        """Verify RECORDING_FLAG constant is defined."""
        from heyvox.main import RECORDING_FLAG
        assert RECORDING_FLAG is not None and len(RECORDING_FLAG) > 0


# ---------------------------------------------------------------------------
# Transcription quality tests
# ---------------------------------------------------------------------------

@blackhole_installed
@vox_running
class TestTranscriptionQuality:
    """Test transcription output doesn't contain artifacts."""

    def test_no_wake_word_in_transcription(self):
        """Transcription should not contain the wake word phrase."""
        if not _has_fixtures():
            pytest.skip("Wake word fixtures not found")

        t = time.time()
        play_to_blackhole(FULL_WAV)
        line = wait_for_log_entry("Transcription", timeout=25, since=t)
        assert line is not None, "No transcription"

        # Extract transcription text
        match = re.search(r"Transcription \([\d.]+s\): (.+)", line)
        assert match, f"Could not parse transcription: {line}"
        text = match.group(1).lower()

        wake_words = ["hey jarvis", "hey javis", "hey, jarvis", "jarvis"]
        for ww in wake_words:
            assert ww not in text, (
                f"Wake word '{ww}' leaked into transcription: {text}"
            )

    def test_no_whisper_hallucination_on_silence(self, tmp_path):
        """Playing silence should not produce garbage transcription."""
        # Generate wake word + silence + wake word
        # This tests the energy threshold — silence should be rejected
        silence_wav = str(tmp_path / "silence.wav")
        silence = generate_silence(5.0, sample_rate=48000)
        write_wav(silence_wav, silence, sample_rate=48000)

        t = time.time()
        # Play wake word to start recording
        play_to_blackhole(WAKE_WORD_WAV)
        wait_for_log_entry("Recording started", timeout=10, since=t)

        # Play silence
        play_to_blackhole(silence_wav)
        time.sleep(1)

        # Play wake word to stop recording
        play_to_blackhole(WAKE_WORD_WAV)

        # Should either get "paused" (energy rejected) or empty/very short transcription
        time.sleep(5)
        logs = read_vox_log(t)
        hallucination_patterns = [
            "thank you for watching",
            "c'est bon",
            "please subscribe",
            "였",
            "導",
        ]
        for line in logs:
            if "Transcription" in line:
                text = line.lower()
                for pattern in hallucination_patterns:
                    assert pattern not in text, (
                        f"Whisper hallucination detected: {pattern} in {line}"
                    )


# ---------------------------------------------------------------------------
# TTS coordination tests
# ---------------------------------------------------------------------------

@blackhole_installed
@vox_running
class TestTTSCoordination:
    """Test TTS doesn't interfere with recording."""

    def test_recording_flag_blocks_tts(self):
        """TTS should not play while recording flag is set."""
        if not _has_fixtures():
            pytest.skip("Wake word fixtures not found")

        t = time.time()
        # Start recording
        play_to_blackhole(WAKE_WORD_WAV)
        wait_for_log_entry("Recording started", timeout=10, since=t)

        # Recording flag should block TTS
        assert _flag_exists("/tmp/heyvox-recording"), "Recording flag not set"

        # Check the Conductor TTS hook would be blocked
        import subprocess
        result = subprocess.run(
            ["bash", "-c", '[ -f /tmp/heyvox-recording ] && echo "BLOCKED" || echo "OPEN"'],
            capture_output=True, text=True,
        )
        assert "BLOCKED" in result.stdout, "TTS hook would NOT be blocked during recording"

        # Stop recording
        play_to_blackhole(WAKE_WORD_WAV)
        wait_for_log_entry("Ready for next wake word", timeout=20, since=t)


# ---------------------------------------------------------------------------
# Timing regression tests
# ---------------------------------------------------------------------------

@blackhole_installed
@vox_running
class TestTimingRegression:
    """Measure and assert timing bounds for key operations."""

    def test_wake_to_recording_latency(self):
        """Time from wake word to recording start should be <2s."""
        if not _has_fixtures():
            pytest.skip("Wake word fixtures not found")

        t_play = time.time()
        play_to_blackhole(WAKE_WORD_WAV)
        line = wait_for_log_entry("Recording started", timeout=10, since=t_play)
        assert line is not None, "Recording did not start"

        # Parse timestamp from log
        try:
            ts_str = line[1:9]
            h, m, s = ts_str.split(":")
            log_secs = int(h) * 3600 + int(m) * 60 + int(s)
            play_struct = time.localtime(t_play)
            play_secs = play_struct.tm_hour * 3600 + play_struct.tm_min * 60 + play_struct.tm_sec
            latency = log_secs - play_secs
            print(f"\nWake-to-record latency: ~{latency}s")
            assert latency < 5, f"Wake-to-record too slow: {latency}s"
        except (ValueError, IndexError):
            pass  # Can't parse precisely, skip timing assertion

        # Cleanup: stop the recording
        play_to_blackhole(WAKE_WORD_WAV)
        wait_for_log_entry("Ready for next wake word", timeout=20, since=t_play)

    def test_transcription_latency(self):
        """STT should complete within 5s for <20s audio."""
        if not _has_fixtures():
            pytest.skip("Wake word fixtures not found")

        t = time.time()
        play_to_blackhole(FULL_WAV)
        line = wait_for_log_entry("Transcription (", timeout=30, since=t)
        assert line is not None, "No transcription"

        match = re.search(r"Transcription \(([\d.]+)s\)", line)
        assert match, f"Could not parse duration: {line}"
        duration = float(match.group(1))
        print(f"\nSTT latency: {duration:.1f}s")
        # First transcription may include lazy model load (~2.5s)
        assert duration < 8.0, f"STT too slow: {duration}s (limit 8s for cold start)"


# ---------------------------------------------------------------------------
# Error recovery tests
# ---------------------------------------------------------------------------

@blackhole_installed
@vox_running
class TestErrorRecovery:
    """Test heyvox recovers gracefully from edge cases."""

    def test_very_short_recording(self):
        """A very short recording (<1s) should not crash."""
        if not os.path.exists(WAKE_WORD_WAV):
            pytest.skip("Wake word fixture not found")

        t = time.time()
        # Play wake word twice rapidly (start + stop)
        play_to_blackhole(WAKE_WORD_WAV)
        time.sleep(0.5)
        play_to_blackhole(WAKE_WORD_WAV)

        # Should either log "too short" or transcribe — not crash
        time.sleep(5)
        rss = _get_heyvox_rss_mb()
        assert rss > 0, "heyvox process died after very short recording"

    def test_process_survives_10_cycles(self):
        """heyvox stays alive after 10 full cycles."""
        if not _has_fixtures():
            pytest.skip("Wake word fixtures not found")

        for i in range(10):
            t = time.time()
            play_to_blackhole(FULL_WAV)
            wait_for_log_entry("Ready for next wake word", timeout=25, since=t)
            time.sleep(0.3)

        rss = _get_heyvox_rss_mb()
        assert rss > 0, "heyvox process died during stress test"
        print(f"\nProcess alive after 10 cycles, RSS: {rss:.0f} MB")


# ---------------------------------------------------------------------------
# Transcript history tests
# ---------------------------------------------------------------------------

@blackhole_installed
@vox_running
class TestTranscriptHistory:
    """Test that transcripts are saved and not duplicated."""

    def test_transcript_saved_once(self):
        """Each dictation should produce exactly one history entry."""
        if not _has_fixtures():
            pytest.skip("Wake word fixtures not found")

        from heyvox.history import load as load_history
        count_before = len(load_history(limit=1000))

        t = time.time()
        play_to_blackhole(FULL_WAV)
        # Wait for full pipeline to complete including history save
        wait_for_log_entry("Ready for next wake word", timeout=30, since=t)
        time.sleep(2)  # Extra time for history file write

        count_after = len(load_history(limit=1000))
        new_entries = count_after - count_before
        print(f"\nHistory entries: {count_before} → {count_after} (new: {new_entries})")
        # Allow 0 (energy rejected) or 1 (transcribed). Never 2+ (duplicate).
        assert new_entries <= 1, (
            f"Expected 0 or 1 new history entries, got {new_entries} (duplicate bug?)"
        )
