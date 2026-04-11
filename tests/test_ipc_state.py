"""Tests for heyvox.ipc.state — atomic state file read/write."""
import json
import pytest

from heyvox.ipc import state


@pytest.fixture()
def state_paths(tmp_path, monkeypatch):
    """Monkeypatch state module paths to use tmp_path."""
    state_file = tmp_path / "heyvox-state.json"
    tmp_file = tmp_path / "heyvox-state.tmp"
    monkeypatch.setattr(state, "_state_path", state_file)
    monkeypatch.setattr(state, "_tmp_path", tmp_file)
    return state_file, tmp_file


def test_write_state_creates_file(state_paths):
    """write_state creates heyvox-state.json with given fields."""
    state_file, _ = state_paths
    from heyvox.ipc import write_state
    write_state({"recording": True})
    assert state_file.exists()
    data = json.loads(state_file.read_text())
    assert data == {"recording": True}


def test_read_state_returns_defaults_when_missing(state_paths):
    """read_state returns {} when state file does not exist."""
    from heyvox.ipc import read_state
    # state_file doesn't exist yet
    assert read_state() == {}


def test_write_state_atomic_rename(state_paths):
    """write_state uses temp file + os.rename (no partial writes)."""
    state_file, tmp_file = state_paths
    from heyvox.ipc import write_state
    write_state({"muted": True})
    assert not tmp_file.exists(), ".tmp file should be gone after atomic rename"
    assert state_file.exists()


def test_read_state_handles_corrupt_json(state_paths):
    """read_state returns {} on corrupt JSON (no crash)."""
    state_file, _ = state_paths
    state_file.write_text("not-json{{{")
    from heyvox.ipc import read_state
    assert read_state() == {}


def test_update_state_merges_fields(state_paths):
    """update_state merges new fields with existing without overwriting unrelated fields."""
    from heyvox.ipc import write_state, update_state, read_state
    write_state({"recording": True, "muted": False})
    update_state({"muted": True})
    result = read_state()
    assert result["recording"] is True
    assert result["muted"] is True


def test_startup_reset_clears_transient_fields(state_paths):
    """reset_transient_state sets recording/tts_playing/herald_playing_pid/paused to defaults."""
    from heyvox.ipc import write_state, reset_transient_state, read_state
    write_state({
        "recording": True,
        "tts_playing": True,
        "herald_playing_pid": 12345,
        "paused": True,
        "muted": True,
        "verbosity": "short",
    })
    reset_transient_state()
    result = read_state()
    assert result["recording"] is False
    assert result["tts_playing"] is False
    assert result["herald_playing_pid"] is None
    assert result["paused"] is False
    # Non-transient fields preserved
    assert result["muted"] is True
    assert result["verbosity"] == "short"
