"""
Target lock and restore for text injection.

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

Phase 15 migration: the old mutable snapshot dataclass is replaced by
TargetLock (frozen dataclass, SPEC R1). capture_lock() supersedes the old
snapshot function. The old AX-walk + sqlite-map workspace helpers are
deleted; their replacement is the Conductor adapter from Plan 15-01 plus
_detect_conductor_branch (salvaged AX walk) here.
"""

import concurrent.futures
import os
import sys
import time as _time
from dataclasses import dataclass, field
from typing import Optional

from heyvox.adapters.conductor import get_active_workspace_and_session


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

# Type alias for role-path hops
RoleHop = tuple[str, int]  # (role, child-index-among-siblings)

# D-03: max hops captured from window down to leaf
MAX_ROLE_PATH_HOPS = 12


@dataclass(frozen=True)
class TargetLock:
    """Immutable record-start target. SPEC R1, R2.

    Stable identity fields (survive PID churn / app rename / workspace renumber):
      - app_bundle_id: NSRunningApplication.bundleIdentifier()
      - window_number: AXWindowNumber (CGWindowID-like)
      - ax_role_path: tuple of (role, index-in-parent) hops from window to leaf
      - leaf_axid / leaf_title / leaf_description: AX tie-breakers for re-find
      - conductor_workspace_id / conductor_session_id: from Plan 15-01 adapter
    """

    app_bundle_id: str
    app_pid: int                           # advisory only — for logs
    window_number: int                     # AXWindowNumber or 0 if unavailable
    ax_role_path: tuple[RoleHop, ...]      # tuple (not list) so frozen actually freezes
    leaf_role: str = ""                    # AXRole of the focused leaf
    leaf_axid: Optional[str] = None
    leaf_title: Optional[str] = None
    leaf_description: Optional[str] = None
    conductor_workspace_id: Optional[str] = None
    conductor_session_id: Optional[str] = None
    focused_was_text_field: bool = False
    captured_at: float = 0.0               # monotonic timestamp
    # Advisory-only fields for log readability:
    app_name: str = ""                     # NSRunningApplication.localizedName


def _detect_conductor_branch(pid: int) -> str:
    """Walk Conductor's AX tree to find the branch name shown in the right panel.

    Conductor's UI shows the branch name in the right pane after the AXSplitter;
    walking AXFocusedWindow -> AXChildren -> past the splitter -> into the title
    bar of the right pane gives us the branch string. This is what's currently
    visible to the user (NOT what state the DB is in — the user may have
    workspaces in 'ready' state that aren't on screen).

    Returns the branch string or "" on any failure (caller treats "" as
    "branch unknown — skip adapter call, leave conductor_workspace_id None").

    Salvaged from the old workspace-detect helper's AX-walk portion. The
    DB-mapping half of the old function is gone — the adapter from Plan 15-01
    takes over once we know the branch.
    """
    try:
        from ApplicationServices import (
            AXUIElementCreateApplication,
            AXUIElementCopyAttributeValue,
        )
        ax = AXUIElementCreateApplication(pid)
        # Try AXFocusedWindow first, then AXMainWindow, then first of AXWindows.
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
            return ""

        # Flatten the tree looking for the first AXStaticText after the first AXSplitter
        items: list[tuple[str, str]] = []  # (role, value)

        def _collect(elem, depth: int = 0) -> None:
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

        past_splitter = False
        for r, v in items:
            if r == "AXSplitter":
                if past_splitter:
                    break
                past_splitter = True
                continue
            if past_splitter and r == "AXStaticText" and v:
                return v
        return ""
    except Exception as e:
        _log(f"_detect_conductor_branch: exception: {e}")
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


