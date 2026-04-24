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
import re
import subprocess  # Module-level per Fact 5 — test patches via
                   # monkeypatch.setattr("heyvox.input.target.subprocess.run", ...)
                   # only intercept when subprocess is imported at module scope.
import sys
import time as _time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from heyvox.adapters.conductor import get_active_workspace_and_session

# W10 (Fact 4): focus_app is NOT imported. NSRunningApplication bundle-ID
# activation in _yank_back_app_and_workspace handles activation; focus_app
# would add a redundant tell-application-activate osascript fork (~50ms).


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


class FailReason(str, Enum):
    """Taxonomy of fail-closed reasons (SPEC R5). Each reason maps to a
    user-readable toast string in _REASON_MESSAGES, all of which format
    uniformly with .format(app_name=...) (W13)."""

    NO_TEXT_FIELD_AT_START = "no_text_field_at_start"
    MULTI_FIELD_NO_SHORTCUT = "multi_field_no_shortcut"
    TARGET_UNREACHABLE = "target_unreachable"


# W13: every message carries {app_name} so .format(app_name=X) works
# uniformly across reasons. Future additions must preserve this invariant.
_REASON_MESSAGES = {
    FailReason.NO_TEXT_FIELD_AT_START: (
        "HeyVox ({app_name}): transcript on clipboard — no text field was "
        "focused when you started speaking."
    ),
    FailReason.MULTI_FIELD_NO_SHORTCUT: (
        "HeyVox ({app_name}): transcript on clipboard — this app has "
        "multiple inputs and no configured chat shortcut."
    ),
    FailReason.TARGET_UNREACHABLE: (
        "HeyVox: transcript on clipboard — original {app_name} target "
        "is unreachable."
    ),
}


@dataclass(frozen=True)
class PasteOutcome:
    """Result of resolve_lock(). Either Ok with the resolved AX element (or None
    for tier-2 / shortcut-only paths) or FailClosed with a categorised reason
    and user-readable message.
    """

    ok: bool
    element: Any = None                           # AXUIElement on tier-1 Ok
    tier_used: int = 0                            # 1, 2, or 0 (fail-closed)
    reason: Optional[FailReason] = None
    message: str = ""                             # toast/log text
    elapsed_ms: int = 0


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


def _walk_role_path(window_element, role_path):
    """Walk the cached role-path starting at the given window element.

    Returns the final AX element if the walk completes, an intermediate
    element if it lands on a text role early (D-03 tolerance for shallow
    tree shrinkage), or None if any hop mismatches on role or sibling-index.
    """
    try:
        from ApplicationServices import AXUIElementCopyAttributeValue
    except ImportError:
        return None

    if window_element is None or not role_path:
        return None

    current = window_element
    for hop_idx, (expected_role, sibling_idx) in enumerate(role_path):
        try:
            err, children = AXUIElementCopyAttributeValue(
                current, "AXChildren", None
            )
            if err != 0 or not children:
                return None
            if sibling_idx >= len(children):
                return None
            candidate = children[sibling_idx]
            err_r, role = AXUIElementCopyAttributeValue(
                candidate, "AXRole", None
            )
            role_str = str(role) if err_r == 0 and role else ""
            if role_str != expected_role:
                return None
            current = candidate
            if role_str in _TEXT_ROLES and hop_idx < len(role_path) - 1:
                return current
        except Exception:
            return None
    return current


def _find_window_by_number(ax_app, window_number: int):
    """Return the AXWindow whose AXWindowNumber matches, or AXFocusedWindow
    as fallback when `window_number` is 0 (was unavailable at capture).
    """
    try:
        from ApplicationServices import AXUIElementCopyAttributeValue
    except ImportError:
        return None

    if window_number == 0:
        try:
            err, win = AXUIElementCopyAttributeValue(
                ax_app, "AXFocusedWindow", None
            )
            return win if err == 0 else None
        except Exception:
            return None

    try:
        err, windows = AXUIElementCopyAttributeValue(ax_app, "AXWindows", None)
        if err != 0 or not windows:
            return None
        for w in windows:
            err_n, wn = AXUIElementCopyAttributeValue(
                w, "AXWindowNumber", None
            )
            if err_n == 0 and wn is not None:
                try:
                    if int(wn) == window_number:
                        return w
                except (TypeError, ValueError):
                    continue
        return None
    except Exception:
        return None


