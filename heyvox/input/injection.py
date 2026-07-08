"""
Text injection into the focused macOS application.

Primary method: clipboard + Cmd-V via osascript (works with any app).

The Hush Chrome extension socket is tried first for Chrome tabs, but this
is a minor optimization — the clipboard path is the reliable default.
"""

import json
import os
import socket
import subprocess
import threading
import time

from heyvox.audio.cues import audio_cue


# Max seconds to wait for osascript subprocess to complete
SUBPROCESS_TIMEOUT = 5

# Max seconds to wait for a PyObjC call into a system framework (NSPasteboard,
# NSWorkspace). These have no built-in timeout of their own — DEF-165 found a
# wedged pasteboard server freezing the main loop for ~3 minutes because
# nothing bounded the wait.
_APPKIT_CALL_TIMEOUT = 2.0


_LOG_PATH_CACHE: str | None = None


def _resolve_log_path() -> str:
    """Resolve and cache the log file path for the life of the process.

    Resolved once (not per call) — a full load_config() per _log() call
    regressed the AX fast-path from <5ms to >10ms (see
    test_ax_inject_text_phase12_fastpath_remains_under_5ms). The path itself
    never changes after startup (main.py's own _LOG_FILE is likewise set
    once, in _init_log()), so caching the string is safe; correctness comes
    from reopening the file BY PATH on every _log() call, not from
    re-resolving the path.
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

    DEF-166: raw stderr prints relied on launchd's fd-level redirect, which
    only points at the log file as of process start. The central log()
    rotates by renaming the file (os.replace) once it crosses the size cap
    — that repoints the *path* but not fds opened before the rename, so
    stderr writes silently ended up in the renamed-away (orphaned) inode
    after the first rotation, invisible from the live log for the rest of
    the process's life. Reopen by path on every call instead (matching
    main.py/media.py) — a fresh open() always finds whatever currently sits
    at that path, so this self-heals across rotations.
    """
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] [injection] {msg}\n"
    try:
        with open(_resolve_log_path(), "a") as f:
            f.write(line)
    except OSError:
        pass


def _call_with_timeout(fn, timeout: float = _APPKIT_CALL_TIMEOUT):
    """Run a zero-arg callable on a throwaway daemon thread, bounded by timeout.

    PyObjC calls into system frameworks (NSPasteboard, NSWorkspace) block the
    calling thread with no way to interrupt them. Each call gets a fresh
    thread (not a shared pool) so one call that never returns only leaks that
    one thread instead of permanently blocking every future call.

    Raises TimeoutError if `fn` hasn't returned within `timeout` seconds.
    Re-raises whatever exception `fn` itself raised.
    """
    box: dict = {}

    def _run():
        try:
            box["result"] = fn()
        except Exception as e:
            box["error"] = e

    t = threading.Thread(target=_run, daemon=True, name="heyvox-appkit-call")
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"AppKit call did not return within {timeout}s")
    if "error" in box:
        raise box["error"]
    return box["result"]


def _get_frontmost_app() -> str:
    """Return the name of the frontmost app (for diagnostic logging)."""
    try:
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of first application process whose frontmost is true'],
            capture_output=True, text=True, timeout=2,
        )
        return r.stdout.strip() if r.returncode == 0 else "?"
    except Exception:
        return "?"

# Hush native messaging host socket
from heyvox.constants import HUSH_SOCK as HUSH_SOCKET
HUSH_TIMEOUT = 2.0  # seconds


# ---------------------------------------------------------------------------
# Chrome injection via Hush socket (best-effort, not critical path)
# ---------------------------------------------------------------------------

def _hush_send(command: dict) -> dict | None:
    """Send a command to the Hush native host and return the response.

    Returns None if the socket is unavailable or the command fails.
    """
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(HUSH_TIMEOUT)
        sock.connect(HUSH_SOCKET)
        payload = json.dumps(command, separators=(",", ":")) + "\n"
        sock.sendall(payload.encode("utf-8"))
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        sock.close()
        if data.strip():
            return json.loads(data.strip())
        return None
    except (OSError, json.JSONDecodeError, TimeoutError) as e:
        _log(f"Hush socket error: {e}")
        return None


def _chrome_type_text(text: str) -> bool:
    """Insert text via the Hush Chrome extension. Returns True on success."""
    resp = _hush_send({"action": "type-text", "text": text})
    if resp and resp.get("ok"):
        _log(f"Chrome type-text OK (tab: {resp.get('title', '?')})")
        return True
    if resp and resp.get("error"):
        _log(f"Chrome type-text failed: {resp['error']}")
    return False


def _chrome_press_enter(count: int) -> bool:
    """Press Enter via the Hush Chrome extension. Returns True on success."""
    resp = _hush_send({"action": "press-enter", "count": count})
    if resp and resp.get("ok"):
        return True
    return False


# ---------------------------------------------------------------------------
# osascript (clipboard + Cmd-V / keystroke)
# ---------------------------------------------------------------------------

def _set_clipboard(text: str) -> tuple[bool, int]:
    """Set clipboard text via NSPasteboard (no subprocess).

    Returns (success, change_count_after_write). On failure returns (False, -1).

    Requirement: PASTE-01
    """
    try:
        import AppKit

        def _do():
            pb = AppKit.NSPasteboard.generalPasteboard()
            pb.clearContents()
            result = pb.setString_forType_(text, AppKit.NSPasteboardTypeString)
            return bool(result), pb.changeCount()

        return _call_with_timeout(_do, timeout=_APPKIT_CALL_TIMEOUT)
    except Exception as e:
        _log(f"_set_clipboard (NSPasteboard) failed: {e}")
        return False, -1