def _capture_role_path(
    focused_element, window_element
) -> tuple[RoleHop, ...]:
    """Walk DOWN from window to focused_element recording (role, sibling-index)
    at each hop. Depth-first search, capped at MAX_ROLE_PATH_HOPS.

    Returns the path as a tuple so the surrounding TargetLock stays immutable.
    Returns () on any AX error or if the focused element isn't reachable under
    the window within the hop budget.
    """
    try:
        from ApplicationServices import AXUIElementCopyAttributeValue
    except ImportError:
        return ()

    if window_element is None or focused_element is None:
        return ()

    def _role(elem) -> str:
        try:
            err, r = AXUIElementCopyAttributeValue(elem, "AXRole", None)
            return str(r) if err == 0 and r else ""
        except Exception:
            return ""

    def _children(elem) -> list:
        try:
            err, c = AXUIElementCopyAttributeValue(elem, "AXChildren", None)
            if err == 0 and c:
                return list(c)
        except Exception:
            pass
        return []

    # DFS from window looking for focused_element; record (role, index) per step.
    path: list[RoleHop] = []

    def _search(elem, depth: int) -> bool:
        if depth > MAX_ROLE_PATH_HOPS:
            return False
        if elem is focused_element:
            return True
        for idx, child in enumerate(_children(elem)):
            role = _role(child)
            path.append((role, idx))
            if _search(child, depth + 1):
                return True
            path.pop()
        return False

    _search(window_element, 0)
    # Truncate just in case the search bailed above the cap
    return tuple(path[:MAX_ROLE_PATH_HOPS])


