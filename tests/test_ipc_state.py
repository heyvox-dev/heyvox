"""Tests for heyvox.ipc.state — atomic state file read/write."""
import pytest

pytestmark = pytest.mark.skip(reason="IPC state module not yet implemented (Plan 08-02)")


def test_write_state_creates_file(tmp_path):
    """write_state creates heyvox-state.json with given fields."""
    pass


def test_read_state_returns_defaults_when_missing(tmp_path):
    """read_state returns {} when state file does not exist."""
    pass


def test_write_state_atomic_rename(tmp_path):
    """write_state uses temp file + os.rename (no partial writes)."""
    pass


def test_read_state_handles_corrupt_json(tmp_path):
    """read_state returns {} on corrupt JSON (no crash)."""
    pass


def test_update_state_merges_fields(tmp_path):
    """update_state merges new fields with existing without overwriting unrelated fields."""
    pass


def test_startup_reset_clears_transient_fields(tmp_path):
    """reset_transient_state sets recording/tts_playing/herald_playing_pid/paused to defaults."""
    pass