def _clipboard_still_ours(expected_count: int) -> bool:
    """Return True if nobody stole the clipboard since we wrote it.

    Compares the current NSPasteboard changeCount against the count captured
    immediately after our write. A mismatch means another process modified the
    clipboard while we were waiting for the settle delay.

    Requirement: PASTE-02
    """
    try:
        import AppKit

        def _do():
            pb = AppKit.NSPasteboard.generalPasteboard()
            return pb.changeCount() == expected_count

        return _call_with_timeout(_do, timeout=_APPKIT_CALL_TIMEOUT)
    except Exception as e:
        _log(f"_clipboard_still_ours failed: {e}")
        return False


def _verify_target_focused(expected_bundle_id: str | None) -> bool:
    """Check if the frontmost app matches the expected target before pasting.

    Uses NSWorkspace.sharedWorkspace().frontmostApplication().bundleIdentifier()
    to verify the correct app is focused. Returns True if:
    - expected_bundle_id is None (skip check)
    - frontmost app bundle ID matches expected_bundle_id

    Returns False if a different app is focused (paste would go to wrong app).
    Fails-open (returns True) on exception — don't block paste on check failure.

    Requirement: PASTE-05
    """
    if expected_bundle_id is None:
        return True
    try:
        import AppKit

        def _do():
            ws = AppKit.NSWorkspace.sharedWorkspace()
            front = ws.frontmostApplication()
            return front.bundleIdentifier()

        actual = _call_with_timeout(_do, timeout=_APPKIT_CALL_TIMEOUT)
        if actual == expected_bundle_id:
            return True
        _log(f"Focus verify FAILED: expected {expected_bundle_id}, got {actual}")
        return False
    except Exception as e:
        _log(f"Focus verify exception: {e}")
        return True  # Fail-open: if check fails, proceed with paste


# AX roles that can receive direct value injection (native AppKit text fields only)
_AX_NATIVE_ROLES = frozenset({"AXTextField", "AXTextArea"})


# 15-02: migrated from legacy snapshot fields (ax_element, element_role,
# detected_workspace) to the new record-start lock fields (leaf_role,
# conductor_workspace_id, app_pid). AX element handle is now acquired live
# from the frontmost app's AXFocusedUIElement at call time (the lock does
# not carry an AX ref — D-04: refs are ephemeral, role-path is the durable
# identity).
def _ax_inject_text(snap, text: str) -> bool:
    """Inject text directly via AX value set — only for native AppKit text fields.

    Bypasses clipboard entirely by setting AXValue directly on the element.
    Only applicable for AXTextField and AXTextArea (native AppKit widgets).
    Explicitly skips AXWebArea (Electron/WebKit apps) where AXValue write has
    no effect.

    Args:
        snap: TargetLock (or None). Reads leaf_role + conductor_workspace_id
            from the lock; element handle is acquired live from the focused
            element of the current frontmost app.
        text: Text to inject.

    Returns:
        True if text was injected via AX, False if not applicable or failed.

    Requirement: PASTE-04 (Phase 12)
    """
    if snap is None:
        return False
    leaf_role = getattr(snap, "leaf_role", None)
    if not leaf_role or leaf_role not in _AX_NATIVE_ROLES:
        return False
    # DEF-192: workspace-managed Electron apps (Conductor) historically broke
    # here — AXValue set returned err=0 but the web framework's (React) state
    # stayed empty, so Enter submitted blank. RE-TESTED 2026-07 against current
    # Conductor: a marker set via AXValue (0.64ms) was correctly submitted on
    # Enter — newer Electron/Chromium (SetValue w/ kDispatchInputAndChangeEvent,
    # Electron PR #38102) now dispatches the input event React needs. Gated
    # behind HEYVOX_AX_CONDUCTOR so the proven osascript path stays the default
    # until validated across apps/versions/long text; the read-back verify below
    # is the hard guard (only claim success if the value actually stuck).
    conductor_ws = getattr(snap, "conductor_workspace_id", None)
    if conductor_ws and not os.environ.get("HEYVOX_AX_CONDUCTOR"):
        app_name = getattr(snap, "app_name", "?")
        _log(f"AX fast-path: skipping for workspace-managed app ({app_name})")
        return False
    pid = getattr(snap, "app_pid", 0)
    if not pid:
        return False
    try:
        from ApplicationServices import (
            AXUIElementCreateApplication,
            AXUIElementCopyAttributeValue,
            AXUIElementSetAttributeValue,
        )
        ax_app = AXUIElementCreateApplication(pid)
        # DEF-192: Chromium builds its a11y tree lazily — force it on so the
        # focused element + AXValue are live. Harmless no-op on native apps and
        # on already-active trees (HeyVox' own AX access usually keeps it awake).
        if conductor_ws:
            AXUIElementSetAttributeValue(ax_app, "AXManualAccessibility", True)
        # DEF-192: after activation Chromium builds the tree async — the focused
        # element can be briefly unavailable. Poll ~150ms (the standalone probe
        # used a 200ms sleep and succeeded; without it the paste falls silently
        # back to osascript).
        err = -1
        focused = None
        for _ in range(10):
            err, focused = AXUIElementCopyAttributeValue(
                ax_app, "AXFocusedUIElement", None
            )
            if err == 0 and focused is not None:
                break
            time.sleep(0.015)
        if err != 0 or focused is None:
            _log(f"AX fast-path: no focused element (err={err}) after activation "
                 f"— falling back to osascript")
            return False
        # osascript Cmd-V inserts at the cursor; Electron's cursor position is
        # structurally unreadable (returns {0,0}), so append-to-end is the
        # closest non-destructive equivalent and preserves any existing text.
        cerr, current = AXUIElementCopyAttributeValue(focused, "AXValue", None)
        current = str(current) if (cerr == 0 and current) else ""
        newval = current + text if current else text
        err = AXUIElementSetAttributeValue(focused, "AXValue", newval)
        if err != 0:
            _log(f"AX fast-path: set failed (err={err}) — falling back")
            return False
        # DEF-192 hard guard: read the value back. The old Conductor bug set
        # AXValue with err=0 but it didn't take. Only claim success if our text
        # is actually present now; otherwise restore the prior value (so the
        # osascript fallback doesn't double-insert) and return False.
        verr, got = AXUIElementCopyAttributeValue(focused, "AXValue", None)
        if verr == 0 and got is not None and text in str(got):
            _log(f"AX fast-path: injected+verified {len(text)} chars into "
                 f"{leaf_role}{' (conductor)' if conductor_ws else ''}")
            return True
        AXUIElementSetAttributeValue(focused, "AXValue", current)
        _log(f"AX fast-path: verify FAILED (got {len(str(got or ''))} chars) — "
             f"restored + falling back to osascript")
        return False
    except Exception as e:
        _log(f"AX fast-path exception: {e}")
        return False


