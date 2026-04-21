"""
Media playback control for heyvox.

Pauses system media (YouTube, Spotify, etc.) during TTS playback and recording,
resumes afterward.

Detection & control strategy (in priority order):
1. Hush (Chrome extension) — for browser media via Unix socket
2. nowplaying-cli + MediaRemote — for native apps (Spotify, Music, Podcasts)

If Hush isn't installed/running and the media is browser-based, we no longer
try to guess via Chrome JavaScript-from-AppleEvents or blindly toggle the
media key — those tiers were unreliable, can actively *start* music that
wasn't playing, and the fix is "install Hush." We log a one-time banner the
first time we see Hush missing while trying to pause.

Only resumes if we were the ones who paused — tracked via the pause-flag file.
This prevents resuming media the user manually paused.

Configurable via ``tts.pause_media`` in config.yaml.
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
# Contents: "hush" (Hush extension) or "mr" (MediaRemote).
from heyvox.constants import HEYVOX_MEDIA_PAUSED_REC as _PAUSE_FLAG

# Lazy-loaded framework handle (guarded by _mr_lock for thread-safe init)
_mr_lib = None
_mr_lock = threading.Lock()

# One-time banner when Hush socket is missing and we had browser media to pause.
_hush_missing_banner_shown = False


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
        # ECONNREFUSED can be transient (accept backlog full, host briefly
        # hung). Do NOT unlink — the Hush host owns the socket lifecycle
        # (DEF-039, DEF-041). Unlinking strands the live host holding an
        # orphaned inode, and Chrome NativeMessaging won't respawn it because
        # the host is still alive. If the host truly died, its atexit handler
        # clears the file; Chrome relaunches on next extension action.
        _log("Hush socket connection refused (leaving file, host owns lifecycle)")
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


# Cache: when pause_media() finds nothing playing, skip the slow detection
# for this many seconds.  Worst case: media started during the window gets
# missed for one TTS utterance, then detected on the next.
_NO_MEDIA_CACHE_TTL = 15.0
_no_media_cache_until = 0.0  # monotonic timestamp


def pause_media() -> bool:
    """Pause system media and set flag so we know we did it.

    Strategy:
    1. If Hush extension is running → use it for browser media.
    2. If nowplaying-cli detects active playback → use MediaRemote.
    3. If media is registered but paused → don't touch (user paused it).

    Browser media without Hush is not handled — the user should install Hush
    if they want browser audio paused during TTS/recording. See heyvox/hush/.
    """
    global _no_media_cache_until, _hush_missing_banner_shown
    t0 = time.time()

    if os.path.exists(_PAUSE_FLAG):
        _log("pause_media: already paused by us (flag exists)")
        return True  # Already paused by us

    # Fast path: recently confirmed nothing was playing — skip slow detection
    if time.monotonic() < _no_media_cache_until:
        _log("pause_media: skipped (no-media cache hit)")
        return False

    # --- Tier 1: Try Hush for browser media ---
    hush_resp = _hush_command("pause")
    if hush_resp is not None:
        paused_count = hush_resp.get("pausedCount", 0)
        if paused_count > 0:
            with open(_PAUSE_FLAG, "w") as f:
                f.write("hush")
            _log(f"pause_media: paused {paused_count} browser tab(s) via Hush ({time.time()-t0:.2f}s)")
            return True
        _log("pause_media: Hush available but no browser media playing")
    elif not os.path.exists(_HUSH_SOCK) and not _hush_missing_banner_shown:
        _hush_missing_banner_shown = True
        _log(
            "pause_media: Hush not installed/running — browser media will not be "
            "paused. Install via `heyvox setup` or the Hush Chrome extension."
        )

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

    # Nothing we can control — cache this result so subsequent TTS sentences
    # skip the slow detection chain (Hush + nowplaying).
    _no_media_cache_until = time.monotonic() + _NO_MEDIA_CACHE_TTL
    _log(f"pause_media: no playing media found ({time.time()-t0:.2f}s), "
         f"caching for {_NO_MEDIA_CACHE_TTL:.0f}s")
    return False


# Delay before resuming media (seconds). Gives natural breathing room.
RESUME_DELAY = 1.0
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

    return False
