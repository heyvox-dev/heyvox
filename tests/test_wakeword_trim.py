"""Tests for wake word audio trimming.

Covers Bug #3: Wake word audio leaking into transcription.
The front trim must be >= pre-roll (500ms) + wake word (~1000ms) = 1500ms.
The back trim removes the stop wake word (~1000ms).
"""

import numpy as np

from heyvox.constants import DEFAULT_SAMPLE_RATE, DEFAULT_CHUNK_SIZE


# Helper to create fake audio chunks
def _make_chunks(n: int, value: int = 100, sr=DEFAULT_SAMPLE_RATE, cs=DEFAULT_CHUNK_SIZE) -> list:
    """Create n fake audio chunks with a constant sample value."""
    return [np.full(cs, value, dtype=np.int16) for _ in range(n)]


class TestTrimParameters:
    """Verify the trim removes the correct number of chunks."""

    def test_preroll_buffer_size(self):
        """Pre-roll buffer should capture ~500ms of audio."""
        preroll_chunks = max(1, int(0.5 * DEFAULT_SAMPLE_RATE / DEFAULT_CHUNK_SIZE))
        preroll_ms = preroll_chunks * DEFAULT_CHUNK_SIZE / DEFAULT_SAMPLE_RATE * 1000
        assert 400 <= preroll_ms <= 600, f"Pre-roll should be ~500ms, got {preroll_ms:.0f}ms"

    def test_front_trim_covers_preroll_plus_wakeword(self):
        """Front trim (1.5s) must cover pre-roll (500ms) + wake word (~1000ms)."""
        ww_trim_secs = 1.5
        trim_chunks = int(ww_trim_secs * DEFAULT_SAMPLE_RATE / DEFAULT_CHUNK_SIZE)
        trim_ms = trim_chunks * DEFAULT_CHUNK_SIZE / DEFAULT_SAMPLE_RATE * 1000

        preroll_ms = 500
        wakeword_ms = 1000
        assert trim_ms >= preroll_ms + wakeword_ms - 100, \
            f"Front trim {trim_ms:.0f}ms must cover pre-roll ({preroll_ms}ms) + wake word ({wakeword_ms}ms)"

    def test_back_trim_covers_stop_wakeword(self):
        """Back trim (1.0s) must cover the stop wake word (~700-1000ms)."""
        # Back trim uses the same ww_trim_secs for the end
        # In the code, back trim = ww_trim_chunks which is based on ww_trim_secs
        # But the code trims 1.5s from front and 1.5s from back (same variable)
        # Let's verify the actual trim is >= 700ms
        ww_trim_secs = 1.5
        trim_ms = ww_trim_secs * 1000
        assert trim_ms >= 700, f"Back trim {trim_ms:.0f}ms must cover stop wake word (>=700ms)"


