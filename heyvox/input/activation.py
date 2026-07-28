"""Shared app-activation helper (poll-verified frontmost-by-PID).

Extracted from heyvox.input.target._activate_app so heyvox.adapters.conductor
can reuse it for WorkspaceProvider.activate() without duplicating the
poll-loop — this logic is proven necessary specifically for Electron/Tauri
apps like Conductor (see DEF-054/061/067), not something safe to reimplement
independently.
"""

from __future__ import annotations

import time as _time
from typing import Callable, Optional


def activate_pid(
    pid: int,
    app_name: str = "",
    log: Optional[Callable[[str], None]] = None,
) -> bool:
    """Activate an app by PID, polling until frontmost matches or timeout.

    Returns True if frontmost PID matches target after activation, else False.

    For multi-PID bundles (Electron apps like Conductor, VS Code, Slack, Cursor),
    `activateWithOptions_` is advisory at the AppKit layer — WindowServer may
    keep a different helper PID as the key window even though the bundle has
    been "activated". We poll frontmost PID up to 500 ms with periodic
    re-activation to force the specific target PID to the front before the
    caller sends keystrokes. Single-PID apps resolve on the first poll.

    See DEF-054 for the failure mode this guards against.
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    try:
        import AppKit
        app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is None:
            _log(f"activate: no NSRunningApplication for pid={pid}, falling back")
        else:
            target_bundle = None
            try:
                target_bundle = app.bundleIdentifier()
            except Exception:
                pass
            app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
            # Poll-verify: frontmost PID may lag or land on a sibling helper PID.
            ws = AppKit.NSWorkspace.sharedWorkspace()
            for i in range(5):
                _time.sleep(0.1)
                front = ws.frontmostApplication()
                front_pid = front.processIdentifier() if front else 0
                if front_pid == pid:
                    if i > 0:
                        _log(f"activate: pid={pid} confirmed frontmost after {i+1} polls")
                    return True
                # Same-bundle sibling handling (DEF-061/067). See original
                # comments before this refactor for full reasoning.
                same_bundle = False
                if front is not None:
                    try:
                        front_bundle = front.bundleIdentifier()
                        if target_bundle and front_bundle:
                            same_bundle = front_bundle == target_bundle
                    except Exception:
                        pass
                    if not same_bundle:
                        try:
                            front_name = front.localizedName() or ""
                            same_bundle = (
                                bool(front_name)
                                and front_name.lower() == (app_name or "").lower()
                            )
                        except Exception:
                            pass
                if same_bundle:
                    _log(
                        f"activate: sibling helper frontmost (pid={front_pid}, "
                        f"target={pid}) — same bundle, skipping further "
                        f"retries (DEF-061/067)"
                    )
                    return False
                if i < 4:
                    app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
            _log(
                f"activate: WARNING target pid={pid} but frontmost pid={front_pid} "
                f"after 500 ms retry (likely different helper PID in same bundle)"
            )
            return False
    except Exception as e:
        _log(f"activate: NSRunningApplication path failed: {e}")
    # No osascript fallback here (W10, Fact 4): an extra
    # `tell application ... activate` fork costs ~50ms/paste and is
    # redundant with the NSRunningApplication bundle-ID path already
    # taken by callers.
    return False
