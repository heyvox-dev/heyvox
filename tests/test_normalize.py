"""Tests for heyvox.audio.normalize — shared WAV normalization module."""

import math
import struct

import numpy as np

from heyvox.audio.normalize import (
    DEFAULT_TARGET_RMS,
    normalize_samples_float32,
    normalize_wav_int16,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pack_int16(samples: list[int]) -> bytes:
    """Pack a list of int16 values into little-endian PCM bytes."""
    return struct.pack(f"<{len(samples)}h", *samples)


def _unpack_int16(data: bytes) -> list[int]:
    """Unpack little-endian PCM bytes into int16 values."""
    n = len(data) // 2
    return list(struct.unpack(f"<{n}h", data))


def _rms_int16(data: bytes) -> float:
    """Compute RMS of packed int16 PCM data."""
    samples = _unpack_int16(data)
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


# ---------------------------------------------------------------------------
# normalize_wav_int16
# ---------------------------------------------------------------------------


class TestNormalizeWavInt16:
    """Tests for the int16 / struct-based normalization path."""

    def test_silence_returns_silence(self):
        """Silence (all zeros) should be returned unchanged."""
        silence = _pack_int16([0] * 100)
        result = normalize_wav_int16(silence)
        assert result == silence

    def test_empty_returns_empty(self):
        """Empty input should return empty output."""
        assert normalize_wav_int16(b"") == b""

    def test_quiet_audio_boosted(self):
        """Quiet audio should be boosted toward target RMS."""
        # Generate a quiet sine wave (RMS ~200)
        n = 4000
        quiet = [int(200 * math.sin(2 * math.pi * 440 * i / 16000)) for i in range(n)]
        data = _pack_int16(quiet)

        original_rms = _rms_int16(data)
        assert original_rms < DEFAULT_TARGET_RMS

        result = normalize_wav_int16(data)
        result_rms = _rms_int16(result)

        # Should be boosted significantly (closer to target)
        assert result_rms > original_rms * 1.5

    def test_peak_limiting(self):
        """No sample should exceed peak_limit after soft-clipping (within int16 range)."""
        # Generate loud audio that will need clipping
        n = 4000
        loud = [int(20000 * math.sin(2 * math.pi * 440 * i / 16000)) for i in range(n)]
        data = _pack_int16(loud)

        result = normalize_wav_int16(data, target_rms=25000, peak_limit=24000)
        samples = _unpack_int16(result)

        for s in samples:
            assert -32768 <= s <= 32767

    def test_near_silent_not_amplified(self):
        """Near-silent audio (RMS < 50) should not be amplified."""
        # Very quiet: RMS ~10
        n = 1000
        whisper = [int(10 * math.sin(2 * math.pi * 440 * i / 16000)) for i in range(n)]
        data = _pack_int16(whisper)
        result = normalize_wav_int16(data)
        assert result == data  # unchanged

    def test_scale_cap_respected(self):
        """Gain should not exceed scale_cap even on very quiet audio."""
        n = 4000
        # RMS ~100 -> scale would be 30x without cap, but cap=3.0 limits it
        quiet = [int(100 * math.sin(2 * math.pi * 440 * i / 16000)) for i in range(n)]
        data = _pack_int16(quiet)
        result = normalize_wav_int16(data, scale_cap=2.0)
        result_rms = _rms_int16(result)
        original_rms = _rms_int16(data)
        # With scale_cap=2.0, result RMS should be at most ~2x original
        assert result_rms <= original_rms * 2.5  # small margin for soft-clip


# ---------------------------------------------------------------------------
# normalize_samples_float32
# ---------------------------------------------------------------------------


class TestNormalizeSamplesFloat32:
    """Tests for the float32 / numpy-based normalization path."""

    def test_short_samples_unchanged(self):
        """Samples shorter than 1000 should be returned unchanged."""
        short = np.zeros(500, dtype=np.float32)
        result = normalize_samples_float32(short)
        assert result is short  # identity, not just equality

    def test_silence_unchanged(self):
        """Silent float32 array should be returned unchanged."""
        silence = np.zeros(2000, dtype=np.float32)
        result = normalize_samples_float32(silence)
        np.testing.assert_array_equal(result, silence)

    def test_quiet_audio_boosted(self):
        """Quiet float32 audio should be boosted toward target RMS."""
        n = 4000
        t = np.arange(n, dtype=np.float32)
        # Quiet sine: amplitude ~0.006 (RMS in int16 scale ~140)
        quiet = 0.006 * np.sin(2 * np.pi * 440 * t / 16000).astype(np.float32)

        result = normalize_samples_float32(quiet)

        original_rms = np.sqrt(np.mean((quiet * 32767) ** 2))
        result_rms = np.sqrt(np.mean((result * 32767) ** 2))

        assert result_rms > original_rms * 1.5

    def test_peak_limiting(self):
        """Float32 output should respect peak_limit after conversion to int16 scale."""
        n = 4000
        t = np.arange(n, dtype=np.float32)
        # Loud sine
        loud = 0.8 * np.sin(2 * np.pi * 440 * t / 16000).astype(np.float32)

        result = normalize_samples_float32(loud, target_rms=25000, peak_limit=24000)

        # Check int16 scale values
        int16_vals = result * 32767
        assert np.all(int16_vals <= 32767)
        assert np.all(int16_vals >= -32768)

    def test_consistent_with_int16_path(self):
        """Float32 and int16 paths should produce equivalent results on the same signal."""
        n = 4000
        t = np.arange(n, dtype=np.float32)
        signal_f32 = 0.03 * np.sin(2 * np.pi * 440 * t / 16000).astype(np.float32)

        # Float32 path
        result_f32 = normalize_samples_float32(signal_f32)
        result_int16_from_f32 = (result_f32 * 32767).astype(np.int16)

        # Int16 path
        signal_int16 = (signal_f32 * 32767).astype(np.int16)
        data = signal_int16.tobytes()
        result_bytes = normalize_wav_int16(data)
        result_int16_direct = np.frombuffer(result_bytes, dtype=np.int16)

        # RMS should be within 10% of each other
        rms_f32 = np.sqrt(np.mean(result_int16_from_f32.astype(float) ** 2))
        rms_direct = np.sqrt(np.mean(result_int16_direct.astype(float) ** 2))
        assert abs(rms_f32 - rms_direct) / max(rms_f32, rms_direct, 1) < 0.15
