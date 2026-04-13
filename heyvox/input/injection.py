"""
Text injection into the focused macOS application.

Primary method: clipboard + Cmd-V via osascript (works with any app).

The Hush Chrome extension socket is tried first for Chrome tabs, but this
is a minor optimization — the clipboard path is the reliable default.
"""

import json
import socket
import subprocess
import sys
import time

from heyvox.audio.cues import audio_cue


# Max seconds to wait for osascript subprocess to complete
SUBPROCESS_TIMEOUT = 5


def _log(msg: str) -> None:
    """Log to stderr with [injection] prefix."""
    print(f"[injection] {msg}", file=sys.stderr, flush=True)


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
        print(f"[injection] Hush socket error: {e}", file=sys.stderr)
        return None


def _chrome_type_text(text: str) -> bool:
    """Insert text via the Hush Chrome extension. Returns True on success."""
    resp = _hush_send({"action": "type-text", "text": text})
    if resp and resp.get("ok"):
        print(f"[injection] Chrome type-text OK (tab: {resp.get('title', '?')})", file=sys.stderr)
        return True
    if resp and resp.get("error"):
        print(f"[injection] Chrome type-text failed: {resp['error']}", file=sys.stderr)
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
        pb = AppKit.NSPasteboard.generalPasteboard()
        pb.clearContents()
        result = pb.setString_forType_(text, AppKit.NSPasteboardTypeString)
        count = pb.changeCount()
        return bool(result), count
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
        pb = AppKit.NSPasteboard.generalPasteboard()
        return pb.changeCount() == expected_count
    except Exception:
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
        ws = AppKit.NSWorkspace.sharedWorkspace()
        front = ws.frontmostApplication()
        actual = front.bundleIdentifier()
        if actual == expected_bundle_id:
            return True
        _log(f"Focus verify FAILED: expected {expected_bundle_id}, got {actual}")
        return False
    except Exception as e:
        _log(f"Focus verify exception: {e}")
        return True  # Fail-open: if check fails, proceed with paste


# AX roles that can receive direct value injection (native AppKit text fields only)
_AX_NATIVE_ROLES = frozenset({"AXTextField", "AXTextArea"})


def _ax_inject_text(snap, text: str) -> bool:
    """Inject text directly via AX value set — only for native AppKit text fields.

    Bypasses clipboard entirely by setting AXValue directly on the element.
    Only applicable for AXTextField and AXTextArea (native AppKit widgets).
    Explicitly skips AXWebArea (Electron/WebKit apps) where AXValue write has no effect.

    Args:
        snap: TargetSnapshot (or None). Must have ax_element and element_role.
        text: Text to inject.

    Returns:
        True if text was injected via AX, False if not applicable or failed.

    Requirement: PASTE-04
    """
    if snap is None:
        return False
    if not hasattr(snap, "element_role") or snap.element_role not in _AX_NATIVE_ROLES:
        return False
    if snap.ax_element is None:
        return False
    # Skip for Electron/Tauri apps — AXValue set returns success but doesn't
    # update the web framework's internal state, so Enter submits empty text.
    app_name = getattr(snap, "app_name", None)
    detected_ws = getattr(snap, "detected_workspace", None)
    if isinstance(detected_ws, str) and detected_ws:
        _log(f"AX fast-path: skipping for workspace-managed app ({app_name})")
        return False
    try:
        from ApplicationServices import AXUIElementSetAttributeValue
        err = AXUIElementSetAttributeValue(snap.ax_element, "AXValue", text)
        if err == 0:
            _log(f"AX fast-path: injected {len(text)} chars into {snap.element_role}")
            return True
        _log(f"AX fast-path: failed (err={err})")
        return False
    except Exception as e:
        _log(f"AX fast-path exception: {e}")
        return False


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


def _save_frontmost_pid() -> int:
    """Return the PID of the currently frontmost app (for restoring later)."""
    try:
        import AppKit
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        return app.processIdentifier() if app else 0
    except Exception:
        return 0


def _restore_frontmost(pid: int) -> None:
    """Re-activate the app that was frontmost before we stole focus."""
    if not pid:
        return
    try:
        import AppKit
        app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app:
            app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
    except Exception:
        pass


