"""Workspace label resolution for Herald TTS announcements.

Shared between heyvox.herald.worker (the hook path) and
heyvox.herald.daemon.watcher (the JSONL-tailing fallback path) so both
produce the same audible prefix.

Resolution order for a given workspace ``directory_name``:

    1. ``HEYVOX_WORKSPACE_LABEL`` env var (ad-hoc override; one-shot tests,
       launchd hooks that already know the friendly name)
    2. ``config.tts.workspace_labels[directory_name]`` (per-workspace short
       name, persisted in config.yaml — the "programmatic" knob)
    3. The workspace-aware app's SQLite DB, mirroring what it renders in
       its own sidebar: ``workspace_name`` when the row has
       ``user_set_workspace_name=1`` (Conductor's drift-proof rename
       field), else ``pr_title`` (which Conductor overwrites with a
       Conventional-Commit string on every PR merge — only used when
       nothing better is set)
    4. The raw ``directory_name`` as last-resort fallback

Returns ``""`` when ``workspace_name`` is empty or announcing is disabled,
so callers can do ``if label:`` to gate prepending.
"""

from __future__ import annotations

import json
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


# Field separator for the multi-column query below. Must be a *printable*
# character, not the raw ASCII unit-separator (0x1F): the sqlite3 CLI
# caret-escapes real control bytes in its output (e.g. 0x1F -> "^_", a
# terminal-safety measure), which silently corrupts a raw-control-byte
# separator before Python ever sees it. U+241F is the printable Unicode
# glyph for "unit separator" — survives the CLI untouched and is not
# something a human would ever type into a workspace/PR title.
_DB_FIELD_SEP = "␟"