def _ax_walk_to_native_role(el, depth: int = 8):
    """Walk up from `el` via AXParent to the nearest AXTextField/AXTextArea.

    Chromium's focused element is often an inner node (the contenteditable's
    text run), so the settable AXTextArea is an ancestor. Returns the matching
    element or None if none is found within `depth` hops.
    """
    from ApplicationServices import AXUIElementCopyAttributeValue

    for _ in range(depth):
        if el is None:
            return None
        err, role = AXUIElementCopyAttributeValue(el, "AXRole", None)
        if err == 0 and role in _AX_NATIVE_ROLES:
            return el
        err, el = AXUIElementCopyAttributeValue(el, "AXParent", None)
        if err != 0:
            return None
    return None


def _ax_set_conductor_value(pid: int, text: str) -> bool:
    """Set AXValue on the app's LIVE keyboard-focused text field (mouse-independent).

    Unlike _ax_inject_text (which gates on the mouse-captured snap.leaf_role),
    this inspects the LIVE focused element AFTER the focus_shortcut has run, so
    it works even when the record-start mouse wasn't over the chat field (the
    Test-2 AXGroup case — DEF-192). Activates Chromium's lazy a11y tree, walks
    the focused element up to its AXTextArea, appends the text, and read-back
    verifies. Returns True only if the value verifiably stuck; restores the
    prior value and returns False otherwise (so the osascript Cmd+V fallback
    doesn't double-insert).
    """
    from ApplicationServices import (
        AXUIElementCreateApplication,
        AXUIElementCopyAttributeValue,
        AXUIElementSetAttributeValue,
    )

    ax_app = AXUIElementCreateApplication(pid)
    # Force Chromium's lazy tree on so the focused element + AXValue are live.
    AXUIElementSetAttributeValue(ax_app, "AXManualAccessibility", True)
    # After activation the tree builds async — poll ~150ms for a focused text
    # field (the standalone probe used a 200ms sleep; without it the set falls
    # silently back to osascript).
    target = None
    for _ in range(10):
        err, focused = AXUIElementCopyAttributeValue(
            ax_app, "AXFocusedUIElement", None
        )
        if err == 0 and focused is not None:
            target = _ax_walk_to_native_role(focused)
            if target is not None:
                break
        time.sleep(0.015)
    if target is None:
        _log("AX conductor: no focused text field after focus_shortcut — fallback")
        return False
    cerr, current = AXUIElementCopyAttributeValue(target, "AXValue", None)
    current = str(current) if (cerr == 0 and current) else ""
    newval = current + text if current else text
    serr = AXUIElementSetAttributeValue(target, "AXValue", newval)
    if serr != 0:
        _log(f"AX conductor: set failed (err={serr}) — fallback")
        return False
    verr, got = AXUIElementCopyAttributeValue(target, "AXValue", None)
    if verr == 0 and got is not None and text in str(got):
        return True
    AXUIElementSetAttributeValue(target, "AXValue", current)
    _log(f"AX conductor: verify FAILED (got {len(str(got or ''))} chars) — "
         f"restored + fallback")
    return False


def _post_return_key(count: int = 1) -> bool:
    """Post `count` Return keypresses via CGEvent (in-process, no subprocess).

    Goes to the frontmost app. ~5ms vs ~400ms for an osascript `keystroke
    return` — eliminating the second subprocess is what makes the AX paste
    path actually faster than the consolidated Cmd+V path (DEF-192). Returns
    True if events were posted, False if Quartz is unavailable (caller falls
    back to osascript).
    """
    try:
        import Quartz  # lazy: pyobjc-framework-Quartz
    except Exception as e:
        _log(f"_post_return_key: Quartz unavailable ({e})")
        return False
    try:
        _kVK_Return = 0x24
        src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
        for i in range(count):
            down = Quartz.CGEventCreateKeyboardEvent(src, _kVK_Return, True)
            up = Quartz.CGEventCreateKeyboardEvent(src, _kVK_Return, False)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
            if i < count - 1:
                time.sleep(0.03)
        return True
    except Exception as e:
        _log(f"_post_return_key: CGEvent post failed ({e})")
        return False