def _yank_back_app_and_workspace(lock, profile, config) -> None:
    """Unconditional app + Conductor workspace + session yank-back (SPEC R6).

    Activates the bundle via NSRunningApplication. When the lock carries a
    conductor_workspace_id and the profile declares a workspace_switch_cmd,
    invokes the extended `conductor-switch-workspace --id ... [--session ...]
    --force` script (B4).

    Session flag appended UNCONDITIONALLY when `conductor_session_id` is set —
    the script's sqlite UPDATE is idempotent so there is no need for a
    current-state comparison (which would require another adapter query,
    inviting the LIMIT-1 landmine from iteration-2).

    B3-resolved: conductor-switch-workspace does not check RECORDING_FLAG
    (verified Task 0); resolve_lock also runs post-stop so the flag is
    cleared by then. The DEF-070 orchestrator guard is for Herald-driven
    switches DURING recording and is NOT touched here.

    W10 (Fact 4): focus_app is intentionally NOT called. NSRunningApplication
    activation already handles app focus; focus_app would add a redundant
    `tell application ... activate` osascript fork (~50ms/paste).
    """
    try:
        import AppKit
    except ImportError:
        return

    bundle_activate_ok = False
    if lock.app_bundle_id:
        try:
            apps = (
                AppKit.NSRunningApplication
                .runningApplicationsWithBundleIdentifier_(lock.app_bundle_id)
            )
            if apps and len(apps) > 0:
                apps[0].activateWithOptions_(
                    AppKit.NSApplicationActivateIgnoringOtherApps
                )
                bundle_activate_ok = True
        except Exception as e:
            _log(
                f"yank: bundle-id activation failed for "
                f"{lock.app_bundle_id!r}: {e}"
            )

    if not bundle_activate_ok and lock.app_pid:
        _activate_app(lock.app_pid, lock.app_name or "")

    if lock.conductor_workspace_id and profile is not None:
        switch_cmd = getattr(profile, "workspace_switch_cmd", "")
        if switch_cmd:
            argv = [
                os.path.expanduser(switch_cmd),
                "--id", lock.conductor_workspace_id,
            ]
            if lock.conductor_session_id:
                # Session flag appended unconditionally — script UPDATE is idempotent.
                argv.extend(["--session", lock.conductor_session_id])
            argv.append("--force")
            try:
                subprocess.run(argv, capture_output=True, timeout=3)
                settle = getattr(profile, "settle_delay", 0.3)
                _time.sleep(settle)
            except Exception as e:
                _log(f"yank: conductor-switch-workspace failed: {e}")
        else:
            _log(
                f"yank: conductor_workspace_id set but profile lacks "
                f"workspace_switch_cmd — skipping workspace switch"
            )


