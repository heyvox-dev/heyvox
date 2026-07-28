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
snapshot function. Workspace detection/resolution is app-agnostic here: the
app profile names a WorkspaceProvider (heyvox.adapters registry) and this
module only calls detect_context()/resolve() on it.
"""

import concurrent.futures
import os
import re
import subprocess  # Module-level per Fact 5 — test patches via
                   # monkeypatch.setattr("heyvox.input.target.subprocess.run", ...)
                   # only intercept when subprocess is imported at module scope.
import time as _time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


# W10 (Fact 4): focus_app is NOT imported. NSRunningApplication bundle-ID
# activation in _yank_back_app_and_workspace handles activation; focus_app
# would add a redundant tell-application-activate osascript fork (~50ms).


_LOG_PATH_CACHE: str | None = None


def _resolve_log_path() -> str:
    """Resolve and cache the log file path for the life of the process.

    Resolved once (not per call) — a full load_config() per _log() call
    measurably regresses hot AX call sites. The path itself never changes
    after startup (main.py's own _LOG_FILE is likewise set once, in
    _init_log()), so caching the string is safe; correctness comes from
    reopening the file BY PATH on every _log() call, not from re-resolving
    the path.
    """
    global _LOG_PATH_CACHE
    if _LOG_PATH_CACHE is None:
        from heyvox.constants import LOG_FILE_DEFAULT
        path = os.environ.get("HEYVOX_LOG_FILE")
        if not path:
            try:
                from heyvox.config import load_config
                path = load_config().log_file or LOG_FILE_DEFAULT
            except Exception:
                path = LOG_FILE_DEFAULT
        _LOG_PATH_CACHE = path
    return _LOG_PATH_CACHE


def _log(msg: str) -> None:
    """Write to the main vox log file (same path/rotation as main.py's log()).

    Timestamp is needed for sub-step timing inside resolve_lock +
    _activate_app (DEF-061) — without it, multi-second hangs inside
    a single call are invisible because only the caller's entry/exit
    lines carry timestamps.

    DEF-166: previously printed to stderr, relying on launchd's fd-level
    redirect to the log file. That redirect only points at the file as of
    process start — the central log() rotates by renaming the file
    (os.replace) once it crosses the size cap, which repoints the *path*
    but not fds opened before the rename. Writes via sys.stderr silently
    ended up in the renamed-away (orphaned) inode after the first rotation.
    Reopen by path on every call instead (matching main.py/media.py) — a
    fresh open() always finds whatever currently sits at that path, so this
    self-heals across rotations.
    """
    ts = _time.strftime("%H:%M:%S")
    line = f"[{ts}] [target] {msg}\n"
    path = _resolve_log_path()
    try:
        with open(path, "a") as f:
            f.write(line)
    except OSError:
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
      - workspace_id / session_id: from the app profile's WorkspaceProvider
        (None for apps without workspace management)
    """

    app_bundle_id: str
    app_pid: int                           # advisory only — for logs
    window_number: int                     # AXWindowNumber or 0 if unavailable
    ax_role_path: tuple[RoleHop, ...]      # tuple (not list) so frozen actually freezes
    leaf_role: str = ""                    # AXRole of the focused leaf
    leaf_axid: Optional[str] = None
    leaf_title: Optional[str] = None
    leaf_description: Optional[str] = None
    workspace_id: Optional[str] = None
    session_id: Optional[str] = None
    focused_was_text_field: bool = False
    captured_at: float = 0.0               # monotonic timestamp
    # Advisory-only fields for log readability:
    app_name: str = ""                     # NSRunningApplication.localizedName


def _workspace_provider_for(profile):
    """Return the WorkspaceProvider declared by an app profile, or None.

    The provider name comes from the profile (`workspace_provider`, with the
    config-level legacy bridge mapping old has_workspace_detection configs);
    the implementation registry lives in heyvox.adapters. No app names here.
    """
    if profile is None:
        return None
    try:
        from heyvox.adapters import get_workspace_provider
        return get_workspace_provider(getattr(profile, "workspace_provider", ""))
    except Exception as e:
        _log(f"workspace provider lookup failed: {e}")
        return None


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

    When the profile declares a workspace_provider, we first detect the
    visible workspace context (provider.detect_context, e.g. Conductor's
    branch) and then resolve it to stable IDs (provider.resolve). Resolving
    with an empty context would return an arbitrary workspace (the SQL
    LIMIT-1 landmine), so an undetected context skips resolution entirely.

    Args:
        config: HeyvoxConfig instance for app profile lookup. If None,
            workspace/session enrichment is skipped.
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

    # Workspace enrichment via the profile's WorkspaceProvider (W-fix:
    # context-filtered — never resolve with an empty context).
    workspace_id: Optional[str] = None
    session_id: Optional[str] = None
    ws_context = ""
    if config is not None:
        try:
            profile = config.get_app_profile(app_name)
        except Exception:
            profile = None
        provider = _workspace_provider_for(profile)
        if provider is not None:
            ws_context = provider.detect_context(app_pid)
            if ws_context:
                # B2: per-call ThreadPoolExecutor with `with` block so the
                # worker thread is join()-ed before capture_lock returns.
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    future = ex.submit(provider.resolve, ws_context, profile)
                    try:
                        identity = future.result(timeout=0.1)
                    except concurrent.futures.TimeoutError:
                        _log(
                            "[TIMING] capture_lock: workspace resolve timed "
                            "out (>100ms), continuing without IDs"
                        )
                        future.cancel()
                        identity = None
                    except Exception as e:
                        _log(f"capture: workspace resolve raised {e!r}")
                        identity = None
                if identity is not None:
                    workspace_id = identity.workspace_id
                    session_id = identity.session_id
            else:
                _log(
                    "[capture_lock] workspace context detection failed; "
                    "skipping resolve"
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
        workspace_id=workspace_id,
        session_id=session_id,
        focused_was_text_field=focused_was_text_field,
        captured_at=_time.monotonic(),
        app_name=app_name,
    )
    _log(
        f"[capture_lock] bundle_id={app_bundle_id!r} pid={app_pid} "
        f"window={window_number} text_field={focused_was_text_field} "
        f"leaf_role={leaf_role!r} role_path_hops={len(ax_role_path)} "
        f"ws_ctx={ws_context!r} ws={workspace_id!r} "
        f"sess={session_id!r} "
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
    """Unconditional app + workspace + session yank-back (SPEC R6).

    Activates the bundle via NSRunningApplication. When the lock carries a
    workspace_id and the profile has a registered workspace_provider,
    delegates workspace+session activation to provider.activate() — which
    includes its own already-on-target short-circuit (formerly duplicated
    here ad-hoc, see DEF-095) and read-back verification, so every caller of
    activate() gets both for free.

    B3-resolved: Conductor's workspace-switch path does not check
    RECORDING_FLAG (verified Task 0); resolve_lock also runs post-stop so the
    flag is cleared by then. The DEF-070 orchestrator guard is for
    Herald-driven switches DURING recording and is NOT touched here.

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

    if lock.workspace_id and profile is not None:
        provider = _workspace_provider_for(profile)
        if provider is not None:
            from heyvox.adapters.base import WorkspaceIdentity
            identity = WorkspaceIdentity(
                workspace_id=lock.workspace_id, session_id=lock.session_id
            )
            try:
                ok = provider.activate(identity, profile, pid=lock.app_pid)
                _log(f"yank: provider.activate -> {ok} (ws={lock.workspace_id!r})")
            except Exception as e:
                _log(f"yank: provider.activate raised {e!r}")
        else:
            _log(
                "yank: workspace_id set but profile has no workspace_provider "
                "— skipping workspace switch"
            )


def _try_activate_and_recapture(lock) -> bool:
    """Multi-monitor fix: when the mouse-target app was not OS-frontmost at
    capture (e.g. Slack visible on monitor 2 while Chrome is system-frontmost
    on monitor 1), AXFocusedUIElement returns nothing because keyboard focus
    belongs to a different process. Activate the target via
    NSRunningApplication and re-query the focused element. Returns True if a
    text-role is now focused.

    Mutates `lock.focused_was_text_field` and `lock.leaf_role` on success so
    downstream tiers see the recovered state. Best-effort; any exception falls
    through to fail-closed.
    """
    try:
        import AppKit
        from ApplicationServices import (
            AXUIElementCreateApplication,
            AXUIElementCopyAttributeValue,
        )
    except ImportError:
        return False

    pid = getattr(lock, "app_pid", 0)
    if not pid:
        return False

    try:
        running = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(
            pid
        )
        if running is None:
            return False
        # NSApplicationActivateIgnoringOtherApps = 1 << 1
        running.activateWithOptions_(1 << 1)
        # Electron/Tauri apps need ~150ms for AX state to settle after activate
        _time.sleep(0.15)

        ax_app = AXUIElementCreateApplication(pid)
        err, focused = AXUIElementCopyAttributeValue(
            ax_app, "AXFocusedUIElement", None
        )
        if err != 0 or focused is None:
            _log(
                f"[activate-recapture] AXFocusedUIElement still empty after "
                f"activate (app={lock.app_name})"
            )
            return False
        err, role = AXUIElementCopyAttributeValue(focused, "AXRole", None)
        if err != 0 or not role:
            return False
        role_str = str(role)
        if role_str not in _TEXT_ROLES:
            _log(
                f"[activate-recapture] new focused role {role_str!r} is not "
                f"a text field (app={lock.app_name})"
            )
            return False
        _log(
            f"[activate-recapture] success: app={lock.app_name} "
            f"new_focused_role={role_str!r}"
        )
        # Mutate lock so caller's downstream tiers see the recovery.
        # TargetLock is a frozen dataclass — use object.__setattr__ to bypass.
        object.__setattr__(lock, "focused_was_text_field", True)
        object.__setattr__(lock, "leaf_role", role_str)
        return True
    except Exception as e:
        _log(f"[activate-recapture] exception: {e}")
        return False


def resolve_lock(lock, config=None) -> PasteOutcome:
    """Three-tier ladder: exact lock -> profile shortcut -> fail-closed (SPEC R4).

    Also performs unconditional yank-back of app + workspace + session
    (SPEC R6) before attempting tiers 1 and 2.

    Requirement: PASTE-15-R4, R6
    """
    _t0 = _time.time()

    profile = config.get_app_profile(lock.app_name) if config else None

    # Pre-tier: nothing focused at capture.
    # DEF-088: the original Phase 15 design fail-closed unconditionally here.
    # That regressed the common "user dictates while looking at the chat
    # window without first clicking the text field" flow — Conductor and
    # similar agents have a deterministic focus-the-input shortcut (Cmd+L),
    # so we can recover via Tier 2 instead of throwing the transcript away.
    # Only fail-close immediately when the app has no profile / no focus
    # shortcut available; otherwise fall through and let Tier 2 try the
    # shortcut. Tier 1 is skipped because the cached role-path is empty by
    # construction in this branch.
    if not lock.focused_was_text_field:
        # Multi-monitor recovery: if profile opts in via activate_on_mismatch
        # AND the app isn't currently frontmost, activate it and re-query AX.
        # If a text field is now focused, fall through to Tier 2 (app_fast_paste
        # will handle the actual paste).
        if profile and getattr(profile, "activate_on_mismatch", False):
            if _try_activate_and_recapture(lock):
                elapsed = int((_time.time() - _t0) * 1000)
                _log(
                    f"[PASTE] tier_used=2 (activate-recovered) "
                    f"reason=n/a elapsed_ms={elapsed}"
                )
                return PasteOutcome(
                    ok=True, element=None, tier_used=2, elapsed_ms=elapsed,
                )

        if not (profile and profile.focus_shortcut):
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
        # Profile has a focus_shortcut — log the recovery attempt and let
        # the function flow naturally into Tier 1 (a no-op here because
        # `lock.ax_role_path` is empty) and then Tier 2 (the shortcut).
        _log(
            f"[PASTE] no_text_field_at_start but profile has focus_shortcut "
            f"({profile.focus_shortcut!r}) — attempting Tier 2 recovery"
        )

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

    # Tier 2: profile shortcut.
    # DEF-089: do NOT fire the focus keystroke here. The caller
    # (recording.py:_send_local) routes any tier_used=2 outcome to
    # `app_fast_paste`, which fires `set frontmost + focus_shortcut +
    # Cmd+V + Enter*N` as a single consolidated osascript. Firing the
    # focus_shortcut twice (once here, once inside app_fast_paste's
    # script) was causing two distinct races:
    #   1. ~1.5–2.5 s extra latency per paste (this osascript +
    #      frontmost-lookup duplicated the work app_fast_paste already
    #      does).
    #   2. The second osascript's `set frontmost to true` re-stole
    #      focus from the chat input that the first Cmd+L had just
    #      focused, then Cmd+L+V+Enter re-fired against a re-arming
    #      window and Enter was occasionally absorbed before the
    #      send-handler bound — message landed in the input field
    #      but never sent.
    # Returning tier_used=2 ok=True without keystrokes is correct: the
    # contract this function exposes is "did we decide on a focus
    # strategy?", not "is the field already focused?". `app_fast_paste`
    # is the single owner of the focus+paste+Enter sequence.
    if profile and profile.focus_shortcut:
        elapsed = int((_time.time() - _t0) * 1000)
        _log(
            f"[PASTE] tier_used=2 (deferred to app_fast_paste) "
            f"reason=n/a elapsed_ms={elapsed}"
        )
        return PasteOutcome(
            ok=True, element=None, tier_used=2, elapsed_ms=elapsed,
        )

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

    Thin wrapper around heyvox.input.activation.activate_pid (extracted there
    so heyvox.adapters.conductor can reuse the same poll-verified logic for
    WorkspaceProvider.activate() without duplicating it — this behavior is
    proven necessary specifically for Electron/Tauri apps like Conductor, see
    DEF-054/061/067, not safe to reimplement independently). Kept as a
    same-signature module-level function (rather than a bare re-export) so
    existing `monkeypatch.setattr("heyvox.input.target._activate_app", ...)`
    call sites in tests keep patching this exact name.
    """
    from heyvox.input.activation import activate_pid
    return activate_pid(pid, app_name, log=_log)


# Legacy _find_window_text_fields and _walk_ax_tree removed in Plan 15-05:
# SPEC R4 rejects promiscuous tree-walk fallback. resolve_lock's three-tier
# ladder (role-path walk -> profile shortcut -> fail-closed) replaces them.
