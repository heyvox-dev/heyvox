"""
Target snapshot and restore for text injection.

Captures which app and text field were focused when recording started,
so injected text goes to the right place even if the user clicks around
during transcription.

Uses macOS Accessibility API (AXUIElement) to identify and refocus
specific text fields.

Fallback logic when no text field was focused at recording start:
  1. Activate the original app
  2. Search the focused window for text input elements
  3. If exactly one text field found → focus it automatically
  4. If zero or multiple → just activate the app (best effort)
"""

import sys
from dataclasses import dataclass
from typing import Any


def _log(msg: str) -> None:
    """Log to stderr with [target] prefix."""
    print(f"[target] {msg}", file=sys.stderr, flush=True)

# AX roles that accept text input
_TEXT_ROLES = frozenset({"AXTextField", "AXTextArea", "AXWebArea", "AXComboBox"})


@dataclass
class TargetSnapshot:
    """Captured state of the focused app and text field at recording start."""
    app_name: str
    app_pid: int
    ax_element: Any = None  # AXUIElement reference (opaque CFType)
    element_role: str = ""
    window_title: str = ""  # AXTitle of focused window (for tab restoration)


def snapshot_target() -> TargetSnapshot | None:
    """Capture the frontmost app and its focused text field.

    Returns None if AppKit/Accessibility APIs are unavailable.
    """
    try:
        import AppKit
        from ApplicationServices import (
            AXUIElementCreateApplication,
            AXUIElementCopyAttributeValue,
        )
    except ImportError:
        _log("WARNING: AppKit/ApplicationServices unavailable — snapshot disabled")
        return None

    ws = AppKit.NSWorkspace.sharedWorkspace()
    front_app = ws.frontmostApplication()
    if front_app is None:
        return None

    app_name = front_app.localizedName() or ""
    app_pid = front_app.processIdentifier()

    snap = TargetSnapshot(app_name=app_name, app_pid=app_pid)

    # Try to get the focused UI element via Accessibility API
    ax_app = AXUIElementCreateApplication(app_pid)
    err, focused = AXUIElementCopyAttributeValue(ax_app, "AXFocusedUIElement", None)

    if err == 0 and focused is not None:
        err2, role = AXUIElementCopyAttributeValue(focused, "AXRole", None)
        role_str = str(role) if err2 == 0 and role else ""
        snap.ax_element = focused
        snap.element_role = role_str

        if role_str in _TEXT_ROLES:
            _log(f"snapshot: {app_name} pid={app_pid}, text field ({role_str})")
        else:
            _log(f"snapshot: {app_name} pid={app_pid}, focused={role_str} (not text)")
    else:
        _log(f"snapshot: {app_name} pid={app_pid}, no focused element")

    # Capture window title — critical for tab-based apps (Chrome, Electron)
    # where the app PID is shared across multiple tabs/workspaces.
    err, window = AXUIElementCopyAttributeValue(ax_app, "AXFocusedWindow", None)
    if err == 0 and window is not None:
        err2, title = AXUIElementCopyAttributeValue(window, "AXTitle", None)
        if err2 == 0 and title:
            snap.window_title = str(title)
            _log(f"snapshot: window='{snap.window_title}'")

    return snap