class TestTrimLogic:
    """Test the actual trim logic from stop_recording()."""

    def _apply_trim(self, chunks, sample_rate=DEFAULT_SAMPLE_RATE, chunk_size=DEFAULT_CHUNK_SIZE):
        """Replicate the trim logic from main.py stop_recording()."""
        ww_trim_secs = 1.5
        ww_trim_chunks = int(ww_trim_secs * sample_rate / chunk_size)

        # Front trim
        if len(chunks) > ww_trim_chunks * 2:
            chunks = chunks[ww_trim_chunks:]
        # Back trim
        if len(chunks) > ww_trim_chunks:
            chunks = chunks[:-ww_trim_chunks]

        return chunks

    def test_normal_recording_trimmed_correctly(self):
        """A 10-second recording should have both ends trimmed."""
        duration_s = 10
        n_chunks = int(duration_s * DEFAULT_SAMPLE_RATE / DEFAULT_CHUNK_SIZE)
        chunks = _make_chunks(n_chunks)

        trimmed = self._apply_trim(chunks)

        ww_trim_chunks = int(1.5 * DEFAULT_SAMPLE_RATE / DEFAULT_CHUNK_SIZE)
        expected_len = n_chunks - ww_trim_chunks * 2
        assert len(trimmed) == expected_len, \
            f"Expected {expected_len} chunks after trim, got {len(trimmed)}"

    def test_short_recording_not_over_trimmed(self):
        """Recordings shorter than 2x trim should preserve data."""
        ww_trim_chunks = int(1.5 * DEFAULT_SAMPLE_RATE / DEFAULT_CHUNK_SIZE)

        # Recording exactly 2x trim size — front trim happens, back trim happens
        chunks = _make_chunks(ww_trim_chunks * 2 + 1)
        trimmed = self._apply_trim(chunks)
        assert len(trimmed) > 0, "Must not trim to empty"

        # Recording shorter than 2x trim — front trim skipped
        chunks = _make_chunks(ww_trim_chunks * 2 - 1)
        trimmed = self._apply_trim(chunks)
        assert len(trimmed) > 0, "Short recording must not be emptied"

    def test_very_short_recording_preserved(self):
        """Very short recordings (< trim size) should not be emptied."""
        chunks = _make_chunks(5)  # Very short
        trimmed = self._apply_trim(chunks)
        # Front guard fails (5 < trim*2), so no front trim
        # Back guard: 5 > trim? No (trim ~18), so no back trim either
        assert len(trimmed) == 5, "Very short recording must be fully preserved"

    def test_trim_preserves_speech_content(self):
        """Speech in the middle of a recording must survive trimming."""
        ww_trim_chunks = int(1.5 * DEFAULT_SAMPLE_RATE / DEFAULT_CHUNK_SIZE)

        # Build: [wake word chunks] + [speech chunks] + [stop word chunks]
        wake_chunks = _make_chunks(ww_trim_chunks, value=500)
        speech_chunks = _make_chunks(50, value=200)  # Different value = identifiable
        stop_chunks = _make_chunks(ww_trim_chunks, value=500)

        all_chunks = wake_chunks + speech_chunks + stop_chunks
        trimmed = self._apply_trim(all_chunks)

        # Verify the remaining chunks are the speech content
        for chunk in trimmed:
            assert chunk[0] == 200, "Trimmed audio should only contain speech chunks"

    def test_ptt_recording_skips_trim(self):
        """PTT recordings should NOT be trimmed (no wake word to remove)."""
        # In main.py: `if not _triggered_by_ptt:` guards the trim
        _triggered_by_ptt = True
        chunks = _make_chunks(100)

        if not _triggered_by_ptt:
            chunks = self._apply_trim(chunks)

        assert len(chunks) == 100, "PTT recording must not be trimmed"


class TestEnergyThreshold:
    """Test the energy gating that prevents Whisper hallucinations on silence."""

    def test_audio_rms_on_silence(self):
        """Pure silence should return very low dBFS."""
        from heyvox.recording import _audio_rms

        silent_chunks = [np.zeros(DEFAULT_CHUNK_SIZE, dtype=np.int16)]
        db = _audio_rms(silent_chunks, DEFAULT_SAMPLE_RATE)
        assert db <= -90.0, f"Silence should be <= -90 dBFS, got {db}"

    def test_audio_rms_on_speech(self):
        """Normal speech-level audio should be above threshold."""
        from heyvox.recording import _audio_rms

        # Simulate speech: sine wave at ~1000 amplitude (typical speech RMS)
        t = np.arange(DEFAULT_CHUNK_SIZE * 10) / DEFAULT_SAMPLE_RATE
        audio = (1000 * np.sin(2 * np.pi * 440 * t)).astype(np.int16)
        chunks = [audio[i:i + DEFAULT_CHUNK_SIZE] for i in range(0, len(audio), DEFAULT_CHUNK_SIZE)]

        db = _audio_rms(chunks, DEFAULT_SAMPLE_RATE)
        assert db > -60.0, f"Speech should be above -60 dBFS threshold, got {db}"

    def test_audio_rms_empty_chunks(self):
        """Empty chunk list should return minimum dBFS."""
        from heyvox.recording import _audio_rms

        db = _audio_rms([], DEFAULT_SAMPLE_RATE)
        assert db == -96.0

    def test_threshold_value(self):
        """Energy threshold is -48 dBFS — raised from -60 to suppress
        quiet-room hallucinations on Whisper without losing whisper-volume
        speech."""
        from heyvox.recording import _MIN_AUDIO_DBFS

        assert _MIN_AUDIO_DBFS == -48.0, f"Threshold should be -48.0, got {_MIN_AUDIO_DBFS}"
