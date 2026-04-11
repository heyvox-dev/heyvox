"""Tests for Herald queue garbage collection."""
import pytest

pytestmark = pytest.mark.skip(reason="Queue GC not yet implemented (Plan 08-03)")


def test_gc_removes_old_wav_files(tmp_path):
    """WAV files older than 1 hour in queue dir are removed."""
    pass


def test_gc_skips_recent_files(tmp_path):
    """WAV files younger than 1 hour are not removed."""
    pass


def test_gc_removes_orphaned_workspace_sidecars(tmp_path):
    """Workspace sidecar files whose sibling WAV is missing are removed."""
    pass


def test_gc_respects_hold_dir_threshold(tmp_path):
    """Hold dir files use 4-hour threshold, not 1-hour."""
    pass


def test_gc_frequency_gate(tmp_path):
    """GC runs at most once per minute (frequency gate)."""
    pass
