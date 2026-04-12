"""Atomic state file for cross-process coordination.

State file: /tmp/heyvox-state.json (from heyvox.constants.HEYVOX_STATE_FILE)
Write pattern: write to .tmp sibling, then os.rename (atomic on POSIX).
Read pattern: best-effort, returns {} on missing/corrupt file.
Thread safety: _state_lock protects concurrent writes within same process.
"""
import json
import os
import threading
from pathlib import Path

from heyvox.constants import HEYVOX_STATE_FILE

_state_lock = threading.Lock()
_state_path = Path(HEYVOX_STATE_FILE)
_tmp_path = _state_path.with_suffix(".tmp")

# Default values for all state fields
DEFAULTS = {
    "recording": False,
    "tts_playing": False,
    "herald_playing_pid": None,
    "muted": False,
    "verbosity": "full",
    "paused": False,
    "herald_mode": "ambient",
    "last_play_ts": 0.0,
}

# Fields that are transient (reset on startup — they reflect live process state)
TRANSIENT_FIELDS = {"recording", "tts_playing", "herald_playing_pid", "paused"}


def read_state() -> dict:
    """Read current state. Returns {} on missing or corrupt file."""
    try:
        return json.loads(_state_path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def write_state(state: dict) -> None:
    """Atomically write full state dict to state file."""
    with _state_lock:
        _tmp_path.write_text(json.dumps(state))
        os.rename(_tmp_path, _state_path)


def update_state(updates: dict) -> None:
    """Atomically merge updates into state file (read-modify-write)."""
    with _state_lock:
        try:
            current = json.loads(_state_path.read_text()) if _state_path.exists() else {}
        except (OSError, json.JSONDecodeError, ValueError):
            current = {}
        current.update(updates)
        _tmp_path.write_text(json.dumps(current))
        os.rename(_tmp_path, _state_path)


def reset_transient_state() -> None:
    """Reset transient fields to defaults. Call on process startup."""
    resets = {k: DEFAULTS[k] for k in TRANSIENT_FIELDS}
    update_state(resets)
