"""
Media playback control for heyvox.

Pauses system media (YouTube, Spotify, etc.) during TTS playback and recording,
resumes afterward.

Detection & control strategy (in priority order):
1. Hush (Chrome extension) — for browser media via Unix socket
2. Scriptable native players (VLC, QuickTime Player, Music, Spotify, ...) —
   driven directly via AppleScript, configured in ``tts.media_players``.
   macOS 15.4+ reportedly hides now-playing data from unentitled processes, so tier 3
   sees nothing for players like VLC (DEF-264).
3. nowplaying-cli + MediaRemote — for native apps that publish Now Playing

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
    """Write to main vox log file (same as main.py's log()).

    DEF-126: prefer the config-resolved log_file path. main.py uses
    `load_config().log_file` (e.g. /tmp/heyvox.log on this host), but this
    module used to fall back to LOG_FILE_DEFAULT which resolves to
    $TMPDIR/heyvox.log — so every [media] line landed in a parallel file and
    was invisible to anyone tailing the main log. Pattern P-log-path-split.
    """
    from heyvox.constants import LOG_FILE_DEFAULT
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] [media] {msg}\n"
    path = os.environ.get("HEYVOX_LOG_FILE")
    if not path:
        try:
            from heyvox.config import load_config
            path = load_config().log_file or LOG_FILE_DEFAULT
        except Exception:
            path = LOG_FILE_DEFAULT
    try:
        with open(path, "a") as f:
            f.write(line)
    except OSError:
        pass

# MediaRemote command constants
_MR_PLAY = 0
_MR_PAUSE = 1

# Flag file to track whether vox (recording) paused the media.
# Shared with Herald orchestrator's _media_pause() — both callers go through
# pause_media() in this module and write the same path (HEYVOX_MEDIA_PAUSED_REC).
# Herald additionally drops its own namespace flag (/tmp/herald-media-paused-*)
# from herald-media.sh for the cross-caller "keep paused" handoff checked below.
# Contents: comma-separated tokens — "hush" (Hush extension), "mr"
# (MediaRemote), "app:<bundle_id>" (scriptable player paused via AppleScript).
from heyvox.constants import HEYVOX_MEDIA_PAUSED_REC as _PAUSE_FLAG

# Lazy-loaded framework handle (guarded by _mr_lock for thread-safe init)
_mr_lib = None
_mr_lock = threading.Lock()

# One-time banner when Hush socket is missing and we had browser media to pause.
_hush_missing_banner_shown = False


# ---------------------------------------------------------------------------
# Hush (Chrome extension) integration
# ---------------------------------------------------------------------------

from heyvox.constants import HUSH_SOCK_GLOB as _HUSH_SOCK_GLOB


def _hush_pid_alive(sock_path: str) -> bool:
    """True if the PID embedded in hush-<pid>.sock is still running.

    DEF-105: PID-suffixed sockets let us detect zombies (host crashed without
    atexit cleanup) and unlink them, which we couldn't safely do for the
    legacy single-path socket because we couldn't tell zombie from alive.
    """
    name = os.path.basename(sock_path)
    if not (name.startswith("hush-") and name.endswith(".sock")):
        return True  # legacy symlink or unrecognised — leave alone
    try:
        pid = int(name[len("hush-"):-len(".sock")])
        os.kill(pid, 0)
        return True
    except (ValueError, OSError):
        return False


def _hush_send_one(sock_path: str, payload_bytes: bytes) -> dict | None:
    """Send a single command to one hush_host socket and parse its response."""
    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as sock:
            sock.settimeout(3.0)
            sock.connect(sock_path)
            sock.sendall(payload_bytes)
            data = b""
            while b"\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        resp = json.loads(data)
        return resp if "error" not in resp else None
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _hush_command(action: str, **kwargs) -> dict | None:
    """Broadcast a command to every live Hush host and merge responses.

    DEF-105: each Chrome profile spawns its own hush_host; we glob-match the
    PID-suffixed sockets and aggregate ``tabs`` / ``pausedCount`` so a pause
    reaches whichever profile owns the audible YouTube tab. Stale sockets
    (PID dead) are unlinked on the way.
    """
    socks = sorted(glob.glob(_HUSH_SOCK_GLOB))
    if not socks:
        return None

    live_socks = []
    for path in socks:
        if _hush_pid_alive(path):
            live_socks.append(path)
        else:
            try:
                os.unlink(path)
                _log(f"Hush stale socket unlinked: {path}")
            except OSError:
                pass

    if not live_socks:
        return None

    payload_bytes = json.dumps({"action": action, **kwargs}).encode() + b"\n"
    merged = {"state": "idle", "tabs": [], "pausedCount": 0}
    any_ok = False

    for sock_path in live_socks:
        resp = _hush_send_one(sock_path, payload_bytes)
        if resp is None:
            continue
        any_ok = True
        tabs = resp.get("tabs")
        if isinstance(tabs, list):
            merged["tabs"].extend(tabs)
        merged["pausedCount"] += int(resp.get("pausedCount", 0) or 0)
        state = resp.get("state")
        if state == "paused":
            merged["state"] = "paused"
        elif state == "playing" and merged["state"] != "paused":
            merged["state"] = "playing"

    return merged if any_ok else None


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


# ---------------------------------------------------------------------------
# Scriptable native players (AppleScript)
# ---------------------------------------------------------------------------

_OSA_TIMEOUT = 2.0
_APP_TOKEN = "app:"
# Players whose Automation permission was denied — warn once per process.
_osa_denied_warned: set[str] = set()


def _scriptable_players() -> list:
    """Enabled players from ``tts.media_players`` (config errors → none)."""
    try:
        from heyvox.config import load_config
        return [p for p in load_config().tts.media_players if p.enabled and p.bundle_id]
    except Exception as e:
        _log(f"players: config unavailable ({e})")
        return []


def _run_osa(script: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=_OSA_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _log_player_problem(player, msg: str) -> None:
    """Log a probe failure once per player and message (probes run on every pause)."""
    key = f"{player.bundle_id}:{msg}"
    if key not in _osa_denied_warned:
        _osa_denied_warned.add(key)
        _log(f"players: {player.name}: {msg}")


def _player_state(player) -> bool | None:
    """True = playing, False = running but not playing, None = not running/unknown.

    ``application id ... is running`` never launches the app, so a closed
    player costs one cheap probe and is never started by us.
    """
    b = player.bundle_id
    script = (
        f'if application id "{b}" is running then\n'
        f'tell application id "{b}" to return ({player.is_playing}) as text\n'
        f'end if\nreturn "off"'
    )
    r = _run_osa(script)
    if r is None:
        _log_player_problem(player, "osascript timeout/unavailable")
        return None
    if r.returncode != 0:
        # -1743 = Automation permission denied (System Settings → Privacy →
        # Automation); without it every pause silently does nothing.
        if "-1743" in r.stderr:
            _log_player_problem(
                player, "Automation permission denied — allow it in "
                "System Settings → Privacy & Security → Automation")
        elif "-1728" not in r.stderr:  # -1728 = app not installed: expected for defaults
            _log_player_problem(player, f"probe failed: {r.stderr.strip()[:120]}")
        return None
    out = r.stdout.strip()
    if out == "true":
        return True
    if out == "false":
        return False
    return None


def _player_command(player, command: str) -> bool:
    b = player.bundle_id
    r = _run_osa(f'tell application id "{b}" to {command}')
    ok = r is not None and r.returncode == 0
    if not ok:
        _log(f"players: {player.name}: '{command}' failed"
             + (f": {r.stderr.strip()[:120]}" if r is not None else " (timeout)"))
    return ok


def _pause_scriptable_players() -> list[str]:
    """Pause every enabled player that is currently playing; return flag tokens.

    States are probed in parallel (one osascript each) to stay well under 1 s
    with several players configured; pausing is only attempted on players
    that reported "playing", which makes toggle commands (VLC ``play``) safe.
    """
    players = _scriptable_players()
    if not players:
        return []
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=len(players)) as pool:
        states = list(pool.map(_player_state, players))
    _log("players: " + ", ".join(f"{p.name}={s}" for p, s in zip(players, states)))
    paused = []
    for player, state in zip(players, states):
        if state is True and _player_command(player, player.pause):
            _log(f"pause_media: paused {player.name} via AppleScript")
            paused.append(_APP_TOKEN + player.bundle_id)
    return paused


def _resume_scriptable_player(bundle_id: str) -> bool:
    """Resume a player we paused — unless it is already playing again or gone."""
    player = next((p for p in _scriptable_players() if p.bundle_id == bundle_id), None)
    if player is None:
        return False
    state = _player_state(player)
    if state is not False:
        _log(f"resume_media: {player.name} state={state}, not resuming")
        return False
    ok = _player_command(player, player.resume)
    _log(f"resume_media: {player.name} resume ok={ok}")
    return ok


# Cache: when pause_media() finds nothing playing, skip the slow detection
# for this many seconds.  Worst case: media started during the window gets
# missed for one TTS utterance, then detected on the next.
_NO_MEDIA_CACHE_TTL = 15.0
_no_media_cache_until = 0.0  # monotonic timestamp


def pause_media() -> bool:
    """Pause system media and set flag so we know we did it.

    Strategy:
    1. If Hush extension is running → use it for browser media.
    2. Configured scriptable players (VLC, QuickTime, ...) that report
       "playing" → pause via AppleScript.
    3. If nothing above paused: nowplaying-cli detects active playback →
       use MediaRemote. Media registered but paused → don't touch (user
       paused it).

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

    paused: list[str] = []

    # --- Tier 1: Try Hush for browser media ---
    hush_resp = _hush_command("pause")
    if hush_resp is not None:
        paused_count = hush_resp.get("pausedCount", 0)
        if paused_count > 0:
            paused.append("hush")
            _log(f"pause_media: paused {paused_count} browser tab(s) via Hush ({time.time()-t0:.2f}s)")
        else:
            _log("pause_media: Hush available but no browser media playing")
        # DEF-128: The previous `hush-noop` info banner ("Hush: no browser
        # media paused") fired on every TTS event where no browser tab had
        # active media. That is the *normal* steady state, not a degraded
        # state, and surfacing it as an info banner cluttered the menu bar
        # with non-actionable content (P-new-visibility inverted — a non-
        # state-change rendered as a state-change). The triage signal it
        # was meant to provide (DEF-105 / DEF-112 broken-extension cluster
        # shows the same pausedCount=0 shape) is still recoverable from
        # the [media] log line below; the menu bar is the wrong surface
        # for it. Removed entirely — see [media] log + heyvox-debug.log
        # for the same forensic signal without UI cost.
    elif not glob.glob(_HUSH_SOCK_GLOB) and not _hush_missing_banner_shown:
        _hush_missing_banner_shown = True
        _log(
            "pause_media: Hush not installed/running — browser media will not be "
            "paused. Install via `heyvox setup` or the Hush Chrome extension."
        )

    # --- Tier 2: Scriptable native players (VLC, QuickTime, Music, ...) ---
    # Independent of Hush: a browser tab and a local player can play at once.
    paused += _pause_scriptable_players()

    if paused:
        with open(_PAUSE_FLAG, "w") as f:
            f.write(",".join(paused))
        _log(f"pause_media: paused {paused} ({time.time()-t0:.2f}s)")
        return True

    # --- Tier 3: MediaRemote for players that publish Now Playing ---
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

    # Read which methods we used to pause (comma-separated tokens)
    try:
        with open(_PAUSE_FLAG) as f:
            method = f.read().strip()
    except OSError:
        method = "mr"
    methods = [m for m in method.split(",") if m]

    # Remove our flag
    try:
        os.unlink(_PAUSE_FLAG)
    except OSError:
        pass

    # Don't actually resume if another caller (orchestrator) still has it paused.
    # Check both heyvox and herald namespaces — Herald's TTS orchestrator uses
    # /tmp/herald-media-paused-* for the same purpose. Dedupe: the two prefixes
    # can overlap when `_TMP` resolution ever drifts, producing duplicate
    # entries in the log (DEF-085). `sorted(set(...))` keeps the list stable
    # for logging while guaranteeing uniqueness.
    from heyvox.constants import HEYVOX_MEDIA_PAUSED_PREFIX, HERALD_MEDIA_PAUSED_PREFIX
    other_flags = sorted({
        f for f in glob.glob(HEYVOX_MEDIA_PAUSED_PREFIX + "*") + glob.glob(HERALD_MEDIA_PAUSED_PREFIX + "*")
        if f != _PAUSE_FLAG
    })
    if other_flags:
        _log(f"resume_media: other pause flags exist {other_flags}, not resuming")
        return False

    # Graceful delay before resuming
    time.sleep(RESUME_DELAY)

    resumed = False

    if "hush" in methods:
        hush_resp = _hush_command(
            "resume", rewindSecs=HUSH_REWIND_SECS, fadeInMs=HUSH_FADE_IN_MS
        )
        if hush_resp is not None:
            _log(f"resume_media: Hush resume result={hush_resp}")
            resumed = True
        else:
            _log("resume_media: Hush unavailable for resume, media may stay paused")

    for token in methods:
        if token.startswith(_APP_TOKEN):
            resumed = _resume_scriptable_player(token[len(_APP_TOKEN):]) or resumed

    if "mr" in methods:
        mr = _get_mr()
        if mr is not None:
            try:
                result = mr.MRMediaRemoteSendCommand(_MR_PLAY, None)
                _log(f"resume_media: MediaRemote play result={result}")
                resumed = bool(result) or resumed
            except Exception as e:
                _log(f"resume_media: MediaRemote failed: {e}")

    return resumed
