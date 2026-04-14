"""
Media playback control for heyvox.

Pauses system media (YouTube, Spotify, etc.) during TTS playback and recording,
resumes afterward.

Detection & control strategy (in priority order):
1. Hush (Chrome extension) — for browser media via Unix socket (most reliable)
2. nowplaying-cli + MediaRemote — for native apps (Spotify, Music, Podcasts)
3. AppleScript + Chrome JavaScript — for browser media if Hush unavailable
   Requires: Chrome → View → Developer → Allow JavaScript from Apple Events
4. Fallback: Chrome tab URL detection + media key — if JS is disabled

Hush is optional — if not running, falls back to existing methods.
Only resumes if we were the ones who paused — tracked via /tmp/heyvox-media-paused flag.
This prevents resuming media the user manually paused.

Configurable via tts.pause_media in config.yaml.
"""

import ctypes
import glob
import json
import os
import socket as _socket
import subprocess
import time

# User-scoped temp dir (cannot import heyvox.constants — may run standalone).
_TMP = os.environ.get("TMPDIR", "/tmp").rstrip("/")


def _log(msg: str) -> None:
    """Write to main vox log file (same as main.py's log())."""
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] [media] {msg}\n"
    try:
        with open(os.environ.get("VOX_LOG_FILE", f"{_TMP}/heyvox.log"), "a") as f:
            f.write(line)
    except OSError:
        pass

# MediaRemote command constants
_MR_PLAY = 0
_MR_PAUSE = 1

# Flag file to track whether vox (recording) paused the media.
# The TTS orchestrator uses /tmp/heyvox-media-paused-orch separately.
# Contents: "hush" (Hush extension), "mr" (MediaRemote), "chrome-js" (Chrome JS),
#           "media-key" (media key toggle)
_PAUSE_FLAG = f"{_TMP}/heyvox-media-paused-rec"

# Lazy-loaded framework handle
_mr_lib = None

# Whether Chrome JS access has been tested and works
_chrome_js_available = None  # None = untested, True/False = tested

# Browsers to check for video playback (in priority order)
_BROWSERS = [
    ("Google Chrome", "google-chrome"),
    ("Arc", "arc"),
    ("Safari", "safari"),
]

# Video sites to detect in browser tabs
_VIDEO_SITES = ["youtube.com", "twitch.tv", "vimeo.com", "netflix.com"]


# ---------------------------------------------------------------------------
# Hush (Chrome extension) integration
# ---------------------------------------------------------------------------

_HUSH_SOCK = f"{_TMP}/hush.sock"


def _hush_command(action: str, **kwargs) -> dict | None:
    """Send a command to the Hush Chrome extension via Unix socket.

    Extra kwargs (e.g. rewindSecs, fadeInMs) are forwarded in the JSON payload.
    Returns the parsed response dict, or None if Hush is unavailable or errored.
    """
    if not os.path.exists(_HUSH_SOCK):
        return None
    try:
        payload = {"action": action, **kwargs}
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        sock.settimeout(3.0)
        sock.connect(_HUSH_SOCK)
        sock.sendall(json.dumps(payload).encode() + b"\n")
        data = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break
        sock.close()
        resp = json.loads(data)
        return resp if "error" not in resp else None
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _get_mr():
    """Load the MediaRemote framework (lazy, cached)."""
    global _mr_lib
    if _mr_lib is None:
        try:
            _mr_lib = ctypes.cdll.LoadLibrary(
                "/System/Library/PrivateFrameworks/MediaRemote.framework/MediaRemote"
            )
            _mr_lib.MRMediaRemoteSendCommand.argtypes = [ctypes.c_int, ctypes.c_void_p]
            _mr_lib.MRMediaRemoteSendCommand.restype = ctypes.c_bool
        except OSError:
            _log("MediaRemote framework not available — media pause disabled")
            return None
    return _mr_lib


