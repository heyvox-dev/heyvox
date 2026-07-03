"""Guard tests for STT engine fallback on non-Apple-Silicon (DEF-175).

MLX Whisper requires arm64. On Intel Macs mlx-whisper can't load, and the old
code blocked _LOAD_TIMEOUT (120s) on *every* dictation before giving up. These
guard the fail-fast path and the Intel-defaults-to-sherpa provisioning.
"""
import platform
import time

import numpy as np

from heyvox.audio import stt
from heyvox.config import _default_stt_engine


def test_default_engine_is_sherpa_off_apple_silicon(monkeypatch):
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    assert _default_stt_engine() == "sherpa"
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    assert _default_stt_engine() == "mlx"


def test_mlx_unavailable_fails_fast_not_120s_hang(monkeypatch):
    # Simulate Intel Mac / missing mlx-whisper: the load path must bail fast,
    # not block the full _LOAD_TIMEOUT on the first dictation.
    monkeypatch.setattr(stt, "_mlx_unavailable", True)
    stt._mlx_loaded.clear()

    chunk = np.zeros(16000, dtype=np.int16)
    t0 = time.time()
    result = stt.transcribe_audio([chunk], engine="mlx")
    elapsed = time.time() - t0

    assert result == "", f"expected empty result, got {result!r}"
    assert elapsed < 5.0, (
        f"fail-fast took {elapsed:.1f}s — must be near-instant, not ~120s"
    )