def _sidebar_label_from_db(workspace_name: str, db_path: str) -> str:
    """Fetch the sidebar-equivalent label for ``workspace_name`` from the DB.

    Mirrors Conductor's own sidebar resolution (see claude-conductor-setup's
    CLAUDE.md "Workspace Naming" section): ``workspace_name`` wins when the
    row has ``user_set_workspace_name=1`` — the drift-proof field written by
    the rename tooling there — otherwise falls back to ``pr_title``, which
    Conductor overwrites with a Conventional-Commit string on every PR merge.

    Returns "" if the DB is missing, sqlite errors, or the row has neither
    field set. Caller falls back to the raw workspace_name in that case.
    """
    if not db_path or not workspace_name:
        return ""
    # Escape single quotes (workspace names with ' would break the literal).
    safe_name = workspace_name.replace("'", "''")
    sep = _DB_FIELD_SEP
    try:
        r = subprocess.run(
            [
                "sqlite3",
                db_path,
                f"SELECT COALESCE(workspace_name,'') || '{sep}' || "
                f"COALESCE(user_set_workspace_name,0) || '{sep}' || "
                f"COALESCE(pr_title,'') FROM workspaces "
                f"WHERE directory_name='{safe_name}' LIMIT 1",
            ],
            capture_output=True,
            text=True,
            timeout=0.5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        log.debug("workspace_label: sqlite3 lookup failed: %s", e)
        return ""
    row = (r.stdout or "").rstrip("\n")
    if not row:
        return ""
    parts = row.split(sep)
    if len(parts) != 3:
        log.debug("workspace_label: unexpected sqlite3 output shape: %r", row)
        return ""
    ws_name, user_set, pr_title = parts
    if user_set == "1" and ws_name:
        return ws_name
    return pr_title


def detect_workspace_from_cwd(
    cwd: str | None = None,
    cfg: "HeyvoxConfig | None" = None,
) -> str:
    """Resolve the ``directory_name`` for the workspace containing ``cwd``.

    Conductor doesn't export ``HEYVOX_WORKSPACE`` or
    ``CONDUCTOR_WORKSPACE_NAME`` into the Claude Code hook environment,
    so the hook-spawned worker has no env signal to identify the
    workspace. The current working directory is the only reliable hint.

    Strategy:
      1. Look up the workspace DB for the row whose ``workspace_path`` is
         either an exact match for ``cwd`` or a parent of ``cwd`` (so a
         shell that has cd'd into a subdir still resolves correctly).
         Return that row's ``directory_name``.
      2. Fall back to ``basename(cwd)`` if the DB is unavailable or has
         no matching row. For the canonical Conductor layout
         (``.../workspaces/<repo>/<workspace>``) this still produces the
         right ``directory_name`` for most cases.
      3. Return "" only if cwd is empty.

    Returns the directory_name string — caller is expected to feed it
    into ``get_workspace_label()`` for the full label resolution.
    """
    # Treat explicit "" as "no signal, don't guess" (distinct from None,
    # which means "use os.getcwd()"). Lets tests pin a known absence.
    if cwd is None:
        cwd = os.getcwd()
    if not cwd:
        return ""

    db_path = _get_workspace_db_path(cfg)
    if db_path:
        # Escape single quotes in cwd (paths can technically contain them).
        safe_cwd = cwd.replace("'", "''")
        try:
            r = subprocess.run(
                [
                    "sqlite3",
                    db_path,
                    # Exact match OR cwd is a subdirectory of workspace_path.
                    # The ``/`` separator on the LIKE pattern prevents matching
                    # ``/workspaces/abc`` against ``/workspaces/abcd``.
                    f"SELECT directory_name FROM workspaces "
                    f"WHERE workspace_path = '{safe_cwd}' "
                    f"OR '{safe_cwd}' LIKE workspace_path || '/%' "
                    f"LIMIT 1",
                ],
                capture_output=True,
                text=True,
                timeout=0.5,
            )
            name = (r.stdout or "").strip()
            if name:
                return name
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
            log.debug("workspace_label: cwd lookup failed: %s", e)

    # Fallback: basename of cwd. Works for the standard Conductor layout
    # and for non-Conductor users who don't have a workspace DB at all.
    return os.path.basename(cwd.rstrip("/")) or ""


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

    # 3. Sidebar-equivalent label from the workspace-aware app's DB — mirrors
    #    Conductor's own resolution (workspace_name when user-set, else
    #    pr_title).
    db_path = _get_workspace_db_path(cfg)
    sidebar_label = _sidebar_label_from_db(workspace_name, db_path)
    if sidebar_label:
        return _normalize_for_speech(sidebar_label)

    # 4. Last resort: the raw directory_name
    return _normalize_for_speech(workspace_name)


def reset_cache() -> None:
    """Drop the cached DB path. Used by tests; not part of the runtime API."""
    global _cached_db_path
    _cached_db_path = None


# ---------------------------------------------------------------------------
# Workspace/session identity for the .workspace switch sidecar (DEF-237)
# ---------------------------------------------------------------------------


def resolve_workspace_id(directory_name: str, cfg: "HeyvoxConfig | None" = None) -> str:
    """Resolve the Conductor workspace UUID for a directory_name.

    Reuses the same DB the label lookup above already reads. Returns "" on
    any failure (no DB configured, no match, locked DB) — callers fall back
    to the plain positional switch, the same fail-soft contract
    get_active_workspace_and_session already documents.
    """
    if not directory_name:
        return ""
    db_path = _get_workspace_db_path(cfg)
    if not db_path:
        return ""
    try:
        from heyvox.adapters.conductor import get_active_workspace_and_session
        identity = get_active_workspace_and_session(directory_name=directory_name, db_path=db_path)
    except Exception:
        return ""
    return identity.workspace_id if identity else ""


def write_switch_sidecar(wav_path: str, workspace: str, workspace_id: str = "", session_id: str = "") -> None:
    """Write the .workspace sidecar that tells the orchestrator what to switch to.

    JSON so workspace_id/session_id can ride along with the label — the
    orchestrator forwards them to conductor-switch-workspace's --id/--session
    flags instead of the fuzzy positional-label search (DEF-237). Both are
    best-effort: empty string when resolution failed, and the orchestrator
    falls back to the old positional-label switch in that case.
    """
    if not workspace:
        return
    sidecar = wav_path.replace(".wav", ".workspace")
    try:
        with open(sidecar, "w") as f:
            json.dump(
                {"workspace": workspace, "workspace_id": workspace_id, "session_id": session_id}, f,
            )
    except OSError as exc:
        log.debug("write_switch_sidecar: failed to write %s: %s", sidecar, exc)


def read_switch_sidecar(text: str) -> dict:
    """Parse .workspace sidecar content.

    Accepts the current JSON format and falls back to treating the whole
    string as a plain label — the format every sidecar used before DEF-237,
    so any sidecar an old worker.py/watcher.py process left in flight across
    a deploy still switches the workspace (just without --session).
    """
    text = text.strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
            return {
                "workspace": data.get("workspace", ""),
                "workspace_id": data.get("workspace_id", ""),
                "session_id": data.get("session_id", ""),
            }
        except (json.JSONDecodeError, AttributeError):
            pass
    return {"workspace": text, "workspace_id": "", "session_id": ""}