def _osascript_type_text(
    text: str,
    app_name: str | None = None,
    settle_secs: float = 0.1,
    expected_bundle_id: str | None = None,
    max_retries: int = 2,
    enter_count: int = 0,
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
        original_pid = _save_frontmost_pid()
        _log(f"paste: frontmost app BEFORE = {frontmost_before} (pid={original_pid})")

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
    # case-sensitive (e.g., "conductor" not "Conductor").
    process_name = frontmost_before if frontmost_before and frontmost_before != "?" else app_name
    already_frontmost = process_name and frontmost_before and process_name.lower() == _get_frontmost_app().lower()

    # Build keystrokes: Cmd+V paste, then optional Enter(s) in the same script
    # to avoid a separate subprocess spawn (~0.2s savings).
    keystrokes = ['keystroke "v" using command down']
    if enter_count > 0:
        keystrokes.append("delay 0.05")  # Brief settle after paste
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
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, timeout=SUBPROCESS_TIMEOUT + 2,
    )

    frontmost_after = _get_frontmost_app()
    if result.returncode != 0:
        _log(f"paste: FAILED (rc={result.returncode}): {result.stderr.decode().strip()}")
        audio_cue("error")
        return False

    _log(f"paste: OK → frontmost app AFTER = {frontmost_after}")

    if app_name and frontmost_after.lower() != app_name.lower() and frontmost_after != "?":
        _log(f"paste: WARNING: target was {app_name} but frontmost is {frontmost_after} — may have pasted to wrong app!")

    return True


def _osascript_press_enter(count: int, app_name: str | None = None) -> None:
    """Press Enter via osascript.

    When app_name is provided, targets that process directly via
    `tell process`. Skips `set frontmost to true` when the app is already
    frontmost — calling it redundantly disrupts web view element focus in
    Electron/Tauri apps (e.g. Conductor), causing Enter to miss the input field.
    """
    _log(f"enter: count={count}, target={app_name or 'frontmost'}")

    enter_script = "\n        ".join(
        ["keystroke return", "delay 0.2"] * count
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

def save_frontmost_pid() -> int:
    """Return the PID of the currently frontmost app (for restoring later)."""
    return _save_frontmost_pid()


def restore_frontmost(pid: int) -> None:
    """Re-activate the app that was frontmost before injection stole focus."""
    _restore_frontmost(pid)


def type_text(
    text: str,
    app_name: str | None = None,
    snap=None,
    settle_secs: float = 0.1,
    max_retries: int = 2,
    enter_count: int = 0,
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
        snap: TargetSnapshot (or None). Used for AX fast-path and focus verification.
        settle_secs: Focus settle delay before Cmd-V (per-app tuned via InjectionConfig).
        max_retries: Number of retries on clipboard theft.
        enter_count: Number of Enter keystrokes after paste (0 = no auto-send).

    Returns:
        True on success, False on failure. Error cue is played on failure.
    """
    if _chrome_type_text(text):
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
    return _osascript_type_text(
        text,
        app_name=app_name,
        settle_secs=settle_secs,
        expected_bundle_id=expected_bundle_id,
        max_retries=max_retries,
        enter_count=enter_count,
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


def conductor_paste_and_send(text: str, enter_count: int = 1) -> bool:
    """One-shot Conductor injection: Cmd+L → Cmd+V → Enter in a single osascript.

    Combines focus-input + clipboard-paste + submit into one subprocess call,
    saving ~0.3s over the multi-step approach. Clipboard is set via NSPasteboard
    (no subprocess) before the osascript runs.

    Returns True on success, False on failure.
    """
    _t0 = time.time()
    _log(f"conductor_paste_and_send: {len(text)} chars + Enter x{enter_count}")

    ok, expected_count = _set_clipboard(text)
    if not ok:
        _log("ERROR: failed to set clipboard")
        audio_cue("error")
        return False

    verify = get_clipboard_text()
    if verify != text:
        _log(f"ERROR: clipboard verify failed ({len(text)} vs {len(verify)} chars)")
        audio_cue("error")
        return False

    # Build single script: Cmd+L (focus input) → brief delay → Cmd+V (paste) → Enter(s)
    keystrokes = [
        'keystroke "l" using command down',  # Focus text input
        "delay 0.1",                          # Let input field focus
        'keystroke "v" using command down',   # Paste
    ]
    if enter_count > 0:
        keystrokes.append("delay 0.05")       # Brief settle after paste
        for i in range(enter_count):
            keystrokes.append("keystroke return")
            if i < enter_count - 1:
                keystrokes.append("delay 0.05")

    keystroke_block = "\n        ".join(keystrokes)

    # Always target "conductor" — System Events process name is lowercase.
    # Include 'set frontmost to true' to ensure Conductor has focus before
    # keystrokes, in case another app (e.g. Chrome) briefly stole focus.
    script = (
        f'tell application "System Events"\n'
        f'    tell process "conductor"\n'
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
        _log(f"conductor_paste_and_send: FAILED (rc={result.returncode}): "
             f"{result.stderr.decode().strip()}")
        audio_cue("error")
        return False

    _log(f"[TIMING] conductor_paste_and_send: OK in {(time.time() - _t0)*1000:.0f}ms")
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
        pb = AppKit.NSPasteboard.generalPasteboard()
        text = pb.stringForType_(AppKit.NSPasteboardTypeString)
        return str(text) if text else ""
    except Exception:
        return ""
