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
  3. If exactly one text field found -> focus it automatically
  4. If zero or multiple -> just activate the app (best effort)
"""

import os
import sys
import time as _time
from dataclasses import dataclass
from typing import Any


def _log(msg: str) -> None:
    """Log to stderr with [HH:MM:SS] [target] prefix.

    Timestamp is needed for sub-step timing inside restore_target
    (DEF-061) — without it, multi-second hangs inside a single call
    are invisible because only the caller's entry/exit lines carry
    timestamps.
    """
    try:
        ts = _time.strftime("%H:%M:%S")
        print(f"[{ts}] [target] {msg}", file=sys.stderr, flush=True)
    except (BrokenPipeError, OSError):
        pass

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
    detected_workspace: str = ""  # Workspace name detected via app profile


def _switch_app_workspace(workspace: str, profile) -> None:
    """Switch an app to a specific workspace tab using its profile's switch command.

    Uses the workspace_switch_cmd from the app profile (e.g. conductor-switch-workspace
    CLI which clicks the sidebar item via Hammerspoon).
    """
    import subprocess
    if not profile or not profile.workspace_switch_cmd:
        _log(f"restore: no workspace_switch_cmd in profile, skipping workspace switch")
        return
    script = os.path.expanduser(profile.workspace_switch_cmd)
    if not os.path.exists(script):
        _log(f"restore: workspace switch cmd not found ({script}), skipping")
        return
    try:
        result = subprocess.run(
            [script, workspace],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            _log(f"restore: switched {profile.name} to workspace '{workspace}'")
        else:
            _log(f"restore: workspace switch failed: {result.stderr.strip()}")
    except Exception as e:
        _log(f"restore: workspace switch error: {e}")


def _detect_app_workspace(pid: int, profile) -> str:
    """Detect the currently visible workspace by reading the AX tree + app DB.

    For apps with workspace detection (has_workspace_detection=True in their
    app profile), walks the AX tree to find the branch name shown in the
    right panel (after the AXSplitter), then maps it to the workspace
    display name via the app's SQLite DB.

    Returns the workspace name (directory_name) or empty string.
    """
    _t0_detect = _time.time()

    if not profile or not profile.has_workspace_detection:
        return ""

    if not profile.workspace_db or not profile.workspace_list_query:
        return ""

    # Step 1: Walk the AX tree to find the branch name shown in the right panel
    branch_name = ""
    try:
        from ApplicationServices import (
            AXUIElementCreateApplication,
            AXUIElementCopyAttributeValue,
        )
        ax = AXUIElementCreateApplication(pid)
        # Try AXFocusedWindow first (works when app is frontmost),
        # then AXMainWindow, then first of AXWindows (works on dual-monitor
        # when another app is macOS-frontmost but target is under the mouse).
        win = None
        for attr in ("AXFocusedWindow", "AXMainWindow"):
            err, w = AXUIElementCopyAttributeValue(ax, attr, None)
            if err == 0 and w is not None:
                win = w
                break
        if win is None:
            err, windows = AXUIElementCopyAttributeValue(ax, "AXWindows", None)
            if err == 0 and windows and len(windows) > 0:
                win = windows[0]
        if win is None:
            _log(f"workspace detect: no window found for pid={pid} "
                 f"({(_time.time() - _t0_detect)*1000:.0f}ms)")
            return ""

        # Flatten the tree looking for the first AXStaticText after the first AXSplitter
        items: list[tuple[str, str]] = []  # (role, value)

        def _collect(elem, depth=0):
            if depth > 6 or len(items) > 300:
                return
            err_r, role = AXUIElementCopyAttributeValue(elem, "AXRole", None)
            r = str(role) if err_r == 0 and role else ""
            err_v, val = AXUIElementCopyAttributeValue(elem, "AXValue", None)
            v = str(val).strip() if err_v == 0 and val else ""
            items.append((r, v))
            err_c, children = AXUIElementCopyAttributeValue(elem, "AXChildren", None)
            if err_c == 0 and children:
                for c in children:
                    _collect(c, depth + 1)

        _collect(win)

        # Find the first AXStaticText with a value after the first AXSplitter
        past_splitter = False
        for r, v in items:
            if r == "AXSplitter":
                if past_splitter:
                    break  # Don't go past the second splitter
                past_splitter = True
                continue
            if past_splitter and r == "AXStaticText" and v:
                branch_name = v
                break

    except Exception as e:
        _log(f"workspace AX detection failed for {profile.name}: {e} "
             f"({(_time.time() - _t0_detect)*1000:.0f}ms)")
        return ""

    _ax_ms = (_time.time() - _t0_detect) * 1000
    if not branch_name:
        _log(f"workspace: no branch found ({_ax_ms:.0f}ms AX walk)")
        return ""

    # Step 2: Map branch name to workspace name via the app's DB.
    # The AX tree shows the branch name (e.g. "review-main-files"),
    # which we map to the workspace display name via the app profile's DB.
    # Uses Python's sqlite3 module directly (no subprocess overhead).
    try:
        import sqlite3 as _sqlite3
        _t0_db = _time.time()
        db_path = os.path.expanduser(profile.workspace_db)
        conn = _sqlite3.connect(db_path, timeout=2)
        try:
            rows = conn.execute(profile.workspace_list_query).fetchall()
        finally:
            conn.close()
        _db_ms = (_time.time() - _t0_db) * 1000
        for row in rows:
            if len(row) >= 2:
                city, db_branch = row[0], row[1]
                if db_branch == branch_name:
                    _total_ms = (_time.time() - _t0_detect) * 1000
                    _log(f"[TIMING] workspace detected: '{city}' "
                         f"(AX={_ax_ms:.0f}ms, db={_db_ms:.0f}ms, total={_total_ms:.0f}ms) "
                         f"for {profile.name}")
                    return city
        _log(f"workspace: branch {branch_name!r} not matched in {profile.name} DB "
             f"(AX={_ax_ms:.0f}ms, db={_db_ms:.0f}ms)")
    except Exception as e:
        _log(f"workspace DB error for {profile.name}: {e}")

    return ""


def _app_under_mouse() -> tuple[str, int] | None:
    """Find the app that owns the window under the mouse cursor.

    On multi-monitor setups, NSWorkspace.frontmostApplication() returns the
    last globally activated app, which may be on a different screen than the
    mouse. This function uses CGWindowListCopyWindowInfo to find the topmost
    window at the mouse position, giving the correct target on the screen
    the user is actually interacting with.

    Returns (app_name, pid) or None if detection fails.
    """
    try:
        import AppKit
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGWindowListOptionOnScreenOnly,
            kCGWindowListExcludeDesktopElements,
            kCGNullWindowID,
        )
    except ImportError:
        return None

    mouse = AppKit.NSEvent.mouseLocation()
    # NSEvent.mouseLocation() uses bottom-left origin; CGWindowList uses top-left.
    # Convert via main screen height.
    main_screen = AppKit.NSScreen.mainScreen()
    if main_screen is None:
        return None
    screen_h = main_screen.frame().size.height
    mouse_x = mouse.x
    mouse_y = screen_h - mouse.y

    windows = CGWindowListCopyWindowInfo(
        kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements,
        kCGNullWindowID,
    )
    if not windows:
        return None

    for win in windows:
        # Skip windows without bounds or with layer > 0 (menu bar, overlays)
        layer = win.get("kCGWindowLayer", 999)
        if layer != 0:
            continue
        bounds = win.get("kCGWindowBounds")
        if not bounds:
            continue
        x, y = bounds["X"], bounds["Y"]
        w, h = bounds["Width"], bounds["Height"]
        if x <= mouse_x <= x + w and y <= mouse_y <= y + h:
            pid = win.get("kCGWindowOwnerPID", 0)
            name = win.get("kCGWindowOwnerName", "")
            if pid and name:
                return (name, pid)

    return None


def snapshot_target(config=None) -> "TargetSnapshot | None":
    """Capture the app and text field the user is interacting with.

    On multi-monitor setups, prefers the app under the mouse cursor over
    NSWorkspace.frontmostApplication(), since the latter can return an app
    on a different screen than where the user is actually working.

    Args:
        config: HeyvoxConfig instance for app profile lookup. If None,
            workspace detection is skipped.

    Returns None if AppKit/Accessibility APIs are unavailable.
    """
    try:
        import AppKit
        from ApplicationServices import (
            AXUIElementCreateApplication,
            AXUIElementCopyAttributeValue,
        )
    except ImportError:
        _log("WARNING: AppKit/ApplicationServices unavailable -- snapshot disabled")
        return None

    # Primary: find the app under the mouse cursor (correct on multi-monitor)
    mouse_app = _app_under_mouse()

    # Fallback: NSWorkspace.frontmostApplication() (single-monitor or detection failure)
    ws = AppKit.NSWorkspace.sharedWorkspace()
    front_app = ws.frontmostApplication()

    if mouse_app:
        app_name, app_pid = mouse_app
        if front_app and front_app.processIdentifier() != app_pid:
            front_name = front_app.localizedName() or "?"
            _log(f"snapshot: mouse is over {app_name} (pid={app_pid}), "
                 f"frontmost={front_name} (pid={front_app.processIdentifier()}) -- using mouse target")
    elif front_app:
        app_name = front_app.localizedName() or ""
        app_pid = front_app.processIdentifier()
    else:
        return None

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

    # Capture window title -- critical for tab-based apps (Chrome, Electron)
    # where the app PID is shared across multiple tabs/workspaces.
    err, window = AXUIElementCopyAttributeValue(ax_app, "AXFocusedWindow", None)
    if err == 0 and window is not None:
        err2, title = AXUIElementCopyAttributeValue(window, "AXTitle", None)
        if err2 == 0 and title:
            snap.window_title = str(title)
            _log(f"snapshot: window='{snap.window_title}'")

    # Detect workspace via app profile (if config available and profile supports it).
    # Uses the AX tree (reliable) not DB timestamps.
    # On restore, we switch back to this workspace before pasting.
    if config is not None:
        profile = config.get_app_profile(app_name)
        if profile and profile.has_workspace_detection:
            detected = _detect_app_workspace(app_pid, profile)
            if detected:
                snap.detected_workspace = detected
                _log(f"snapshot: workspace='{detected}' (via {profile.name} profile)")

    return snap


def restore_target(snap: "TargetSnapshot", config=None) -> bool:
    """Refocus the app and text field from a snapshot.

    Returns True if the app was activated (text field focus is best-effort),
    False if activation failed entirely.

    Args:
        snap: The TargetSnapshot captured at recording start.
        config: HeyvoxConfig instance for app profile lookup. If None,
            workspace restoration is skipped.

    Strategy: activate the app and give it time to restore its own focus.
    Most apps (including Electron) restore the last-focused text field
    automatically when re-activated. Trying to set AXFocused on a stale
    element (err -25202) is unreliable after app switching, so we only
    attempt it as a bonus -- the app activation is the real fix.
    """
    if snap is None:
        return False

    try:
        from ApplicationServices import (
            AXUIElementCreateApplication,
            AXUIElementSetAttributeValue,
        )
        from CoreFoundation import kCFBooleanTrue
    except ImportError:
        return False

    # Fast path: check if we're already on the right app
    _already_frontmost = False
    try:
        import AppKit
        ws = AppKit.NSWorkspace.sharedWorkspace()
        actual = ws.frontmostApplication()
        actual_name = actual.localizedName() if actual else ""
        actual_pid = actual.processIdentifier() if actual else -1
        # Match by PID or by app name (handles Chrome stealing focus briefly)
        _already_frontmost = (
            actual_pid == snap.app_pid
            or (actual_name and actual_name.lower() == (snap.app_name or "").lower())
        )
    except Exception:
        pass

    # Step 0: For apps with workspace detection, switch back to the workspace
    # that was active when recording started (before activating the app).
    # CRITICAL: Only switch if the current workspace differs from the snapshot.
    # Unnecessary switches click the sidebar, stealing focus from the text input
    # and causing paste to go nowhere.
    if snap.detected_workspace and config is not None:
        profile = config.get_app_profile(snap.app_name)
        if profile:
            current_ws = _detect_app_workspace(snap.app_pid, profile)
            if current_ws and current_ws == snap.detected_workspace:
                _log(f"restore: already on workspace '{current_ws}', skipping switch")
                snap._workspace_switched = False
            else:
                _log(f"restore: switching {profile.name} to workspace "
                     f"'{snap.detected_workspace}' (current='{current_ws}')")
                _switch_app_workspace(snap.detected_workspace, profile)
                _time.sleep(profile.settle_delay)
                snap._workspace_switched = True

    # Step 1: Activate the app (skip if already frontmost)
    if _already_frontmost:
        _log(f"restore: {snap.app_name} already frontmost, skipping activate")
        activate_ok = True
    else:
        _log(f"restore: activating {snap.app_name} (pid={snap.app_pid})")
        activate_ok = _activate_app(snap.app_pid, snap.app_name)
        _time.sleep(0.3)

    # When activate fails (target PID != frontmost — common for multi-PID
    # Electron bundles rotating helper PIDs between capture and restore), the
    # captured AX element lives on a background helper. Any AX call on it
    # blocks ~6 s waiting for the unfocused helper's AX server (DEF-059).
    # Skip AX refocus; the paste path's focus shortcut (Cmd+L) will target
    # the current frontmost helper and land the keystrokes correctly.
    if not activate_ok:
        _log(
            f"restore: skipping AX refocus (activate failed — stale element "
            f"would hang); relying on paste-path focus shortcut"
        )
        snap._activate_failed = True
        return True

    # Step 2: Try to refocus the captured element directly.
    # Skip AXWebArea -- in Electron/Tauri apps this is the conversation content
    # area, not the text input. Pasting there silently fails. Go straight to
    # the text field search instead.
    is_web_area = snap.element_role == "AXWebArea"
    if snap.ax_element is not None and snap.element_role in _TEXT_ROLES and not is_web_area:
        err = AXUIElementSetAttributeValue(snap.ax_element, "AXFocused", kCFBooleanTrue)
        if err == 0:
            _log(f"restore: refocused text field ({snap.element_role}) in {snap.app_name}")
            return True
        # -25202 = kAXErrorCannotComplete (stale ref after app switch) -- expected
        if err != -25202:
            _log(f"restore: WARNING: AX refocus failed (err={err})")
        else:
            _log(f"restore: AX element stale (-25202), relying on app's own focus restore")
    elif is_web_area:
        _log(f"restore: skipping AXWebArea refocus (not an input field), searching for text input")

    # Step 3: Fallback -- find text fields in the focused window.
    # Prefer AXTextArea/AXTextField over AXWebArea -- the latter is typically
    # a content view (e.g. chat history) that doesn't accept pasted input.
    ax_app = AXUIElementCreateApplication(snap.app_pid)
    text_fields = _find_window_text_fields(ax_app)
    _log(f"restore: found {len(text_fields)} text fields in window")
    input_fields = [(e, r) for e, r in text_fields if r != "AXWebArea"]
    if not input_fields:
        input_fields = text_fields

    if len(input_fields) == 1:
        elem, role = input_fields[0]
        err = AXUIElementSetAttributeValue(elem, "AXFocused", kCFBooleanTrue)
        if err == 0:
            _log(f"restore: focused text field ({role}) in {snap.app_name}")
            return True

    # App was activated -- even if we couldn't pinpoint the text field,
    # the app likely restored its own focus state. Return True so the
    # caller proceeds with pasting into the now-frontmost app.
    _log(f"restore: done (app activated, text field focus = best-effort)")
    return True


def _activate_app(pid: int, app_name: str) -> bool:
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
    try:
        import AppKit
        app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is None:
            _log(f"activate: no NSRunningApplication for pid={pid}, falling back")
        else:
            app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
            # Poll-verify: frontmost PID may lag or land on a sibling helper PID.
            # 5 × 100 ms = 500 ms total, re-activate between iterations.
            ws = AppKit.NSWorkspace.sharedWorkspace()
            for i in range(5):
                _time.sleep(0.1)
                front = ws.frontmostApplication()
                front_pid = front.processIdentifier() if front else 0
                if front_pid == pid:
                    if i > 0:
                        _log(f"activate: pid={pid} confirmed frontmost after {i+1} polls")
                    return True
                if i < 4:
                    app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
            _log(
                f"activate: WARNING target pid={pid} but frontmost pid={front_pid} "
                f"after 500 ms retry (likely different helper PID in same bundle)"
            )
            return False
    except Exception as e:
        _log(f"activate: NSRunningApplication path failed: {e}")
    # Fallback — osascript-based focus, cannot verify PID
    from heyvox.input.injection import focus_app
    focus_app(app_name)
    return False


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
