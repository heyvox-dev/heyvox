"""
Transcript history — persistent log of all dictations.

Every successful transcription is saved to a JSONL file before paste is
attempted. If paste fails (focus lost, app crash, etc.), the text is still
recoverable via ``vox history`` CLI.

Storage: ~/.local/share/heyvox/transcripts.jsonl (XDG-compliant via platformdirs)
Format: one JSON object per line, newest last.
"""

import json
import time
from pathlib import Path

from platformdirs import user_data_dir

# Use "heyvox" as app name; also check old "vox" dir for migration
_DATA_DIR = Path(user_data_dir("heyvox"))
_OLD_DATA_DIR = Path(user_data_dir("vox"))
_HISTORY_FILE = _DATA_DIR / "transcripts.jsonl"

# Maximum file size before rotation (5 MB)
_MAX_BYTES = 5_000_000

# Migrate old "vox" history to "heyvox" on first access
_OLD_HISTORY = _OLD_DATA_DIR / "transcripts.jsonl"
if _OLD_HISTORY.exists() and not _HISTORY_FILE.exists():
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.move(str(_OLD_HISTORY), str(_HISTORY_FILE))
        # Also move rotated file if present
        _old_rotated = _OLD_HISTORY.with_suffix(".jsonl.1")
        if _old_rotated.exists():
            shutil.move(str(_old_rotated), str(_HISTORY_FILE.with_suffix(".jsonl.1")))
    except OSError:
        pass


def save(text: str, duration: float = 0.0, ptt: bool = False) -> None:
    """Append a transcript entry to the history file.

    Called immediately after STT succeeds, before any paste attempt.
    This guarantees the text is persisted even if injection fails.

    Args:
        text: The transcribed text (after wake word stripping).
        duration: Recording duration in seconds.
        ptt: True if triggered by push-to-talk, False for wake word.
    """
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _rotate_if_needed()

    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "epoch": int(time.time()),
        "text": text,
        "duration": round(duration, 1),
        "trigger": "ptt" if ptt else "wakeword",
    }
    with open(_HISTORY_FILE, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load(limit: int = 20) -> list[dict]:
    """Return the most recent transcript entries (newest first).

    Args:
        limit: Maximum number of entries to return.

    Returns:
        List of transcript dicts, newest first.
    """
    if not _HISTORY_FILE.exists():
        return []

    # Read all lines, take last N, reverse for newest-first
    lines = _HISTORY_FILE.read_text().strip().splitlines()
    recent = lines[-limit:] if limit < len(lines) else lines
    recent.reverse()

    entries = []
    for line in recent:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def last() -> dict | None:
    """Return the single most recent transcript, or None."""
    entries = load(limit=1)
    return entries[0] if entries else None


def _rotate_if_needed() -> None:
    """Rotate the history file if it exceeds _MAX_BYTES."""
    if not _HISTORY_FILE.exists():
        return
    try:
        if _HISTORY_FILE.stat().st_size > _MAX_BYTES:
            rotated = _HISTORY_FILE.with_suffix(".jsonl.1")
            _HISTORY_FILE.rename(rotated)
    except OSError:
        pass
