"""Workspace label resolution for Herald TTS announcements.

Shared between heyvox.herald.worker (the hook path) and
heyvox.herald.daemon.watcher (the JSONL-tailing fallback path) so both
produce the same audible prefix.

Resolution order for a given workspace ``directory_name``:

    1. ``HEYVOX_WORKSPACE_LABEL`` env var (ad-hoc override; one-shot tests,
       launchd hooks that already know the friendly name)
    2. ``config.tts.workspace_labels[directory_name]`` (per-workspace short
       name, persisted in config.yaml — the "programmatic" knob)
    3. ``pr_title`` from the workspace-aware app's SQLite DB (e.g. the
       string Conductor renders in its sidebar)
    4. The raw ``directory_name`` as last-resort fallback

Returns ``""`` when ``workspace_name`` is empty or announcing is disabled,
so callers can do ``if label:`` to gate prepending.
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from heyvox.config import HeyvoxConfig

log = logging.getLogger(__name__)


# Conductor renders titles like "Personal · Source · Spell" with U+00B7
# (middle dot) separators. Kokoro reads that as "middle dot", so we swap
# in ", " for natural speech.
_MIDDLE_DOT = " · "


def _load_workspace_db_path(cfg: "HeyvoxConfig | None") -> str:
    """Pull the workspace SQLite path from the first profile that has one.

    Cached at module level — workspace DBs don't move while HeyVox runs.
    """
    if cfg is None:
        try:
            from heyvox.config import load_config
            cfg = load_config()
        except Exception:
            return ""
    for profile in cfg.app_profiles:
        if profile.has_workspace_detection and profile.workspace_db:
            return os.path.expanduser(profile.workspace_db)
    return ""


_cached_db_path: str | None = None


def _get_workspace_db_path(cfg: "HeyvoxConfig | None") -> str:
    global _cached_db_path
    if _cached_db_path is None:
        _cached_db_path = _load_workspace_db_path(cfg)
    return _cached_db_path


def _pr_title_from_db(workspace_name: str, db_path: str) -> str:
    """Fetch ``pr_title`` for ``workspace_name`` from the workspace DB.

    Returns "" if the DB is missing, sqlite errors, or the row has no
    pr_title. Caller falls back to the raw workspace_name in that case.
    """
    if not db_path or not workspace_name:
        return ""
    # Escape single quotes (workspace names with ' would break the literal).
    safe_name = workspace_name.replace("'", "''")
    try:
        r = subprocess.run(
            [
                "sqlite3",
                db_path,
                f"SELECT COALESCE(pr_title, '') FROM workspaces "
                f"WHERE directory_name='{safe_name}'",
            ],
            capture_output=True,
            text=True,
            timeout=0.5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        log.debug("workspace_label: sqlite3 lookup failed: %s", e)
        return ""
    return (r.stdout or "").strip()


def _normalize_for_speech(label: str) -> str:
    """Make a DB label sound natural when spoken.

    Currently just replaces Conductor's middle-dot separator with ", ".
    """
    return label.replace(_MIDDLE_DOT, ", ")


def get_workspace_label(
    workspace_name: str,
    cfg: "HeyvoxConfig | None" = None,
) -> str:
    """Resolve the spoken label for a workspace.

    Returns "" when nothing should be announced (empty workspace_name, or
    announce_workspace=False in config). Otherwise returns the speech-ready
    label string, with separator normalisation applied.

    Args:
        workspace_name: The workspace's ``directory_name`` (matches the
            sidecar value Herald writes next to each TTS WAV).
        cfg: Optional pre-loaded config — pass it when you've already
            loaded one this call to avoid a re-read.
    """
    if not workspace_name:
        return ""

    if cfg is None:
        try:
            from heyvox.config import load_config
            cfg = load_config()
        except Exception as e:
            log.debug("workspace_label: load_config failed (%s), using raw name", e)
            return _normalize_for_speech(workspace_name)

    # Respect the master switch — if the user turned announcing off,
    # callers see "" and skip the prepend entirely.
    if not getattr(cfg.tts, "announce_workspace", True):
        return ""

    # 1. Env var wins (one-shot overrides, launchd-injected friendly names)
    env_label = os.environ.get("HEYVOX_WORKSPACE_LABEL", "").strip()
    if env_label:
        return _normalize_for_speech(env_label)

    # 2. Per-workspace config override — the "programmatic short name" knob
    overrides = getattr(cfg.tts, "workspace_labels", {}) or {}
    custom = overrides.get(workspace_name, "").strip() if isinstance(overrides, dict) else ""
    if custom:
        return _normalize_for_speech(custom)

    # 3. pr_title from the workspace-aware app's DB (what's shown on the left)
    db_path = _get_workspace_db_path(cfg)
    pr_title = _pr_title_from_db(workspace_name, db_path)
    if pr_title:
        return _normalize_for_speech(pr_title)

    # 4. Last resort: the raw directory_name
    return _normalize_for_speech(workspace_name)


def reset_cache() -> None:
    """Drop the cached DB path. Used by tests; not part of the runtime API."""
    global _cached_db_path
    _cached_db_path = None
