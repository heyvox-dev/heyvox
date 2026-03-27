"""
Text injection into the focused macOS application.

Uses clipboard + Cmd-V for speed (keystroke simulation is very slow for
long text). Saves and restores the previous clipboard content around each paste.
"""

import subprocess
import time


# Max seconds to wait for osascript subprocess to complete
SUBPROCESS_TIMEOUT = 5


def type_text(text: str) -> None:
    """Paste text into the focused app via clipboard + Cmd-V.

    Saves current clipboard content, sets it to text, sends Cmd-V,
    then restores the original clipboard.

    Args:
        text: Text to inject.
    """
    old_clip = get_clipboard_text()
    old_was_image = clipboard_is_image()

    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(
        ["osascript", "-e", f'set the clipboard to "{escaped}"'],
        capture_output=True, timeout=SUBPROCESS_TIMEOUT,
    )
    time.sleep(0.05)
    subprocess.run(
        ["osascript", "-e", 'tell application "System Events"\n    keystroke "v" using command down\nend tell'],
        capture_output=True, timeout=SUBPROCESS_TIMEOUT,
    )
    time.sleep(0.1)

    # Restore previous clipboard
    if old_was_image:
        pass  # Cannot restore image clipboard from Python — leave as-is
    elif old_clip:
        old_escaped = old_clip.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.run(
            ["osascript", "-e", f'set the clipboard to "{old_escaped}"'],
            capture_output=True, timeout=SUBPROCESS_TIMEOUT,
        )


def press_enter(count: int = 1) -> None:
    """Press the Return key N times via osascript.

    Args:
        count: Number of times to press Return.
    """
    enter_script = "\n    ".join(
        ["keystroke return\n    delay 0.15"] * count
    )
    subprocess.run(
        ["osascript", "-e", f'tell application "System Events"\n    {enter_script}\nend tell'],
        capture_output=True, timeout=SUBPROCESS_TIMEOUT,
    )


def focus_app(app_name: str) -> None:
    """Bring an application to the front.

    Args:
        app_name: Application name as it appears in the Dock/Activity Monitor.
    """
    subprocess.run(
        ["osascript", "-e", f'tell application "{app_name}" to activate'],
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