def _is_media_playing_native() -> bool | None:
    """Check if system media is currently playing via nowplaying-cli.

    Returns True if media is actively playing, False if paused/stopped.
    Returns None if nowplaying-cli returns "null" (no media session registered).
    """
    try:
        r = subprocess.run(
            ["nowplaying-cli", "get", "playbackRate"],
            capture_output=True, text=True, timeout=0.5,
        )
        rate = r.stdout.strip()
        if rate in ("null", ""):
            return None  # No media session — unknown state
        return rate != "0"  # "0" = paused, "1" = playing
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return None


def _test_chrome_js_access() -> bool:
    """Test if Chrome allows JavaScript from Apple Events. Cached after first call."""
    global _chrome_js_available
    if _chrome_js_available is not None:
        return _chrome_js_available

    try:
        r = subprocess.run(
            ["osascript", "-e", '''
tell application "System Events"
    if not (exists process "Google Chrome") then return "no-app"
end tell
tell application "Google Chrome"
    execute front window's active tab javascript "'js-ok'"
end tell'''],
            capture_output=True, text=True, timeout=2.0,
        )
        if "Executing JavaScript through AppleScript is turned off" in r.stderr:
            _chrome_js_available = False
            _log("WARNING: Chrome JS from Apple Events is DISABLED. "
                 "Enable via: View → Developer → Allow JavaScript from Apple Events. "
                 "Using media key fallback instead.")
        elif r.stdout.strip() == "js-ok" or r.returncode == 0:
            _chrome_js_available = True
            _log("Chrome JS from Apple Events: enabled")
        else:
            _chrome_js_available = False
            _log(f"Chrome JS test inconclusive: rc={r.returncode} stderr={r.stderr.strip()[:100]}")
    except Exception as e:
        _chrome_js_available = False
        _log(f"Chrome JS test failed: {e}")

    return _chrome_js_available


def _browser_has_video_tab(app_name: str) -> bool:
    """Check if a browser has a video site tab open (no JS needed)."""
    site_checks = " or ".join(f'u contains "{site}"' for site in _VIDEO_SITES)

    if app_name == "Safari":
        script = f'''
tell application "System Events"
    if not (exists process "Safari") then return "no-app"
end tell
tell application "Safari"
    repeat with w in every window
        repeat with t in every tab of w
            set u to URL of t
            if {site_checks} then return "has-video"
        end repeat
    end repeat
    return "no-video"
end tell'''
    else:
        script = f'''
tell application "System Events"
    if not (exists process "{app_name}") then return "no-app"
end tell
tell application "{app_name}"
    repeat with w in every window
        repeat with t in every tab of w
            set u to URL of t
            if {site_checks} then return "has-video"
        end repeat
    end repeat
    return "no-video"
end tell'''

    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=1.5,
        )
        return r.stdout.strip() == "has-video"
    except Exception:
        return False


def _browser_video_state_js(app_name: str) -> str | None:
    """Check if a browser has a playing video via AppleScript + JavaScript.

    Returns "playing", "paused", or None (no video / app not running / error).
    Requires Chrome → View → Developer → Allow JavaScript from Apple Events.
    """
    site_checks = " or ".join(f'u contains "{site}"' for site in _VIDEO_SITES)

    if app_name == "Safari":
        js_exec = 'do JavaScript'
        js_in = 'in t'
    else:
        js_exec = 'execute t javascript'
        js_in = ''

    script = f'''
tell application "System Events"
    if not (exists process "{app_name}") then return "no-app"
end tell
tell application "{app_name}"
    repeat with w in every window
        repeat with t in every tab of w
            set u to URL of t
            if {site_checks} then
                set r to {js_exec} "document.querySelector('video') ? (document.querySelector('video').paused ? 'paused' : 'playing') : 'no-video'" {js_in}
                if r is "playing" or r is "paused" then return r
            end if
        end repeat
    end repeat
    return "no-video"
end tell'''

    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=1.5,
        )
        result = r.stdout.strip()
        if result in ("playing", "paused"):
            return result
        if r.stderr and "JavaScript" in r.stderr:
            _log(f"_browser_video_state_js: JS disabled in {app_name}: {r.stderr.strip()[:80]}")
        return None
    except subprocess.TimeoutExpired:
        _log(f"_browser_video_state_js: timeout for {app_name}")
        return None
    except Exception as e:
        _log(f"_browser_video_state_js: error for {app_name}: {e}")
        return None


