"""
Kokoro TTS engine for vox.

Provides an interruptible queue-based TTS engine using Kokoro synthesis and
sounddevice playback. Features verbosity filtering, volume boost, audio
ducking (TTS-04), echo suppression flag IPC, and cross-process CLI control
via a command file.

Also retains check_voice_command() and execute_voice_command() for backward
compatibility with main.py's voice command dispatch (Phase 1/2 bridge).

Requirements: TTS-01 through TTS-06, AUDIO-12, AUDIO-09, CLI-05, CLI-06
"""

import os
import queue
import re
import subprocess
import threading
from enum import Enum
from typing import Optional

from vox.constants import (
    TTS_PLAYING_FLAG,
    TTS_MAX_HELD,
    TTS_SAMPLE_RATE,
    TTS_DEFAULT_VOICE,
    TTS_DEFAULT_SPEED,
    TTS_DEFAULT_VOLUME_BOOST,
    TTS_DEFAULT_DUCKING_PERCENT,
    TTS_CMD_FILE,
)


# ---------------------------------------------------------------------------
# Verbosity filtering
# ---------------------------------------------------------------------------

class Verbosity(str, Enum):
    """Controls how much of a TTS message is spoken."""
    FULL = "full"
    SUMMARY = "summary"
    SHORT = "short"
    SKIP = "skip"


def apply_verbosity(text: str, verbosity: "Verbosity | str") -> Optional[str]:
    """Filter text according to the given verbosity level.

    Args:
        text: The original message text.
        verbosity: One of Verbosity.FULL/SUMMARY/SHORT/SKIP (or string equivalent).

    Returns:
        Filtered string, or None if verbosity is SKIP.
    """
    if isinstance(verbosity, str):
        verbosity = Verbosity(verbosity)

    if verbosity == Verbosity.SKIP:
        return None

    if verbosity == Verbosity.FULL:
        return text

    if verbosity == Verbosity.SHORT:
        # Return first sentence (up to first .!? boundary), max 100 chars
        match = re.search(r'[.!?]', text)
        if match:
            sentence = text[:match.end()].strip()
            return sentence[:100]
        return text[:100]

    if verbosity == Verbosity.SUMMARY:
        # Truncate at 150 chars at word boundary, append "..." if truncated
        if len(text) <= 150:
            return text
        truncated = text[:150]
        # Find last space before the cut point
        last_space = truncated.rfind(' ')
        if last_space > 0:
            truncated = truncated[:last_space]
        return truncated + "..."

    return text


# ---------------------------------------------------------------------------
# Volume control helpers
# ---------------------------------------------------------------------------

def _get_system_volume() -> int:
    """Read current macOS output volume (0-100). Returns 50 on error."""
    try:
        result = subprocess.run(
            ["osascript", "-e", "output volume of (get volume settings)"],
            capture_output=True, text=True, timeout=3,
        )
        return int(result.stdout.strip())
    except Exception:
        return 50