def resolve_lock(lock, config=None) -> PasteOutcome:
    """Three-tier ladder: exact lock -> profile shortcut -> fail-closed (SPEC R4).

    Also performs unconditional yank-back of app + workspace + session
    (SPEC R6) before attempting tiers 1 and 2.

    Requirement: PASTE-15-R4, R6
    """
    _t0 = _time.time()

    # Pre-tier: nothing focused at capture -> fail closed (no yank needed)
    if not lock.focused_was_text_field:
        msg = _REASON_MESSAGES[FailReason.NO_TEXT_FIELD_AT_START].format(
            app_name=lock.app_name or "app"
        )
        elapsed = int((_time.time() - _t0) * 1000)
        _log(
            f"[PASTE] tier_used=fail_closed "
            f"reason={FailReason.NO_TEXT_FIELD_AT_START.value} "
            f"elapsed_ms={elapsed}"
        )
        return PasteOutcome(
            ok=False, tier_used=0,
            reason=FailReason.NO_TEXT_FIELD_AT_START,
            message=msg, elapsed_ms=elapsed,
        )

    profile = config.get_app_profile(lock.app_name) if config else None

    # Yank back: app + workspace + session — UNCONDITIONAL (SPEC R6)
    _yank_back_app_and_workspace(lock, profile, config)

    # Tier 1: walk the cached role-path
    try:
        from ApplicationServices import (
            AXUIElementCreateApplication,
            AXUIElementSetAttributeValue,
        )
        from CoreFoundation import kCFBooleanTrue
    except ImportError:
        elapsed = int((_time.time() - _t0) * 1000)
        return PasteOutcome(
            ok=False, tier_used=0,
            reason=FailReason.TARGET_UNREACHABLE,
            message=_REASON_MESSAGES[FailReason.TARGET_UNREACHABLE].format(
                app_name=lock.app_name or "app"
            ),
            elapsed_ms=elapsed,
        )

    ax_app = AXUIElementCreateApplication(lock.app_pid)
    window = _find_window_by_number(ax_app, lock.window_number)
    if window is not None and lock.ax_role_path:
        leaf = _walk_role_path(window, lock.ax_role_path)
        if leaf is not None:
            try:
                AXUIElementSetAttributeValue(leaf, "AXFocused", kCFBooleanTrue)
            except Exception:
                pass
            elapsed = int((_time.time() - _t0) * 1000)
            _log(f"[PASTE] tier_used=1 reason=n/a elapsed_ms={elapsed}")
            return PasteOutcome(
                ok=True, element=leaf, tier_used=1, elapsed_ms=elapsed,
            )

    # Tier 2: profile shortcut
    if profile and profile.focus_shortcut:
        try:
            from heyvox.input.injection import _get_frontmost_app

            _time.sleep(profile.settle_delay)
            safe_proc = (_get_frontmost_app() or lock.app_name or "").replace(
                '"', '\\"'
            )
            script = (
                f'tell application "System Events"\n'
                f'    tell process "{safe_proc}"\n'
                f'        keystroke "{profile.focus_shortcut}" using command down\n'
                f'    end tell\n'
                f'end tell'
            )
            r = subprocess.run(
                ["osascript", "-e", script], capture_output=True, timeout=3
            )
            if r.returncode == 0:
                elapsed = int((_time.time() - _t0) * 1000)
                _log(f"[PASTE] tier_used=2 reason=n/a elapsed_ms={elapsed}")
                return PasteOutcome(
                    ok=True, element=None, tier_used=2, elapsed_ms=elapsed,
                )
        except Exception as e:
            _log(f"resolver: tier 2 exception: {e}")

    # Tier 3: fail-closed
    if profile and not profile.focus_shortcut:
        reason = FailReason.MULTI_FIELD_NO_SHORTCUT
    else:
        reason = FailReason.TARGET_UNREACHABLE
    msg = _REASON_MESSAGES[reason].format(app_name=lock.app_name or "app")
    elapsed = int((_time.time() - _t0) * 1000)
    _log(
        f"[PASTE] tier_used=fail_closed reason={reason.value} "
        f"elapsed_ms={elapsed}"
    )
    return PasteOutcome(
        ok=False, tier_used=0, reason=reason, message=msg, elapsed_ms=elapsed,
    )


# ---------------------------------------------------------------------------
# Plan 15-06 — verify_paste (SPEC R7)
# ---------------------------------------------------------------------------


_WS_RUN = re.compile(r"\s+")


def _normalize_text(s: str) -> str:
    """Strip + collapse whitespace runs (incl newlines) to single space.
    Case-preserved per D-05."""
    if s is None:
        return ""
    return _WS_RUN.sub(" ", s).strip()


def _read_ax_value(element) -> Optional[str]:
    """Read AXValue of an AXUIElement; return string or None on any error."""
    try:
        from ApplicationServices import AXUIElementCopyAttributeValue

        err, value = AXUIElementCopyAttributeValue(element, "AXValue", None)
        if err != 0 or value is None:
            return None
        return str(value)
    except Exception:
        return None


