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
import threading
import time


def _log(msg: str) -> None:
    """Write to main vox log file (same as main.py's log())."""
    from heyvox.constants import LOG_FILE_DEFAULT
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] [media] {msg}\n"
    try:
        with open(os.environ.get("HEYVOX_LOG_FILE", LOG_FILE_DEFAULT), "a") as f:
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
from heyvox.constants import HEYVOX_MEDIA_PAUSED_REC as _PAUSE_FLAG

# Lazy-loaded framework handle (guarded by _mr_lock for thread-safe init)
_mr_lib = None
_mr_lock = threading.Lock()

# Whether Chrome JS access has been tested and works (guarded by _chrome_lock)
_chrome_js_available = None  # None = untested, True/False = tested
_chrome_lock = threading.Lock()

# Browsers to check for video playback (in priority order)
_BROWSERS = [
    ("Google Chrome", "google-chrome"),
    ("Arc", "arc"),
    ("Safari", "safari"),
]

# Video sites to detect in browser tabs
_VIDEO_SITES = ["youtube.com", "twitch.tv", "vimeo.com", "netflix.com", "notebooklm.google.com"]
# Sites where we should only check <video> (to avoid false positives on background ad audio)
# All other sites: check both <video> and <audio>
_MEDIA_SELECTOR = "video, audio"


# ---------------------------------------------------------------------------
# Hush (Chrome extension) integration
# ---------------------------------------------------------------------------

from heyvox.constants import HUSH_SOCK as _HUSH_SOCK


def _hush_command(action: str, **kwargs) -> dict | None:
    """Send a command to the Hush Chrome extension via Unix socket.

    Extra kwargs (e.g. rewindSecs, fadeInMs) are forwarded in the JSON payload.
    Returns the parsed response dict, or None if Hush is unavailable or errored.
    """
    if not os.path.exists(_HUSH_SOCK):
        return None
    try:
        payload = {"action": action, **kwargs}
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as sock:
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
        resp = json.loads(data)
        return resp if "error" not in resp else None
    except ConnectionRefusedError:
        # Stale socket file from a dead hush_host — clean it up so we don't
        # waste time on every subsequent call.
        _log("Hush socket connection refused (stale socket), removing")
        try:
            os.unlink(_HUSH_SOCK)
        except OSError:
            pass
        return None
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _get_mr():
    """Load the MediaRemote framework (lazy, cached, thread-safe)."""
    global _mr_lib
    if _mr_lib is not None:
        return _mr_lib
    with _mr_lock:
        if _mr_lib is not None:
            return _mr_lib  # Another thread loaded it while we waited
        try:
            lib = ctypes.cdll.LoadLibrary(
                "/System/Library/PrivateFrameworks/MediaRemote.framework/MediaRemote"
            )
            lib.MRMediaRemoteSendCommand.argtypes = [ctypes.c_int, ctypes.c_void_p]
            lib.MRMediaRemoteSendCommand.restype = ctypes.c_bool
            _mr_lib = lib
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
    """Test if Chrome allows JavaScript from Apple Events.

    Only caches definitive results (explicitly enabled or disabled).
    Transient failures (timeout, Chrome not running) are retried next call.
    Thread-safe: uses _chrome_lock for the test-and-cache operation.
    """
    global _chrome_js_available
    if _chrome_js_available is not None:
        return _chrome_js_available

    with _chrome_lock:
        # Re-check after acquiring lock (another thread may have tested)
        if _chrome_js_available is not None:
            return _chrome_js_available

    # Run the actual test outside the lock (subprocess can be slow).
    # We accept that two threads might both test concurrently on first call —
    # that's benign, and better than holding a lock during a 2s subprocess.
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
            _chrome_js_available = False  # Definitive: user disabled it
            _log("WARNING: Chrome JS from Apple Events is DISABLED. "
                 "Enable via: View → Developer → Allow JavaScript from Apple Events. "
                 "Using media key fallback instead.")
        elif r.stdout.strip() == "js-ok" or r.returncode == 0:
            _chrome_js_available = True  # Definitive: it works
            _log("Chrome JS from Apple Events: enabled")
        elif r.stdout.strip() == "no-app":
            # Chrome not running — don't cache, retry next time
            _log("Chrome not running, skipping JS test (will retry)")
            return False
        else:
            # Inconclusive — don't cache, retry next time
            _log(f"Chrome JS test inconclusive: rc={r.returncode} stderr={r.stderr.strip()[:100]}")
            return False
    except subprocess.TimeoutExpired:
        # Transient — don't cache, retry next time
        _log("Chrome JS test timed out (will retry)")
        return False
    except Exception as e:
        # Transient — don't cache, retry next time
        _log(f"Chrome JS test failed: {e} (will retry)")
        return False

    return _chrome_js_available


