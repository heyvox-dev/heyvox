"""Shared WAV normalization — RMS-based loudness matching.

Consolidates the three duplicate normalization implementations:
  - heyvox/herald/worker.py normalize_wav_in_place() (int16 / struct path)
  - heyvox/herald/daemon/kokoro-daemon.py normalize_samples() (float32 / numpy path)
  - heyvox/herald/orchestrator.py normalize_wav() (int16 / struct path, legacy fallback)

Algorithm (identical across both paths):
  1. Compute RMS of samples
  2. If RMS < 50 (near-silent), skip — don't amplify noise floor
  3. Scale = target_rms / rms, capped at scale_cap
  4. Soft-clip above peak_limit: overshoot compressed by 0.2x
  5. Hard-clamp to int16 range [-32768, 32767]

Requirement: HERALD-02 (WAV normalization)
"""

from __future__ import annotations

import math
import struct
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

# ---------------------------------------------------------------------------
# Constants (shared defaults, int16 scale)
# ---------------------------------------------------------------------------

DEFAULT_TARGET_RMS: int = 3000
DEFAULT_SCALE_CAP: float = 3.0
DEFAULT_PEAK_LIMIT: int = 24000


# ---------------------------------------------------------------------------
# int16 path (wave + struct, no numpy dependency)
# ---------------------------------------------------------------------------

def normalize_wav_int16(
    data: bytes,
    target_rms: int = DEFAULT_TARGET_RMS,
    scale_cap: float = DEFAULT_SCALE_CAP,
    peak_limit: int = DEFAULT_PEAK_LIMIT,
) -> bytes:
    """RMS-normalize packed int16 PCM data and return normalized bytes.

    Args:
        data: Raw PCM bytes (little-endian signed 16-bit, mono).
        target_rms: Target RMS level (int16 scale, max 32767).
        scale_cap: Maximum gain multiplier (prevents boosting near-silent clips).
        peak_limit: Soft-clip threshold — samples above this are compressed.

    Returns:
        Normalized PCM bytes (same length as input). Returns input unchanged
        if the audio is near-silent (RMS < 50) or empty.
    """
    n_samples = len(data) // 2
    if n_samples == 0:
        return data

    samples = list(struct.unpack(f"<{n_samples}h", data))

    rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    if rms < 50:
        # Near-silent — don't amplify noise floor
        return data

    scale = target_rms / rms if rms > 0 else 1.0
    scale = min(scale, scale_cap)

    out: list[int] = []
    for s in samples:
        s_scaled = s * scale
        if s_scaled > peak_limit:
            s_scaled = peak_limit + (s_scaled - peak_limit) * 0.2
        elif s_scaled < -peak_limit:
            s_scaled = -peak_limit + (s_scaled + peak_limit) * 0.2
        out.append(max(-32768, min(32767, int(s_scaled))))

    return struct.pack(f"<{len(out)}h", *out)


# ---------------------------------------------------------------------------
# float32 path (numpy, for Kokoro daemon)
# ---------------------------------------------------------------------------

def normalize_samples_float32(
    samples: "np.ndarray",
    target_rms: int = DEFAULT_TARGET_RMS,
    scale_cap: float = DEFAULT_SCALE_CAP,
    peak_limit: int = DEFAULT_PEAK_LIMIT,
) -> "np.ndarray":
    """RMS-normalize float32 audio samples (range approx -1.0 to 1.0).

    Operates in int16 scale internally for RMS calculation (consistent with
    the int16 path), then converts back to float32.

    Args:
        samples: numpy float32 array of audio samples.
        target_rms: Target RMS level (int16 scale).
        scale_cap: Maximum gain multiplier.
        peak_limit: Soft-clip threshold (int16 scale).

    Returns:
        Normalized float32 numpy array. Returns input unchanged if the audio
        is too short (< 1000 samples) or near-silent (RMS < 50 in int16 scale).
    """
    import numpy as np

    if len(samples) < 1000:
        return samples

    # Work in int16 scale for RMS calculation
    int16_view = samples * 32767.0
    rms = np.sqrt(np.mean(int16_view ** 2))
    if rms < 50:
        return samples  # silence, skip

    scale = min(target_rms / rms if rms > 0 else 1.0, scale_cap)
    scaled = int16_view * scale

    # Soft clip above peak_limit
    above = scaled > peak_limit
    below = scaled < -peak_limit
    scaled[above] = peak_limit + (scaled[above] - peak_limit) * 0.2
    scaled[below] = -peak_limit + (scaled[below] + peak_limit) * 0.2

    # Clamp and convert back to float32
    scaled = np.clip(scaled, -32768, 32767)
    return scaled / 32767.0