def _set_system_volume(level: int) -> None:
    """Set macOS output volume (0-100)."""
    try:
        subprocess.run(
            ["osascript", "-e", f"set volume output volume {level}"],
            capture_output=True, timeout=3,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Echo suppression flag IPC
# ---------------------------------------------------------------------------

def _set_tts_flag(active: bool) -> None:
    """Write or remove the TTS_PLAYING_FLAG file.

    Must be called in try/finally around all playback so the flag is always
    cleaned up even if the TTS process crashes.

    Requirement: AUDIO-09
    """
    if active:
        try:
            open(TTS_PLAYING_FLAG, "w").close()
        except Exception:
            pass
    else:
        try:
            os.unlink(TTS_PLAYING_FLAG)
        except FileNotFoundError:
            pass
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Pipeline (lazy init)
# ---------------------------------------------------------------------------

_pipeline = None
_pipeline_lock = threading.Lock()


def _get_pipeline(lang_code: str = 'a'):
    """Lazily initialize and return the Kokoro KPipeline instance.

    Lazy import keeps module load time fast and avoids errors on systems
    without kokoro installed (e.g., CI environments).
    """
    global _pipeline
    with _pipeline_lock:
        if _pipeline is None:
            from kokoro import KPipeline  # noqa: lazy import
            _pipeline = KPipeline(lang_code=lang_code)
    return _pipeline


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_tts_queue: queue.Queue = queue.Queue()
_stop_event = threading.Event()
_muted: bool = False
_verbosity: Verbosity = Verbosity.FULL
_worker_thread: Optional[threading.Thread] = None

# Config values stored at start_worker time
_voice_default: str = TTS_DEFAULT_VOICE
_speed_default: float = TTS_DEFAULT_SPEED
_volume_boost: int = TTS_DEFAULT_VOLUME_BOOST
_ducking_percent: int = TTS_DEFAULT_DUCKING_PERCENT

# HUD client — optional, never crashes TTS worker (Phase 5)
_hud_client = None


def _hud_send(msg: dict) -> None:
    """Send a message to the HUD overlay. No-op if not connected.

    All HUD sends go through here so the TTS worker never has to guard
    against HUD failures — the HUD is strictly optional.
    """
    global _hud_client
    if _hud_client is None:
        return
    try:
        _hud_client.send(msg)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Command file IPC (cross-process CLI control)
# ---------------------------------------------------------------------------

def _check_cmd_file() -> None:
    """Read and process TTS_CMD_FILE if it exists.

    Commands: skip | stop | mute-toggle | quiet

    Read atomically (read + unlink before processing) to avoid race
    conditions between concurrent CLI invocations.
    """
    global _muted, _verbosity

    try:
        with open(TTS_CMD_FILE, 'r') as f:
            cmd = f.read().strip()
        os.unlink(TTS_CMD_FILE)
    except FileNotFoundError:
        return
    except Exception:
        return

    if cmd == "skip":
        _stop_event.set()
    elif cmd == "stop":
        _stop_event.set()
        # Drain the queue
        while not _tts_queue.empty():
            try:
                _tts_queue.get_nowait()
                _tts_queue.task_done()
            except queue.Empty:
                break
    elif cmd == "mute-toggle":
        _muted = not _muted
        if _muted:
            _stop_event.set()
            while not _tts_queue.empty():
                try:
                    _tts_queue.get_nowait()
                    _tts_queue.task_done()
                except queue.Empty:
                    break
    elif cmd == "quiet":
        _verbosity = Verbosity.SHORT


# ---------------------------------------------------------------------------
# Worker thread
# ---------------------------------------------------------------------------

def _tts_worker(voice_default: str, speed_default: float, volume_boost: int, ducking_percent: int) -> None:
    """Background worker: dequeue items and synthesize/play via Kokoro + sounddevice.

    Each item is a tuple of (text, voice, speed).
    Sentinel value None signals shutdown.
    """
    import sounddevice as sd  # noqa: lazy import — avoids conflict with pyaudio mic stream

    while True:
        # Check for cross-process commands before blocking on queue
        _check_cmd_file()

        item = _tts_queue.get()
        if item is None:
            _tts_queue.task_done()
            break

        text, voice, speed = item
        voice = voice or voice_default
        speed = speed or speed_default

        _hud_send({"type": "tts_start", "text": text})
        _hud_send({"type": "state", "state": "speaking"})

        # Clear stop flag for this new item
        _stop_event.clear()

        original_volume = _get_system_volume()

        try:
            _set_tts_flag(True)

            # Audio ducking: reduce system volume during TTS playback (TTS-04)
            if ducking_percent > 0 and ducking_percent < 100:
                ducked = max(0, int(original_volume * ducking_percent / 100))
                _set_system_volume(ducked)

            # Set boosted TTS playback volume
            boosted = min(100, original_volume + volume_boost)
            _set_system_volume(boosted)

            # Synthesize and play in chunks
            pipeline = _get_pipeline()
            for gs, ps, audio in pipeline(text, voice=voice, speed=speed):
                # Check for stop signal and cross-process commands between chunks
                _check_cmd_file()
                if _stop_event.is_set():
                    sd.stop()
                    break

                sd.play(audio, samplerate=TTS_SAMPLE_RATE)
                sd.wait()  # Non-blocking play + wait allows sd.stop() from another thread

                # Check again after finishing a chunk
                _check_cmd_file()
                if _stop_event.is_set():
                    sd.stop()
                    break

        except Exception:
            pass
        finally:
            _set_system_volume(original_volume)
            _set_tts_flag(False)
            _tts_queue.task_done()
            _hud_send({"type": "tts_end"})
            if _tts_queue.empty():
                _hud_send({"type": "state", "state": "idle"})
            _hud_send({"type": "queue_update", "count": _tts_queue.qsize()})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start_worker(config=None) -> None:
    """Start the background TTS worker thread.

    Reads initial verbosity, voice, speed, volume_boost, and ducking_percent
    from config. Safe to call multiple times — only starts one worker.

    Args:
        config: VoxConfig instance. If None, uses built-in defaults.
    """
    global _worker_thread, _verbosity, _voice_default, _speed_default
    global _volume_boost, _ducking_percent

    if _worker_thread is not None and _worker_thread.is_alive():
        return

    if config is not None:
        tts_cfg = config.tts
        _verbosity = Verbosity(tts_cfg.verbosity)
        _voice_default = tts_cfg.voice
        _speed_default = tts_cfg.speed
        _volume_boost = tts_cfg.volume_boost
        _ducking_percent = tts_cfg.ducking_percent

    _worker_thread = threading.Thread(
        target=_tts_worker,
        args=(_voice_default, _speed_default, _volume_boost, _ducking_percent),
        daemon=True,
    )
    _worker_thread.start()

    # Connect HUD client (optional — silent fail if HUD not running)
    # Requirement: HUD-08
    global _hud_client
    try:
        from vox.hud.ipc import HUDClient
        from vox.constants import HUD_SOCKET_PATH
        _hud_client = HUDClient(HUD_SOCKET_PATH)
        _hud_client.connect()
    except ImportError:
        pass
    except Exception:
        pass


def shutdown() -> None:
    """Gracefully shut down the TTS worker thread.

    Sends None sentinel to the queue and joins the worker thread.
    """
    global _worker_thread, _hud_client
    _tts_queue.put(None)
    if _worker_thread is not None:
        _worker_thread.join(timeout=10)
        _worker_thread = None
    if _hud_client:
        _hud_client.close()
        _hud_client = None


def speak(
    text: str,
    voice: str = None,
    speed: float = None,
    verbosity: str = None,
) -> None:
    """Enqueue text for TTS playback.

    Applies verbosity filtering (per-call override > session level).
    Drops oldest message if queue is at MAX_HELD capacity.
    No-ops if muted.

    Args:
        text: Text to speak.
        voice: Kokoro voice name override (None = use config default).
        speed: Speed multiplier override (None = use config default).
        verbosity: Verbosity mode override ("full"/"summary"/"short"/"skip").
    """
    global _muted

    if _muted:
        return

    # Resolve verbosity: per-call > session-level
    v = Verbosity(verbosity) if verbosity else _verbosity
    filtered = apply_verbosity(text, v)
    if filtered is None:
        return

    # Enforce queue cap: drop oldest if at limit
    while _tts_queue.qsize() >= TTS_MAX_HELD:
        try:
            _tts_queue.get_nowait()
            _tts_queue.task_done()
        except queue.Empty:
            break

    _tts_queue.put((filtered, voice, speed))
    _hud_send({"type": "queue_update", "count": _tts_queue.qsize()})


def interrupt() -> None:
    """Stop current TTS playback immediately.

    Called by main.py when wake word or PTT triggers. Stops sounddevice
    and sets the stop event so the worker drops the current item.

    Requirement: TTS-02
    """
    try:
        import sounddevice as sd  # noqa: lazy import
        sd.stop()
    except Exception:
        pass
    _stop_event.set()


def skip_current() -> None:
    """Stop current TTS item; worker picks up the next item in queue."""
    interrupt()


def stop_all() -> None:
    """Stop current playback and clear the entire queue."""
    interrupt()
    while not _tts_queue.empty():
        try:
            _tts_queue.get_nowait()
            _tts_queue.task_done()
        except queue.Empty:
            break


def clear_queue() -> None:
    """Drain the queue without stopping current playback."""
    while not _tts_queue.empty():
        try:
            _tts_queue.get_nowait()
            _tts_queue.task_done()
        except queue.Empty:
            break


def set_muted(muted: bool) -> None:
    """Mute or unmute TTS output. Muting also stops all current/queued playback."""
    global _muted
    _muted = muted
    if muted:
        stop_all()


def is_muted() -> bool:
    """Return current mute state."""
    return _muted


def set_verbosity(level: str) -> None:
    """Set session-level verbosity mode."""
    global _verbosity
    _verbosity = Verbosity(level)


def get_verbosity() -> str:
    """Return current session-level verbosity as string."""
    return _verbosity.value


# ---------------------------------------------------------------------------
# Backward compatibility: voice command interception (Phase 1/2 bridge)
#
# These functions are called from main.py to intercept spoken control
# commands (skip, mute, etc.) in the transcription pipeline.
# They dispatch to an optional external TTS script (deprecated, Phase 1)
# or can be wired to the native engine in Phase 3.
# ---------------------------------------------------------------------------

# Voice commands: pattern -> (action_key, user-visible feedback string)
VOICE_COMMANDS = {
    r"^(play\s+)?next(\s+message)?$": ("tts-next", "Playing next message"),
    r"^skip(\s+(this|current|audio))?$": ("tts-skip", "Skipping"),
    r"^stop(\s+(all|audio|everything))?$": ("tts-stop", "Stopping all audio"),
    r"^(toggle\s+)?mute$": ("tts-mute", "Toggling mute"),
    r"^replay(\s+last)?$": ("tts-replay", "Replaying last message"),
}


def check_voice_command(text: str):
    """Check if a transcription string is a voice command.

    Args:
        text: Raw transcription text.

    Returns:
        Tuple of (action_key, feedback_str) if matched, else None.
    """
    clean = text.strip().lower().rstrip(".,!?")
    for pattern, (action, feedback) in VOICE_COMMANDS.items():
        if re.match(pattern, clean):
            return action, feedback
    return None


def _make_actions(tts_script_path: str) -> dict:
    """Build the action dispatch table for a given TTS script path."""
    def _run(cmd):
        return subprocess.run(["bash", tts_script_path, cmd], timeout=5)

    return {
        "tts-next": lambda: _run("next"),
        "tts-skip": lambda: _run("skip"),
        "tts-stop": lambda: _run("stop"),
        "tts-mute": lambda: _run("mute"),
        "tts-replay": lambda: _run("replay"),
    }


def execute_voice_command(action_key: str, feedback: str, tts_script_path: str = None, log_fn=None) -> None:
    """Execute a voice command action.

    When tts_script_path is None or empty, the command is logged as a warning
    and skipped — no crash, no exception. This allows the package to run without
    any TTS configuration.

    Args:
        action_key: Action identifier from VOICE_COMMANDS (e.g. "tts-skip").
        feedback: Human-readable description for logging.
        tts_script_path: Absolute path to the TTS control script.
            If None or empty, the command is logged but not executed.
        log_fn: Optional callable(str) for log output.

    Requirement: DECP-05
    """
    def _log(msg):
        if log_fn:
            log_fn(msg)
        else:
            print(msg, flush=True)

    _log(f"Voice command: {action_key} ({feedback})")

    if not tts_script_path:
        _log(
            f"Voice command '{action_key}' ignored: TTS not configured "
            f"(set tts.script_path in ~/.config/vox/config.yaml)"
        )
        return

    try:
        actions = _make_actions(tts_script_path)
        actions[action_key]()
    except Exception as e:
        _log(f"Voice command error: {e}")
