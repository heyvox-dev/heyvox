"""
Vox main event loop.

Orchestrates the wake word listener, push-to-talk, STT transcription,
text injection, and recording indicator. Loads configuration from
~/.config/heyvox/config.yaml via the pydantic config system.

Entry point: heyvox.cli calls run() which calls main().

Requirement: CONF-01, DECP-01 through DECP-06
"""

import os
import sys
import time
import signal
import threading
import numpy as np

from heyvox.config import load_config, HeyvoxConfig, CONFIG_DIR
from heyvox.app_context import AppContext
from heyvox.device_manager import DeviceManager
from heyvox.audio.profile import MicProfileManager
from heyvox.recording import RecordingStateMachine
from heyvox.constants import (
    RECORDING_FLAG,
    TTS_PLAYING_FLAG,
    TTS_PLAYING_MAX_AGE_SECS,
    HUD_SOCKET_PATH,
    STT_DEBUG_DIR,
    LOG_FILE_DEFAULT,
    HEYVOX_PID_FILE,
    HEYVOX_HEARTBEAT_FILE,
    HEYVOX_MEDIA_PAUSED_REC,
    HEYVOX_MEDIA_PAUSED_PREFIX,
    HERALD_MEDIA_PAUSED_PREFIX,
    HERALD_PAUSE_FLAG,
    HERALD_MUTE_FLAG,
    HERALD_AMBIENT_FLAG,
    HERALD_MODE_FILE,
    HERALD_LAST_PLAY,
    HERALD_WORKSPACE_FILE,
    HERALD_GENERATING_WAV_PREFIX,
    HERALD_PLAYING_PID,
    VERBOSITY_FILE,
    GRACE_AFTER_TTS,
    ensure_run_dirs,
    cleanup_ipc_files,
)
from heyvox.audio.cues import audio_cue, is_suppressed, get_cues_dir
from heyvox.audio.stt import init_local_stt
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
_HUD_RECONNECT_INTERVAL = 1.0  # Retry every 1s (fast reconnect after overlay startup)
_HUD_LEVEL_INTERVAL = 0.05    # 20fps throttle for audio_level messages


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

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGUSR1, handle_cancel)

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
    def hud_send(msg: dict) -> None:
        """Send a message to the HUD overlay. No-op if not connected."""
        if ctx.hud_client is None:
            log(f"[HUD-DBG] hud_client is None, skipping {msg.get('type')}")
            return
        if ctx.hud_client._sock is None:
            now = time.time()
            if now - ctx.hud_last_reconnect < _HUD_RECONNECT_INTERVAL:
                return
            ctx.hud_last_reconnect = now
            log(f"[HUD-DBG] Attempting reconnect for {msg.get('type')}...")
            try:
                ctx.hud_client.reconnect()
            except Exception as e:
                log(f"[HUD-DBG] Reconnect failed: {e}")
                return
            if ctx.hud_client._sock is None:
                log("[HUD-DBG] Reconnect succeeded but sock still None")
                return
            log("[HUD-DBG] Reconnected!")
        try:
            ctx.hud_client.send(msg)
            log(f"[HUD-DBG] Sent {msg.get('type')}: {msg.get('state', '')}")
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
        )

    # Build adapter and create RecordingStateMachine
    ctx.adapter = _build_adapter(config)
    log(f"Target mode: {config.target_mode} (adapter: {type(ctx.adapter).__name__})")

    recording = RecordingStateMachine(ctx=ctx, config=config, log_fn=log, hud_send=hud_send)

    # Start push-to-talk listener if enabled
    if config.push_to_talk.enabled:
        from heyvox.input.ptt import start_ptt_listener

        ptt_callbacks = {
            "on_start": lambda: recording.start(ptt=True),
            "on_stop": lambda: recording.stop(),
            "on_cancel_transcription": lambda: ctx.cancel_transcription.set(),
            "on_cancel_recording": lambda: recording.cancel(),
            "on_cancel_tts": lambda: _stop_tts_from_escape(hud_send),
            "is_busy": lambda: ctx.busy,
            "is_recording": lambda: ctx.is_recording,
            "is_speaking": lambda: (
                os.path.exists(TTS_PLAYING_FLAG) or os.path.exists(HERALD_PLAYING_PID)
            ),
        }
        start_ptt_listener(config.push_to_talk.key, ptt_callbacks, log_fn=log)

    # Load wake word models
    from heyvox.audio.wakeword import load_models
    model, use_separate_words = load_models(
        start_word, stop_word, config.wake_words.models_dir,
        also_load=config.wake_words.also_load,
    )
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

    # Local aliases for frequently read config values (avoid attribute lookups in hot loop)
    threshold = config.threshold
    cooldown = config.cooldown_secs
    sample_rate = config.audio.sample_rate
    chunk_size = config.audio.chunk_size
    silence_timeout = config.silence_timeout_secs
    silence_threshold = config.silence_threshold
    # Override silence_threshold from device profile if available
    if devices.active_profile and devices.active_profile.silence_threshold is not None:
        silence_threshold = devices.active_profile.silence_threshold
    max_recording_secs = getattr(config, 'max_recording_secs', 30.0)
    start_word = config.wake_words.start
    stop_word = config.wake_words.stop

    cues_dir = get_cues_dir(config.cues_dir)
    last_trigger = 0.0
    consecutive_errors = 0

    # Hard negative mining: passively collect audio clips for wake word training
    _neg_collector = None
    if config.wake_words.collect_negatives:
        from heyvox.audio.negative_collector import NegativeCollector
        neg_dir = config.wake_words.negatives_dir
        if not neg_dir:
            neg_dir = os.path.join(str(CONFIG_DIR), "negatives")
        _neg_collector = NegativeCollector(
            negatives_dir=neg_dir,
            max_clips=config.wake_words.negatives_max_clips,
            score_range=tuple(config.wake_words.negatives_score_range),
            interval_secs=config.wake_words.negatives_interval_secs,
            sample_rate=sample_rate,
        )
        log(f"Training mining enabled → negatives: {neg_dir}, positives: {os.path.join(os.path.dirname(neg_dir), 'positives')} (max {config.wake_words.negatives_max_clips} clips each)")

    # Pre-roll ring buffer: captures ~500ms of audio before wake word trigger
    # so the first words of the command aren't clipped.
    from collections import deque
    _PREROLL_CHUNKS = max(1, int(0.5 * sample_rate / chunk_size))  # ~500ms
    _preroll_buffer: deque = deque(maxlen=_PREROLL_CHUNKS)

    # Memory watchdog
    _MEM_WARN_MB = 2000
    _MEM_CRITICAL_MB = 2500
    _last_mem_check = time.time()
    _MEM_CHECK_INTERVAL = 15.0

    # SIGKILL-proof heartbeat
    _HEARTBEAT_FILE = HEYVOX_HEARTBEAT_FILE
    _HEARTBEAT_INTERVAL = 10.0
    _last_heartbeat = 0.0

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
    _is_rec: bool = False
    _last_model_reset: float = 0.0  # Periodic model reset during recording

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
            if _now_hb - _last_heartbeat >= _HEARTBEAT_INTERVAL:
                _last_heartbeat = _now_hb
                try:
                    with open(_HEARTBEAT_FILE, "w") as _hbf:
                        _hbf.write(f"{int(_now_hb)}\n")
                except Exception:
                    pass

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
                    if _stall > 5.0:
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
                consecutive_errors = 0
                ctx.last_read_time = time.monotonic()

                # AUDIO-13: track last time we saw real audio (level >= 10)
                if int(np.abs(audio).max()) >= 10:
                    ctx.last_good_audio_time = time.time()

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
                # Update silence_threshold from new device profile after reinit
                if devices.active_profile and devices.active_profile.silence_threshold is not None:
                    silence_threshold = devices.active_profile.silence_threshold
                else:
                    silence_threshold = config.silence_threshold
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
                    _preroll_buffer.append(audio.copy())

            # Reset first-speech tracker and model reset timer when recording starts
            if _is_rec and not _was_rec:
                _first_speech_time = 0.0
                _last_model_reset = time.time()

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

            # After scan, update silence_threshold if device changed
            if devices.active_profile and devices.active_profile.silence_threshold is not None:
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
                    recording.stop()
                    continue

            # Track when user first starts speaking during recording
            if _is_rec and not _is_ptt:
                _level = int(np.abs(audio).max())
                if _level >= silence_threshold and _first_speech_time == 0.0:
                    _first_speech_time = time.time()

            # Silence watchdog — two modes:
            # 1) No speech yet: cancel after 5s (false wake word trigger)
            # 2) After speech: stop+transcribe after silence_timeout (4s)
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
                                recording.stop()
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
                    _release_recording_guard()
                    hud_send({"type": "state", "state": "idle"})
                    # Fall through to wake word processing
                else:
                    continue
            else:
                _busy_since = 0.0

            # Suppress wake word detection while audio cue plays
            if is_suppressed():
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

            model.predict(audio)

            # Periodic model reset during recording — clears accumulated speech
            # features so the stop wake word can be detected even after long
            # continuous speech (without this, rapid "Hey Vox" after talking
            # scores below threshold because the feature window is polluted).
            _MODEL_RESET_INTERVAL = 2.5  # seconds
            if _is_rec and time.time() - _last_model_reset > _MODEL_RESET_INTERVAL:
                model.reset()
                _last_model_reset = time.time()

            # Hard negative mining: feed audio and check scores
            if _neg_collector is not None and not _is_rec:
                _neg_collector.feed(audio)
                _neg_max_score = max(
                    (score[-1] for score in model.prediction_buffer.values()),
                    default=0.0,
                )
                _neg_collector.maybe_save(_neg_max_score)

            # ECHO-02: Dynamic threshold in speaker mode
            _speaker_mult = (
                config.echo_suppression.speaker_threshold_multiplier
                if not devices.headset_mode else 1.0
            )

            # Cooldown is shorter during recording
            stop_cooldown = min(cooldown, 0.5)

            _model_thresholds = config.wake_words.model_thresholds

            for ww_name, score in model.prediction_buffer.items():
                s = score[-1]
                base_thr = _model_thresholds.get(ww_name, threshold)
                active_threshold = (base_thr * _speaker_mult * 0.85) if _is_rec else (base_thr * _speaker_mult)
                active_cooldown = stop_cooldown if _is_rec else cooldown
                log_threshold = active_threshold * 0.5
                if s > log_threshold:
                    triggered = s > active_threshold
                    msg = f"  [{ww_name}] score={s:.3f} (thr={active_threshold:.2f}) {'>>> TRIGGER' if triggered else ''}"
                    log(msg)
                    if triggered:
                        _safe_stderr(f"[wakeword] {msg.strip()}")
                if s > active_threshold:
                    now = time.time()
                    if now - last_trigger > active_cooldown:
                        # Save confirmed positive for training
                        if _neg_collector is not None and not _is_rec:
                            _neg_collector.save_positive(s)
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
                                recording.start(preroll=_preroll_buffer)
                            elif stop_word in ww_name and _is_rec:
                                recording.stop()
                        else:
                            if not _is_rec:
                                recording.start(preroll=_preroll_buffer)
                            else:
                                recording.stop()
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

    ctx, devices, recording, model, use_separate_words, hud_send, aec_active, profile_manager = _setup(config)

    from heyvox.audio.tts import shutdown as _shutdown_tts

    try:
        _run_loop(ctx, devices, recording, config, model, use_separate_words, hud_send, aec_active,
                  profile_manager=profile_manager)
    finally:
        log("Cleaning up...")
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
        devices.cleanup()
        log("Shutdown complete.")


def run() -> None:
    """CLI entry point -- called by vox.cli on 'heyvox start'."""
    main()


if __name__ == "__main__":
    run()
