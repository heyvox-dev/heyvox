"""
Vox main event loop.

Orchestrates the wake word listener, push-to-talk, STT transcription,
text injection, and recording indicator. Loads configuration from
~/.config/heyvox/config.yaml via the pydantic config system.

Entry point: heyvox.cli calls run() which calls main().

Requirement: CONF-01, DECP-01 through DECP-06
"""

import collections
import os
import sys
import time
import signal
import numpy as np

from heyvox.config import load_config, HeyvoxConfig, CONFIG_DIR
from heyvox.app_context import AppContext
from heyvox.device_manager import DeviceManager
from heyvox.audio.profile import MicProfileManager
from heyvox.audio.normalize import apply_input_gain
from heyvox.audio.keepalive import OutputKeepAlive
from heyvox.recording import RecordingStateMachine
from heyvox.constants import (
    RECORDING_FLAG,
    HOTPLUG_RESTART_MARKER,
    PA_STORM_RESTART_MARKER,
    TTS_PLAYING_FLAG,
    TTS_PLAYING_MAX_AGE_SECS,
    HUD_SOCKET_PATH,
    LOG_FILE_DEFAULT,
    HEYVOX_PID_FILE,
    HEYVOX_HEARTBEAT_FILE,
    HEYVOX_MEDIA_PAUSED_REC,
    HEYVOX_MEDIA_PAUSED_PREFIX,
    HERALD_MEDIA_PAUSED_PREFIX,
    HERALD_PAUSE_FLAG,
    HERALD_MUTE_FLAG,
    MIC_MUTE_FLAG,
    HERALD_AMBIENT_FLAG,
    HERALD_MODE_FILE,
    HERALD_LAST_PLAY,
    HERALD_WORKSPACE_FILE,
    HERALD_GENERATING_WAV_PREFIX,
    HERALD_PLAYING_PID,
    VERBOSITY_FILE,
    ensure_run_dirs,
    cleanup_ipc_files,
)
from heyvox.audio.cues import is_suppressed
from heyvox.audio.stt import init_local_stt, preload_model
from heyvox.hud.process import (
    launch_hud_overlay,
    stop_hud_overlay,
    kill_orphan_indicators,
    kill_duplicate_overlays,
    get_indicator_proc,
)


# ---------------------------------------------------------------------------
# Constants (non-global; kept at module level as read-only configuration)
# ---------------------------------------------------------------------------

_INJECT_DEDUP_SECS = 2.0    # Suppress duplicate injections within this window
_ZOMBIE_FAIL_THRESHOLD = 2  # Force reinit after N consecutive failed recordings
_BUSY_TIMEOUT = 60.0        # Force-reset busy after this many seconds
_DEAD_MIC_TIMEOUT = 30.0    # Force reinit after this many seconds of silence
_POST_SWITCH_STALL_SECS = 12.0  # DEF-132: first-packet grace for a freshly-switched mic (BT A2DP→HFP cold-start) before the no-data stall guard evicts it
_HUD_RECONNECT_INTERVAL = 1.0  # Retry every 1s (fast reconnect after overlay startup)
_HUD_LEVEL_INTERVAL = 0.05    # 20fps throttle for audio_level messages


# ---------------------------------------------------------------------------
# [WW_LATENCY] t1 threading: set by _run_loop just before recording.start(),
# consumed once by RecordingStateMachine.start() to pass into audio_cue().
# Both reset to 0.0 after use so stale values don't leak across activations.
# ---------------------------------------------------------------------------
_ww_t1: float = 0.0       # perf_counter at trigger commit (t1)
_ww_detect_ms: float = 0.0  # (t1-t0)*1000 — pre-computed for the log line


# ---------------------------------------------------------------------------
# Logging (module-level path set from config at startup)
# ---------------------------------------------------------------------------

_LOG_FILE = LOG_FILE_DEFAULT
_LOG_MAX_BYTES = 1_000_000


def _init_log(log_file: str, log_max_bytes: int) -> None:
    """Set the log file path and rotation limit from config."""
    global _LOG_FILE, _LOG_MAX_BYTES
    _LOG_FILE = log_file
    _LOG_MAX_BYTES = log_max_bytes


def _safe_stderr(msg: str) -> None:
    """Print to stderr, silently ignoring BrokenPipeError."""
    try:
        print(msg, file=sys.stderr, flush=True)
    except (BrokenPipeError, OSError):
        pass


