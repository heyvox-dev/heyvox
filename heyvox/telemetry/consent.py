"""Telemetry opt-in state + anonymous ID management.

Source of truth for "is telemetry enabled":
1. config.yaml ``telemetry.enabled`` (canonical persistent setting)
2. env override ``HEYVOX_TELEMETRY=0`` to force-off for one process

The anonymous ID is a random UUID4 stored at
``~/.config/heyvox/telemetry/anon-id``. It is generated lazily on first
``enable()`` and never includes any identifying information. The user can
delete the file at any time to reset their ID.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from heyvox.constants import TELEMETRY_DIR, TELEMETRY_ID_FILE


def _ensure_dir() -> None:
    Path(TELEMETRY_DIR).mkdir(parents=True, exist_ok=True)


def is_enabled() -> bool:
    """Return True iff telemetry is enabled.

    Env override ``HEYVOX_TELEMETRY=0`` always wins (force-off).
    Otherwise reads ``telemetry.enabled`` from the loaded config.
    """
    env = os.environ.get("HEYVOX_TELEMETRY", "").strip().lower()
    if env in {"0", "off", "false", "no"}:
        return False

    try:
        from heyvox.config import load_config
        return bool(load_config().telemetry.enabled)
    except Exception:
        return False


def get_anon_id(create_if_missing: bool = True) -> str:
    """Return the persisted anonymous ID.

    If ``create_if_missing`` is True (default) and no ID exists yet, generates
    one and persists it. Pass ``create_if_missing=False`` from status / preview
    paths that should not materialise an ID for users who haven't opted in.
    """
    p = Path(TELEMETRY_ID_FILE)
    if p.exists():
        existing = p.read_text().strip()
        if existing:
            return existing
    if not create_if_missing:
        return ""
    _ensure_dir()
    new_id = str(uuid.uuid4())
    p.write_text(new_id + "\n")
    try:
        p.chmod(0o600)
    except Exception:
        pass
    return new_id


def reset_anon_id() -> str:
    """Delete the existing anon-id and generate a fresh one."""
    p = Path(TELEMETRY_ID_FILE)
    try:
        p.unlink()
    except FileNotFoundError:
        pass
    return get_anon_id()


def enable() -> None:
    """Persist ``telemetry.enabled = True`` to config.yaml.

    Generates the anonymous ID immediately so the user can inspect it via
    ``heyvox telemetry status``.
    """
    from heyvox.config import update_config
    update_config(telemetry={"enabled": True})
    get_anon_id()


def disable() -> None:
    """Persist ``telemetry.enabled = False`` to config.yaml."""
    from heyvox.config import update_config
    update_config(telemetry={"enabled": False})