def restore_target(snap: TargetSnapshot) -> bool:
    """Refocus the app and text field from a snapshot.

    Returns True if the app was activated (text field focus is best-effort),
    False if activation failed entirely.

    Strategy: activate the app and give it time to restore its own focus.
    Most apps (including Electron) restore the last-focused text field
    automatically when re-activated. Trying to set AXFocused on a stale
    element (err -25202) is unreliable after app switching, so we only
    attempt it as a bonus — the app activation is the real fix.
    """
    if snap is None:
        return False

    import time as _time

    try:
        from ApplicationServices import (
            AXUIElementCreateApplication,
            AXUIElementSetAttributeValue,
        )
        from CoreFoundation import kCFBooleanTrue
    except ImportError:
        return False

    # Step 1: Activate the app — this is the critical step
    _log(f"restore: activating {snap.app_name} (pid={snap.app_pid})")
    _activate_app(snap.app_pid, snap.app_name)
    _time.sleep(0.3)

    # Verify activation worked
    try:
        import AppKit
        ws = AppKit.NSWorkspace.sharedWorkspace()
        actual = ws.frontmostApplication()
        actual_name = actual.localizedName() if actual else "?"
        actual_pid = actual.processIdentifier() if actual else -1
        if actual_pid != snap.app_pid:
            _log(f"restore: WARNING: wanted {snap.app_name} (pid={snap.app_pid}) "
                 f"but frontmost is {actual_name} (pid={actual_pid})")
        else:
            _log(f"restore: activated {actual_name} (pid={actual_pid}) OK")
    except Exception:
        _log("restore: activated (couldn't verify)")

    # Step 2: Try to refocus the captured text field (often stale, best-effort)
    if snap.ax_element is not None and snap.element_role in _TEXT_ROLES:
        err = AXUIElementSetAttributeValue(snap.ax_element, "AXFocused", kCFBooleanTrue)
        if err == 0:
            _log(f"restore: refocused text field ({snap.element_role}) in {snap.app_name}")
            return True
        # -25202 = kAXErrorCannotComplete (stale ref after app switch) — expected
        if err != -25202:
            _log(f"restore: WARNING: AX refocus failed (err={err})")
        else:
            _log(f"restore: AX element stale (-25202), relying on app's own focus restore")

    # Step 3: Fallback — find text fields in the focused window
    ax_app = AXUIElementCreateApplication(snap.app_pid)
    text_fields = _find_window_text_fields(ax_app)
    _log(f"restore: found {len(text_fields)} text fields in window")

    if len(text_fields) == 1:
        elem, role = text_fields[0]
        err = AXUIElementSetAttributeValue(elem, "AXFocused", kCFBooleanTrue)
        if err == 0:
            _log(f"restore: focused sole text field ({role}) in {snap.app_name}")
            return True

    # App was activated — even if we couldn't pinpoint the text field,
    # the app likely restored its own focus state. Return True so the
    # caller proceeds with pasting into the now-frontmost app.
    _log(f"restore: done (app activated, text field focus = best-effort)")
    return True


def _activate_app(pid: int, app_name: str) -> None:
    """Activate an app by PID, falling back to osascript."""
    try:
        import AppKit
        app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is not None:
            app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
            return
    except Exception:
        pass
    # Fallback
    from heyvox.input.injection import focus_app
    focus_app(app_name)


def _find_window_text_fields(ax_app) -> list[tuple]:
    """Find text input fields in the app's focused window.

    Walks the AX tree up to a limited depth. Returns list of
    (AXUIElement, role_str) tuples. Depth is capped to avoid
    hanging on complex Electron DOM trees.
    """
    from ApplicationServices import AXUIElementCopyAttributeValue

    err, window = AXUIElementCopyAttributeValue(ax_app, "AXFocusedWindow", None)
    if err != 0 or window is None:
        return []

    results = []
    _walk_ax_tree(window, results, depth=6)
    return results


def _walk_ax_tree(element, results: list, depth: int) -> None:
    """Recursively collect text input elements from an AX subtree."""
    if depth <= 0 or len(results) >= 10:
        return

    from ApplicationServices import AXUIElementCopyAttributeValue

    err, role = AXUIElementCopyAttributeValue(element, "AXRole", None)
    role_str = str(role) if err == 0 and role else ""

    if role_str in _TEXT_ROLES:
        results.append((element, role_str))
        return  # Don't recurse into text fields

    err, children = AXUIElementCopyAttributeValue(element, "AXChildren", None)
    if err != 0 or children is None:
        return

    for child in children:
        _walk_ax_tree(child, results, depth - 1)
