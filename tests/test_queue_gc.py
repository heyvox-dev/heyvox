"""Tests for Herald queue garbage collection."""
import os
import time


import heyvox.herald.orchestrator as orch_module
from heyvox.herald.orchestrator import OrchestratorConfig, _gc_queue_dirs


def _make_cfg(tmp_path) -> OrchestratorConfig:
    """Create an OrchestratorConfig wired to tmp_path subdirectories."""
    queue_dir = tmp_path / "herald-queue"
    hold_dir = tmp_path / "herald-hold"
    history_dir = tmp_path / "herald-history"
    claim_dir = tmp_path / "herald-claim"
    for d in (queue_dir, hold_dir, history_dir, claim_dir):
        d.mkdir()
    debug_log = tmp_path / "herald-debug.log"
    return OrchestratorConfig(
        queue_dir=queue_dir,
        hold_dir=hold_dir,
        history_dir=history_dir,
        claim_dir=claim_dir,
        debug_log=debug_log,
    )


def _set_old(path, seconds_ago: float) -> None:
    """Set the mtime of *path* to *seconds_ago* seconds in the past."""
    t = time.time() - seconds_ago
    os.utime(path, (t, t))


def setup_function():
    """Reset the GC frequency gate before each test."""
    orch_module._last_gc = 0.0


def test_gc_removes_old_wav_files(tmp_path):
    """WAV files older than 1 hour in queue dir are removed."""
    cfg = _make_cfg(tmp_path)
    wav = cfg.queue_dir / "old.wav"
    wav.write_bytes(b"\x00" * 44)
    _set_old(wav, 7200)  # 2 hours old

    removed = _gc_queue_dirs(cfg, cfg.debug_log)

    assert not wav.exists(), "Old WAV should have been removed"
    assert removed >= 1


def test_gc_skips_recent_files(tmp_path):
    """WAV files younger than 1 hour are not removed."""
    cfg = _make_cfg(tmp_path)
    wav = cfg.queue_dir / "fresh.wav"
    wav.write_bytes(b"\x00" * 44)
    # default mtime is now — no need to utime

    _gc_queue_dirs(cfg, cfg.debug_log)

    assert wav.exists(), "Recent WAV should NOT have been removed"


def test_gc_removes_orphaned_workspace_sidecars(tmp_path):
    """Workspace sidecar files whose sibling WAV is missing are removed when old."""
    cfg = _make_cfg(tmp_path)
    # .workspace without a matching .wav
    sidecar = cfg.queue_dir / "msg.workspace"
    sidecar.write_text("some-workspace")
    _set_old(sidecar, 7200)  # 2 hours old

    removed = _gc_queue_dirs(cfg, cfg.debug_log)

    assert not sidecar.exists(), "Orphaned old .workspace sidecar should be removed"
    assert removed >= 1


def test_gc_respects_hold_dir_threshold(tmp_path):
    """Hold dir files use 4-hour threshold; 2-hour-old file should survive."""
    cfg = _make_cfg(tmp_path)
    wav = cfg.hold_dir / "held.wav"
    wav.write_bytes(b"\x00" * 44)
    _set_old(wav, 7200)  # 2 hours — within 4-hour hold threshold

    _gc_queue_dirs(cfg, cfg.debug_log)

    assert wav.exists(), "2-hour-old hold-dir file should NOT be removed (threshold is 4h)"

    # Now age it beyond the 4-hour threshold and re-run
    _set_old(wav, 18000)  # 5 hours old
    orch_module._last_gc = 0.0  # reset gate for second call
    removed = _gc_queue_dirs(cfg, cfg.debug_log)

    assert not wav.exists(), "5-hour-old hold-dir file SHOULD be removed"
    assert removed >= 1


def test_gc_frequency_gate(tmp_path):
    """GC runs at most once per minute; second call within 1s is a no-op."""
    cfg = _make_cfg(tmp_path)
    wav = cfg.queue_dir / "old.wav"
    wav.write_bytes(b"\x00" * 44)
    _set_old(wav, 7200)

    _gc_queue_dirs(cfg, cfg.debug_log)
    # The file is gone after first call; restore it to confirm second call is skipped
    wav.write_bytes(b"\x00" * 44)
    _set_old(wav, 7200)

    second = _gc_queue_dirs(cfg, cfg.debug_log)

    assert second == 0, "Second call within <1s should be a no-op (frequency gate)"
    # File should still exist because GC was gated
    assert wav.exists(), "File should not have been removed by gated second call"