# US/German-QWERTY physical virtual-keycodes for letters+digits — enough to
# post a Cmd+<char> focus shortcut via CGEvent. 'l' (Conductor) = 0x25. Letters
# sit in the same physical position on US-QWERTY and German-QWERTZ, so this map
# holds for both; an unmapped char falls back to the osascript keystroke.
_CHAR_TO_VK = {
    'a': 0x00, 's': 0x01, 'd': 0x02, 'f': 0x03, 'h': 0x04, 'g': 0x05, 'z': 0x06,
    'x': 0x07, 'c': 0x08, 'v': 0x09, 'b': 0x0B, 'q': 0x0C, 'w': 0x0D, 'e': 0x0E,
    'r': 0x0F, 'y': 0x10, 't': 0x11, 'o': 0x1F, 'u': 0x20, 'i': 0x22, 'p': 0x23,
    'l': 0x25, 'j': 0x26, 'k': 0x28, 'n': 0x2D, 'm': 0x2E,
    '1': 0x12, '2': 0x13, '3': 0x14, '4': 0x15, '5': 0x17, '6': 0x16, '7': 0x1A,
    '8': 0x1C, '9': 0x19, '0': 0x1D,
}
_kVK_Command = 0x37


def _post_focus_shortcut(char: str) -> bool:
    """Post Cmd+<char> via CGEvent (in-process, no subprocess) to the frontmost app.

    ~5ms vs ~200-480ms for an osascript `keystroke` (the subprocess spawn +
    Apple-Events IPC dominates under system load) — this is the DEF-192 focus
    step, the last big cost after the CGEvent-Enter win. The Command modifier is
    posted as an explicit key press/release AROUND the char (not just an event
    flag) so the app reliably sees the Cmd-down state — plain flag-only Cmd
    combos are the finicky part of synthetic modifier events. Returns False if
    the char isn't mappable or Quartz is unavailable (caller falls back to
    osascript). Correctness is still guarded downstream by the AX read-back
    verify, so a misfire degrades to the fallback rather than a wrong paste.
    """
    keycode = _CHAR_TO_VK.get(char.lower())
    if keycode is None:
        return False
    try:
        import Quartz  # lazy: pyobjc-framework-Quartz
    except Exception as e:
        _log(f"_post_focus_shortcut: Quartz unavailable ({e})")
        return False
    try:
        src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
        cmd = Quartz.kCGEventFlagMaskCommand
        cmd_down = Quartz.CGEventCreateKeyboardEvent(src, _kVK_Command, True)
        key_down = Quartz.CGEventCreateKeyboardEvent(src, keycode, True)
        Quartz.CGEventSetFlags(key_down, cmd)
        key_up = Quartz.CGEventCreateKeyboardEvent(src, keycode, False)
        Quartz.CGEventSetFlags(key_up, cmd)
        cmd_up = Quartz.CGEventCreateKeyboardEvent(src, _kVK_Command, False)
        for ev in (cmd_down, key_down, key_up, cmd_up):
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        return True
    except Exception as e:
        _log(f"_post_focus_shortcut: post failed ({e})")
        return False


def _ax_conductor_paste(profile, text: str, enter_count: int, snap) -> bool:
    """DEF-192: fast, mouse-independent paste for workspace-managed Electron.

    Focuses the chat field via the profile shortcut (Cmd+L), sets the value via
    AX (synchronous — no Electron paste-IPC settle needed, which is the actual
    latency win over Cmd+V), then submits with Enter. Returns False on ANY
    failure so app_fast_paste falls back to the proven osascript Cmd+V path
    (the clipboard is already set).

    Requires the target to be ALREADY frontmost (defers to the fallback
    otherwise) so it can skip the costly Electron set-frontmost activation cycle
    that both slows the path and resets web-view focus.

    Behind HEYVOX_AX_CONDUCTOR. Reads app_pid from the record-start lock; the
    field is located from the LIVE keyboard focus, not the mouse.
    """
    _t0 = time.time()
    pid = getattr(snap, "app_pid", 0)
    if not pid:
        return False

    # 1. Focus the chat field. Per project_set_frontmost_focus_disruption, an
    #    unnecessary `set frontmost` on an ALREADY-frontmost Electron app costs a
    #    ~300-450ms activation cycle AND resets the web-view focus. So this fast
    #    path runs ONLY when the target is already frontmost (the dictation
    #    norm); otherwise it defers to the osascript Cmd+V fallback, which
    #    handles activation itself (and avoids us activating the wrong app). That
    #    lets the focus step drop both set-frontmost and the process-name lookup
    #    — just a bare Cmd+L to the frontmost app. (DEF-192 focus step: this cut
    #    the 557ms focus block, the last big cost after the CGEvent-Enter win.)
    if save_frontmost_pid() != pid:
        _log("AX conductor: target not frontmost — deferring to osascript path")
        return False
    if profile.focus_shortcut:
        # Prefer in-process CGEvent Cmd+<shortcut> (no subprocess); fall back to
        # the osascript keystroke only if Quartz / the char-map is unavailable.
        if not _post_focus_shortcut(profile.focus_shortcut):
            focus_script = (
                f'tell application "System Events"\n'
                f'    keystroke "{profile.focus_shortcut}" using command down\n'
                f'end tell'
            )
            r = subprocess.run(
                ["osascript", "-e", focus_script],
                capture_output=True, timeout=SUBPROCESS_TIMEOUT,
            )
            if r.returncode != 0:
                _log(f"AX conductor: focus osascript failed rc={r.returncode} — fallback")
                return False
        time.sleep(0.03)  # brief; the AX poll below absorbs focus readiness
    t_focus = time.time()

    # 2. AX-set on the LIVE focused field (mouse-independent — fixes DEF-192
    #    Test-2 where the mouse-captured leaf was AXGroup).
    if not _ax_set_conductor_value(pid, text):
        return False
    t_axset = time.time()

    # 3. Submit with Enter via CGEvent (in-process, NO second subprocess). The
    #    osascript Enter cost ~400ms and made the whole AX path SLOWER than the
    #    consolidated Cmd+V path (DEF-192 first live test: 1268ms vs 1036ms).
    #    CGEvent goes to the frontmost app (Conductor — verified above) with no
    #    set-frontmost → no DEF-089 focus-steal race. The osascript fallback is a
    #    bare keystroke (also frontmost-targeted) for the rare Quartz-missing case.
    if enter_count > 0:
        if not _post_return_key(enter_count):
            enter_lines = []
            for i in range(enter_count):
                enter_lines.append("keystroke return")
                if i < enter_count - 1:
                    enter_lines.append("delay 0.05")
            enter_block = "\n    ".join(enter_lines)
            enter_script = (
                f'tell application "System Events"\n'
                f'    {enter_block}\n'
                f'end tell'
            )
            er = subprocess.run(
                ["osascript", "-e", enter_script],
                capture_output=True, timeout=SUBPROCESS_TIMEOUT,
            )
            if er.returncode != 0:
                # Text is already in the field (AX-set verified). Do NOT return
                # False (that would trigger the Cmd+V fallback → double-insert).
                _log(f"AX conductor: Enter fallback failed rc={er.returncode} "
                     f"(text is in field, not submitted)")
    t_enter = time.time()
    _log(f"[TIMING] AX conductor paste: OK in {(t_enter-_t0)*1000:.0f}ms "
         f"(focus={(t_focus-_t0)*1000:.0f} axset={(t_axset-t_focus)*1000:.0f} "
         f"enter={(t_enter-t_axset)*1000:.0f} pid={pid} chars={len(text)} "
         f"enter_n={enter_count})")
    return True