def _focus_unchanged(lock) -> bool:
    """Best-effort focus-unchanged check for non-AX-capable apps (D-07).

    Returns True iff frontmost app's bundle_id matches lock.app_bundle_id.
    PID match is advisory (Electron rotates helpers); bundle_id is the
    real signal.
    """
    try:
        import AppKit

        front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if front is None:
            return False
        front_bundle = front.bundleIdentifier() or ""
        return front_bundle == lock.app_bundle_id
    except Exception:
        # AppKit unavailable (test env etc.): fail-open for non-AX path.
        return True


def _acquire_focused_element(lock):
    """Acquire the LIVE focused AX element of the locked app (W3).

    Used by verify_paste when the resolver returned tier_used=2 (element=None
    because the profile shortcut focused an input we don't have a handle to).
    Without this, Tier-2 pastes would fall back to focus-unchanged best-effort
    even on AX-capable apps — losing strong AX content verification.

    Returns the AXUIElement of the currently focused element in the locked
    application's process, or None if AX is unavailable / focus has moved
    to a different app.

    Requirement: PASTE-15-R7 (Tier 2 verification parity)
    """
    try:
        import AppKit
        from ApplicationServices import (
            AXUIElementCopyAttributeValue,
            AXUIElementCreateApplication,
        )

        front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if front is None:
            return None
        front_bundle = front.bundleIdentifier() or ""
        if front_bundle != lock.app_bundle_id:
            _log(
                f"_acquire_focused_element: frontmost is {front_bundle!r}, "
                f"expected {lock.app_bundle_id!r}; focus moved"
            )
            return None
        live_pid = front.processIdentifier()
        ax_app = AXUIElementCreateApplication(live_pid)
        err, focused = AXUIElementCopyAttributeValue(
            ax_app, "AXFocusedUIElement", None
        )
        if err != 0 or focused is None:
            return None
        return focused
    except Exception as e:
        _log(f"_acquire_focused_element: exception: {e}")
        return None


@dataclass(frozen=True)
class VerifyResult:
    """Result of verify_paste(). Four meaningful combinations:

    verified=True  retried=False drift=False  - first-try content match
    verified=True  retried=True  drift=False  - second-try content match
    verified=False retried=False drift=True   - non-AX focus moved
    verified=False retried=True  drift=True   - persistent content drift
    """

    verified: bool
    retried: bool
    drift: bool
    detail: str = ""