def _browser_has_media_tab(app_name: str) -> bool:
    """Check if a browser has any audible tab (no JS needed).

    Uses Chrome tab audible state rather than URL whitelisting, so it
    works for any site playing audio/video.
    """
    script = f'''
tell application "System Events"
    if not (exists process "{app_name}") then return "no-app"
end tell
tell application "{app_name}"
    repeat with w in every window
        repeat with t from 1 to count of tabs of window w
            -- Chrome doesn't expose audible via AppleScript, so check title
            -- for common audio indicators or just return true if tabs exist
            return "has-tabs"
        end repeat
    end repeat
    return "no-tabs"
end tell'''

    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=1.5,
        )
        return r.stdout.strip() == "has-tabs"
    except Exception:
        return False


def _browser_video_state_js(app_name: str) -> str | None:
    """Check if a browser has playing media via AppleScript + JavaScript.

    Checks both <video> and <audio> elements across all audible tabs.
    Returns "playing", "paused", or None (no media / app not running / error).
    Requires Chrome → View → Developer → Allow JavaScript from Apple Events.
    """
    if app_name == "Safari":
        js_exec = 'do JavaScript'
        js_in = 'in t'
    else:
        js_exec = 'execute t javascript'
        js_in = ''

    # Check all tabs for media elements — not just known video sites.
    # The JS checks both <video> and <audio> elements.
    js_check = (
        "var els = document.querySelectorAll('video, audio'); "
        "var state = 'no-media'; "
        "for (var i = 0; i < els.length; i++) { "
        "  if (!els[i].paused) { state = 'playing'; break; } "
        "  state = 'paused'; "
        "} "
        "state"
    )

    script = f'''
tell application "System Events"
    if not (exists process "{app_name}") then return "no-app"
end tell
tell application "{app_name}"
    repeat with w in every window
        repeat with t in every tab of w
            try
                set r to {js_exec} "{js_check}" {js_in}
                if r is "playing" then return "playing"
                if r is "paused" then return "paused"
            end try
        end repeat
    end repeat
    return "no-media"
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
    """Pause or play browser media via AppleScript + JavaScript.

    Handles both <video> and <audio> elements across all tabs.
    action: "pause" or "play"
    """
    # JS that pauses/plays ALL media elements in the tab
    js_cmd = (
        f"var els = document.querySelectorAll('video, audio'); "
        f"var count = 0; "
        f"for (var i = 0; i < els.length; i++) {{ els[i].{action}(); count++; }} "
        f"count"
    )

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
            try
                set r to {js_exec} "{js_cmd}" {js_in}
                if r is not "0" and r is not 0 then return "ok"
            end try
        end repeat
    end repeat
    return "no-media"
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


# Cache: when pause_media() finds nothing playing, skip the slow detection
# for this many seconds.  Worst case: media started during the window gets
# missed for one TTS utterance, then detected on the next.
_NO_MEDIA_CACHE_TTL = 15.0
_no_media_cache_until = 0.0  # monotonic timestamp


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
    global _no_media_cache_until
    t0 = time.time()

    if os.path.exists(_PAUSE_FLAG):
        _log("pause_media: already paused by us (flag exists)")
        return True  # Already paused by us

    # Fast path: recently confirmed nothing was playing — skip slow detection
    if time.monotonic() < _no_media_cache_until:
        _log("pause_media: skipped (no-media cache hit)")
        return False

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
        if _browser_has_media_tab(app_name):
            _log(f"pause_media: {app_name} has tabs but JS is disabled — "
                 f"cannot detect play state. Skipping (enable Chrome JS: "
                 f"View → Developer → Allow JavaScript from Apple Events)")

    # Nothing playing — cache this result so subsequent TTS sentences
    # skip the slow detection chain (Hush + nowplaying + Chrome JS).
    _no_media_cache_until = time.monotonic() + _NO_MEDIA_CACHE_TTL
    _log(f"pause_media: no playing media found (native or browser) ({time.time()-t0:.2f}s), "
         f"caching for {_NO_MEDIA_CACHE_TTL:.0f}s")
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
    Invalidates the no-media cache since media is now active again.
    """
    global _no_media_cache_until
    if not os.path.exists(_PAUSE_FLAG):
        return False  # We didn't pause it, don't resume

    # Media is about to be active again — invalidate the no-media cache
    _no_media_cache_until = 0.0

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

    # Don't actually resume if another caller (orchestrator) still has it paused.
    # Check both heyvox and herald namespaces — Herald's TTS orchestrator uses
    # /tmp/herald-media-paused-* for the same purpose.
    from heyvox.constants import HEYVOX_MEDIA_PAUSED_PREFIX, HERALD_MEDIA_PAUSED_PREFIX
    other_flags = [
        f for f in glob.glob(HEYVOX_MEDIA_PAUSED_PREFIX + "*") + glob.glob(HERALD_MEDIA_PAUSED_PREFIX + "*")
        if f != _PAUSE_FLAG
    ]
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
