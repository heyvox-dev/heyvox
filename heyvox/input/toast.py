"""User-facing failure toasts for the paste pipeline.

Two-tier delivery:
1. Hammerspoon `hs.alert.show()` - rich, matches Herald's UX style
2. osascript `display notification` - universally available fallback

Silent failure is NOT acceptable for fail-closed paste - the user MUST be
told why their transcript landed on the clipboard instead of in their app.

Imported by:
- heyvox/input/target.py resolve_lock() - fail-closed (Plan 15-05)
- heyvox/input/target.py verify_paste() - drift (Plan 15-06)

Pattern mirrors heyvox/herald/orchestrator.py (DEF-074 hardened).

Requirement: PASTE-15-R5
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path


def _log(msg: str) -> None:
    try:
        print(f"[toast] {msg}", file=sys.stderr, flush=True)
    except (BrokenPipeError, OSError):
        pass


def _hammerspoon_running() -> bool:
    """True iff the Hammerspoon.app process is running.

    DEF-074: When Hammerspoon is not running, `hs -c` triggers the macOS
    "Hammerspoon is not running -- Launch?" dialog, interrupting the user.
    Gate every `hs` invocation with this check.

    DEF-090 follow-up: pgrep is normally fast, but on a heavily loaded
    machine (or during a fork-storm in highly multithreaded Python with
    Cocoa frameworks held) it has been observed to stall for tens of
    seconds. The toast is best-effort user feedback — never worth a
    multi-second block of the calling thread, which is the recording
    pipeline's send phase. Hard 1 s ceiling; if pgrep hasn't finished
    by then, fall back to "not running" and use the osascript path.
    """
    try:
        return subprocess.call(
            ["pgrep", "-q", "Hammerspoon"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1.0,
        ) == 0
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
        return False


def show_failure_toast(
    reason_message: str, title: str = "HeyVox paste"
) -> None:
    """Surface a paste-failure toast to the user.

    Tier 1: Hammerspoon `hs.alert.show()` (rich, ~2.5s on-screen).
    Tier 2: osascript `display notification` (native, no rich styling).

    Both tiers are best-effort - subprocess failures are swallowed silently
    after a stderr log. The caller must NOT depend on toast delivery to
    decide further action; this is purely user-feedback.

    Args:
        reason_message: Short user-readable string. Quotes/newlines safe
            (json-quoted before shell-out).
        title: Notification title for tier 2 (Hammerspoon shows alert text only).

    Requirement: PASTE-15-R5
    """
    if _hammerspoon_running():
        hs = shutil.which("hs") or "/opt/homebrew/bin/hs"
        if Path(hs).exists():
            script = f"hs.alert.show({json.dumps(reason_message)}, 2.5)"
            try:
                subprocess.Popen(
                    [hs, "-c", script],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                _log(f"hs alert: {reason_message[:80]}")
                return
            except (OSError, subprocess.SubprocessError) as e:
                _log(f"hs alert FAILED: {e} - falling through to osascript")

    # Fallback: native macOS notification via osascript
    safe_msg = reason_message.replace("\\", "\\\\").replace('"', '\\"')
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        f'display notification "{safe_msg}" with title "{safe_title}"'
    )
    try:
        subprocess.Popen(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _log(f"osascript notification: {reason_message[:80]}")
    except (OSError, subprocess.SubprocessError) as e:
        _log(f"osascript notification FAILED: {e} - toast delivery dead")