def _settle_delay_for(app_name: str | None, app_delays: dict[str, float], default: float) -> float:
    """Resolve the focus settle delay for a given app name.

    Uses case-insensitive substring match against keys in app_delays.
    Returns default if app_name is None or no key matches.

    Requirement: PASTE-03
    """
    if not app_name:
        return default
    name_lower = app_name.lower()
    for key, delay in app_delays.items():
        if key.lower() in name_lower:
            return delay
    return default


def save_frontmost_pid() -> int:
    """Return the PID of the currently frontmost app (for restoring later)."""
    try:
        import AppKit
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        return app.processIdentifier() if app else 0
    except Exception:
        return 0


def _osascript_type_text(
    text: str,
    app_name: str | None = None,
    settle_secs: float = 0.1,
    expected_bundle_id: str | None = None,
    expected_pid: int = 0,
    max_retries: int = 2,
    enter_count: int = 0,
    enter_delay: float = 0.05,
    focus_shortcut: str = "",
) -> bool:
    """Paste text via clipboard + Cmd-V (osascript), optionally followed by Enter.

    When app_name is provided, targets that process directly. Briefly
    activates the target for the paste, then restores the previously
    focused app so the user isn't interrupted on multi-monitor setups.

    settle_secs: focus settle delay (Python sleep before Cmd-V). Replaces
    the old hardcoded AppleScript 'delay 0.3' — now controlled by
    InjectionConfig.app_delays per-app profiles.

    expected_bundle_id: if set, verifies frontmost app bundle ID before paste.
    Aborts with error cue if focus has moved to a different app (PASTE-05).

    max_retries: number of times to retry if clipboard is stolen during settle.

    enter_count: if > 0, appends Enter keystrokes after Cmd-V in the same
    osascript call — avoids a separate subprocess spawn (~0.2s savings).

    Returns:
        True on successful paste, False on any failure.

    Requirement: PASTE-02, PASTE-03, PASTE-05
    """
    _log(f"paste: target={app_name or 'frontmost'}, text={len(text)} chars"
         f"{f' + Enter x{enter_count}' if enter_count else ''}: {text[:60]!r}")

    # Step 1: Proactive focus verification before touching clipboard (PASTE-05)
    if not _verify_target_focused(expected_bundle_id):
        _log(f"ERROR: focus verification failed (expected={expected_bundle_id}), aborting paste")
        audio_cue("error")
        return False

    attempt = 0
    while attempt <= max_retries:
        ok, expected_count = _set_clipboard(text)
        if not ok:
            _log("ERROR: failed to set clipboard, aborting paste")
            audio_cue("error")
            return False

        verify = get_clipboard_text()
        if verify != text:
            _log(f"ERROR: clipboard verify failed — expected {len(text)} chars, got {len(verify)} chars, aborting paste")
            audio_cue("error")
            return False

        _log(f"paste: clipboard verified OK ({len(text)} chars)")

        frontmost_before = _get_frontmost_app()
        original_pid = save_frontmost_pid()
        _log(f"paste: frontmost app BEFORE = {frontmost_before} (pid={original_pid})")

        # DEF-054: PID-aware guard. For Electron bundles (Conductor, VS Code,
        # Slack, Cursor…) the same bundle name maps to many helper PIDs.
        # Activating the target bundle doesn't guarantee the *correct* helper
        # PID becomes key window. If we see a mismatch here, log a WARNING so
        # the next time paste lands in the wrong window we know why.
        if (
            expected_pid
            and original_pid
            and original_pid != expected_pid
        ):
            _log(
                f"paste: WARNING: expected pid={expected_pid} but frontmost "
                f"pid={original_pid} ({frontmost_before}) — likely wrong "
                f"window within same bundle (DEF-054)"
            )

        time.sleep(settle_secs)

        # Step 2: Check that nobody stole the clipboard during the settle delay (PASTE-02)
        if not _clipboard_still_ours(expected_count):
            _log(f"ERROR: clipboard stolen during settle (attempt {attempt + 1}/{max_retries + 1})")
            if attempt < max_retries:
                _log("paste: retrying after clipboard theft...")
                attempt += 1
                continue
            _log("paste: max retries exceeded after clipboard theft, aborting")
            audio_cue("error")
            return False

        break  # clipboard is ours, proceed with paste

    # Use the actual process name from System Events (frontmost_before) for the
    # AppleScript target, not the user-facing app_name — macOS process names are
    # case-sensitive and often differ from the app's display name (DEF-027).
    process_name = frontmost_before if frontmost_before and frontmost_before != "?" else app_name
    already_frontmost = process_name and frontmost_before and process_name.lower() == _get_frontmost_app().lower()

    # Build keystrokes: optional Cmd+shortcut (focus input), Cmd+V paste,
    # then optional Enter(s) — all in one atomic osascript call.
    # focus_shortcut is used when a workspace switch may have moved focus away
    # from the text input (e.g. sidebar click). When no switch was needed,
    # the caller passes focus_shortcut="" and we skip it.
    keystrokes = []
    if focus_shortcut:
        keystrokes.append(f'keystroke "{focus_shortcut}" using command down')
        keystrokes.append("delay 0.1")  # Let input field focus
        _log(f"paste: including Cmd+{focus_shortcut} focus shortcut in atomic script")
    keystrokes.append('keystroke "v" using command down')
    if enter_count > 0:
        keystrokes.append(f"delay {enter_delay}")  # Settle after paste (Electron needs 0.3s)
        for i in range(enter_count):
            keystrokes.append("keystroke return")
            if i < enter_count - 1:
                keystrokes.append("delay 0.05")
    keystroke_block = "\n        ".join(keystrokes)

    if process_name:
        safe_name = process_name.replace('\\', '\\\\').replace('"', '\\"')
        if already_frontmost:
            # App is already frontmost — skip 'set frontmost to true' to preserve
            # element focus (target restore already focused the correct text field).
            # Calling set frontmost again disrupts web view focus in Electron/Tauri apps.
            script = (
                f'tell application "System Events"\n'
                f'    tell process "{safe_name}"\n'
                f'        {keystroke_block}\n'
                f'    end tell\n'
                f'end tell'
            )
        else:
            # App is not frontmost — activate it with a delay for focus to settle
            script = (
                f'tell application "System Events"\n'
                f'    tell process "{safe_name}"\n'
                f'        set frontmost to true\n'
                f'        delay 0.2\n'
                f'        {keystroke_block}\n'
                f'    end tell\n'
                f'end tell'
            )
    else:
        script = f'tell application "System Events"\n    {keystroke_block}\nend tell'
    _paste_t0 = time.time()
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, timeout=SUBPROCESS_TIMEOUT + 2,
    )
    _paste_elapsed_ms = int((time.time() - _paste_t0) * 1000)

    frontmost_after = _get_frontmost_app()
    if result.returncode != 0:
        _stderr = result.stderr.decode(errors="replace").strip()
        _log(
            f"paste: FAILED (rc={result.returncode}, {_paste_elapsed_ms} ms): {_stderr}"
        )
        audio_cue("error")
        return False

    # DEF-052 diagnostic: log osascript duration + any stdout/stderr. Silent
    # "success" where the keystroke vanished tends to manifest as an unusually
    # short elapsed time (<100 ms skips settle delays) or stderr noise the
    # user can't see otherwise.
    _stderr = result.stderr.decode(errors="replace").strip()
    _stdout = result.stdout.decode(errors="replace").strip()
    _extra = ""
    if _stderr:
        _extra += f" stderr={_stderr!r}"
    if _stdout:
        _extra += f" stdout={_stdout!r}"
    _log(
        f"paste: OK → frontmost app AFTER = {frontmost_after} "
        f"(osascript {_paste_elapsed_ms} ms){_extra}"
    )

    if app_name and frontmost_after.lower() != app_name.lower() and frontmost_after != "?":
        _log(f"paste: WARNING: target was {app_name} but frontmost is {frontmost_after} — may have pasted to wrong app!")

    # DEF-054: PID-level post-paste check. For multi-PID bundles the name
    # guard above always passes (same process name on both sides) even when
    # paste lands in a different window within the same bundle. Compare PIDs
    # to catch that case.
    if expected_pid:
        frontmost_after_pid = save_frontmost_pid()
        if frontmost_after_pid and frontmost_after_pid != expected_pid:
            _log(
                f"paste: WARNING: target pid={expected_pid} but frontmost "
                f"pid={frontmost_after_pid} — paste likely landed in wrong "
                f"window within same bundle (DEF-054)"
            )

    return True


