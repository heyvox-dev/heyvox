"""
macOS permission checking and System Settings deep-links for heyvox setup.

Checks Accessibility, Microphone, and Screen Recording permissions.
Opens the correct macOS System Preferences/Settings pane when a permission
is missing.

Requirements: CLI-03
"""

import subprocess
import sys


# macOS System Settings deep-link URLs for each permission type.
# These work on both macOS 12 (System Preferences) and 13+ (System Settings).
PERMISSION_URLS = {
    "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    "microphone": "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
    "screen_recording": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
}


def check_accessibility() -> bool:
    """Check if vox has Accessibility (AX) permission.

    Uses AXIsProcessTrusted() from ApplicationServices (PyObjC). Accessibility
    is required for the Quartz event tap used by push-to-talk (PTT).

    Returns:
        True if the current process is trusted for accessibility.

    DEF-225: an ImportError used to return True ("assume granted"). Combined
    with the missing pyobjc-framework-ApplicationServices dependency that made
    `heyvox doctor` print a green Accessibility line on every fresh install,
    regardless of the real TCC state. A check that cannot run must not report
    success — off macOS it is not applicable (True), on macOS it fails closed.
    """
    try:
        import ApplicationServices  # lazy PyObjC import
        return bool(ApplicationServices.AXIsProcessTrusted())
    except ImportError:
        # Not applicable off macOS (and in the non-macOS test env); on macOS a
        # missing module means "cannot determine", which must not read as granted.
        return sys.platform != "darwin"
    except Exception:
        return False


def check_microphone() -> bool:
    """Check if vox has Microphone permission by briefly opening an audio stream.

    Opens a minimal pyaudio stream and reads one chunk. If this succeeds,
    microphone access is granted. PermissionError or OSError means denied.

    Returns:
        True if microphone access is available.
    """
    try:
        import pyaudio  # lazy import
        pa = pyaudio.PyAudio()
        try:
            # Find default input device
            default_index = pa.get_default_input_device_info()["index"]
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=16000,
                input=True,
                input_device_index=default_index,
                frames_per_buffer=1280,
            )
            stream.read(1280, exception_on_overflow=False)
            stream.stop_stream()
            stream.close()
            return True
        except (PermissionError, OSError):
            return False
        except Exception:
            # Other errors (no default device, etc.) — treat as unknown, return True
            return True
        finally:
            pa.terminate()
    except ImportError:
        # pyaudio not installed — skip check
        return True


def check_screen_recording() -> bool:
    """Check if vox has Screen Recording permission via osascript heuristic.

    Screen Recording is required for text injection via osascript. Attempts
    to enumerate window names — this fails if Screen Recording is denied.

    Returns:
        True if Screen Recording appears to be granted.
    """
    result = subprocess.run(
        [
            "osascript",
            "-e",
            'tell application "System Events" to get name of first window of first process',
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    # If osascript succeeds (returncode 0), permission is granted.
    # If it fails with an error about "not allowed", permission is denied.
    return result.returncode == 0


def open_permission_settings(permission: str) -> None:
    """Open the macOS System Settings pane for the given permission.

    Args:
        permission: One of "accessibility", "microphone", "screen_recording".
    """
    url = PERMISSION_URLS.get(permission)
    if url:
        subprocess.run(["open", url])
