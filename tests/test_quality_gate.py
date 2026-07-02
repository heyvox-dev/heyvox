"""Guard tests for tools/quality_gate.py's never-delete / never-overwrite guarantee.

These use SILENT wavs so the gate's RMS pre-gate short-circuits before any
mlx_whisper call — the move/collision logic is exercised without a model load,
so the suite stays fast and deterministic (no Metal GPU dependency).
"""
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import quality_gate  # noqa: E402


def _write_silent_wav(path: Path, seconds: float = 0.5, sample_rate: int = 16000) -> None:
    """Write a near-silent int16 wav (RMS well below quality_gate._SILENCE_RMS)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = np.zeros(int(seconds * sample_rate), dtype=np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(frames.tobytes())


def test_safe_dest_avoids_collision(tmp_path: Path):
    d = tmp_path / "q"
    d.mkdir()
    # Free name → returned as-is.
    assert quality_gate._safe_dest(d, "a.wav") == d / "a.wav"
    # Occupied name → suffixed, never the occupied path.
    (d / "a.wav").touch()
    first = quality_gate._safe_dest(d, "a.wav")
    assert first == d / "a_dup1.wav"
    # Occupied + first-alt occupied → next suffix.
    (d / "a_dup1.wav").touch()
    assert quality_gate._safe_dest(d, "a.wav") == d / "a_dup2.wav"


def test_silent_positive_is_quarantined_not_deleted(tmp_path: Path):
    pos = tmp_path / "tp"
    positives = tmp_path / "positives"
    quarantine = tmp_path / "quarantine"
    state = tmp_path / "state"
    _write_silent_wav(pos / "clip.wav")

    quality_gate.run_gate([pos], [], positives, quarantine, state_dir=state)

    # Never deleted — the clip is GONE from the positive dir but PRESENT in quarantine.
    assert not (pos / "clip.wav").exists()
    assert (quarantine / "clip.wav").exists()


def test_name_collision_preserves_both_clips(tmp_path: Path):
    # Two DIFFERENT clips that happen to share a filename across two positive dirs.
    pos_a = tmp_path / "tp"
    pos_b = tmp_path / "fn"
    positives = tmp_path / "positives"
    quarantine = tmp_path / "quarantine"
    state = tmp_path / "state"
    _write_silent_wav(pos_a / "same.wav")
    _write_silent_wav(pos_b / "same.wav")

    quality_gate.run_gate([pos_a, pos_b], [], positives, quarantine, state_dir=state)

    # Both must survive in quarantine — the second must NOT have overwritten the first.
    survivors = sorted(p.name for p in quarantine.glob("*.wav"))
    assert survivors == ["same.wav", "same_dup1.wav"], survivors


def test_gate_is_resumable_skips_already_processed(tmp_path: Path):
    pos = tmp_path / "tp"
    positives = tmp_path / "positives"
    quarantine = tmp_path / "quarantine"
    state = tmp_path / "state"
    _write_silent_wav(pos / "clip.wav")

    quality_gate.run_gate([pos], [], positives, quarantine, state_dir=state)
    # Second run over the same (now-empty) dir must not error and must not
    # re-quarantine anything — the moved clip left the gated dir, and its
    # results.jsonl entry persists.
    summary = quality_gate.run_gate([pos], [], positives, quarantine, state_dir=state)
    assert summary["quarantined"] == 0
    assert (quarantine / "clip.wav").exists()