def verify_paste(lock, element, transcript: str, profile) -> VerifyResult:
    """Post-paste verification per SPEC R7.

    AX-capable apps (profile.supports_ax_verify=True OR profile is None):
      read AXValue, normalized-substring-match against transcript. On fail,
      re-set clipboard (W11) + retry Cmd+V + re-read AXValue.

    Non-AX-capable apps (profile.supports_ax_verify=False):
      focus-unchanged best-effort check only (no content readback).

    When element=None AND we would take the AX path (Tier 2), re-acquire
    the focused element via AXFocusedUIElement for strong verification (W3).

    Requirement: PASTE-15-R7
    """
    settle = profile.ax_settle_before_verify if profile else 0.1

    # Non-AX path: focus-unchanged check
    if profile is not None and not profile.supports_ax_verify:
        _time.sleep(settle)
        if _focus_unchanged(lock):
            _log(
                "[PASTE] verified=true retried=false drift=false "
                f"(non-AX focus-unchanged, profile={profile.name}, "
                "supports_ax_verify=False)"
            )
            return VerifyResult(
                verified=True, retried=False, drift=False,
                detail="focus-unchanged",
            )
        _log(
            "[PASTE] verified=false retried=false drift=true "
            f"(non-AX focus moved, profile={profile.name}, "
            "supports_ax_verify=False)"
        )
        return VerifyResult(
            verified=False, retried=False, drift=True, detail="focus-moved",
        )

    # W7 — explicit log when profile is None so debugging is unambiguous.
    if profile is None:
        _log(
            f"[PASTE] verify: profile=None (treating as AX-capable, "
            f"app={lock.app_name!r})"
        )

    # AX path — need an element handle. Tier 2 returns element=None;
    # W3 fix: re-acquire focused element via AXFocusedUIElement.
    if element is None:
        element = _acquire_focused_element(lock)
        if element is not None:
            _log(
                "[PASTE] verify: re-acquired focused element for "
                f"Tier-2 AX verify (app={lock.app_name!r})"
            )
        else:
            _time.sleep(settle)
            if _focus_unchanged(lock):
                _log(
                    "[PASTE] verified=true retried=false drift=false "
                    "(Tier-2 acquire-fail, focus-unchanged fallback)"
                )
                return VerifyResult(
                    verified=True, retried=False, drift=False,
                    detail="tier2-acquire-fail-focus-unchanged",
                )
            _log(
                "[PASTE] verified=false retried=false drift=true "
                "(Tier-2 acquire-fail, focus moved)"
            )
            return VerifyResult(
                verified=False, retried=False, drift=True,
                detail="tier2-acquire-fail-focus-moved",
            )

    norm_transcript = _normalize_text(transcript)

    # First attempt
    _time.sleep(settle)
    val = _read_ax_value(element)
    if val is not None and norm_transcript in _normalize_text(val):
        _log(
            f"[PASTE] verified=true retried=false drift=false "
            f"(ax_value_len={len(val)})"
        )
        return VerifyResult(
            verified=True, retried=False, drift=False,
            detail=f"ax_value_len={len(val)}",
        )

    # Retry: W11 — re-set clipboard from transcript BEFORE Cmd+V so the
    # retry pastes the transcript, not whatever the target app may have
    # mutated the pasteboard to.
    _log(
        f"[PASTE] first-verify miss (ax_value_len={len(val) if val else 0}), "
        f"re-setting clipboard + retrying paste"
    )
    try:
        from heyvox.input.injection import _get_frontmost_app, _set_clipboard

        clip_ok, _ignored = _set_clipboard(transcript)
        if not clip_ok:
            _log(
                "[PASTE] retry: WARNING clipboard re-set failed — "
                "proceeding with stale clipboard"
            )
        proc = (_get_frontmost_app() or lock.app_name or "").replace(
            '"', '\\"'
        )
        script = (
            f'tell application "System Events"\n'
            f'    tell process "{proc}"\n'
            f'        keystroke "v" using command down\n'
            f'    end tell\n'
            f'end tell'
        )
        subprocess.run(
            ["osascript", "-e", script], capture_output=True, timeout=3
        )
    except Exception as e:
        _log(f"verify_paste: retry osascript exception: {e}")

    _time.sleep(settle)
    val2 = _read_ax_value(element)
    if val2 is not None and norm_transcript in _normalize_text(val2):
        _log(
            f"[PASTE] verified=true retried=true drift=false "
            f"(ax_value_len={len(val2)})"
        )
        return VerifyResult(
            verified=True, retried=True, drift=False,
            detail=f"retry-ax_value_len={len(val2)}",
        )

    detail = (
        f"drift first_len={len(val) if val else 0} "
        f"second_len={len(val2) if val2 else 0}"
    )
    _log(f"[PASTE] verified=false retried=true drift=true ({detail})")
    return VerifyResult(
        verified=False, retried=True, drift=True, detail=detail,
    )


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
    # No osascript fallback here (W10, Fact 4): an extra
    # `tell application ... activate` fork costs ~50ms/paste and is
    # redundant with the NSRunningApplication bundle-ID path already
    # taken by _yank_back_app_and_workspace. Callers treat False as
    # "activation best-effort failed" and continue.
    return False


# Legacy _find_window_text_fields and _walk_ax_tree removed in Plan 15-05:
# SPEC R4 rejects promiscuous tree-walk fallback. resolve_lock's three-tier
# ladder (role-path walk -> profile shortcut -> fail-closed) replaces them.