def _browser_video_control_js(app_name: str, action: str) -> bool:
    """Pause or play browser video via AppleScript + JavaScript.

    action: "pause" or "play"
    """
    site_checks = " or ".join(f'u contains "{site}"' for site in _VIDEO_SITES)
    js_cmd = f"document.querySelector('video')?.{action}()"

    if app_name == "Safari":
        js_exec = 'do JavaScript'
        js_in = 'in t'
    else:
        js_exec = 'execute t javascript'
        js_in = ''

    script = f'''
tell application "{app_name}"
    repeat with w in every window
        repeat with t in every tab of w
            set u to URL of t
            if {site_checks} then
                {js_exec} "{js_cmd}" {js_in}
                return "ok"
            end if
        end repeat
    end repeat
    return "no-video"
end tell'''

    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=1.5,
        )
        success = r.stdout.strip() == "ok"
        if not success and r.stderr:
            _log(f"_browser_video_control_js({app_name}, {action}): {r.stderr.strip()[:80]}")
        return success
    except Exception as e:
        _log(f"_browser_video_control_js({app_name}, {action}): {e}")
        return False


def _send_media_key():
    """Send the system play/pause media key via Quartz.

    This is a TOGGLE — use only when you're sure media is playing (to pause)
    or paused by us (to resume). Never use blindly.
    """
    try:
        import Quartz

        # Key down
        e1 = Quartz.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            14, (0, 0), 0xA00, 0, 0, None, 8, (16 << 16) | (0xA << 8), -1
        )
        Quartz.CGEventPost(0, e1.CGEvent())
        time.sleep(0.05)
        # Key up
        e2 = Quartz.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            14, (0, 0), 0xB00, 0, 0, None, 8, (16 << 16) | (0xB << 8), -1
        )
        Quartz.CGEventPost(0, e2.CGEvent())
        return True
    except Exception as e:
        _log(f"_send_media_key failed: {e}")
        return False


def pause_media() -> bool:
    """Pause system media and set flag so we know we did it.

    Strategy:
    1. If Hush extension is running → use it for browser media (most reliable)
    2. If nowplaying-cli detects active playback → use MediaRemote (precise)
    3. If media is registered but paused → don't touch (user paused it)
    4. If no native media → check browsers:
       a. If Chrome JS enabled → use video.pause() (precise)
       b. If Chrome JS disabled → detect video tab + send media key (toggle)
    """
    t0 = time.time()

    if os.path.exists(_PAUSE_FLAG):
        _log("pause_media: already paused by us (flag exists)")
        return True  # Already paused by us

    # --- Tier 1: Try Hush for browser media (most reliable) ---
    hush_resp = _hush_command("pause")
    if hush_resp is not None:
        paused_count = hush_resp.get("pausedCount", 0)
        if paused_count > 0:
            with open(_PAUSE_FLAG, "w") as f:
                f.write("hush")
            _log(f"pause_media: paused {paused_count} browser tab(s) via Hush ({time.time()-t0:.2f}s)")
            return True
        _log("pause_media: Hush available but no browser media playing")

    # --- Tier 2: Try native media (Spotify, Music, Podcasts) ---
    native_state = _is_media_playing_native()
    _log(f"pause_media: native_state={native_state} ({time.time()-t0:.2f}s)")

    if native_state is True:
        mr = _get_mr()
        if mr is not None:
            try:
                result = mr.MRMediaRemoteSendCommand(_MR_PAUSE, None)
                if result:
                    with open(_PAUSE_FLAG, "w") as f:
                        f.write("mr")
                    _log("pause_media: paused via MediaRemote")
                    return True
            except Exception as e:
                _log(f"pause_media: MediaRemote failed: {e}")
    elif native_state is False:
        _log("pause_media: native media paused by user, skipping")
        return False

    # --- Tier 3: No native media session: check browsers (fallback) ---
    _log("pause_media: no native media, checking browsers...")

    for app_name, short in _BROWSERS:
        # Try JS-based detection first (precise)
        if app_name == "Google Chrome" and _test_chrome_js_access():
            state = _browser_video_state_js(app_name)
            _log(f"pause_media: {app_name} JS state={state}")
            if state == "playing":
                if _browser_video_control_js(app_name, "pause"):
                    with open(_PAUSE_FLAG, "w") as f:
                        f.write("chrome-js")
                    _log(f"pause_media: paused {app_name} video via JS")
                    return True
            elif state == "paused":
                _log(f"pause_media: {app_name} video already paused by user")
                return False
            continue

        # Fallback: check if video tab exists + use media key
        # DISABLED: media key is a blind toggle — it can START paused media.
        # Only use this path if we can confirm media is actually playing,
        # which requires Chrome JS access. Log a warning instead.
        if _browser_has_video_tab(app_name):
            _log(f"pause_media: {app_name} has video tab but JS is disabled — "
                 f"cannot detect play state. Skipping (enable Chrome JS: "
                 f"View → Developer → Allow JavaScript from Apple Events)")

    _log(f"pause_media: no playing media found (native or browser) ({time.time()-t0:.2f}s)")
    return False