def log(msg: str) -> None:
    """Write timestamped message to log file with rotation.

    Only writes to the file directly -- avoids double-logging when
    stdout is redirected to the same log file.
    """
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        if os.path.exists(_LOG_FILE) and os.path.getsize(_LOG_FILE) > _LOG_MAX_BYTES:
            rotated = _LOG_FILE + ".1"
            try:
                os.replace(_LOG_FILE, rotated)
            except OSError:
                pass
        with open(_LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# HUD overlay lifecycle (delegated to heyvox.hud.process)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Adapter builder
# ---------------------------------------------------------------------------

def _build_adapter(config: HeyvoxConfig):
    """Resolve the correct adapter from config.target_mode.

    Requirement: INPT-03 (adapter selection via config)
    """
    mode = config.target_mode
    if mode == "pinned-app" and config.target_app:
        from heyvox.adapters.generic import GenericAdapter
        return GenericAdapter(target_app=config.target_app, enter_count=config.enter_count)
    elif mode == "last-agent":
        from heyvox.adapters.last_agent import LastAgentAdapter
        return LastAgentAdapter(agents=config.agents, enter_count=config.enter_count)
    else:  # "always-focused" default
        from heyvox.adapters.generic import GenericAdapter
        return GenericAdapter(enter_count=config.enter_count)


# ---------------------------------------------------------------------------
# Singleton / PID management
# ---------------------------------------------------------------------------

_PID_FILE = HEYVOX_PID_FILE
_pid_fd = None  # File descriptor kept open to hold the flock for the process lifetime.


def _acquire_singleton():
    """Ensure only one vox instance runs at a time via PID file lock.

    If a previous instance is still running, SIGTERM it (then SIGKILL after 1s).
    Also cleans up stale flag files left by a forcefully killed predecessor.
    """
    if os.path.exists(_PID_FILE):
        try:
            with open(_PID_FILE) as _f:
                old_pid = int(_f.read().strip())
            os.kill(old_pid, 0)  # Check if alive
            # Verify the process is actually heyvox (not a recycled PID).
            try:
                import psutil as _psutil
                _proc = _psutil.Process(old_pid)
                _cmdline = " ".join(_proc.cmdline()).lower()
                if "heyvox" not in _cmdline:
                    log(
                        f"PID {old_pid} is not heyvox "
                        f"(cmd: {_cmdline!r}), removing stale PID file"
                    )
                    raise ProcessLookupError("not heyvox")
            except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                raise ProcessLookupError("gone or inaccessible")
            log(f"Killing previous vox instance (PID {old_pid})")
            os.kill(old_pid, signal.SIGTERM)
            time.sleep(1)
            try:
                os.kill(old_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        except (ProcessLookupError, ValueError):
            pass  # Old process already dead or not vox
        except PermissionError:
            log(f"WARNING: Cannot kill existing vox (PID {old_pid}), permission denied")

    # Clean up stale flag files and sockets from previous instance
    import glob as _glob
    for pattern in (RECORDING_FLAG, HEYVOX_MEDIA_PAUSED_PREFIX + "*",
                     HERALD_MEDIA_PAUSED_PREFIX + "*", HERALD_PAUSE_FLAG,
                     HUD_SOCKET_PATH,
                     HERALD_MUTE_FLAG, VERBOSITY_FILE,
                     # Herald state files that can go stale after crash
                     HERALD_AMBIENT_FLAG, HERALD_MODE_FILE,
                     HERALD_LAST_PLAY, HERALD_WORKSPACE_FILE,
                     # Temp WAVs from crashed TTS worker
                     HERALD_GENERATING_WAV_PREFIX + "*.wav"):
        for stale in _glob.glob(pattern):
            try:
                os.unlink(stale)
            except (FileNotFoundError, IsADirectoryError):
                pass

    # Reset transient state (recording/tts_playing/herald_playing_pid/paused) on startup
    from heyvox.ipc import reset_transient_state
    reset_transient_state()

    # Write PID file and hold an advisory lock for the lifetime of the process.
    import fcntl
    global _pid_fd
    _pid_fd = open(_PID_FILE, "w")
    try:
        fcntl.flock(_pid_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("Another vox instance holds the PID lock -- exiting")
        _pid_fd.close()
        sys.exit(1)
    _pid_fd.write(str(os.getpid()))
    _pid_fd.flush()


def _release_singleton():
    """Release PID lock and remove PID file on exit."""
    global _pid_fd
    try:
        if _pid_fd is not None:
            import fcntl
            fcntl.flock(_pid_fd, fcntl.LOCK_UN)
            _pid_fd.close()
            _pid_fd = None
        if os.path.exists(_PID_FILE):
            with open(_PID_FILE) as _f:
                pid = int(_f.read().strip())
            if pid == os.getpid():
                os.unlink(_PID_FILE)
    except (OSError, ValueError):
        pass


def _read_hotplug_marker():
    """Return ``(epoch, device_name)`` from the DEF-104 restart marker, or None."""
    try:
        with open(HOTPLUG_RESTART_MARKER) as f:
            ts, _, dev = f.read().partition("\n")
        return float(ts), dev.strip()
    except (OSError, ValueError):
        return None


def _write_hotplug_marker(device: str) -> None:
    """Stamp the restart marker so the post-restart process can detect a loop."""
    try:
        with open(HOTPLUG_RESTART_MARKER, "w") as f:
            f.write(f"{time.time()}\n{device}")
    except OSError:
        pass


def _maybe_restart_for_hotplug(devices, config, log, hud_send, ctx, cooldown):
    """DEF-104: self-restart if a higher-priority mic is live in CoreAudio but
    missing from PortAudio's cached enumeration.

    A device hotplugged after the daemon's first PortAudio init is invisible to
    every PortAudio code path (scan, find_best_mic, the hotplug watcher) until
    the process restarts — PortAudio's process-global HAL cache survives
    ``pa.terminate()`` + re-init. CoreAudio's live HAL still sees it, so the
    mismatch is the detection signal. The only reliable in-process fix is a
    full restart (the documented manual ``launchctl kickstart`` workaround,
    automated here via the same ``os.execv`` path the memory watchdog uses).

    Caller must gate on idle (not recording / not busy) and minimum daemon age.
    This adds the loop guard: if we already restarted for this same device
    within ``cooldown`` seconds and it's STILL missing, we stop — a device that
    stays invisible even after a restart is a different problem and must not
    spin the daemon.
    """
    try:
        from heyvox.audio.mic import (
            get_live_input_device_names,
            get_default_input_device_name,
            _enumerate_pa_signature,
        )
        from heyvox.audio.device_handle import detect_missed_hotplug
    except Exception as e:  # pragma: no cover - import guard
        log(f"[hotplug] import failed (skipping check): {e}")
        return

    try:
        live = get_live_input_device_names()
        if not live:
            return  # CoreAudio unavailable — degrade to no-op, never false-fire
        pa_names = {
            n.lower()
            for _i, n, c in _enumerate_pa_signature(devices.pa)
            if c > 0
        }
        # DEF-104 (2026-07-05): the CoreAudio default input is a self-heal
        # candidate even when it isn't in mic_priority — macOS makes a
        # freshly-plugged USB headset the default input (the G435 incident).
        default_name = get_default_input_device_name()
    except Exception as e:
        log(f"[hotplug] enumeration failed (skipping check): {e}")
        return

    missed = detect_missed_hotplug(
        live, pa_names, config.mic_priority, devices.dev_name, default_name
    )
    if not missed:
        return

    _restart_for_hotplug_candidate(missed, log, hud_send, ctx, cooldown)


def _restart_for_hotplug_candidate(missed, log, hud_send, ctx, cooldown) -> None:
    """Guarded DEF-104 restart for a named cache-miss candidate.

    Shared by the periodic ``_maybe_restart_for_hotplug`` scan and the manual
    HUD-menu path (DeviceManager's ``pop_hotplug_restart_request``): both end
    up with a device that is live in CoreAudio but invisible to PortAudio's
    process-wide cache, and both must pass the same guards — never restart for
    Bluetooth (DEF-147) and never loop on a device that stays invisible even
    after a restart (marker cooldown).
    """
    # DEF-147: never self-restart for a Bluetooth mic. A BT-HFP device is
    # chronically "live in CoreAudio but absent from PortAudio" as it flaps
    # A2DP<->HFP, so detect_missed_hotplug misreads it as a USB cache-miss
    # hotplug — and each restart tears the fragile SCO link apart (observed:
    # G435 over BT died repeatedly until HeyVox was stopped). BT has its own
    # A2DP->HFP path (_bt_trigger_hfp_switch) and never needs a process restart;
    # DEF-104 is strictly for USB devices the PortAudio cache missed.
    try:
        from heyvox.audio.bt import get_bluetooth_input_device_names
        bt_names = get_bluetooth_input_device_names()
        if any(missed.lower() in n for n in bt_names):
            log(
                f"MIC_HOTPLUG_MISSED: '{missed}' is a Bluetooth device — "
                f"NOT self-restarting (DEF-147; BT has its own A2DP->HFP path)"
            )
            return
    except Exception as e:
        log(f"[hotplug] BT-transport check failed (proceeding): {e}")

    marker = _read_hotplug_marker()
    if marker is not None:
        ts, dev = marker
        age = time.time() - ts
        if dev == missed and age < cooldown:
            log(
                f"MIC_HOTPLUG_MISSED: '{missed}' still absent from PortAudio "
                f"{age:.0f}s after a restart — not looping (cooldown {cooldown:.0f}s)"
            )
            return

    log(
        f"MIC_HOTPLUG_MISSED: '{missed}' is live in CoreAudio but absent from "
        f"PortAudio's cache (DEF-104 hotplug) — self-restarting to clear it"
    )
    try:
        hud_send({"type": "error", "text": f"New mic '{missed}' — restarting"})
    except Exception:
        pass
    _write_hotplug_marker(missed)
    time.sleep(0.3)
    _release_singleton()
    try:
        os.execv(sys.executable, [sys.executable, "-m", "heyvox.main"])
    except Exception as exc:
        log(f"[hotplug] execv failed ({exc}), falling back to subprocess restart")
        import subprocess as _sp
        _sp.Popen(
            [sys.executable, "-m", "heyvox.main"], start_new_session=True
        )
        ctx.shutdown.set()


# ---------------------------------------------------------------------------
# DEF-209: -9986 PortAudio-corruption storm → self-restart
# ---------------------------------------------------------------------------

_PA_STORM_CHECK_INTERVAL = 5.0
_PA_STORM_RESTART_COOLDOWN = 600.0


def _maybe_restart_for_pa_storm(log, hud_send, ctx) -> None:
    """Restart the process when a -9986 open-failure storm is detected.

    -9986 (paInternalError) on stream opens across MULTIPLE devices means the
    process-wide PortAudio context is corrupted. In-process recovery
    (terminate + fresh ``PyAudio()``) is proven insufficient — 2026-07-10 the
    storm survived it all night; only a full process restart cleared it.

    Loop guard: if the storm re-appears while the restart marker is younger
    than ``_PA_STORM_RESTART_COOLDOWN``, we do NOT restart again — a
    corruption that survives a fresh process won't be fixed by more restarts
    (coreaudiod / USB hardware state needs external help). Surface a banner
    and go quiet instead.
    """
    from heyvox.audio.mic import pa_storm_detected, clear_pa_storm
    if not pa_storm_detected():
        return
    try:
        marker_age = time.time() - os.path.getmtime(PA_STORM_RESTART_MARKER)
    except OSError:
        marker_age = None
    if marker_age is not None and marker_age < _PA_STORM_RESTART_COOLDOWN:
        log(
            f"PA_STORM: -9986 storm re-detected {marker_age:.0f}s after a storm "
            f"restart — NOT restarting again (cooldown "
            f"{_PA_STORM_RESTART_COOLDOWN:.0f}s); audio state needs external help"
        )
        try:
            from heyvox.hud.surface import HUDSurface
            HUDSurface.banner(
                level="error",
                source="pa-storm",
                text="Audio system wedged (-9986) — replug the headset or restart the Mac",
                ttl_secs=600,
            )
        except Exception:
            pass
        clear_pa_storm()  # stop re-firing every check interval
        return
    log(
        "PA_STORM: -9986 on stream opens across devices — PortAudio context "
        "corrupted, in-process recovery can't clear it; self-restarting (DEF-209)"
    )
    try:
        hud_send({"type": "error", "text": "Audio subsystem corrupted — restarting"})
    except Exception:
        pass
    try:
        with open(PA_STORM_RESTART_MARKER, "w") as f:
            f.write(f"{time.time()}\n")
    except OSError:
        pass
    time.sleep(0.3)
    _release_singleton()
    try:
        os.execv(sys.executable, [sys.executable, "-m", "heyvox.main"])
    except Exception as exc:
        log(f"PA_STORM: execv failed ({exc}), falling back to subprocess restart")
        import subprocess as _sp
        _sp.Popen([sys.executable, "-m", "heyvox.main"], start_new_session=True)
        ctx.shutdown.set()


# ---------------------------------------------------------------------------
# DEF-210: main-loop wedge supervisor
# ---------------------------------------------------------------------------

_WEDGE_RESTART_SECS = 300.0
_WEDGE_CHECK_INTERVAL = 30.0


def _start_wedge_supervisor(heartbeat: dict, ctx, log, hud_send) -> None:
    """Force-restart when the main loop stops iterating (DEF-210).

    2026-07-10 the daemon wedged at ~21:16 (a PortAudio call never returned)
    and sat dead for 10.75h. Every existing watchdog (DEF-104 hotplug,
    DEF-163 keepalive-stale, memory) lives INSIDE the main loop, so none of
    them could act. This supervisor is a separate daemon thread watching the
    in-process heartbeat the loop updates every ``_HEARTBEAT_INTERVAL``: no
    update for ``_WEDGE_RESTART_SECS`` means the loop is stuck in a call that
    will never return, and the only recovery is a fresh process.

    The threshold is deliberately generous — legitimate main-loop blocking
    (STT model cold-load, paste-injection hangs, BT HFP settling) tops out
    around 60-90s. Restart via execv; if that fails, ``os._exit(1)`` lets
    launchd's ``KeepAlive/SuccessfulExit=false`` relaunch us.

    Limitation: if the wedged C call holds the GIL, no Python thread —
    including this one — can run. The observed incident did not (the
    keepalive thread kept logging all night), so the heartbeat supervisor
    covers the failure mode we have actually seen.
    """
    def _loop() -> None:
        while not ctx.shutdown.wait(_WEDGE_CHECK_INTERVAL):
            age = time.time() - heartbeat["ts"]
            if age <= _WEDGE_RESTART_SECS:
                continue
            log(
                f"SUPERVISOR: main loop wedged — no heartbeat for {age:.0f}s "
                f"(threshold {_WEDGE_RESTART_SECS:.0f}s), force-restarting (DEF-210)"
            )
            try:
                hud_send({"type": "error", "text": "Voice daemon wedged — restarting"})
            except Exception:
                pass
            _release_singleton()
            try:
                os.execv(sys.executable, [sys.executable, "-m", "heyvox.main"])
            except Exception as exc:
                log(f"SUPERVISOR: execv failed ({exc}) — hard exit for launchd relaunch")
                os._exit(1)

    import threading
    threading.Thread(target=_loop, name="wedge-supervisor", daemon=True).start()
    log(
        f"Wedge supervisor armed (DEF-210): restart after "
        f"{_WEDGE_RESTART_SECS:.0f}s without a main-loop heartbeat"
    )


def _stop_tts_from_escape(hud_send_fn) -> None:
    """Stop TTS playback and clear queue (used by Escape key handler)."""
    from heyvox.audio.tts import stop_all
    stop_all()
    hud_send_fn({"type": "state", "state": "idle"})
    log("TTS stopped via Escape key.")


# ---------------------------------------------------------------------------
# Setup phase (D-05)
# ---------------------------------------------------------------------------

def _setup(config: HeyvoxConfig):
    """Initialize all subsystems and return (ctx, devices, recording).

    Handles: singleton check, logging init, STT init, PTT setup,
    wake word model loading, adapter creation, AppContext creation,
    DeviceManager init, RecordingStateMachine creation.

    Returns:
        Tuple of (ctx, devices, recording, model, use_separate_words, wake_config)
        where wake_config is a dict of wake word settings needed by _run_loop.
    """
    global _recording

    _init_log(config.log_file, config.log_max_bytes)
    # Diagnostic: verify log() is working
    _safe_stderr(f"[diag] _LOG_FILE={_LOG_FILE}, exists={os.path.exists(_LOG_FILE)}")
    log("STARTUP: log() initialized")
    try:
        with open(_LOG_FILE) as f:
            last = f.readlines()[-1].strip()
        _safe_stderr(f"[diag] log() wrote: {last}")
    except Exception as e:
        _safe_stderr(f"[diag] log() verification FAILED: {e}")

    # Last-resort crash logging
    def _excepthook(exc_type, exc_value, exc_tb):
        import traceback as _tb
        msg = "".join(_tb.format_exception(exc_type, exc_value, exc_tb))
        log(f"UNHANDLED EXCEPTION (excepthook):\n{msg}")
    sys.excepthook = _excepthook

    # Singleton: kill any previous instance and write our PID
    _acquire_singleton()
    import atexit
    atexit.register(_release_singleton)
    atexit.register(lambda: log("EXIT: atexit handler fired -- process is terminating"))

    # Startup cleanup: remove stale flags from previous crash/kill
    try:
        hb_age = time.time() - os.path.getmtime(HEYVOX_HEARTBEAT_FILE)
        if hb_age > 30:
            log(f"WARNING: Previous instance died without clean shutdown (heartbeat stale by {hb_age:.0f}s)")
    except FileNotFoundError:
        pass
    for stale_flag in (RECORDING_FLAG, TTS_PLAYING_FLAG,
                       HEYVOX_MEDIA_PAUSED_REC):
        try:
            age = time.time() - os.path.getmtime(stale_flag)
            if age > 60:
                os.unlink(stale_flag)
                log(f"Removed stale flag: {stale_flag} (age={age:.0f}s)")
            else:
                os.unlink(stale_flag)
                log(f"Removed leftover flag: {stale_flag}")
        except FileNotFoundError:
            pass
        except OSError:
            pass

    # Clean up stale daemon PID/socket files from previous instance.
    # These cause the HUD to show "TTS crashed" after a restart.
    from heyvox.constants import HERALD_ORCH_PID, KOKORO_DAEMON_PID, KOKORO_DAEMON_SOCK
    for _daemon_file in (HERALD_ORCH_PID, KOKORO_DAEMON_PID, KOKORO_DAEMON_SOCK):
        if os.path.exists(_daemon_file):
            _pid_alive = False
            if _daemon_file.endswith(".pid"):
                try:
                    _dpid = int(open(_daemon_file).read().strip())
                    os.kill(_dpid, 0)
                    _pid_alive = True
                except (ValueError, ProcessLookupError, PermissionError, OSError):
                    pass
            if not _pid_alive:
                try:
                    os.unlink(_daemon_file)
                    log(f"Removed stale daemon file: {_daemon_file}")
                except OSError:
                    pass

    # B6: Clean up orphaned media-pause flags
    import glob as _glob_b6
    for _mp_pattern in (HERALD_MEDIA_PAUSED_PREFIX + "*", HEYVOX_MEDIA_PAUSED_PREFIX + "*"):
        for _mp_file in _glob_b6.glob(_mp_pattern):
            try:
                _mp_age = time.time() - os.path.getmtime(_mp_file)
                if _mp_age > 60:
                    os.unlink(_mp_file)
                    log(f"Cleaned stale media-pause flag: {_mp_file} (age={_mp_age:.0f}s)")
            except OSError:
                pass

    # Start native TTS worker if enabled
    from heyvox.audio.tts import start_worker as _start_tts
    if config.tts.enabled:
        _start_tts(config)
        log("TTS worker started (Kokoro native engine)")

    # Create AppContext -- holds all shared mutable state
    ctx = AppContext()
    ctx.last_good_audio_time = time.time()

    # Signal handlers use ctx events
    def handle_signal(signum, frame):
        log(f"Received signal {signum}, shutting down...")
        ctx.shutdown.set()

    def handle_cancel(signum, frame):
        """SIGUSR1 = request recording cancellation (deferred to main loop).

        Signal handlers must avoid I/O and locks -- just set an event.
        The main loop checks ctx.cancel_requested and does the actual cleanup.
        """
        ctx.cancel_requested.set()

    def handle_relaunch(signum, frame):
        """SIGUSR2 = exit non-zero so launchd respawns us (DEF-071).

        The plist uses KeepAlive: { SuccessfulExit: false }, so clean exit 0
        is treated as a user-initiated quit (no respawn). Exit code 42
        tells launchd this was NOT a successful exit, triggering the
        respawn path. os._exit skips atexit handlers so the non-zero code
        is reported verbatim.
        """
        os._exit(42)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGUSR1, handle_cancel)
    signal.signal(signal.SIGUSR2, handle_relaunch)

    # Wake word settings
    start_word = config.wake_words.start
    stop_word = config.wake_words.stop
    use_separate_words = start_word != stop_word

    kill_orphan_indicators(log_fn=log)

    # Launch HUD overlay process (persistent -- stays alive for entire session)
    if config.hud_enabled or config.hud_menu_bar_only:
        launch_hud_overlay(menu_bar_only=config.hud_menu_bar_only, log_fn=log)
        from heyvox.hud.ipc import HUDClient
        ctx.hud_client = HUDClient(HUD_SOCKET_PATH)
        try:
            ctx.hud_client.connect()
        except Exception:
            pass
    else:
        log("HUD overlay disabled via config")

    # HUD send function (closure over ctx)
    def _hud_reconnect_now(reason: str) -> bool:
        """Rate-limited reconnect attempt. Returns whether the client is
        connected afterward."""
        now = time.time()
        if now - ctx.hud_last_reconnect < _HUD_RECONNECT_INTERVAL:
            return False
        ctx.hud_last_reconnect = now
        log(f"[HUD-DBG] Attempting reconnect for {reason}...")
        try:
            ctx.hud_client.reconnect()
        except Exception as e:
            log(f"[HUD-DBG] Reconnect failed: {e}")
            return False
        if ctx.hud_client._sock is None:
            log("[HUD-DBG] Reconnect succeeded but sock still None")
            return False
        log("[HUD-DBG] Reconnected!")
        return True

    def hud_send(msg: dict) -> None:
        """Send a message to the HUD overlay. No-op if not connected."""
        if ctx.hud_client is None:
            log(f"[HUD-DBG] hud_client is None, skipping {msg.get('type')}")
            return
        if ctx.hud_client._sock is None:
            if not _hud_reconnect_now(msg.get("type")):
                return
        try:
            ok = ctx.hud_client.send(msg)
            # DEF-215: `_sock` looks connected right up until a send() into a
            # peer that already restarted (e.g. the HUD overlay process being
            # relaunched) actually fails — so the first message after a
            # restart, often the "state" transition the overlay's rendering
            # gates on, would otherwise be silently dropped and desync the
            # overlay's view of the world until the NEXT transition. Retry
            # once immediately instead of waiting for the next hud_send() call
            # to notice the break.
            if not ok and ctx.hud_client._sock is None:
                if _hud_reconnect_now(f"{msg.get('type')} (stale socket)"):
                    ok = ctx.hud_client.send(msg)
            if ok:
                # DEF-053: audio_level fires ~20 Hz and floods the log with empty
                # "state=" lines (msg.state is absent on that message type). Skip
                # per-message logging for audio_level; the other message types are
                # infrequent enough that the debug trail stays useful.
                if msg.get("type") != "audio_level":
                    log(
                        f"[HUD-DBG] Sent {msg.get('type')}: "
                        f"{msg.get('state', msg.get('text', ''))}"
                    )
        except Exception as e:
            log(f"[HUD-DBG] Send failed: {e}")

    # Initialize STT backend
    log(f"STT backend: {config.stt.backend}")
    if config.stt.backend == "local":
        script_dir = os.path.dirname(os.path.abspath(__file__))
        init_local_stt(
            engine=config.stt.local.engine,
            mlx_model=config.stt.local.mlx_model,
            model_dir=os.path.join(script_dir, config.stt.local.model_dir),
            language=config.stt.local.language,
            threads=config.stt.local.threads,
            log_fn=log,
            initial_prompt=config.stt.local.initial_prompt,   # Phase 16
            unload_secs=config.stt.local.unload_secs,
        )
        # Start-preload: warm the MLX model at daemon startup instead of lazily
        # on the first recording. Every daemon (re)start otherwise makes the
        # first command pay a full cold-load (~6.5s avg); with frequent SIGTERM
        # restarts (8 in one day observed) that is the dominant tail-latency
        # source now that unload_secs=86400 keeps the model warm between
        # recordings. Non-blocking: preload_model() spawns a daemon thread;
        # _load_mlx_model is double-guarded against a race with the first
        # wake-word preload. MLX-only (sherpa has no preload path).
        if config.stt.local.engine == "mlx":
            preload_model()
            log("STT: warming MLX model at startup (start-preload)")

    # Build adapter and create RecordingStateMachine
    ctx.adapter = _build_adapter(config)
    log(f"Target mode: {config.target_mode} (adapter: {type(ctx.adapter).__name__})")

    recording = RecordingStateMachine(ctx=ctx, config=config, log_fn=log, hud_send=hud_send)

    # Pre-roll ring buffer: ~500ms of audio captured while idle and prepended
    # to a recording so the first words aren't clipped. Lives on ctx so the PTT
    # callbacks (built here in _setup) and the fill/consume sites (in _run_loop)
    # share one buffer. A held key starts recording ~tap_max_secs late (gesture
    # detection); snapshotting this buffer back-fills that window so no speech
    # is lost.
    from collections import deque
    _PREROLL_CHUNKS = max(1, int(0.5 * config.audio.sample_rate / config.audio.chunk_size))
    ctx.preroll_buffer = deque(maxlen=_PREROLL_CHUNKS)

    # Start push-to-talk listener if enabled
    if config.push_to_talk.enabled:
        from heyvox.input.ptt import start_ptt_listener

        def _stop_wake_via_ptt():
            # DEF-116: mark the recording as PTT-interrupted so the end-trim
            # is skipped (no stop-wake-word at the end to remove). Also serves
            # as the hands-free toggle-off stop (handsfree zeroes both trims).
            ctx.stopped_via_ptt_mid_recording = True
            recording.stop(reason="ptt_interrupt")

        ptt_callbacks = {
            # Snapshot the idle pre-roll so the ~tap_max_secs hold-detection
            # delay doesn't clip the user's opening words (Variant A latency).
            "on_start": lambda: recording.start(ptt=True, preroll=list(ctx.preroll_buffer)),
            "on_stop": lambda: recording.stop(reason="ptt"),
            # Double-tap → hands-free: ends on silence / stop wake word / Escape
            # like a wake-word recording (triggered_by_ptt stays False so the
            # main-loop watchdogs run), but with no spoken wake word, so the
            # stop() trim is skipped via ctx.handsfree.
            "on_start_handsfree": lambda: recording.start(
                handsfree=True, preroll=list(ctx.preroll_buffer)
            ),
            "on_stop_wake_via_ptt": _stop_wake_via_ptt,
            "on_cancel_transcription": lambda: ctx.cancel_transcription.set(),
            "on_cancel_recording": lambda: recording.cancel(),
            "on_cancel_tts": lambda: _stop_tts_from_escape(hud_send),
            "is_busy": lambda: ctx.busy,
            "is_recording": lambda: ctx.is_recording,
            "is_speaking": lambda: (
                os.path.exists(TTS_PLAYING_FLAG) or os.path.exists(HERALD_PLAYING_PID)
            ),
        }
        start_ptt_listener(
            config.push_to_talk.key, ptt_callbacks, log_fn=log,
            double_tap=config.push_to_talk.double_tap_handsfree,
            tap_max_secs=config.push_to_talk.tap_max_secs,
            double_tap_secs=config.push_to_talk.double_tap_secs,
        )

    # Load wake word models
    from heyvox.audio.wakeword import load_models
    try:
        model, use_separate_words = load_models(
            start_word, stop_word, config.wake_words.models_dir,
            also_load=config.wake_words.also_load,
        )
    except Exception as e:
        # Bundled models missing (broken wheel) or a custom wake word with no
        # .onnx file — surface an actionable message, not a raw traceback.
        log(f"FATAL: wake word model load failed ({start_word!r}/{stop_word!r}): {e}")
        _safe_stderr(
            "\n[heyvox] Wake word model failed to load.\n"
            f"  start={start_word!r} stop={stop_word!r}\n"
            "  Fix: run `heyvox setup` to re-provision models, or check that a\n"
            "  custom wake word has a matching .onnx in ~/.config/heyvox/models/.\n"
            f"  Details: {e}\n"
        )
        raise SystemExit(1)
    _loaded_models = list(model.models.keys()) if hasattr(model, 'models') else []
    _safe_stderr(f"[wakeword] Loaded models: {_loaded_models}, also_load={config.wake_words.also_load}")
    log(f"Wake word models loaded: {_loaded_models}")

    # Create MicProfileManager — per-device audio profiles (Plan 13-01)
    from pathlib import Path
    from platformdirs import user_cache_dir
    _cache_dir = Path(user_cache_dir("heyvox"))
    _cache_dir.mkdir(parents=True, exist_ok=True)
    profile_manager = MicProfileManager(config.mic_profiles, _cache_dir)

    # Open audio stream -- delegate to DeviceManager
    devices = DeviceManager(ctx=ctx, config=config, log_fn=log, hud_send=hud_send,
                            profile_manager=profile_manager)
    devices.init()

    # DEF-104 diagnostic: USB hotplug watcher polls PA device list every 10s
    # and logs [USB_HOTPLUG] on changes. Catches the moment of re-enumeration
    # that strands HeyVox on a stale PA index, instead of finding out minutes
    # later via the next AUDIO-13 reinit side-effect.
    try:
        from heyvox.audio.mic import start_pa_hotplug_watcher
        start_pa_hotplug_watcher(lambda: devices.pa, interval_secs=10.0)
    except Exception as e:
        log(f"PA hotplug watcher init failed (continuing): {e}")

    # ECHO-05: Initialize WebRTC AEC if configured and in speaker mode
    _aec_active = False
    if config.echo_suppression.aec_enabled and not devices.headset_mode:
        try:
            from heyvox.audio.echo import init_aec
            _aec_active = init_aec(delay_ms=config.echo_suppression.aec_delay_ms)
            if _aec_active:
                log(f"WebRTC AEC active (delay={config.echo_suppression.aec_delay_ms}ms)")
            else:
                log("WebRTC AEC requested but not available (pip install heyvox[aec])")
        except Exception as e:
            log(f"WebRTC AEC init error: {e}")

    if use_separate_words:
        log(f"Ready! Say '{start_word}' to start, '{stop_word}' to stop.")
    else:
        log(f"Ready! Say '{start_word}' to start/stop voice input.")

    return ctx, devices, recording, model, use_separate_words, hud_send, _aec_active, profile_manager


# ---------------------------------------------------------------------------
# Run loop (D-05)
# ---------------------------------------------------------------------------

def _run_loop(ctx: AppContext, devices: DeviceManager, recording: RecordingStateMachine,
              config: HeyvoxConfig, model, use_separate_words: bool, hud_send, aec_active: bool,
              profile_manager: "MicProfileManager | None" = None) -> None:
    """Main audio processing event loop.

    Reads audio from the microphone, runs wake word detection, manages device
    health, and delegates recording to RecordingStateMachine.

    Args:
        ctx: Shared application context (all mutable state).
        devices: DeviceManager for microphone lifecycle.
        recording: RecordingStateMachine for start/stop/cancel.
        config: HeyvoxConfig instance.
        model: Wake word model (openwakeword).
        use_separate_words: Whether start/stop words differ.
        hud_send: HUD send closure.
        aec_active: Whether WebRTC AEC is active.
        profile_manager: MicProfileManager for per-device profiles and calibration.
    """
    # Threaded audio read: protects against stream.read() blocking after AUHAL errors
    import concurrent.futures as _cf
    _read_executor = _cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="audio-read")

    # DEF-148: output keep-alive for USB power-saving headsets (G535 over
    # Lightspeed). Holds a silent output stream open ONLY while the default
    # output is USB, so the device never parks and swallows short cues' cold
    # start. No-op on built-in/BT/virtual outputs. Stopped in main()'s finally.
    # DEF-153: owns its own PA context (NOT devices.pa) — a captured shared
    # context goes stale after a USB flap and wedges the keep-alive forever.
    if config.audio.output_keepalive and getattr(devices, "keepalive", None) is None:
        try:
            devices.keepalive = OutputKeepAlive(log)
            devices.keepalive.start()
        except Exception as _ka_e:
            log(f"[keepalive] failed to start (skipping): {_ka_e}")

    # Local aliases for frequently read config values (avoid attribute lookups in hot loop)
    threshold = config.threshold
    cooldown = config.cooldown_secs
    sample_rate = config.audio.sample_rate
    chunk_size = config.audio.chunk_size
    silence_timeout = config.silence_timeout_secs
    silence_threshold = config.silence_threshold
    # Override silence_threshold from device profile if available.
    # DEF-097: also reject 0 as if it were None — a hardware-gated mic
    # that calibrated at noise_floor=0 leaves a stale 0 in the profile,
    # which would disable the VAD silent gate (and DEF-096 with it).
    # Real speech is always > 1000, so any positive threshold is safer
    # than 0; the global config default kicks in if the profile is bad.
    if (
        devices.active_profile
        and devices.active_profile.silence_threshold is not None
        and devices.active_profile.silence_threshold > 0
    ):
        silence_threshold = devices.active_profile.silence_threshold
    log(f"[mic-init] silence_threshold={silence_threshold} "
        f"(profile={devices.active_profile.silence_threshold if devices.active_profile else None}, "
        f"config={config.silence_threshold})")
    max_recording_secs = getattr(config, 'max_recording_secs', 30.0)
    start_word = config.wake_words.start
    stop_word = config.wake_words.stop

    last_trigger = 0.0
    consecutive_errors = 0

    # Training data collection: passively collect labeled audio clips (TP/FP/TN/FN)
    _training_collector = None
    if config.wake_words.collect_negatives:
        from heyvox.audio.training_collector import TrainingCollector
        neg_dir = config.wake_words.negatives_dir
        if not neg_dir:
            neg_dir = os.path.join(str(CONFIG_DIR), "negatives")
        # Training collector uses parent of negatives_dir as base (creates tp/fp/tn/fn subdirs)
        training_base = os.path.join(os.path.dirname(neg_dir), "training")
        _training_collector = TrainingCollector(
            base_dir=training_base,
            max_clips_per_category=config.wake_words.negatives_max_clips,
            tn_score_range=tuple(config.wake_words.negatives_score_range),
            tn_interval_secs=config.wake_words.negatives_interval_secs,
            sample_rate=sample_rate,
            get_mic_name=lambda: devices.dev_name,
        )
        # Pass collector to recording state machine for FP/FN/TP-stop hooks
        recording.training_collector = _training_collector
        log(f"Training collector enabled → {training_base} (max {config.wake_words.negatives_max_clips} clips/category)")
        log("  Categories: tp/ fp/ tn/ fn/")

    # Memory watchdog
    _MEM_WARN_MB = 2000
    _MEM_CRITICAL_MB = 2500
    _last_mem_check = time.time()
    _MEM_CHECK_INTERVAL = 15.0

    # DEF-104: hotplug self-restart. Detect a higher-priority mic that's live in
    # CoreAudio but missing from PortAudio's per-process cache (plugged in after
    # startup) and restart to clear the cache. Min-age lets the startup scan +
    # BT A2DP→HFP settle before any restart; cooldown (in the marker) prevents a
    # loop if a device stays invisible even after restarting.
    _HOTPLUG_CHECK_INTERVAL = 15.0
    _HOTPLUG_RESTART_COOLDOWN = 300.0
    _HOTPLUG_MIN_AGE = 90.0
    _last_hotplug_check = time.time()
    _hotplug_start_time = time.time()

    # SIGKILL-proof heartbeat
    _HEARTBEAT_FILE = HEYVOX_HEARTBEAT_FILE
    _HEARTBEAT_INTERVAL = 10.0
    _last_heartbeat = 0.0

    # DEF-210: wedge supervisor — separate thread that force-restarts when the
    # loop below stops iterating (all other watchdogs live inside the loop).
    _heartbeat = {"ts": time.time()}
    _start_wedge_supervisor(_heartbeat, ctx, log, hud_send)

    # DEF-209: -9986 storm check timer
    _last_pa_storm_check = time.time()

    # ECHO-01: Post-TTS cooldown
    _tts_last_seen = 0.0

    # Busy watchdog state
    _busy_since: float = 0.0

    # Auto-calibration state (D-04): collect ~50 chunks from new device without blocking wake word
    _calibration_chunks: list = []
    _calibrating = False
    _last_calibrated_device = ""
    # Trigger calibration for initial device if no noise_floor data exists
    if (profile_manager
            and devices.active_profile
            and devices.active_profile.noise_floor is None):
        _calibrating = True

    # Silence timeout: tracks when user first speaks during recording
    _first_speech_time: float = 0.0
    _rec_started_at: float = 0.0  # timestamp when current recording started (for warmup)
    _STOP_WARMUP_SECS = 2.0       # suppress stop-word re-trigger for this long after start
    _is_rec: bool = False
    _last_model_reset: float = 0.0  # Periodic model reset during recording
    # Consecutive-frame detection: require N consecutive above-threshold frames
    # before triggering. Filters single-frame false positives from passing speech.
    # Bumped to 3 after DEF-045 — model emits 2-frame high-score bursts during
    # silence. DEF-063 (2026-04-20): dropped back to 2 because the VAD gate
    # (`not _vad_silent` guard on line ~1124) already blocks silence-burst hits
    # from incrementing this counter, so the 3-frame rationale was redundant.
    # 2 × 80 ms = 160 ms post-detection floor instead of 240 ms — saves one
    # audio chunk of latency on every wake word without weakening the filter.
    # DEF-067 (2026-04-21): bifurcated — START stays at 2 (fast wake), STOP
    # requires 3. User reported mid-sentence false stops at score 0.997 during
    # natural speech; 2 frames = 160 ms is too easy for phoneme runs to hit,
    # while start-word latency matters much more than stop-word latency.
    # DEF-086 (2026-04-24): strict "consecutive" resets were too fragile — a
    # single noisy below-threshold dip mid-"Hey Vox" zeroed the counter and
    # the user's real stop intent had to wait for the 4 s silence_timeout.
    # Miss-frames now DECAY the counter by _STOP_HIT_DECAY instead of zeroing
    # it (see line ~1228), so 3 strong hits with one brief sag still fire.
    # Start-wake keeps the hard reset (idle mic noise should not accumulate).
    # DEF-099 (2026-04-28): STOP back to 2 frames (160 ms). Evidence: user
    # underhit case at 15:37:22 fired ONE frame at score 0.826 then user
    # gave up before 3-frame accumulation completed. With frames=2, single
    # hit at 0.826 + one decay-tolerant follow-up triggers stop reliably.
    # Regression risk: DEF-067's mid-sentence phantom-phoneme false stops
    # (scores 0.997+) can re-emerge in continuous speech without prior
    # pause (DEF-096-B's threshold discount only applies post-pause).
    # ROLLBACK: if "I was cut off mid-sentence" reports return, revert to 3
    # and pursue DEF-099 follow-on (Whisper-fallback) instead.
    _CONSECUTIVE_FRAMES_REQUIRED_START = 2
    _CONSECUTIVE_FRAMES_REQUIRED_STOP = 2
    # DEF-086: on a miss frame during recording, subtract this many from the
    # stop-hit accumulator instead of fully resetting. 1 tolerates exactly
    # one transient dip in a 3-hit burst — enough for real "Hey Vox" stops
    # while still rejecting isolated phoneme flares (which fire once and
    # decay back to 0 before the next flare).
    _STOP_HIT_DECAY = 1
    # DEF-103: stop-wake at G435 / BT-HFP audio quality often peaks on
    # exactly ONE frame at 0.99+ with neighbors falling under threshold.
    # The strict consecutive+decay gate above lost ~59% of such stops in
    # production telemetry. Two additional trigger paths repair this:
    #   A) Fast-stop on a single high-confidence frame (>= 0.92). Real "Hey
    #      Vox" peaks reliably exceed this; DEF-043's mid-sentence phoneme
    #      false-stops topped out around 0.85 — well below this gate.
    #   B) Sliding-window 2-of-4 stop hits. Tolerates peak→dip→peak that
    #      the strict consecutive gate kills. Frame indices are tracked
    #      per wake-word in `_stop_hit_window` and pruned per frame.
    # Warmup window (`_STOP_WARMUP_SECS`) and cooldown still apply to
    # both paths, preserving mid-sentence protection. Start-wake is
    # unchanged (the consecutive+hard-reset path remains authoritative
    # for idle-mic noise rejection).
    _HIGH_CONFIDENCE_FAST_STOP = 0.92
    # Ultra-confidence single-frame stop: fires WITHOUT the DEF-117/118
    # pre-silence gate. Rationale: setups exist where the VAD never sees a
    # silent frame (2026-06-10 20:09: last_silent=3033s, ambient level above
    # silence_threshold) — fast-path, window-path AND the DEF-096-B discount
    # are all dead there, so a lone 0.999 peak dies even though the model is
    # as sure as it ever gets. Log evidence 06-09/10: 78 high-confidence
    # frames blocked by the silence gate, 60 of them >= 0.999 (53 at a flat
    # 1.000). The bar must clear every documented mid-sentence phoneme
    # flare: 0.982 (DEF-117) and 0.997 (DEF-118) — hence 0.999, NOT lower.
    # FP-rate of this bypass is measurable via STOP_PATH path=ultra lines.
    _ULTRA_CONFIDENCE_FAST_STOP = 0.999
    _STOP_WINDOW_FRAMES = 4
    _STOP_WINDOW_HITS_REQUIRED = 2
    _stop_hit_window: dict[str, "collections.deque[int]"] = {}
    _frame_idx: int = 0
    # Backward-compat alias used by log lines — resolved dynamically below.
    _CONSECUTIVE_FRAMES_REQUIRED = _CONSECUTIVE_FRAMES_REQUIRED_START
    _consecutive_hits: dict[str, int] = {}  # ww_name → count of consecutive above-threshold frames
    # DEF-063: timestamp of the first confirmed hit in the current accumulation run.
    # Used to report wake→recording.start latency so future slowdowns are measurable.
    # [WW_LATENCY] t0 = perf_counter at the first above-threshold frame (proxy for
    # "last frame containing the wake word"). Renamed to _t0_wakeword for clarity;
    # _first_hit_time alias kept to avoid touching stop-word paths accidentally.
    _t0_wakeword: float = 0.0
    _first_hit_time: float = 0.0  # alias — set together with _t0_wakeword
    # VAD gate multiplier: skip wake-word eval when idle and audio level is below
    # silence_threshold * this factor. Prevents silence-driven false triggers.
    _VAD_GATE_MULT = 0.8
    # DEF-053: Grace window during recording — a chunk is treated as "silent"
    # only if no non-silent activity in the last N seconds. Covers the wake-word
    # model's feature-window lag (user says "Hey Vox", model reports high score
    # on trailing silence). Kept ≤ the feature window (~1 s) so dead-mic silence
    # bursts still fall inside the silent gate within a couple of chunks.
    # DEF-190 (2026-07-05): 0.5 -> 0.3. At 0.5, a stop "Hey Vox" appended to an
    # utterance with only a <0.5s word-boundary pause never registered a silent
    # frame, so the DEF-096 pre-silence gate blocked the stop unless score
    # >= 0.999 (ultra bypass) — the "start works, stop needs 2-5 tries" symptom
    # (G535: ambient VAD ~6, so silence_threshold was never the issue; the gap
    # was the grace, not the level). 0.3 lets a normal pause count as silence so
    # the DEF-096-B discount / window path can fire. Trade-off (user-accepted,
    # 2026-07-05): slightly higher mid-sentence false-stop risk. Still << the
    # ~1s feature window, so dead-mic silence bursts still register.
    _VAD_SILENT_GRACE = 0.3
    _last_nonsilent_time: float = 0.0

    # DEF-096: pre-silence-aware stop-wake adjustments. The wake-word model's
    # feature window fills with prior speech during recording, suppressing
    # stop-word detection when "Hey Vox" comes mid-flow (user reported
    # scores 0.5–0.6 vs 0.99 for clean isolated wake words). Three levers:
    #   A) Reset the model on the speech→silence transition so the
    #      post-pause wake word hits a near-blank feature window.
    #   B) During recording, if VAD reported silence within
    #      _PRE_SILENCE_DISCOUNT_WINDOW seconds, apply
    #      _PRE_SILENCE_THRESHOLD_FACTOR to the threshold — pause-then-
    #      Hey-Vox is the natural stop pattern; mid-sentence phoneme
    #      bursts (DEF-043) have no preceding silence so they keep the
    #      strict threshold and DEF-067's protection still applies.
    #   C) Periodic reset interval lowered as fallback for continuous
    #      speech with no natural pauses.
    _was_vad_silent: bool = False
    _last_silent_frame_time: float = 0.0
    # DEF-149: widened 0.5 -> 1.0s. The pre-silence window must cover the brief
    # pause PLUS the spoken "Hey Vox" itself (~0.5s): the stop-trigger fires at
    # the END of "Hey Vox", so by then the pause is already ~0.5-0.7s old.
    # At 0.5s that consistently just-missed (observed last_silent=0.56s blocked),
    # forcing the slow consecutive path and repeated tries. 1.0s catches the
    # natural "...sentence. Hey Vox" cadence while still expiring fast enough to
    # keep mid-sentence protection (DEF-043 flares have no preceding silence).
    _PRE_SILENCE_DISCOUNT_WINDOW = 1.0  # seconds since last silent frame
    _PRE_SILENCE_THRESHOLD_FACTOR = 0.85  # 15 % discount post-pause

    # User-effort metric: timestamps of every above-threshold wake attempt while
    # not recording. When recording finally starts, if the list has > 1 entry
    # within the last _USER_EFFORT_WINDOW seconds, the user had to repeat the
    # wake word — emit [USER_EFFORT] so the log-health digest can count those
    # days. Cleared on every recording.start.
    _recent_wake_attempts: list[float] = []
    _USER_EFFORT_WINDOW = 10.0
    # DEF-151: one spoken "Hey Vox" produces 2+ consecutive trigger-frames
    # ~30-100 ms apart; each used to count as a separate "attempt", inflating
    # USER_EFFORT ~2x (the attempts=2 window=0.1s artifact class — 75 of 87
    # events on 2026-06-10 were this). A real repeat requires re-saying the
    # phrase (>= ~0.6 s), so frames closer than this gap are one utterance.
    _USER_EFFORT_DEDUP_GAP = 0.5

    def _flush_user_effort() -> None:
        """Emit [USER_EFFORT] if multiple wake attempts piled up before start."""
        if len(_recent_wake_attempts) > 1:
            span = _recent_wake_attempts[-1] - _recent_wake_attempts[0]
            log(
                f"[USER_EFFORT] attempts={len(_recent_wake_attempts)} "
                f"window={span:.1f}s"
            )
        _recent_wake_attempts.clear()

    def _hud_ensure_connected() -> None:
        """Attempt periodic reconnect if the HUD connection was lost."""
        if ctx.hud_client is None:
            return
        if ctx.hud_client._sock is not None:
            return
        now = time.time()
        if now - ctx.hud_last_reconnect >= _HUD_RECONNECT_INTERVAL:
            ctx.hud_last_reconnect = now
            try:
                ctx.hud_client.reconnect()
            except Exception:
                pass

    ctx.last_read_time = time.monotonic()

    try:
        while not ctx.shutdown.is_set():
            # Heartbeat: touch file periodically as proof of life
            _now_hb = time.time()
            _heartbeat["ts"] = _now_hb  # DEF-210: in-process liveness for the supervisor
            if _now_hb - _last_heartbeat >= _HEARTBEAT_INTERVAL:
                _last_heartbeat = _now_hb
                try:
                    with open(_HEARTBEAT_FILE, "w") as _hbf:
                        _hbf.write(f"{int(_now_hb)}\n")
                except Exception:
                    pass

            # DEF-209: -9986 storm check. Placed BEFORE the read/IOError paths
            # because an active storm keeps those paths in `continue` loops
            # that never reach the idle watchdog block further down.
            if _now_hb - _last_pa_storm_check >= _PA_STORM_CHECK_INTERVAL:
                _last_pa_storm_check = _now_hb
                _maybe_restart_for_pa_storm(log, hud_send, ctx)

            # Handle deferred cancel from SIGUSR1 (signal-safe: no I/O in handler)
            if ctx.cancel_requested.is_set():
                ctx.cancel_requested.clear()
                if ctx.is_recording:
                    log("Received cancel signal (USR1)")
                    recording.cancel()
                    log("Recording cancelled via signal.")

            try:
                # Check if audio data is available before blocking read.
                # Bluetooth streams can stall indefinitely in stream.read()
                # without raising IOError, holding the GIL and freezing
                # the entire process (watchdog threads can't run either).
                _read_avail = devices.stream.get_read_available()
                if _read_avail < 1:
                    # No data ready — check for prolonged stall.
                    # Must check < 1, not < chunk_size: Bluetooth SCO
                    # delivers in 1024-frame periods (< chunk_size 1280)
                    # but stream.read() accumulates internally.
                    _stall = time.monotonic() - ctx.last_read_time
                    # DEF-132: a freshly-(re)selected mic — especially Bluetooth
                    # after an A2DP→HFP renegotiation — can take several seconds
                    # to deliver its first PCM packet. Grant a longer first-read
                    # window so the just-chosen device isn't evicted before it
                    # warms up; reverts to 5 s the moment any data flows (flag
                    # cleared on the successful-read path below).
                    _stall_limit = _POST_SWITCH_STALL_SECS if ctx.mic_just_switched else 5.0
                    if _stall > _stall_limit:
                        log(f"WARNING: No audio data for {_stall:.1f}s, forcing mic recovery")
                        try:
                            print(f"[mic] No audio data for {_stall:.1f}s, recovering", file=sys.stderr, flush=True)
                        except (BrokenPipeError, OSError):
                            pass
                        _read_executor.shutdown(wait=False, cancel_futures=True)
                        _read_executor = _cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="audio-read")
                        if not devices.handle_io_error():
                            ctx.last_read_time = time.monotonic()
                            continue
                        model.reset()
                        consecutive_errors = 0
                        ctx.last_read_time = time.monotonic()
                        continue
                    time.sleep(0.01)  # Brief yield, don't spin
                    continue

                # Guarded read: get_read_available() can lie after AUHAL
                # errors, causing stream.read() to block indefinitely.
                # Use a thread with timeout to prevent main loop freeze.
                try:
                    _raw = _read_executor.submit(
                        devices.stream.read, chunk_size, False
                    ).result(timeout=3.0)
                except (_cf.TimeoutError, TimeoutError):
                    _stall = time.monotonic() - ctx.last_read_time
                    log(f"WARNING: stream.read() blocked for 3s (stall={_stall:.1f}s), recovering")
                    # Kill the stuck executor and create a fresh one
                    _read_executor.shutdown(wait=False, cancel_futures=True)
                    _read_executor = _cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="audio-read")
                    if not devices.handle_io_error():
                        ctx.last_read_time = time.monotonic()
                        continue
                    model.reset()
                    consecutive_errors = 0
                    ctx.last_read_time = time.monotonic()
                    continue
                audio = np.frombuffer(_raw, dtype=np.int16)
                # DEF-101: per-mic software capture gain. BT-HFP devices (G435
                # etc.) deliver a low level the macOS input slider can't lift
                # (slider is decoupled from the HFP codec gain). A per-mic
                # `gain` in the active profile boosts the signal HERE — before
                # level tracking, calibration, wake word, recording and STT —
                # so every consumer sees the same lifted audio. No-op (zero
                # copy) when gain is None/1.0; switches with the active profile.
                if devices.active_profile is not None and devices.active_profile.gain:
                    audio = apply_input_gain(audio, devices.active_profile.gain)
                consecutive_errors = 0
                ctx.last_read_time = time.monotonic()
                if ctx.mic_just_switched:
                    # DEF-132: first PCM packet after a switch arrived — drop the
                    # extended first-read grace, back to the normal 5 s stall guard.
                    ctx.mic_just_switched = False

                # AUDIO-13: track last time we saw real audio (level >= 10).
                # Also tally zero vs low-level chunks since the last reset so
                # check_dead_mic_timeout can distinguish a dead stream (all
                # zeros) from a quiet room (steady 1-9 ambient).
                _chunk_max = int(np.abs(audio).max())
                if _chunk_max >= 10:
                    ctx.last_good_audio_time = time.time()
                    ctx.dead_mic_zero_chunks = 0
                    ctx.dead_mic_low_chunks = 0
                elif _chunk_max == 0:
                    ctx.dead_mic_zero_chunks += 1
                else:
                    ctx.dead_mic_low_chunks += 1

                # Auto-calibration: collect chunks in parallel with normal processing (D-04)
                # IMPORTANT: do NOT gate wake word during calibration (Pitfall 4)
                if _calibrating:
                    _calibration_chunks.append(np.frombuffer(audio, dtype=np.int16).copy()
                                               if not isinstance(audio, np.ndarray)
                                               else audio.copy())
                    if len(_calibration_chunks) == 1:
                        log(f"[calibration] Collecting {devices.dev_name}...")
                    if len(_calibration_chunks) >= 50:
                        nf, st = profile_manager.run_calibration(_calibration_chunks)
                        profile_manager.save_calibration(devices.dev_name, nf, st)
                        devices.active_profile = profile_manager.get_profile(devices.dev_name)
                        silence_threshold = devices.active_profile.silence_threshold or config.silence_threshold
                        _last_calibrated_device = devices.dev_name
                        _calibrating = False
                        _calibration_chunks = []
                        log(f"Auto-calibrated {devices.dev_name}: noise_floor={nf}, silence_threshold={st}")

            except IOError as e:
                consecutive_errors += 1
                if consecutive_errors <= 2:
                    log(f"Audio read error ({consecutive_errors}/2): {e}")
                    time.sleep(0.1)
                    continue
                # Replace executor: old thread may be blocked on dead stream
                _read_executor.shutdown(wait=False, cancel_futures=True)
                _read_executor = _cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="audio-read")
                if not devices.handle_io_error():
                    continue
                model.reset()  # Clear corrupted wake word state (AUDIO-12)
                consecutive_errors = 0
                continue

            # AUDIO-13: time-based dead mic detection -- delegates to DeviceManager
            devices.check_dead_mic_timeout()

            # Zombie stream reinit -- triggered by consecutive failed recordings (AUDIO-12)
            # or time-based dead mic timeout (AUDIO-13)
            if ctx.zombie_mic_reinit:
                ctx.zombie_mic_reinit = False
                # Replace executor: old thread may be blocked on dead stream
                _read_executor.shutdown(wait=False, cancel_futures=True)
                _read_executor = _cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="audio-read")
                if not devices.reinit(require_audio=True):
                    continue
                # Update silence_threshold from new device profile after reinit.
                # DEF-097: reject 0 from profile (stale hardware-gated calibration).
                if (
                    devices.active_profile
                    and devices.active_profile.silence_threshold is not None
                    and devices.active_profile.silence_threshold > 0
                ):
                    silence_threshold = devices.active_profile.silence_threshold
                else:
                    silence_threshold = config.silence_threshold
                log(f"[mic-reinit] silence_threshold={silence_threshold}")
                # Trigger calibration if new device has no noise_floor data
                if (devices.dev_name != _last_calibrated_device
                        and profile_manager
                        and devices.active_profile
                        and devices.active_profile.noise_floor is None):
                    _calibrating = True
                    _calibration_chunks = []
                model.reset()
                consecutive_errors = 0
                continue

            # Buffer audio during recording (for local STT)
            with ctx.lock:
                _was_rec = _is_rec
                _is_rec = ctx.is_recording
                _is_busy = ctx.busy
                _is_ptt = ctx.triggered_by_ptt
                if _is_rec and config.stt.backend == "local":
                    ctx.audio_buffer.append(audio.copy())
                elif not _is_rec and not _is_busy:
                    # Feed pre-roll buffer when idle -- captures audio before wake word
                    ctx.preroll_buffer.append(audio.copy())

            # Reset first-speech tracker and model reset timer when recording starts
            if _is_rec and not _was_rec:
                _first_speech_time = 0.0
                _last_model_reset = time.time()
                _rec_started_at = time.time()  # for stop-word warmup suppression

            # DEF-078: While recording, observe whether TTS is playing. The
            # recording path also checks at start(), but Herald can fire a
            # notify-hook or a held message can release mid-recording; any of
            # those puts speaker bleed into the audio buffer. Sticky flag:
            # once set during a recording, stays set until the recording ends
            # (cleared at the end of _send_local).
            if _is_rec and not ctx.tts_seen_during_recording:
                if os.path.exists(TTS_PLAYING_FLAG):
                    ctx.tts_seen_during_recording = True

            # Send live audio level to HUD at ~20fps during recording (HUD-08)
            if _is_rec:
                now_level = time.time()
                if now_level - ctx.hud_last_level_send >= _HUD_LEVEL_INTERVAL:
                    ctx.hud_last_level_send = now_level
                    rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))
                    import math
                    if rms > 1.0:
                        db = 20.0 * math.log10(rms)
                        level = max(0.0, min(1.0, (db - 30.0) / 50.0))
                    else:
                        level = 0.0
                    hud_send({"type": "audio_level", "level": round(level, 3)})

            # Proactive silent-mic health check (catches A2DP bad-state without IOError)
            if not _is_rec and not _is_busy:
                _hud_ensure_connected()
                now = time.time()
                _prev_stream = devices.stream
                devices.health_check(audio)
                # If health_check recovered to a new stream, update
                if devices.stream is not _prev_stream and devices.stream is not None:
                    model.reset()  # Clear corrupted wake word state (AUDIO-12)
                    consecutive_errors = 0

                # DEF-104: hotplug self-restart check (idle-gated by this block,
                # plus min daemon age + cooldown marker inside the helper).
                if (now - _last_hotplug_check >= _HOTPLUG_CHECK_INTERVAL
                        and now - _hotplug_start_time >= _HOTPLUG_MIN_AGE):
                    _last_hotplug_check = now
                    _maybe_restart_for_hotplug(
                        devices, config, log, hud_send, ctx,
                        cooldown=_HOTPLUG_RESTART_COOLDOWN,
                    )

                # DEF-163: keepalive PA staleness watchdog — auto-restart when
                # the output stream can't reopen for 2min.
                _ka = getattr(devices, "keepalive", None)
                if _ka is not None and _ka.stale.is_set():
                    log("WATCHDOG: keepalive PA context stale for 2min — auto-restarting to clear it")
                    hud_send({"type": "error", "text": "Cues silent — restarting"})
                    time.sleep(0.5)
                    _release_singleton()
                    try:
                        os.execv(sys.executable, [sys.executable, "-m", "heyvox.main"])
                    except Exception as _ka_exc:
                        log(f"WATCHDOG: execv failed ({_ka_exc}), falling back to subprocess restart")
                        import subprocess as _sp
                        _sp.Popen([sys.executable, "-m", "heyvox.main"],
                                  start_new_session=True)
                        ctx.shutdown.set()

                # Memory watchdog -- check RSS every 60s
                if now - _last_mem_check >= _MEM_CHECK_INTERVAL:
                    _last_mem_check = now
                    import psutil
                    rss_mb = psutil.Process().memory_info().rss / 1024 / 1024
                    if rss_mb > _MEM_CRITICAL_MB:
                        log(f"WATCHDOG: Memory critical ({rss_mb:.0f} MB), auto-restarting...")
                        hud_send({"type": "error", "text": f"Restarting: {rss_mb:.0f}MB"})
                        time.sleep(0.5)
                        _release_singleton()
                        try:
                            os.execv(sys.executable, [sys.executable, "-m", "heyvox.main"])
                        except Exception as exc:
                            log(f"WATCHDOG: execv failed ({exc}), falling back to subprocess restart")
                            import subprocess as _sp
                            _sp.Popen([sys.executable, "-m", "heyvox.main"],
                                      start_new_session=True)
                            ctx.shutdown.set()
                    elif rss_mb > _MEM_WARN_MB:
                        log(f"WARNING: Memory usage high: {rss_mb:.0f} MB (threshold: {_MEM_WARN_MB} MB)")
                        hud_send({"type": "error", "text": f"Memory: {rss_mb:.0f}MB"})

                    # System-wide RAM pressure -> menu-bar banner. Distinct from
                    # the self-RSS watchdog above (that restarts heyvox); this is
                    # read-only and watches the WHOLE machine, because RAM pressure
                    # is what forces MLX Whisper cold-reloads that silently slow STT.
                    # Surfaced via HUDSurface (same banner primitive as mic-zombie).
                    _mem_cfg = getattr(config, "memory", None)
                    if _mem_cfg is None or _mem_cfg.monitor_system_ram:
                        try:
                            from heyvox import ram_pressure as _ram_pressure
                            _ram_pressure.check_and_surface(
                                warn_mb=(_mem_cfg.system_ram_warn_mb if _mem_cfg else 2048),
                                crit_mb=(_mem_cfg.system_ram_critical_mb if _mem_cfg else 1024),
                                log=log,
                            )
                        except Exception as _ram_exc:
                            log(f"RAM-pressure check error: {_ram_exc}")

                    # Volume-zero guard: warn whenever system output is near zero
                    # so the user sees it in the menu bar / overlay before TTS arrives.
                    # Skip while TTS is actively ducking (3% is intentional, not broken).
                    # TTL 20s (slightly > 15s interval) so the banner persists
                    # between ticks but auto-expires if the daemon dies.
                    if not os.path.exists(TTS_PLAYING_FLAG):
                        try:
                            from heyvox.herald.coreaudio import get_system_volume_cached
                            from heyvox.hud.surface import HUDSurface as _HUDSurface
                            _vol = get_system_volume_cached(ttl=1.0)
                            if _vol <= 0.05:
                                _HUDSurface.banner(
                                    "warn", "vol-zero",
                                    f"Volume {int(round(_vol * 100))}% — TTS muted",
                                    ttl_secs=20.0,
                                )
                                log(f"[VOL-ZERO] vol={_vol:.2f} — TTS will be inaudible")
                            else:
                                _HUDSurface.clear("vol-zero")
                        except Exception:
                            pass

            # Overlay health: relaunch if dead, kill duplicates
            _now_scan = time.time()
            if not _is_rec and not _is_busy and _now_scan - devices._last_device_scan >= devices._DEVICE_SCAN_INTERVAL:
                _proc = get_indicator_proc()
                if _proc is not None:
                    if _proc.poll() is not None:
                        log(f"WARNING: HUD overlay exited (rc={_proc.returncode}), relaunching")
                        launch_hud_overlay(menu_bar_only=config.hud_menu_bar_only, log_fn=log)
                    else:
                        kill_duplicate_overlays(keep_pid=_proc.pid, log_fn=log)

            # Device hotplug -- delegated to DeviceManager
            devices.scan()

            # DEF-104: a manual HUD-menu pick of a USB mic that PortAudio's
            # cache can't see yet (scan flags it instead of misrouting it into
            # the BT HFP probe) is fulfilled here via the guarded restart.
            _manual_hotplug_missed = devices.pop_hotplug_restart_request()
            if _manual_hotplug_missed:
                _restart_for_hotplug_candidate(
                    _manual_hotplug_missed, log, hud_send, ctx,
                    cooldown=_HOTPLUG_RESTART_COOLDOWN,
                )

            # After scan, update silence_threshold if device changed.
            # DEF-097: reject 0 from profile (stale hardware-gated calibration).
            if (
                devices.active_profile
                and devices.active_profile.silence_threshold is not None
                and devices.active_profile.silence_threshold > 0
            ):
                silence_threshold = devices.active_profile.silence_threshold
            else:
                silence_threshold = config.silence_threshold
            # Start calibration for new device with no noise_floor data
            # Only trigger on actual device change (not every scan iteration)
            if (not _calibrating
                    and devices.dev_name != _last_calibrated_device
                    and profile_manager
                    and devices.active_profile
                    and devices.active_profile.noise_floor is None):
                _calibrating = True
                _calibration_chunks = []

            # Max recording duration -- safety cap to prevent runaway recordings
            if _is_rec and not _is_ptt and max_recording_secs > 0:
                elapsed = time.time() - ctx.recording_start_time
                if elapsed > max_recording_secs:
                    log(f"Max recording duration ({max_recording_secs}s) reached, stopping")
                    recording.stop(reason="max_duration")
                    continue

            # Track when user first starts speaking during recording
            if _is_rec and not _is_ptt:
                _level = int(np.abs(audio).max())
                if _level >= silence_threshold and _first_speech_time == 0.0:
                    _first_speech_time = time.time()

            # Silence watchdog — two modes:
            # 1) No speech yet: cancel after 5s (false wake word trigger)
            # 2) After speech: stop+transcribe after silence_timeout (4s)
            #
            # The only absolute ceiling on recording is config.max_recording_secs
            # (5 min by default) — see the cap earlier in this loop. DEF-038 and
            # DEF-049 previously added a 30 s post-speech hard cap here, but that
            # cap truncated long dictation mid-sentence and was reverted as DEF-050.
            # The original DEF-038 noisy-mic scenario (G435 sidetone wedging
            # herald-pause for up to 5 min) is addressed at the hardware level by
            # DEF-036's mute-button workaround and at the wake-word level by
            # DEF-045/DEF-047 VAD gates.
            _NO_SPEECH_CANCEL_SECS = 5.0
            if _is_rec and not _is_ptt and silence_timeout > 0:
                _elapsed = time.time() - ctx.recording_start_time

                if _first_speech_time == 0.0:
                    # No speech detected yet — cancel if too long (false trigger).
                    # Use percentage-based check to handle Bluetooth noise spikes.
                    if _elapsed > _NO_SPEECH_CANCEL_SECS:
                        with ctx.lock:
                            all_chunks = list(ctx.audio_buffer)
                        if all_chunks:
                            quiet_count = sum(1 for c in all_chunks if int(np.abs(c).max()) < silence_threshold)
                            quiet_pct = quiet_count / len(all_chunks)
                        else:
                            quiet_pct = 1.0
                        if quiet_pct >= 0.85:
                            log(f"No speech after {_NO_SPEECH_CANCEL_SECS}s ({quiet_pct:.0%} quiet), cancelling (false trigger)")
                            recording.cancel()
                            log("Ready for next wake word.")
                            continue
                        else:
                            # Sustained audio above threshold — real speech
                            _first_speech_time = time.time()
                else:
                    # Speech was detected — timeout on post-speech silence.
                    # Use percentage of quiet chunks (not single-spike max)
                    # to handle Bluetooth mics with occasional noise spikes.
                    elapsed_since_speech = time.time() - _first_speech_time
                    if elapsed_since_speech > silence_timeout:
                        with ctx.lock:
                            recent_chunks = ctx.audio_buffer[-int(silence_timeout * sample_rate / chunk_size):]
                        if recent_chunks:
                            quiet_count = sum(1 for c in recent_chunks if int(np.abs(c).max()) < silence_threshold)
                            quiet_pct = quiet_count / len(recent_chunks)
                            if quiet_pct >= 0.85:
                                log(f"Silence timeout ({silence_timeout}s after speech, {quiet_pct:.0%} quiet), transcribing")
                                recording.stop(reason="silence_timeout")
                                continue

            if _is_busy:
                # Busy flag watchdog -- force-reset if stuck (AUDIO-12)
                if _busy_since == 0.0:
                    _busy_since = time.time()
                elif time.time() - _busy_since > _BUSY_TIMEOUT:
                    log(f"WARNING: busy flag stuck for {_BUSY_TIMEOUT}s, force-resetting (watchdog)")
                    _safe_stderr(f"[watchdog] busy flag stuck for {_BUSY_TIMEOUT}s, resetting")
                    with ctx.lock:
                        ctx.busy = False
                    _busy_since = 0.0
                    try:
                        os.remove(RECORDING_FLAG)
                    except FileNotFoundError:
                        pass
                    hud_send({"type": "state", "state": "idle"})
                    # Fall through to wake word processing
                else:
                    continue
            else:
                _busy_since = 0.0

            # Suppress wake word detection while audio cue plays
            if is_suppressed():
                continue

            # Mic mute: skip all wake word processing when mic is muted
            if os.path.exists(MIC_MUTE_FLAG):
                continue

            # Echo suppression: skip wake word while ANY TTS is playing.
            # Check atomic state file first (primary, written by Herald orchestrator).
            # Fall back to legacy flag files for processes that predate the state file.
            _tts_active = False
            try:
                from heyvox.ipc.state import read_state as _read_ipc_state
                if _read_ipc_state().get("tts_playing"):
                    _tts_active = True
                    _tts_last_seen = time.time()
            except Exception:
                pass
            if not _tts_active:
                for _tts_flag in (TTS_PLAYING_FLAG,):
                    if os.path.exists(_tts_flag):
                        try:
                            flag_age = time.time() - os.path.getmtime(_tts_flag)
                            if flag_age < TTS_PLAYING_MAX_AGE_SECS:
                                _tts_active = True
                                _tts_last_seen = time.time()
                                break
                        except OSError:
                            pass
            # D-08/D-09: Only suppress wake word during TTS when NOT echo_safe
            # Echo safe = headset mode (auto) or profile override or force_disabled
            _echo_safe = devices.headset_mode  # D-08: headset = echo_safe by default
            if devices.active_profile and devices.active_profile.echo_safe is not None:
                _echo_safe = devices.active_profile.echo_safe  # Profile override
            if getattr(config.echo_suppression, 'force_disabled', False):
                _echo_safe = True  # D-11: force bypass

            if _tts_active and not _echo_safe:
                continue  # Suppress wake word during TTS in speaker mode only

            # ECHO-01: Post-TTS cooldown (D-10: 0.5s for headset, 2.0s for speaker mode)
            _echo_grace = 0.5 if _echo_safe else 2.0
            if _echo_grace > 0 and _tts_last_seen > 0:
                since_tts = time.time() - _tts_last_seen
                if since_tts < _echo_grace:
                    continue  # Still in reverb tail grace period

            # ECHO-05: Process mic frame through WebRTC AEC if enabled
            if aec_active:
                from heyvox.audio.echo import process_mic_frame
                audio = process_mic_frame(audio, sample_rate=sample_rate)

            # VAD gate (DEF-045 / DEF-047): skip wake-word eval when audio is near-silent.
            # Model emits high-confidence bursts on pure silence (observed during dead
            # Jabra HFP streams — entire recording at -89 dBFS + three 0.9+ bursts that
            # fired the stop-word and truncated the recording). We still feed model.predict
            # while recording to preserve feature continuity for the stop-word detector,
            # but we never ACT on triggers from silent frames.
            _vad_level = int(np.abs(audio).max())
            _raw_vad_silent = _vad_level < silence_threshold * _VAD_GATE_MULT
            if not _raw_vad_silent:
                _last_nonsilent_time = time.time()
            # DEF-053: During recording the wake-word model's feature window lags
            # the audio by ~500 ms. When the user says "Hey Vox" at the end of
            # an utterance, the model reports score≥0.9 on chunks AFTER the tail
            # (which are near-silent). A strict VAD gate resets consecutive_hits
            # on those trailing chunks and the stop-trigger never accumulates.
            # Grace: a chunk is treated as silent only if the last non-silent
            # sample was > _VAD_SILENT_GRACE ago. Dead-mic scenario (DEF-047)
            # still satisfied because an all-silent stream never updates
            # _last_nonsilent_time, so the grace window expires in 0.5 s.
            if _is_rec:
                _vad_silent = (
                    _raw_vad_silent
                    and (time.time() - _last_nonsilent_time) > _VAD_SILENT_GRACE
                )
                # DEF-096-A: reset model on the speech→silence transition.
                # When the user pauses (commonly right before saying "Hey
                # Vox" to stop), wipe accumulated speech features so the
                # upcoming wake word lands on a near-blank window. The
                # transition fires at most once per silence period so the
                # model has time to refill features before the wake word
                # arrives.
                if _vad_silent and not _was_vad_silent:
                    model.reset()
                    _last_model_reset = time.time()
                _was_vad_silent = _vad_silent
                # DEF-096-B / DEF-149: track the most recent RAW silent-frame
                # timestamp so the pre-silence discount recognises the
                # "...sentence. [brief pause] Hey Vox" stop pattern. This MUST
                # use _raw_vad_silent (instantaneous level < gate), NOT
                # _vad_silent — the latter carries the DEF-053 grace (~0.5 s),
                # which delays silence recognition by exactly as long as the
                # pre-silence window (_PRE_SILENCE_DISCOUNT_WINDOW = 0.5 s), so
                # the two never line up: _last_silent_frame_time stayed unset
                # (silent=True count = 0), the discount + fast/window stop paths
                # were blocked, and the user had to repeat "Hey Vox" 3x. The
                # grace still governs the VAD eval-gate and the DEF-096-A model
                # reset above; only the pre-silence clock reads the raw level.
                if _raw_vad_silent:
                    _last_silent_frame_time = time.time()
            else:
                _vad_silent = _raw_vad_silent
                # Reset transition tracking when we leave recording so the
                # next recording's first silent frame fires the reset.
                _was_vad_silent = False
            if not _is_rec and _vad_silent:
                # Clear consecutive-hits so a single pre-silence high score doesn't
                # combine with a post-silence high score to cross the threshold.
                _consecutive_hits.clear()
                continue  # skip model.predict + trigger loop entirely

            model.predict(audio)

            # Periodic model reset during recording — clears accumulated speech
            # features so the stop wake word can be detected even after long
            # continuous speech (without this, rapid "Hey Vox" after talking
            # scores below threshold because the feature window is polluted).
            # DEF-096-C: 2.5 s → 1.0 s. The DEF-096-A silence-transition
            # reset handles the natural pause-then-stop pattern; the
            # periodic reset is now strictly the fallback for continuous
            # speech, where a faster cadence keeps the feature window
            # cleaner without leaning on a user pause.
            _MODEL_RESET_INTERVAL = 1.0  # seconds
            if _is_rec and time.time() - _last_model_reset > _MODEL_RESET_INTERVAL:
                model.reset()
                _last_model_reset = time.time()

            # Training data: feed audio buffer and save hard negatives (TN)
            if _training_collector is not None and not _is_rec:
                _training_collector.feed(audio)
                _neg_max_score = max(
                    (score[-1] for score in model.prediction_buffer.values()),
                    default=0.0,
                )
                _training_collector.save_tn(_neg_max_score)

            # ECHO-02: Dynamic threshold in speaker mode — IDLE ONLY. During
            # a recording no TTS is playing (media paused via Hush, new TTS
            # held behind RECORDING_FLAG, active TTS interrupted on wake), so
            # the speaker-echo self-trigger this multiplier defends against
            # cannot hit the stop path; echo suppression additionally mutes
            # the mic during speaker-mode TTS. Keeping the 1.4x penalty while
            # recording cost real stops: 2026-06-10 20:09 score=0.999 vs
            # thr=0.91 (0.65 x 1.4) died as a single blocked frame.
            _speaker_mult = (
                config.echo_suppression.speaker_threshold_multiplier
                if (not devices.headset_mode and not _is_rec) else 1.0
            )

            # Cooldown is shorter during recording
            stop_cooldown = min(cooldown, 0.5)

            _model_thresholds = config.wake_words.model_thresholds

            # DEF-096-B: pre-silence-aware stop-wake threshold discount.
            # When the user paused recently (the natural "...sentence end.
            # [pause] Hey Vox" pattern), apply a 15 % discount so the
            # wake word triggers reliably even when the model's feature
            # window hasn't fully refilled. Mid-sentence phoneme bursts
            # (DEF-043) have NO preceding silence, so they keep the
            # strict threshold and DEF-067's protection still applies.
            _now_for_pre_silence = time.time()
            _recent_silence = (
                _is_rec
                and _last_silent_frame_time > 0.0
                and (_now_for_pre_silence - _last_silent_frame_time)
                    < _PRE_SILENCE_DISCOUNT_WINDOW
            )
            _pre_silence_factor = (
                _PRE_SILENCE_THRESHOLD_FACTOR if _recent_silence else 1.0
            )

            for ww_name, score in model.prediction_buffer.items():
                s = score[-1]
                # DEF-155: track the recording's peak score for stop-miss
                # diagnostics — saved into fn_stop/tp_stop clip filenames so
                # model-blind misses (peak≈0) and gate-blocked misses (peak
                # near threshold) are distinguishable without log archaeology.
                if _is_rec and s > ctx.rec_stop_score_max:
                    ctx.rec_stop_score_max = s
                base_thr = _model_thresholds.get(ww_name, threshold)
                # Cap at 0.95 — openwakeword scores are in [0, 1], so any higher
                # threshold makes triggering physically impossible. Prevents the
                # speaker_mult (1.4) × high base_thr (0.8) = 1.12 dead zone.
                # Stop-wake threshold matches start threshold (no 0.85 discount):
                # the old discount made stop-word easier to detect but caused
                # mid-sentence phonemes to falsely stop recording (DEF-043).
                # DEF-096-B: re-introduce a 0.85 stop-discount but ONLY when
                # `_pre_silence_factor` is active (recent VAD silence). This
                # preserves DEF-043's mid-flow protection while making
                # post-pause stop-wake reliable.
                active_threshold = min(
                    0.95, base_thr * _speaker_mult * _pre_silence_factor
                )
                active_cooldown = stop_cooldown if _is_rec else cooldown
                # DEF-067: stop-wake requires more frames than start-wake to
                # resist mid-sentence false stops on phoneme runs. User-facing
                # wake latency matters far more than stop latency.
                active_frames_required = (
                    _CONSECUTIVE_FRAMES_REQUIRED_STOP if _is_rec
                    else _CONSECUTIVE_FRAMES_REQUIRED_START
                )
                log_threshold = active_threshold * 0.5
                # Always compute `triggered` — used by the stop-wake _fast_stop
                # path further below (`_is_rec and triggered`). Hoisted out of
                # the `if s > log_threshold` logging block so a quiet stretch
                # (no wake-word activity) followed by PTT-recording can't
                # access an unbound local. Comparison is a single float op.
                triggered = s > active_threshold
                if s > log_threshold:
                    # DEF-053 diagnostic: include VAD state + consecutive_hits
                    # during recording so we can tell whether a stop-trigger
                    # that didn't stop recording was killed by the VAD gate
                    # (silent frame → hits reset to 0) or by the cooldown.
                    # DEF-103: also surface sliding-window count so failed
                    # stop attempts are diagnosable from the same line.
                    _hit_info = ""
                    if _is_rec:
                        _hits_now = _consecutive_hits.get(ww_name, 0)
                        _win_now = len(_stop_hit_window.get(ww_name, ()))
                        _hit_info = (
                            f" vad={_vad_level}/{int(silence_threshold * _VAD_GATE_MULT)}"
                            f" silent={_vad_silent} hits={_hits_now}/{active_frames_required}"
                            f" win={_win_now}/{_STOP_WINDOW_HITS_REQUIRED}"
                        )
                    msg = (
                        f"  [{ww_name}] score={s:.3f} (thr={active_threshold:.2f}) "
                        f"{'>>> TRIGGER' if triggered else ''}{_hit_info}"
                    )
                    log(msg)
                    if triggered:
                        _safe_stderr(f"[wakeword] {msg.strip()}")

                    # Track every "model heard the wake word" attempt while not
                    # recording — feeds USER_EFFORT when recording finally starts.
                    # Includes triggers that get killed by VAD / accumulator /
                    # cooldown, since each represents a real wake utterance.
                    if triggered and not _is_rec:
                        _now_attempt = time.time()
                        _recent_wake_attempts[:] = [
                            t for t in _recent_wake_attempts
                            if _now_attempt - t < _USER_EFFORT_WINDOW
                        ]
                        # DEF-151: collapse trigger-frames of the same
                        # utterance into one attempt (see constant above).
                        if (
                            not _recent_wake_attempts
                            or _now_attempt - _recent_wake_attempts[-1]
                            > _USER_EFFORT_DEDUP_GAP
                        ):
                            _recent_wake_attempts.append(_now_attempt)

                    # Dedicated tags for log-health digest grep:
                    # WAKE_VAD_DROP — model triggered but VAD post-filter killed
                    # it as silent. A confident wake word lost to over-rejection.
                    if triggered and _vad_silent:
                        log(
                            f"[WAKE_VAD_DROP] [{ww_name}] score={s:.3f} "
                            f"thr={active_threshold:.2f} "
                            f"vad={_vad_level}/{int(silence_threshold * _VAD_GATE_MULT)} "
                            f"is_rec={_is_rec}"
                        )
                    # NEAR_MISS — strong score that didn't quite reach threshold
                    # while idle. Aggregated counts surface model drift, mic
                    # placement issues, or speaker_mult set too aggressively.
                    elif (
                        not triggered
                        and not _is_rec
                        and s > active_threshold * 0.7
                    ):
                        log(
                            f"[NEAR_MISS] [{ww_name}] score={s:.3f} "
                            f"thr={active_threshold:.2f}"
                        )
                # DEF-047: During recording, silent frames must not count toward a
                # stop-trigger. The model emits silence-bursts on dead HFP streams;
                # without this gate, a mic that stops delivering audio mid-recording
                # will self-stop within ~4s. Feature-continuity is preserved because
                # we still call model.predict above.
                # DEF-103: track frame index per loop to drive sliding-window
                # stop detection. _frame_idx is monotonic for the lifetime of
                # the process; only relative deltas matter inside the window.
                _frame_idx += 1
                if s > active_threshold and not _vad_silent:
                    prev = _consecutive_hits.get(ww_name, 0)
                    _consecutive_hits[ww_name] = prev + 1
                    # DEF-063: capture the moment the run of consecutive hits
                    # started, so wake→start latency can be measured below.
                    if prev == 0 and not _is_rec:
                        _t0_wakeword = time.perf_counter()
                        _first_hit_time = _t0_wakeword  # keep alias in sync
                    # DEF-103-B: append this frame to the per-ww sliding window
                    # used for the 2-of-4 stop-trigger path. Only relevant
                    # while recording; idle hits are ignored to avoid
                    # accidental cross-state accumulation.
                    if _is_rec:
                        _sw = _stop_hit_window.setdefault(
                            ww_name, collections.deque()
                        )
                        _sw.append(_frame_idx)
                        while _sw and (
                            _frame_idx - _sw[0] >= _STOP_WINDOW_FRAMES
                        ):
                            _sw.popleft()
                elif _is_rec:
                    # DEF-086: while recording, decay instead of hard-reset so
                    # one transient miss doesn't kill a real 3-hit "Hey Vox"
                    # stop. Outside recording, keep the hard reset below —
                    # idle-mic noise must not accumulate toward a false start.
                    _consecutive_hits[ww_name] = max(
                        0,
                        _consecutive_hits.get(ww_name, 0) - _STOP_HIT_DECAY,
                    )
                    # DEF-103-B: age the sliding window on miss frames so old
                    # hits expire even when no new hits arrive.
                    _sw = _stop_hit_window.get(ww_name)
                    if _sw:
                        while _sw and (
                            _frame_idx - _sw[0] >= _STOP_WINDOW_FRAMES
                        ):
                            _sw.popleft()
                else:
                    _consecutive_hits[ww_name] = 0
                    # DEF-103-B: clear the stop window on the idle path; it
                    # is a recording-only tool and stale entries should not
                    # leak into the next recording's first frames.
                    _stop_hit_window.pop(ww_name, None)

                # DEF-103: stop-wake fires via THREE paths during recording.
                # All three respect the warmup + cooldown gates downstream.
                #   1) Consecutive: existing _consecutive_hits >= frames_required
                #   2) Fast-path: single frame at score >= 0.92 (high-confidence
                #      peaks of real "Hey Vox" — DEF-043 phoneme flares stay <0.85)
                #   3) Sliding window: 2 hits in last 4 frames (~320 ms) —
                #      tolerates peak→dip→peak that path 1 loses.
                _consec_trigger = (
                    _consecutive_hits.get(ww_name, 0) >= active_frames_required
                )
                # DEF-117: fast-path stop-wake also requires a recent silent
                # frame (the natural "...sentence end. [pause] Hey Vox"
                # pattern). Mid-sentence phoneme FPs at score>0.92 had no
                # gate before — Whisper Large ground-truth on the 2026-05-25
                # 09:07:19 sample showed the user saying "...momentan
                # irrelevant" continuously while the model fired score=0.982.
                # _recent_silence reuses DEF-096-B's _PRE_SILENCE_DISCOUNT_WINDOW
                # so threshold-discount (favours post-pause) and fast-stop-gate
                # (requires post-pause) share one time horizon — no drift.
                _fast_stop = (
                    _is_rec
                    and triggered
                    and s > _HIGH_CONFIDENCE_FAST_STOP
                    and not _vad_silent
                    and _recent_silence
                )
                # Ultra-confidence bypass: a single frame at >= 0.999 may
                # stop WITHOUT the pre-silence gate (see constant rationale
                # above — covers setups where the VAD never reports silence
                # and every pre_silence-dependent lever is dead). Keeps the
                # DEF-047 VAD-silent guard: dead-stream silence bursts must
                # not self-stop the recording.
                _ultra_stop = (
                    _is_rec
                    and triggered
                    and s >= _ULTRA_CONFIDENCE_FAST_STOP
                    and not _vad_silent
                )
                # Forensic tag: high-confidence stop-wake that the silence
                # gate REJECTED. Pairs with the existing NEAR_MISS tag for
                # idle-state borderline scores; same triage idea. Scores
                # >= _ULTRA_CONFIDENCE_FAST_STOP no longer count as blocked
                # — the ultra path fires for them.
                if (
                    _is_rec
                    and triggered
                    and s > _HIGH_CONFIDENCE_FAST_STOP
                    and s < _ULTRA_CONFIDENCE_FAST_STOP
                    and not _vad_silent
                    and not _recent_silence
                ):
                    if _last_silent_frame_time > 0.0:
                        _silent_age = (
                            f"{_now_for_pre_silence - _last_silent_frame_time:.2f}s ago"
                        )
                    else:
                        _silent_age = "never"
                    log(
                        f"[NEAR_MISS_FAST_BLOCKED] [{ww_name}] score={s:.3f} "
                        f"thr={active_threshold:.2f} "
                        f"vad={_vad_level}/{int(silence_threshold * _VAD_GATE_MULT)} "
                        f"last_silent={_silent_age} window={_PRE_SILENCE_DISCOUNT_WINDOW}s"
                    )
                # DEF-118: extend DEF-117's silence-gate to the window-path.
                # 2026-05-27 08:01:40 false stop fired with win=2/2 hits=2/2
                # pre_silence=False on "Ich denke der Grund ist ein..." — two
                # consecutive high-score frames in continuous German speech
                # without any pause. Window was deliberately left ungated in
                # DEF-117 on the assumption "sustained hits ... naturally
                # more resistant to single-burst FPs"; field evidence on
                # German speech-flow showed two-frame bursts occur there too.
                # Shares _PRE_SILENCE_DISCOUNT_WINDOW with DEF-096-B and
                # DEF-117 so all three knobs use the same time horizon.
                _window_stop = (
                    _is_rec
                    and len(_stop_hit_window.get(ww_name, ()))
                    >= _STOP_WINDOW_HITS_REQUIRED
                    and _recent_silence
                )
                # DEF-118 forensic: window-path that would have fired but
                # the silence gate rejected it. Pattern P-detector-without-action.
                if (
                    _is_rec
                    and len(_stop_hit_window.get(ww_name, ()))
                    >= _STOP_WINDOW_HITS_REQUIRED
                    and not _recent_silence
                ):
                    if _last_silent_frame_time > 0.0:
                        _silent_age = (
                            f"{_now_for_pre_silence - _last_silent_frame_time:.2f}s ago"
                        )
                    else:
                        _silent_age = "never"
                    log(
                        f"[NEAR_MISS_WINDOW_BLOCKED] [{ww_name}] score={s:.3f} "
                        f"thr={active_threshold:.2f} "
                        f"vad={_vad_level}/{int(silence_threshold * _VAD_GATE_MULT)} "
                        f"win={len(_stop_hit_window.get(ww_name, ()))}"
                        f"/{_STOP_WINDOW_HITS_REQUIRED} "
                        f"last_silent={_silent_age} window={_PRE_SILENCE_DISCOUNT_WINDOW}s"
                    )

                if _consec_trigger or _fast_stop or _window_stop or _ultra_stop:
                    now = time.time()
                    if now - last_trigger > active_cooldown:
                        # DEF-063: wake→trigger latency (first hit → accumulation complete).
                        # Does not include model feature-window lag (~500 ms) or afplay
                        # subprocess spawn, but gives an apples-to-apples number for
                        # regressions inside the consecutive-frame gate.
                        if not _is_rec and _t0_wakeword > 0:
                            _t1_wakeword = time.perf_counter()
                            _detect_ms = (_t1_wakeword - _t0_wakeword) * 1000
                            log(
                                f"[WW_LATENCY] detect={_detect_ms:.0f}ms "
                                f"frames={active_frames_required} score={s:.3f}"
                            )
                            _first_hit_time = 0.0
                            _t0_wakeword = 0.0
                        else:
                            _t1_wakeword = 0.0
                            _detect_ms = 0.0
                        # DEF-103: surface which trigger path fired so a
                        # follow-up regression in stop reliability can be
                        # attributed (consec / fast / window). Recording
                        # path only — start-wake always uses consec.
                        if _is_rec:
                            # "ultra" labels stops ONLY the gate-bypass made
                            # possible (fast would have fired anyway when
                            # pre_silence was present) — its count is the
                            # direct measure of the bypass's benefit, and
                            # any FP-stop complaints trace back to it.
                            _path = (
                                "fast" if _fast_stop
                                else "ultra" if _ultra_stop
                                else "window" if _window_stop
                                else "consec"
                            )
                            # DEF-151 observability: log the silent-frame age on
                            # SUCCESSFUL stops too, so its distribution can be
                            # compared against the *_BLOCKED tags' last_silent
                            # — that comparison is what justified DEF-149's
                            # 0.5->1.0s window and will justify (or veto) any
                            # future re-tune without guesswork.
                            if _last_silent_frame_time > 0.0:
                                _silent_age_ok = (
                                    f"{_now_for_pre_silence - _last_silent_frame_time:.2f}s"
                                )
                            else:
                                _silent_age_ok = "never"
                            log(
                                f"[STOP_PATH] [{ww_name}] path={_path} "
                                f"score={s:.3f} "
                                f"win={len(_stop_hit_window.get(ww_name, ()))}"
                                f"/{_STOP_WINDOW_HITS_REQUIRED} "
                                f"hits={_consecutive_hits.get(ww_name, 0)}"
                                f"/{active_frames_required} "
                                f"pre_silence={_recent_silence} "
                                f"last_silent={_silent_age_ok}"
                            )
                        # Training data: save TP-start. Evidence-based fn recovery
                        # (a TN clip that actually contains the wake word) now
                        # happens via the batch quality gate (tools/quality_gate.py)
                        # run before featurization, not via retroactive heuristics.
                        if _training_collector is not None and not _is_rec:
                            _training_collector.save_tp_start(s)
                        last_trigger = now
                        # D-05: If TTS is playing (echo_safe mode), interrupt it and start recording
                        if _tts_active and _echo_safe and not _is_rec:
                            from heyvox.audio.tts import interrupt as tts_interrupt
                            from pathlib import Path
                            # Write recording flag FIRST (Pitfall 3: orchestrator needs to see _is_paused)
                            Path(RECORDING_FLAG).touch()
                            tts_interrupt()  # Kill afplay, orchestrator purges current message parts
                            _tts_last_seen = 0  # Clear TTS tracking so grace period doesn't block
                            last_trigger = 0  # Bypass cooldown for this trigger (start recording instantly)
                            log("Wake word during TTS — interrupted playback, starting recording")
                        # PTT owns the recording lifecycle -- ignore wake words
                        if _is_ptt and _is_rec:
                            pass
                        elif use_separate_words:
                            if start_word in ww_name and not _is_rec:
                                _flush_user_effort()
                                # [WW_LATENCY] thread t1/detect_ms to recording.start via module globals
                                import heyvox.main as _self_mod
                                _self_mod._ww_t1 = _t1_wakeword
                                _self_mod._ww_detect_ms = _detect_ms
                                recording.start(preroll=ctx.preroll_buffer)
                            elif stop_word in ww_name and _is_rec:
                                # Warmup: ignore stop-word in first _STOP_WARMUP_SECS
                                # to prevent user's own speech from self-stopping.
                                if time.time() - _rec_started_at < _STOP_WARMUP_SECS:
                                    log(f"[wakeword] stop-trigger suppressed (warmup: {time.time() - _rec_started_at:.1f}s < {_STOP_WARMUP_SECS}s)")
                                else:
                                    recording.stop(reason="stop_wake")
                        else:
                            if not _is_rec:
                                _flush_user_effort()
                                # [WW_LATENCY] thread t1/detect_ms to recording.start via module globals
                                import heyvox.main as _self_mod
                                _self_mod._ww_t1 = _t1_wakeword
                                _self_mod._ww_detect_ms = _detect_ms
                                recording.start(preroll=ctx.preroll_buffer)
                            elif time.time() - _rec_started_at < _STOP_WARMUP_SECS:
                                # Warmup: ignore self-trigger in first _STOP_WARMUP_SECS
                                log(f"[wakeword] stop-trigger suppressed (warmup: {time.time() - _rec_started_at:.1f}s < {_STOP_WARMUP_SECS}s)")
                            else:
                                recording.stop(reason="stop_wake")
                    else:
                        # DEF-151 observability: a full trigger swallowed by the
                        # wake-cooldown was silent until now — the log showed
                        # ">>> TRIGGER" with no recording.start and no reason
                        # (observed 2026-06-10 07:53:33: two full-score triggers
                        # vanished, user repeated and filed USER_EFFORT). Tagged
                        # so log-health can attribute "heard but ignored" misses.
                        log(
                            f"[WAKE_COOLDOWN_DROP] [{ww_name}] score={s:.3f} "
                            f"since_last={now - last_trigger:.1f}s "
                            f"cooldown={active_cooldown:.1f}s is_rec={_is_rec}"
                        )
                    model.reset()

    except KeyboardInterrupt:
        log("Stopped by user")
    except SystemExit as e:
        log(f"FATAL: SystemExit({e.code}) in main loop")
        import traceback
        log(traceback.format_exc())
    except Exception:
        log("FATAL: Unhandled exception in main loop")
        import traceback
        log(traceback.format_exc())


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Main event loop -- loads config, starts PTT, runs wake word detection."""
    ensure_run_dirs()
    config = load_config()

    # Telemetry: only start the background thread when the user has opted in.
    # No-op when telemetry.enabled is False — the thread checks the flag each
    # cycle, so a runtime toggle takes effect within one batch interval.
    telemetry_started = False
    try:
        if config.telemetry.enabled:
            from heyvox.telemetry.sender import start_background as _tm_start
            _tm_start()
            telemetry_started = True
            log("Telemetry thread started")
    except Exception as exc:
        log(f"Telemetry init failed (continuing): {exc}")

    ctx, devices, recording, model, use_separate_words, hud_send, aec_active, profile_manager = _setup(config)

    from heyvox.audio.tts import shutdown as _shutdown_tts

    try:
        _run_loop(ctx, devices, recording, config, model, use_separate_words, hud_send, aec_active,
                  profile_manager=profile_manager)
    finally:
        log("Cleaning up...")
        if telemetry_started:
            try:
                from heyvox.telemetry.sender import stop_background as _tm_stop
                _tm_stop(timeout=2.0)
            except Exception:
                pass
        cleanup_ipc_files(herald_too=False)
        if ctx.hud_client:
            ctx.hud_client.close()
        # Only kill HUD on explicit stop (SIGTERM/SIGINT), not on watchdog restart
        if ctx.shutdown.is_set():
            stop_hud_overlay()
        # Shut down native TTS worker cleanly (drains queue + joins thread)
        if config.tts.enabled:
            _shutdown_tts()
            log("TTS worker stopped")
        _ka = getattr(devices, "keepalive", None)
        if _ka is not None:
            _ka.stop()
        devices.cleanup()
        log("Shutdown complete.")


def run() -> None:
    """CLI entry point -- called by vox.cli on 'heyvox start'."""
    main()


if __name__ == "__main__":
    run()