def _osascript_press_enter(count: int, app_name: str | None = None, enter_delay: float = 0.2) -> None:
    """Press Enter via osascript.

    When app_name is provided, targets that process directly via
    `tell process`. Skips `set frontmost to true` when the app is already
    frontmost — calling it redundantly disrupts web view element focus in
    Electron/Tauri apps (e.g. Conductor), causing Enter to miss the input field.
    """
    _log(f"enter: count={count}, target={app_name or 'frontmost'}")

    enter_script = "\n        ".join(
        ["keystroke return", f"delay {enter_delay}"] * count
    )
    # Use actual process name from System Events (case-sensitive)
    process_name = _get_frontmost_app() if app_name else None
    if process_name and process_name == "?":
        process_name = app_name
    target_name = process_name or app_name
    if target_name:
        safe_name = target_name.replace('\\', '\\\\').replace('"', '\\"')
        # Check if already frontmost — skip set frontmost to avoid disrupting
        # Electron/Tauri web view element focus (same pattern as _osascript_type_text)
        already_frontmost = process_name and process_name.lower() == _get_frontmost_app().lower()
        if already_frontmost:
            _log(f"enter: {target_name} already frontmost, skipping set frontmost")
            script = (
                f'tell application "System Events"\n'
                f'    tell process "{safe_name}"\n'
                f'        {enter_script}\n'
                f'    end tell\n'
                f'end tell'
            )
        else:
            script = (
                f'tell application "System Events"\n'
                f'    tell process "{safe_name}"\n'
                f'        set frontmost to true\n'
                f'        delay 0.2\n'
                f'        {enter_script}\n'
                f'    end tell\n'
                f'end tell'
            )
    else:
        script = f'tell application "System Events"\n    {enter_script}\nend tell'
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, timeout=SUBPROCESS_TIMEOUT + 2,
    )
    if result.returncode != 0:
        _log(f"enter: FAILED (rc={result.returncode}): {result.stderr.decode().strip()}")
    else:
        _log(f"enter: OK (x{count} → {app_name or 'frontmost'})")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def type_text(
    text: str,
    app_name: str | None = None,
    snap=None,
    settle_secs: float = 0.1,
    max_retries: int = 2,
    enter_count: int = 0,
    enter_delay: float = 0.05,
    focus_shortcut: str = "",
) -> bool:
    """Insert text into an app, optionally pressing Enter to submit.

    When app_name is provided, targets that specific process for paste.
    This prevents pasting into the wrong app if focus changed during STT.

    Tries in order:
    1. Chrome extension (via Hush socket) — fastest, DOM-level injection
    2. AX fast-path (AXTextField/AXTextArea via AXValue) — native AppKit only
    3. Clipboard + Cmd-V via osascript — universal fallback

    When enter_count > 0, the Enter keystrokes are combined into the same
    osascript call as the paste — avoids a separate subprocess spawn.

    Args:
        text: Text to inject.
        app_name: Target app process name (for osascript targeting).
        snap: TargetLock (or None). Used for AX fast-path and focus verification.
        settle_secs: Focus settle delay before Cmd-V (per-app tuned via InjectionConfig).
        max_retries: Number of retries on clipboard theft.
        enter_count: Number of Enter keystrokes after paste (0 = no auto-send).
        enter_delay: Delay (seconds) between Cmd+V and first Enter. Electron apps
            need ~0.3s for paste to propagate through IPC before Enter can submit.

    Returns:
        True on success, False on failure. Error cue is played on failure.
    """
    # Chrome extension path is only for browser targets. When app_name points
    # to a specific non-browser app (e.g. Conductor), routing through Chrome
    # would paste into whatever tab is active in the browser — completely
    # wrong target. Only try Chrome when the target is a browser or unknown.
    _browser_names = ("chrome", "arc", "brave", "edge", "vivaldi", "opera")
    _is_browser_target = (
        app_name is None
        or any(b in app_name.lower() for b in _browser_names)
    )
    if _is_browser_target and _chrome_type_text(text):
        _log(f"type_text: done via Chrome extension ({len(text)} chars)")
        if enter_count > 0:
            _chrome_press_enter(enter_count)
        return True

    if _ax_inject_text(snap, text):
        _log(f"type_text: done via AX fast-path ({len(text)} chars)")
        # AX path doesn't support combined Enter — fall through to separate call
        if enter_count > 0:
            _osascript_press_enter(enter_count, app_name)
        return True

    _log(f"type_text: using osascript → {app_name or 'frontmost'}"
         f"{f' + Enter x{enter_count}' if enter_count else ''}")
    expected_bundle_id = getattr(snap, "app_bundle_id", None) if snap is not None else None
    expected_pid = getattr(snap, "app_pid", 0) if snap is not None else 0
    return _osascript_type_text(
        text,
        app_name=app_name,
        settle_secs=settle_secs,
        expected_bundle_id=expected_bundle_id,
        expected_pid=expected_pid,
        max_retries=max_retries,
        enter_count=enter_count,
        enter_delay=enter_delay,
        focus_shortcut=focus_shortcut,
    )


