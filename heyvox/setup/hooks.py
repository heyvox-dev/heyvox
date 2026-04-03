"""Herald hooks installer for Claude Code.

Registers Herald hook shims in ~/.claude/settings.json so that Claude Code
triggers TTS on responses, ambient sounds, notifications, and session lifecycle.
"""

import json
import sys
from pathlib import Path

from heyvox.herald import HERALD_HOOKS


# Hook event → shell script mapping
_HOOKS = {
    "Stop": {
        "script": "on-response.sh",
        "desc": "TTS on Claude response",
    },
    "Notification": {
        "script": "on-notify.sh",
        "desc": "Voice warnings for dangerous operations",
    },
    "Stop_session": {
        "script": "on-session-end.sh",
        "desc": "Cleanup on session end",
    },
}


def install_herald_hooks() -> list[tuple[bool, str]]:
    """Install Herald hooks into ~/.claude/settings.json.

    Returns list of (success, message) tuples.
    """
    settings_path = Path.home() / ".claude" / "settings.json"
    results = []

    try:
        if settings_path.exists():
            with open(settings_path) as f:
                settings = json.load(f)
        else:
            settings = {}

        if "hooks" not in settings:
            settings["hooks"] = {}

        for event, info in _HOOKS.items():
            hook_script = HERALD_HOOKS / info["script"]

            if not hook_script.exists():
                results.append((False, f"{info['desc']}: script not found ({hook_script})"))
                continue

            hook_entry = {
                "command": f"bash {hook_script}",
            }

            # Get or create the event's hook list
            if event not in settings["hooks"]:
                settings["hooks"][event] = []

            existing = settings["hooks"][event]

            # Check if a Herald hook already exists for this event
            already_installed = any(
                "herald" in h.get("command", "").lower()
                for h in existing
                if isinstance(h, dict)
            )

            if already_installed:
                # Update the existing hook path (in case package location changed)
                for h in existing:
                    if isinstance(h, dict) and "herald" in h.get("command", "").lower():
                        h["command"] = hook_entry["command"]
                results.append((True, f"{info['desc']}: updated ({event})"))
            else:
                existing.append(hook_entry)
                results.append((True, f"{info['desc']}: installed ({event})"))

        # Write back
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        with open(settings_path, "w") as f:
            json.dump(settings, f, indent=2)
            f.write("\n")

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
            original_len = len(hooks[event])
            hooks[event] = [
                h for h in hooks[event]
                if not (isinstance(h, dict) and "herald" in h.get("command", "").lower())
            ]
            removed = original_len - len(hooks[event])
            if removed:
                results.append((True, f"Removed {removed} Herald hook(s) from {event}"))

        with open(settings_path, "w") as f:
            json.dump(settings, f, indent=2)
            f.write("\n")

        if not results:
            results.append((True, "No Herald hooks found to remove"))

    except Exception as e:
        results.append((False, f"Failed: {e}"))

    return results
