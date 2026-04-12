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

def _set_clipboard(text: str) -> bool:
    """Set clipboard text via pbcopy."""
    try:
        result = subprocess.run(
            ["pbcopy"],
            input=text.encode("utf-8"),
            capture_output=True, timeout=SUBPROCESS_TIMEOUT,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError) as e:
        _log(f"_set_clipboard failed: {e}")
        return False


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


def _osascript_type_text(text: str, app_name: str | None = None) -> None:
    """Paste text via clipboard + Cmd-V (osascript).

    When app_name is provided, targets that process directly. Briefly
    activates the target for the paste, then restores the previously
    focused app so the user isn't interrupted on multi-monitor setups.
    """
    _log(f"paste: target={app_name or 'frontmost'}, text={len(text)} chars: {text[:60]!r}")

    if not _set_clipboard(text):
        _log("ERROR: failed to set clipboard, aborting paste")
        return

    verify = get_clipboard_text()
    if verify != text:
        _log(f"ERROR: clipboard verify failed — expected {len(text)} chars, got {len(verify)} chars, aborting paste")
        return

    _log(f"paste: clipboard verified OK ({len(text)} chars)")

    frontmost_before = _get_frontmost_app()
    original_pid = _save_frontmost_pid()
    _log(f"paste: frontmost app BEFORE = {frontmost_before} (pid={original_pid})")

    time.sleep(0.05)
    if app_name:
        safe_name = app_name.replace('\\', '\\\\').replace('"', '\\"')
        script = (
            f'tell application "System Events"\n'
            f'    tell process "{safe_name}"\n'
            f'        set frontmost to true\n'
            f'        delay 0.3\n'
            f'        keystroke "v" using command down\n'
            f'    end tell\n'
            f'end tell'
        )
    else:
        script = 'tell application "System Events"\n    keystroke "v" using command down\nend tell'
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, timeout=SUBPROCESS_TIMEOUT + 2,
    )

    frontmost_after = _get_frontmost_app()
    if result.returncode != 0:
        _log(f"paste: FAILED (rc={result.returncode}): {result.stderr.decode().strip()}")
    else:
        _log(f"paste: OK → frontmost app AFTER = {frontmost_after}")

    if app_name and frontmost_after != app_name and frontmost_after != "?":
        _log(f"paste: WARNING: target was {app_name} but frontmost is {frontmost_after} — may have pasted to wrong app!")


def _osascript_press_enter(count: int, app_name: str | None = None) -> None:
    """Press Enter via osascript.

    When app_name is provided, targets that process directly via
    `tell process`. Must set frontmost briefly because macOS only
    delivers keystrokes to the frontmost process.
    """
    _log(f"enter: count={count}, target={app_name or 'frontmost'}")

    enter_script = "\n        ".join(
        ["keystroke return", "delay 0.2"] * count
    )
    if app_name:
        safe_name = app_name.replace('\\', '\\\\').replace('"', '\\"')
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


def type_text(text: str, app_name: str | None = None) -> None:
    """Insert text into an app.

    When app_name is provided, targets that specific process for paste.
    This prevents pasting into the wrong app if focus changed during STT.

    Tries Chrome extension (via Hush socket) first for direct DOM insertion.
    Falls back to clipboard + Cmd-V via osascript.
    """
    if _chrome_type_text(text):
        _log(f"type_text: done via Chrome extension ({len(text)} chars)")
        return
    _log(f"type_text: Chrome unavailable, using osascript → {app_name or 'frontmost'}")
    _osascript_type_text(text, app_name=app_name)


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


def clipboard_is_image() -> bool:
    """Return True if the current clipboard contains an image (PNG, TIFF, JPEG)."""
    result = subprocess.run(
        ["osascript", "-e", 'try\nclipboard info\non error\nreturn ""\nend try'],
        capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
    )
    out = result.stdout.strip()
    return "PNGf" in out or "TIFF" in out or "JPEG" in out


def get_clipboard_text() -> str:
    """Return the current clipboard text, or "" if clipboard is empty or not text."""
    result = subprocess.run(
        ["osascript", "-e",
         'try\nset c to (the clipboard as text)\nreturn c\non error\nreturn ""\nend try'],
        capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
    )
    return result.stdout.strip()
