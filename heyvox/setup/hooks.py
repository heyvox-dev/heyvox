"""Herald hooks installer for Claude Code.

Registers Herald hook shims in ~/.claude/settings.json so that Claude Code
triggers TTS on responses, ambient sounds, notifications, and session lifecycle.

Hook scripts are installed to ~/.claude/hooks/herald/ (a stable path that
survives Conductor workspace archiving) rather than pointing directly into
the heyvox package directory.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

from heyvox.herald import HERALD_HOOKS

# Stable install location — survives workspace archival
_STABLE_HOOKS_DIR = Path.home() / ".claude" / "hooks" / "herald"

# Hook event → shell script mapping.
#
# DEF-223: event names and entry shape must match Claude Code's settings schema
# exactly. Two bugs lived here until 2026-07-26:
#   1. "Stop_session" is not a Claude Code event — the correct name is
#      "SessionEnd", so the cleanup hook was registered under a key nothing
#      ever dispatched.
#   2. Entries were written flat as {"command": ...}. The schema requires a
#      matcher group wrapping a handler list: {"hooks": [{"type": "command",
#      "command": ...}]}, and "type" is mandatory. Flat entries are not
#      executed, so Herald never spoke on a fresh `heyvox setup` — the
#      maintainer's own machine worked only because its entries were
#      hand-written in the correct shape.
_HOOKS = {
    "Stop": {
        "script": "on-response.sh",
        "desc": "TTS on Claude response",
    },
    "Notification": {
        "script": "on-notify.sh",
        "desc": "Voice warnings for dangerous operations",
    },
    "SessionEnd": {
        "script": "on-session-end.sh",
        "desc": "Cleanup on session end",
    },
}

# Events written by earlier versions that are not real Claude Code events.
# Herald entries found under these keys are migrated to the correct event.
_LEGACY_EVENTS = {"Stop_session": "SessionEnd"}


def _handlers(entry: dict) -> list[dict]:
    """Return the handler dicts inside one settings hook entry.

    Handles both the correct nested matcher-group shape and the flat shape
    written by heyvox <= 1.1.3, so detection and removal work against configs
    produced by either version.
    """
    if not isinstance(entry, dict):
        return []
    nested = entry.get("hooks")
    if isinstance(nested, list):
        return [h for h in nested if isinstance(h, dict)]
    if "command" in entry:
        return [entry]  # legacy flat entry
    return []


def _is_herald(entry: dict) -> bool:
    """True if this settings hook entry belongs to Herald."""
    return any(
        "herald" in str(h.get("command", "")).lower()
        for h in _handlers(entry)
    )


def _write_settings(settings_path: Path, settings: dict) -> None:
    """Write settings.json atomically.

    DEF-226: the previous open(path, "w") truncated the user's live Claude Code
    config before writing. An interrupt mid-write left it invalid, taking every
    unrelated hook, permission and plugin setting down with it.
    """
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(settings_path.parent), prefix=".settings-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(settings, f, indent=2)
            f.write("\n")
        os.replace(tmp_name, settings_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def install_herald_hooks() -> list[tuple[bool, str]]:
    """Install Herald hooks into ~/.claude/settings.json.

    Copies hook scripts from the package to ~/.claude/hooks/herald/ (stable path)
    and registers them in settings.json. Returns list of (success, message) tuples.
    """
    settings_path = Path.home() / ".claude" / "settings.json"
    results = []

    try:
        # Copy hook scripts to stable location
        _STABLE_HOOKS_DIR.mkdir(parents=True, exist_ok=True)
        # DEF-111: also ship the shared helper that hooks source for the
        # workspace-aware PYTHONPATH walk-up logic. Without it the hooks
        # would fail with "no such file" on every Claude Code event.
        lib_src = HERALD_HOOKS / "_lib.sh"
        if lib_src.exists():
            lib_dest = _STABLE_HOOKS_DIR / "_lib.sh"
            shutil.copy2(lib_src, lib_dest)
            lib_dest.chmod(0o755)
        for info in _HOOKS.values():
            src = HERALD_HOOKS / info["script"]
            if src.exists():
                dest = _STABLE_HOOKS_DIR / info["script"]
                shutil.copy2(src, dest)
                dest.chmod(0o755)

        if settings_path.exists():
            with open(settings_path) as f:
                settings = json.load(f)
        else:
            settings = {}

        if "hooks" not in settings:
            settings["hooks"] = {}

        # DEF-223: drop Herald entries left under bogus event names by earlier
        # versions. They never fired, so there is nothing to preserve — the
        # correct event gets a fresh entry below.
        for stale_event in _LEGACY_EVENTS:
            stale = settings["hooks"].get(stale_event)
            if not isinstance(stale, list):
                continue
            kept = [e for e in stale if not _is_herald(e)]
            if len(kept) != len(stale):
                results.append((True, f"Migrated stale hook event ({stale_event})"))
            if kept:
                settings["hooks"][stale_event] = kept
            else:
                settings["hooks"].pop(stale_event, None)

        for event, info in _HOOKS.items():
            hook_script = _STABLE_HOOKS_DIR / info["script"]

            if not hook_script.exists():
                results.append((False, f"{info['desc']}: script not found ({hook_script})"))
                continue

            command = f"bash {hook_script}"

            # Get or create the event's hook list
            if event not in settings["hooks"]:
                settings["hooks"][event] = []

            existing = settings["hooks"][event]

            # Rewrite any legacy flat Herald entry into the nested shape; a
            # correctly-shaped one is updated in place so the path stays
            # current when the package moves.
            updated = False
            rebuilt = []
            for entry in existing:
                if not _is_herald(entry):
                    rebuilt.append(entry)
                    continue
                if "hooks" in entry and not updated:
                    for h in _handlers(entry):
                        if "herald" in str(h.get("command", "")).lower():
                            h["type"] = h.get("type", "command")
                            h["command"] = command
                    rebuilt.append(entry)
                    updated = True
                # legacy flat entries (and any duplicate) are dropped

            if not updated:
                rebuilt.append({
                    "hooks": [{"type": "command", "command": command}],
                })

            settings["hooks"][event] = rebuilt
            verb = "updated" if updated else "installed"
            results.append((True, f"{info['desc']}: {verb} ({event})"))

        _write_settings(settings_path, settings)

    except Exception as e:
        results.append((False, f"Failed to update settings: {e}"))

    return results


def uninstall_herald_hooks() -> list[tuple[bool, str]]:
    """Remove Herald hooks from ~/.claude/settings.json."""
    settings_path = Path.home() / ".claude" / "settings.json"
    results = []

    try:
        if not settings_path.exists():
            return [(True, "No settings file found — nothing to remove")]

        with open(settings_path) as f:
            settings = json.load(f)

        hooks = settings.get("hooks", {})
        for event in list(hooks.keys()):
            if not isinstance(hooks[event], list):
                continue
            original_len = len(hooks[event])
            # _is_herald covers both the nested shape and the legacy flat one,
            # so this also cleans up configs written by heyvox <= 1.1.3.
            hooks[event] = [e for e in hooks[event] if not _is_herald(e)]
            removed = original_len - len(hooks[event])
            if removed:
                results.append((True, f"Removed {removed} Herald hook(s) from {event}"))
            if not hooks[event]:
                hooks.pop(event, None)

        _write_settings(settings_path, settings)

        if not results:
            results.append((True, "No Herald hooks found to remove"))

    except Exception as e:
        results.append((False, f"Failed: {e}"))

    return results
