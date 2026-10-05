"""Herald jump key — go to the workspace of the last announced message.

workspace_switch.mode "jump_key" (the default, see WorkspaceSwitchConfig in
heyvox/config.py): Herald no longer switches on its own. The orchestrator
writes the identity of every new (non-continuation) message to
HERALD_JUMP_TARGET_FILE, replacing the previous one; a clean double-tap of the
jump key (heyvox.input.ptt) calls jump_to_target(), which brings the app to the
front and lets the app's WorkspaceProvider select workspace and session tab.

The target stays valid until the next announcement, so the user can jump
during or after playback; Escape only stops the audio and leaves it alone.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Callable, Optional

from heyvox.constants import HERALD_JUMP_TARGET_FILE, RECORDING_FLAG

log = logging.getLogger(__name__)

_FIELDS = ("workspace", "workspace_id", "session_id", "cwd", "provider")


def write_jump_target(path: Path, identity: dict) -> None:
    """Store a switch-sidecar identity (read_switch_sidecar) as the jump target."""
    data = {k: identity.get(k, "") or "" for k in _FIELDS}
    data["ts"] = time.time()
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data))
        os.replace(tmp, path)
    except OSError as e:
        log.debug("jump target write failed: %s", e)


def clear_jump_target(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def read_jump_target(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return {k: str(data.get(k, "") or "") for k in _FIELDS}


def _activate_app(app_name: str) -> bool:
    """Bring the running app with this localized name to the front."""
    if not app_name:
        return False
    try:
        import AppKit
    except ImportError:
        return False
    try:
        for app in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
            if (app.localizedName() or "").lower() == app_name.lower():
                return bool(app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps))
    except Exception as e:
        log.debug("activating %r failed: %s", app_name, e)
    return False


def jump_to_target(
    target_file: Path = Path(HERALD_JUMP_TARGET_FILE),
    *,
    log_fn: Optional[Callable[[str], None]] = None,
    recording_flag: Path = Path(RECORDING_FLAG),
) -> bool:
    """Jump to the stored target: app to front, then workspace + session tab.

    Returns True when the provider confirmed the workspace switch (details
    in the Herald debug log). Never raises. Skipped while HeyVox records or injects (DEF-070: a focus change
    mid-paste sends the text to the wrong place).
    """
    def _log(msg: str) -> None:
        (log_fn or log.info)(msg)

    target = read_jump_target(target_file)
    if target is None:
        _log("Jump: no announced workspace to jump to")
        return False
    if recording_flag.exists():
        _log("Jump: skipped while recording/injecting")
        return False

    try:
        from heyvox.config import load_config
        from heyvox.herald.orchestrator import (
            OrchestratorConfig, _switch_workspace, _workspace_target,
            workspace_apps_from_profiles,
        )
        heyvox_cfg = load_config()
        apps = workspace_apps_from_profiles(heyvox_cfg.app_profiles)
        default_provider, default_entry = next(iter(apps.items()), ("", {}))
        cfg = OrchestratorConfig(
            workspace_provider=default_provider,
            workspace_app_name=default_entry.get("app_name", ""),
            workspace_db=default_entry.get("db", ""),
            workspace_apps=apps,
        )
        provider_name, app_name, _ = _workspace_target(cfg, target["provider"])
        if not provider_name:
            _log(f"Jump: no app profile for provider {target['provider']!r}")
            return False
        _activate_app(app_name)
        ok = _switch_workspace(
            target["workspace"], cfg,
            workspace_id=target["workspace_id"], session_id=target["session_id"],
            cwd=target["cwd"], provider_name=provider_name,
        )
    except Exception as e:
        _log(f"Jump: failed ({e!r})")
        return False
    _log(
        f"Jump: -> {target['workspace']!r} (session {target['session_id'] or '-'}) ok={ok}"
    )
    return ok