def _capture_leaf_tiebreakers(
    element,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (axid, title, description) from the leaf element.

    Each attribute is read independently — a failure on AXIdentifier does not
    disable the AXTitle read. Each field is None on per-attribute error (D-02).
    """
    try:
        from ApplicationServices import AXUIElementCopyAttributeValue
    except ImportError:
        return (None, None, None)

    def _read(attr: str) -> Optional[str]:
        try:
            err, v = AXUIElementCopyAttributeValue(element, attr, None)
            if err == 0 and v:
                return str(v)
        except Exception:
            return None
        return None

    return (
        _read("AXIdentifier"),
        _read("AXTitle"),
        _read("AXDescription"),
    )


def capture_lock(config=None) -> Optional[TargetLock]:
    """Capture the app and text field the user is interacting with.

    Returns a frozen TargetLock or None when AppKit/Accessibility APIs are
    unavailable. Runs in well under 100ms per SPEC R3 — adapter call is gated
    by a per-call ThreadPoolExecutor with 100ms timeout (B2).

    On multi-monitor setups, prefers the app under the mouse cursor over
    NSWorkspace.frontmostApplication(), since the latter can return an app
    on a different screen than where the user is actually working.

    When the profile has_workspace_detection=True, we first detect the
    Conductor branch via _detect_conductor_branch and then call the adapter
    with branch=detected_branch. Calling the adapter with branch=None would
    return a random ready workspace (SQL LIMIT-1 landmine).

    Args:
        config: HeyvoxConfig instance for app profile lookup. If None,
            conductor workspace/session enrichment is skipped.
    """
    _t_start = _time.time()
    try:
        import AppKit
        from ApplicationServices import (
            AXUIElementCreateApplication,
            AXUIElementCopyAttributeValue,
        )
    except ImportError:
        _log("WARNING: AppKit/ApplicationServices unavailable — capture disabled")
        return None

    # Primary: find the app under the mouse cursor (correct on multi-monitor)
    mouse_app = _app_under_mouse()
    ws = AppKit.NSWorkspace.sharedWorkspace()
    front_app = ws.frontmostApplication()

    if mouse_app:
        app_name, app_pid = mouse_app
        if front_app and front_app.processIdentifier() != app_pid:
            front_name = front_app.localizedName() or "?"
            _log(
                f"capture: mouse is over {app_name} (pid={app_pid}), "
                f"frontmost={front_name} (pid={front_app.processIdentifier()}) "
                f"— using mouse target"
            )
    elif front_app:
        app_name = front_app.localizedName() or ""
        app_pid = front_app.processIdentifier()
    else:
        return None

    # Resolve bundle id for the captured pid (SPEC R2 stable identity field).
    app_bundle_id = ""
    try:
        running = (
            AppKit.NSRunningApplication
            .runningApplicationWithProcessIdentifier_(app_pid)
        )
        if running is not None:
            bid = running.bundleIdentifier()
            if bid:
                app_bundle_id = str(bid)
    except Exception as e:
        _log(f"capture: bundleIdentifier lookup failed for pid={app_pid}: {e}")

    # Focused UI element + role via the application AX element.
    ax_app = AXUIElementCreateApplication(app_pid)
    focused = None
    leaf_role = ""
    try:
        err, focused = AXUIElementCopyAttributeValue(
            ax_app, "AXFocusedUIElement", None
        )
        if err == 0 and focused is not None:
            err2, role = AXUIElementCopyAttributeValue(focused, "AXRole", None)
            leaf_role = str(role) if err2 == 0 and role else ""
    except Exception as e:
        _log(f"capture: AXFocusedUIElement failed: {e}")
        focused = None

    focused_was_text_field = leaf_role in _TEXT_ROLES

    # Focused window + window number.
    window = None
    window_number = 0
    try:
        err, window = AXUIElementCopyAttributeValue(
            ax_app, "AXFocusedWindow", None
        )
        if err == 0 and window is not None:
            err2, wn = AXUIElementCopyAttributeValue(
                window, "AXWindowNumber", None
            )
            if err2 == 0 and wn is not None:
                try:
                    window_number = int(wn)
                except (TypeError, ValueError):
                    window_number = 0
    except Exception as e:
        _log(f"capture: AXFocusedWindow failed: {e}")
        window = None

    # Role-path from window to focused leaf.
    ax_role_path = _capture_role_path(focused, window)

    # Leaf tie-breakers.
    if focused is not None:
        leaf_axid, leaf_title, leaf_description = _capture_leaf_tiebreakers(focused)
    else:
        leaf_axid, leaf_title, leaf_description = (None, None, None)

    # Conductor adapter integration WITH BRANCH FILTER (W-fix).
    conductor_workspace_id: Optional[str] = None
    conductor_session_id: Optional[str] = None
    detected_branch = ""
    if config is not None:
        try:
            profile = config.get_app_profile(app_name)
        except Exception:
            profile = None
        if (
            profile is not None
            and getattr(profile, "has_workspace_detection", False)
            and getattr(profile, "workspace_db", "")
        ):
            detected_branch = _detect_conductor_branch(app_pid)
            if detected_branch:
                # B2: per-call ThreadPoolExecutor with `with` block so the
                # worker thread is join()-ed before capture_lock returns.
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    future = ex.submit(
                        get_active_workspace_and_session,
                        directory_name=None,
                        branch=detected_branch,
                        db_path=os.path.expanduser(profile.workspace_db),
                    )
                    try:
                        identity = future.result(timeout=0.1)
                    except concurrent.futures.TimeoutError:
                        _log(
                            "[TIMING] capture_lock: conductor adapter timed "
                            "out (>100ms), continuing without IDs"
                        )
                        future.cancel()
                        identity = None
                    except Exception as e:
                        _log(f"capture: adapter call raised {e!r}")
                        identity = None
                if identity is not None:
                    conductor_workspace_id = identity.workspace_id
                    conductor_session_id = identity.session_id
            else:
                _log(
                    "[capture_lock] branch detection failed; "
                    "skipping adapter"
                )

    lock = TargetLock(
        app_bundle_id=app_bundle_id,
        app_pid=app_pid,
        window_number=window_number,
        ax_role_path=ax_role_path,
        leaf_role=leaf_role,
        leaf_axid=leaf_axid,
        leaf_title=leaf_title,
        leaf_description=leaf_description,
        conductor_workspace_id=conductor_workspace_id,
        conductor_session_id=conductor_session_id,
        focused_was_text_field=focused_was_text_field,
        captured_at=_time.monotonic(),
        app_name=app_name,
    )
    _log(
        f"[capture_lock] bundle_id={app_bundle_id!r} pid={app_pid} "
        f"window={window_number} text_field={focused_was_text_field} "
        f"leaf_role={leaf_role!r} role_path_hops={len(ax_role_path)} "
        f"branch={detected_branch!r} conductor_ws={conductor_workspace_id!r} "
        f"conductor_sess={conductor_session_id!r} "
        f"elapsed_ms={int((_time.time() - _t_start)*1000)}"
    )
    return lock


def restore_target(snap, config=None) -> bool:
    """Refocus the app and text field from a TargetLock (or legacy snapshot).

    TODO(15-05): replaced by resolve_lock() in Plan 15-05.

    Returns True if the app was activated (text field focus is best-effort),
    False if activation failed entirely. Reads fields via getattr with sane
    defaults so it tolerates both the new frozen TargetLock and the transitional
    old consumer-site state.
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

    snap_app_name = getattr(snap, "app_name", "")
    snap_app_pid = getattr(snap, "app_pid", 0)
    snap_conductor_ws = getattr(snap, "conductor_workspace_id", None)
    # Legacy snapshot had ax_element/element_role; TargetLock does not.
    snap_ax_element = getattr(snap, "ax_element", None)
    snap_element_role = getattr(snap, "element_role", "")

    # Workspace detection moved to heyvox/adapters/conductor.py (Plan 15-01);
    # workspace switch deferred to resolve_lock (Plan 15-05).

    # Fast path: check if we're already on the right app
    _already_frontmost = False
    try:
        import AppKit
        ws = AppKit.NSWorkspace.sharedWorkspace()
        actual = ws.frontmostApplication()
        actual_name = actual.localizedName() if actual else ""
        actual_pid = actual.processIdentifier() if actual else -1
        _already_frontmost = (
            actual_pid == snap_app_pid
            or (actual_name and actual_name.lower() == (snap_app_name or "").lower())
        )
    except Exception:
        pass

    # Step 1: Activate the app (skip if already frontmost).
    # Workspace-managed apps (detected via conductor_workspace_id) use the
    # app-level osascript activate path because PID-level activation can burn
    # ~7s rotating between sibling helper PIDs (DEF-067).
    if _already_frontmost:
        _log(f"restore: {snap_app_name} already frontmost, skipping activate")
        activate_ok = True
    elif snap_conductor_ws:
        _log(
            f"restore: workspace-managed app ({snap_app_name}) — "
            f"using app-level activate + Cmd+L fallback (skipping PID-level)"
        )
        _t0 = _time.time()
        from heyvox.input.injection import focus_app
        focus_app(snap_app_name)
        _time.sleep(0.15)
        _log(
            f"restore: [TIMING] app-level activate: "
            f"{int((_time.time()-_t0)*1000)}ms"
        )
        try:
            snap._activate_failed = True  # Signal paste to include Cmd+L
        except Exception:
            pass  # frozen TargetLock can't accept attribute writes
        return True
    else:
        _log(f"restore: activating {snap_app_name} (pid={snap_app_pid})")
        activate_ok = _activate_app(snap_app_pid, snap_app_name)
        _time.sleep(0.3)

    if not activate_ok:
        _log(
            f"restore: skipping AX refocus (activate failed — stale element "
            f"would hang); relying on paste-path focus shortcut"
        )
        try:
            snap._activate_failed = True
        except Exception:
            pass
        return True

    # Step 2: Try to refocus the captured element directly (legacy path —
    # only triggers for transitional legacy-snapshot instances). TargetLock
    # doesn't carry an AX element ref; Plan 15-05 re-finds the leaf via role-path.
    is_web_area = snap_element_role == "AXWebArea"
    if (
        snap_ax_element is not None
        and snap_element_role in _TEXT_ROLES
        and not is_web_area
    ):
        err = AXUIElementSetAttributeValue(
            snap_ax_element, "AXFocused", kCFBooleanTrue
        )
        if err == 0:
            _log(
                f"restore: refocused text field ({snap_element_role}) "
                f"in {snap_app_name}"
            )
            return True
        if err != -25202:
            _log(f"restore: WARNING: AX refocus failed (err={err})")
        else:
            _log(
                "restore: AX element stale (-25202), relying on "
                "app's own focus restore"
            )
    elif is_web_area:
        _log(
            "restore: skipping AXWebArea refocus (not an input field), "
            "searching for text input"
        )

    # Step 3: Fallback — find text fields in the focused window.
    ax_app = AXUIElementCreateApplication(snap_app_pid)
    text_fields = _find_window_text_fields(ax_app)
    _log(f"restore: found {len(text_fields)} text fields in window")
    input_fields = [(e, r) for e, r in text_fields if r != "AXWebArea"]
    if not input_fields:
        input_fields = text_fields

    if len(input_fields) == 1:
        elem, role = input_fields[0]
        err = AXUIElementSetAttributeValue(elem, "AXFocused", kCFBooleanTrue)
        if err == 0:
            _log(f"restore: focused text field ({role}) in {snap_app_name}")
            return True

    _log("restore: done (app activated, text field focus = best-effort)")
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

    results: list[tuple] = []
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
