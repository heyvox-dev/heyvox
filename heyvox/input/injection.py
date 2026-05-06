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
    try:
        print(f"[injection] {msg}", file=sys.stderr, flush=True)
    except (BrokenPipeError, OSError):
        pass


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
    # Skip for workspace-managed Electron/Tauri apps — AXValue set returns
    # success but doesn't update the web framework's internal state, so Enter
    # submits empty text. Presence of conductor_workspace_id (set by adapter
    # at capture time) marks a workspace-managed app.
    conductor_ws = getattr(snap, "conductor_workspace_id", None)
    if conductor_ws:
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
        err, focused = AXUIElementCopyAttributeValue(
            ax_app, "AXFocusedUIElement", None
        )
        if err != 0 or focused is None:
            return False
        err = AXUIElementSetAttributeValue(focused, "AXValue", text)
        if err == 0:
            _log(f"AX fast-path: injected {len(text)} chars into {leaf_role}")
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


def app_fast_paste(profile, text: str, enter_count: int | None = None) -> bool:
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
        pb = AppKit.NSPasteboard.generalPasteboard()
        text = pb.stringForType_(AppKit.NSPasteboardTypeString)
        return str(text) if text else ""
    except Exception:
        return ""