def press_enter(count: int = 1, app_name: str | None = None) -> None:
    """Press Enter in the focused app.

    Tries Chrome extension first, falls back to osascript.
    """
    if _chrome_press_enter(count):
        _log(f"press_enter: done via Chrome extension (x{count})")
        return
    _osascript_press_enter(count, app_name)


def focus_app(app_name: str) -> None:
    """Bring an application to the front."""
    safe_name = app_name.replace('\\', '\\\\').replace('"', '\\"')
    subprocess.run(
        ["osascript", "-e", f'tell application "{safe_name}" to activate'],
        capture_output=True, timeout=SUBPROCESS_TIMEOUT,
    )


def focus_input(app_name: str, shortcuts: dict[str, str] | None = None) -> None:
    """Focus the text input field in a known app via keyboard shortcut."""
    if shortcuts is None:
        shortcuts = {}
    key = shortcuts.get(app_name.lower())
    if key:
        subprocess.run(
            ["osascript", "-e",
             f'tell application "System Events"\n    keystroke "{key}" using command down\nend tell'],
            capture_output=True, timeout=SUBPROCESS_TIMEOUT,
        )


def app_fast_paste(profile, text: str, enter_count: int | None = None, snap=None) -> bool:
    """One-shot paste using profile-driven shortcuts: focus-shortcut -> Cmd+V -> Enter*N.

    Combines focus + paste + Enter into a single osascript subprocess call
    (saves ~0.3s vs multi-step). Clipboard is set via NSPasteboard before
    the osascript runs.

    Args:
        profile: AppProfileConfig - provides focus_shortcut, settle_delay,
            is_electron, and the default enter_count. NEVER hardcoded.
        text: Text to paste.
        enter_count: Optional override for the number of Enter presses.
            Pass 0 to suppress auto-send (e.g. PTT mode). Pass None to use
            profile.enter_count (the wake-word default).
        snap: Optional TargetLock from record-start. When set AND
            HEYVOX_AX_CONDUCTOR is enabled AND the lock is a workspace-managed
            Electron app (conductor_workspace_id), the AX fast-path is tried
            first (mouse-independent, no Electron settle_delay), falling back to
            the osascript Cmd+V path below on any failure. DEF-192.

    Returns True on success, False on failure.

    Process name for `tell process` is read from the LIVE frontmost app
    (via _get_frontmost_app) rather than profile.name, because macOS process
    names are case-sensitive and frequently differ from display names
    (DEF-027 - lowercase System Events form vs TitleCase bundle display name).

    Requirement: PASTE-15-R8
    """
    _t0 = time.time()
    effective_enter_count = profile.enter_count if enter_count is None else enter_count
    _log(
        f"app_fast_paste: profile={profile.name} focus_shortcut="
        f"{profile.focus_shortcut!r} enter_count={effective_enter_count} "
        f"text_len={len(text)}"
    )

    # 1. Clipboard write + verify
    ok, expected_count = _set_clipboard(text)
    if not ok:
        _log("app_fast_paste: ERROR failed to set clipboard")
        audio_cue("error")
        return False
    verify = get_clipboard_text()
    if verify != text:
        _log(
            f"app_fast_paste: ERROR clipboard verify failed "
            f"({len(text)} vs {len(verify)} chars)"
        )
        audio_cue("error")
        return False

    # DEF-192: AX fast-path for workspace-managed Electron (Conductor), behind
    # HEYVOX_AX_CONDUCTOR. Mouse-independent focus + synchronous AX-set (no
    # Electron paste-IPC settle_delay). Falls through to the osascript Cmd+V
    # path below on any failure — the clipboard is already set as the fallback.
    if (
        snap is not None
        and os.environ.get("HEYVOX_AX_CONDUCTOR")
        and getattr(snap, "conductor_workspace_id", None)
    ):
        if _ax_conductor_paste(profile, text, effective_enter_count, snap):
            _log(
                f"[TIMING] app_fast_paste: OK via AX profile={profile.name} "
                f"in {(time.time() - _t0)*1000:.0f}ms"
            )
            return True
        _log("app_fast_paste: AX path declined — falling back to osascript Cmd+V")

    # 2. Build keystroke block from profile (NO hardcoded shortcuts)
    keystrokes = []
    if profile.focus_shortcut:
        keystrokes.append(
            f'keystroke "{profile.focus_shortcut}" using command down'
        )
        keystrokes.append("delay 0.1")  # Brief settle for input focus to land
    keystrokes.append('keystroke "v" using command down')
    if effective_enter_count > 0:
        # Use profile.settle_delay for Electron/Tauri paste-IPC settle
        keystrokes.append(f"delay {profile.settle_delay}")
        for i in range(effective_enter_count):
            keystrokes.append("keystroke return")
            if i < effective_enter_count - 1:
                keystrokes.append("delay 0.05")
    keystroke_block = "\n        ".join(keystrokes)

    # 3. Live frontmost name preserves DEF-027 lowercase fix
    process_name = _get_frontmost_app()
    if not process_name or process_name == "?":
        process_name = profile.name
    safe_name = process_name.replace('\\', '\\\\').replace('"', '\\"')

    script = (
        f'tell application "System Events"\n'
        f'    tell process "{safe_name}"\n'
        f'        set frontmost to true\n'
        f'        delay 0.1\n'
        f'        {keystroke_block}\n'
        f'    end tell\n'
        f'end tell'
    )

    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, timeout=SUBPROCESS_TIMEOUT + 2,
    )
    if result.returncode != 0:
        _log(
            f"app_fast_paste: FAILED rc={result.returncode}: "
            f"{result.stderr.decode().strip()}"
        )
        audio_cue("error")
        return False

    _log(
        f"[TIMING] app_fast_paste: OK profile={profile.name} "
        f"in {(time.time() - _t0)*1000:.0f}ms"
    )
    return True


def clipboard_is_image() -> bool:
    """Return True if the current clipboard contains an image (PNG, TIFF, JPEG)."""
    result = subprocess.run(
        ["osascript", "-e", 'try\nclipboard info\non error\nreturn ""\nend try'],
        capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
    )
    out = result.stdout.strip()
    return "PNGf" in out or "TIFF" in out or "JPEG" in out


def get_clipboard_text() -> str:
    """Return the current clipboard text via NSPasteboard, or "" if empty or not text.

    Requirement: PASTE-01
    """
    try:
        import AppKit

        def _do():
            pb = AppKit.NSPasteboard.generalPasteboard()
            text = pb.stringForType_(AppKit.NSPasteboardTypeString)
            return str(text) if text else ""

        return _call_with_timeout(_do, timeout=_APPKIT_CALL_TIMEOUT)
    except Exception as e:
        _log(f"get_clipboard_text failed: {e}")
        return ""