def _send_rewind(secs: int = 4):
    """Rewind browser media by sending left-arrow keys.

    Each left-arrow = 5 seconds back in YouTube. Only targets
    browsers (Chrome, Safari, Arc) since native apps use MediaRemote.
    """
    presses = max(1, (secs + 4) // 5)
    try:
        for _ in range(presses):
            subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to key code 123'],
                capture_output=True, timeout=1,
            )
            time.sleep(0.1)
    except Exception:
        pass


# Delay before resuming media (seconds). Gives natural breathing room.
RESUME_DELAY = 1.0
# Rewind browser media by this many seconds on resume.
REWIND_SECS = 4
# Hush resume: rewind N seconds before playing.
HUSH_REWIND_SECS = 3
# Hush resume: fade volume in over N milliseconds (0 = instant).
HUSH_FADE_IN_MS = 1000


def resume_media() -> bool:
    """Resume system media, but only if we were the ones who paused it.

    Waits RESUME_DELAY seconds before resuming for natural feel.
    """
    if not os.path.exists(_PAUSE_FLAG):
        return False  # We didn't pause it, don't resume

    # Read which method we used to pause
    try:
        method = open(_PAUSE_FLAG).read().strip()
    except OSError:
        method = "mr"

    _log(f"resume_media: method={method}")

    # Remove our flag
    try:
        os.unlink(_PAUSE_FLAG)
    except OSError:
        pass

    # Don't actually resume if another caller (orchestrator) still has it paused
    other_flags = [f for f in glob.glob(f"{_TMP}/heyvox-media-paused-*") if f != _PAUSE_FLAG]
    if other_flags:
        _log(f"resume_media: other pause flags exist {other_flags}, not resuming")
        return False

    # Graceful delay before resuming
    time.sleep(RESUME_DELAY)

    if method == "hush":
        hush_resp = _hush_command(
            "resume", rewindSecs=HUSH_REWIND_SECS, fadeInMs=HUSH_FADE_IN_MS
        )
        if hush_resp is not None:
            _log(f"resume_media: Hush resume result={hush_resp}")
            return True
        # Hush unavailable — can't fall back since we don't know what was playing
        _log("resume_media: Hush unavailable for resume, media may stay paused")
        return False

    if method == "mr":
        mr = _get_mr()
        if mr is not None:
            try:
                result = mr.MRMediaRemoteSendCommand(_MR_PLAY, None)
                _log(f"resume_media: MediaRemote play result={result}")
                return result
            except Exception as e:
                _log(f"resume_media: MediaRemote failed: {e}")
                return False

    elif method == "chrome-js":
        success = _browser_video_control_js("Google Chrome", "play")
        _log(f"resume_media: Chrome JS play result={success}")
        return success

    elif method == "media-key":
        _log("resume_media: sending media key to resume")
        return _send_media_key()

    return False
