"""
Text injection into the focused macOS application.

Uses clipboard + Cmd-V for speed (keystroke simulation is very slow for
long text). Saves and restores the previous clipboard content around each paste.
"""

import subprocess
import time


# Max seconds to wait for osascript subprocess to complete
SUBPROCESS_TIMEOUT = 5


def _set_clipboard(text: str) -> bool:
    """Set clipboard text via pbcopy (more robust than osascript for special chars).

    Returns True on success, False on failure.
    """
    try:
        result = subprocess.run(
            ["pbcopy"],
            input=text.encode("utf-8"),
            capture_output=True, timeout=SUBPROCESS_TIMEOUT,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError) as e:
        import sys
        print(f"[injection] _set_clipboard failed: {e}", file=sys.stderr)
        return False


def type_text(text: str) -> None:
    """Paste text into the focused app via clipboard + Cmd-V.

    Saves current clipboard content, sets it to text, sends Cmd-V,
    then restores the original clipboard.

    Args:
        text: Text to inject.
    """
    import sys

    old_clip = get_clipboard_text()
    old_was_image = clipboard_is_image()

    # Set clipboard via pbcopy (handles all characters safely)
    if not _set_clipboard(text):
        print("[injection] ERROR: failed to set clipboard, aborting paste", file=sys.stderr)
        return

    # Verify clipboard was actually set before pasting
    verify = get_clipboard_text()
    if verify != text:
        print(f"[injection] ERROR: clipboard verify failed — expected {len(text)} chars, "
              f"got {len(verify)} chars, aborting paste", file=sys.stderr)
        return

    time.sleep(0.05)
    subprocess.run(
        ["osascript", "-e", 'tell application "System Events"\n    keystroke "v" using command down\nend tell'],
        capture_output=True, timeout=SUBPROCESS_TIMEOUT,
    )
    time.sleep(0.3)  # Electron apps need time to read clipboard after Cmd-V

    # Restore previous clipboard
    if old_was_image:
        pass  # Cannot restore image clipboard from Python — leave as-is
    elif old_clip:
        _set_clipboard(old_clip)


def press_enter(count: int = 1, app_name: str | None = None) -> None:
    """Press the Return key N times via osascript.

    When app_name is provided, targets that specific app's process in
    System Events rather than relying on the frontmost app. This prevents
    Enter going to the wrong window when focus is stolen between paste and
    Enter (common with multiple Conductor workspaces open).

    Args:
        count: Number of times to press Return.
        app_name: Target application name. If None, sends to frontmost.
    """
    enter_script = "\n        ".join(
        ["keystroke return", "delay 0.2"] * count
    )
    if app_name:
        # Target the specific app's process — robust against focus changes
        script = (
            f'tell application "System Events"\n'
            f'    tell process "{app_name}"\n'
            f'        {enter_script}\n'
            f'    end tell\n'
            f'end tell'
        )
    else:
        script = f'tell application "System Events"\n    {enter_script}\nend tell'
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, timeout=SUBPROCESS_TIMEOUT,
    )
    if result.returncode != 0:
        import sys
        print(f"[injection] press_enter failed (rc={result.returncode}): "
              f"{result.stderr.decode().strip()}", file=sys.stderr)


def focus_app(app_name: str) -> None:
    """Bring an application to the front.

    Args:
        app_name: Application name as it appears in the Dock/Activity Monitor.
    """
    subprocess.run(
        ["osascript", "-e", f'tell application "{app_name}" to activate'],
        capture_output=True, timeout=SUBPROCESS_TIMEOUT,
    )


def focus_input(app_name: str, shortcuts: dict[str, str] | None = None) -> None:
    """Focus the text input field in a known app via keyboard shortcut.

    Args:
        app_name: Application name to match (case-insensitive).
        shortcuts: Map of lowercase app name → key for Cmd+key shortcut.
            Loaded from config.yaml input.focus_shortcuts if not provided.
            Example: {"cursor": "l", "windsurf": "l"}
    """
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
