"""
TTS delegation layer for heyvox.

Delegates all TTS operations to Herald (the dedicated TTS orchestration
service). HeyVox handles voice input; Herald handles voice output.

Herald provides: Kokoro TTS daemon, queue management, multi-part streaming,
mood/language detection, workspace-aware playback, audio ducking, and media
pause/resume.

Also retains check_voice_command() and execute_voice_command() for backward
compatibility with main.py's voice command dispatch.

Requirements: TTS-01 through TTS-06
"""

import logging
import os
import re
import subprocess
import threading
from enum import Enum
from typing import Optional

log = logging.getLogger(__name__)

HERALD_CMD = "herald"
_SUBPROCESS_TIMEOUT = 5


# ---------------------------------------------------------------------------
# Verbosity filtering
# ---------------------------------------------------------------------------

class Verbosity(str, Enum):
    """Controls TTS playback mode."""
    FULL = "full"
    SUMMARY = "summary"  # Kept for backward compat — treated as FULL
    SHORT = "short"
    SKIP = "skip"


def apply_verbosity(text: str, verbosity: "Verbosity | str") -> Optional[str]:
    """Filter text according to the given TTS playback mode.

    Modes:
    - full/summary: Speak the entire text as-is
    - short: Speak only the first sentence
    - skip: Drop silently (return None)
    """
    if isinstance(verbosity, str):
        verbosity = Verbosity(verbosity)

    if verbosity == Verbosity.SKIP:
        return None
    if verbosity == Verbosity.SHORT:
        match = re.search(r'[.!?]', text)
        if match:
            return text[:match.end()].strip()
        return text[:100]
    # FULL and SUMMARY both play everything
    return text


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_tts_lock = threading.Lock()
_muted: bool = False
_verbosity: Verbosity = Verbosity.FULL
_style: str = "detailed"
from heyvox.constants import TTS_STYLE_FILE as _STYLE_FILE

# Style descriptions — returned to Claude via MCP so it knows how to write TTS
TTS_STYLE_PROMPTS = {
    "detailed": "Explain what you found and what you did — not just 'fixed it' but the key insight (what was wrong, what changed, why). 3-5 sentences, ~400-800 chars.",
    "concise": "Key takeaway only. One or two short sentences, ~100-200 chars. Front-load the most important information.",
    "technical": "Include function names, file paths, error messages, and what changed in the code. Be precise and specific. 2-4 sentences, ~300-600 chars.",
    "casual": "Talk like a coworker who just finished the task. Conversational, friendly, maybe a light observation. 2-3 sentences, ~200-400 chars.",
    "briefing": "Comprehensive spoken briefing — the user is listening, not reading, and needs enough context to act. First sentence: the key takeaway or decision at hand. Then cover what you did, what you found, what's surprising, any tradeoffs considered, and any open question the user must weigh in on. Include specific numbers, names, and paths — not just 'done'. 6-10 sentences, ~800-1500 chars. If there is genuinely nothing decision-relevant, say so in one sentence and stop — don't pad.",
}


# ---------------------------------------------------------------------------
# Herald CLI helpers
# ---------------------------------------------------------------------------

_herald_warned = False

def _herald(cmd: str, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess:
    """Call herald CLI command. Returns CompletedProcess, never raises."""
    global _herald_warned
    try:
        return subprocess.run(
            [HERALD_CMD, cmd, *args],
            input=input_text,
            capture_output=True, text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
    except FileNotFoundError:
        if not _herald_warned:
            log.warning(f"Herald not found at {HERALD_CMD}. TTS disabled.")
            _herald_warned = True
        return subprocess.CompletedProcess([HERALD_CMD, cmd], 1, "", "herald not found")
    except subprocess.TimeoutExpired:
        log.warning(f"Herald command '{cmd}' timed out")
        return subprocess.CompletedProcess([HERALD_CMD, cmd], 1, "", "timeout")
    except Exception as e:
        log.warning(f"Herald command '{cmd}' failed: {e}")
        return subprocess.CompletedProcess([HERALD_CMD, cmd], 1, "", str(e))


# ---------------------------------------------------------------------------
# Public API (delegates to Herald)
# ---------------------------------------------------------------------------

def start_worker(config=None) -> None:
    """Initialize TTS settings from config. No worker thread needed — Herald runs independently."""
    global _verbosity, _muted, _style

    if config is not None:
        # Use set_verbosity to persist to file — ensures Herald's bash scripts
        # see the same value (otherwise Herald defaults to "full")
        set_verbosity(config.tts.verbosity)
        set_tts_style(config.tts.style)

        # Pass engine selection to Herald via env var (read by worker.py)
        os.environ["HEYVOX_TTS_ENGINE"] = config.tts.engine


def shutdown() -> None:
    """No-op — Herald manages its own lifecycle."""
    pass


def speak(
    text: str,
    voice: str | None = None,
    speed: float | None = None,
    verbosity: str | None = None,
) -> None:
    """Send text to Herald for TTS playback.

    Applies verbosity filtering before sending.
    No-ops if muted.
    """
    if is_muted():
        return

    # Resolve verbosity: per-call > file-level > session-level
    if verbosity:
        v = Verbosity(verbosity)
    else:
        v = Verbosity(get_verbosity())
    filtered = apply_verbosity(text, v)
    if filtered is None:
        return

    # Register with echo suppression buffer before speaking so filter_tts_echo()
    # has something to match against when the mic picks up speaker output.
    try:
        from heyvox.audio.echo import register_tts_text
        register_tts_text(filtered)
    except Exception:
        pass  # Echo module not loaded or not initialised — skip silently

    _herald("speak", input_text=filtered)


def set_recording(active: bool) -> None:
    """Signal Herald that recording is active/inactive.

    Uses Herald's pause/resume API for clean coordination.
    """
    if active:
        _herald("pause")
    else:
        _herald("resume")


def interrupt() -> None:
    """Stop current TTS playback immediately (selective: preserves unrelated queued messages)."""
    _herald("interrupt")


def skip_current() -> None:
    """Stop current TTS item; Herald picks up the next item in queue."""
    _herald("skip")


def stop_all() -> None:
    """Stop current playback and clear the entire queue."""
    _herald("stop")


def clear_queue() -> None:
    """Clear queued messages without stopping current playback."""
    _herald("skip")


def set_muted(muted: bool) -> None:
    """Mute or unmute TTS output. Syncs file flags for cross-process consistency."""
    global _muted
    with _tts_lock:
        _muted = muted
        from heyvox.constants import HERALD_MUTE_FLAG
        _MUTE_FLAGS = [HERALD_MUTE_FLAG]
        if muted:
            for flag in _MUTE_FLAGS:
                try:
                    open(flag, "w").close()
                except OSError:
                    pass
        else:
            for flag in _MUTE_FLAGS:
                try:
                    os.remove(flag)
                except FileNotFoundError:
                    pass
    # stop_all() outside lock to avoid holding lock during subprocess call
    if muted:
        stop_all()


def _is_system_muted() -> bool:
    """Check if macOS system audio is muted."""
    try:
        result = subprocess.run(
            ["osascript", "-e", "output muted of (get volume settings)"],
            capture_output=True, text=True, timeout=2,
        )
        return result.stdout.strip() == "true"
    except Exception:
        return False


def is_muted() -> bool:
    """Return current mute state (in-memory flag, file flag, or macOS system mute)."""
    from heyvox.constants import HERALD_MUTE_FLAG
    return _muted or os.path.exists(HERALD_MUTE_FLAG) or _is_system_muted()


def set_verbosity(level: str) -> None:
    """Set verbosity mode (persisted to file for cross-process access).

    Also syncs the legacy file-flag mute mechanism: "skip" creates mute
    flags, anything else removes them. This keeps Herald's bash scripts
    and the Python is_muted() check consistent.
    """
    global _verbosity, _muted
    with _tts_lock:
        _verbosity = Verbosity(level)
        # Write to shared file so Herald hooks/watcher can read it
        from heyvox.constants import VERBOSITY_FILE
        try:
            if level == "full":
                # Remove file = default (full)
                os.remove(VERBOSITY_FILE)
            else:
                with open(VERBOSITY_FILE, "w") as f:
                    f.write(level)
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning(f"Failed to write verbosity file: {e}")
        # Sync legacy mute flags and in-memory state
        from heyvox.constants import HERALD_MUTE_FLAG
        _MUTE_FLAGS = [HERALD_MUTE_FLAG]
        if level == "skip":
            _muted = True
            for flag in _MUTE_FLAGS:
                try:
                    open(flag, "w").close()
                except OSError:
                    pass
        else:
            _muted = False
            for flag in _MUTE_FLAGS:
                try:
                    os.remove(flag)
                except FileNotFoundError:
                    pass


def get_verbosity() -> str:
    """Return current verbosity (reads from shared file for cross-process consistency)."""
    from heyvox.constants import VERBOSITY_FILE
    try:
        with open(VERBOSITY_FILE) as f:
            level = f.read().strip()
        if level in ("full", "summary", "short", "skip"):
            return level
    except FileNotFoundError:
        pass
    except OSError:
        pass
    return _verbosity.value


def get_tts_style() -> str:
    """Return the current TTS style name (reads from shared file for cross-process consistency)."""
    try:
        with open(_STYLE_FILE) as f:
            style = f.read().strip()
        if style in TTS_STYLE_PROMPTS:
            return style
    except FileNotFoundError:
        pass
    except OSError:
        pass
    return _style


def get_tts_style_prompt() -> str:
    """Return the style instruction for Claude to follow when writing <tts> blocks."""
    return TTS_STYLE_PROMPTS.get(get_tts_style(), TTS_STYLE_PROMPTS["detailed"])


def set_tts_style(style: str) -> None:
    """Set the TTS style and persist to config + shared file for cross-process access."""
    global _style
    valid = set(TTS_STYLE_PROMPTS.keys())
    if style not in valid:
        log.warning(f"Invalid TTS style '{style}', ignoring (valid: {valid})")
        return
    with _tts_lock:
        _style = style
        # Write to shared file so MCP server (separate process) can read it
        try:
            if style == "detailed":
                # Remove file = default (detailed)
                os.remove(_STYLE_FILE)
            else:
                with open(_STYLE_FILE, "w") as f:
                    f.write(style)
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning(f"Failed to write style file: {e}")
    try:
        from heyvox.config import update_config
        update_config(**{"tts.style": style})
    except Exception as e:
        log.warning(f"Failed to persist TTS style: {e}")


# ---------------------------------------------------------------------------
# Voice command interception
# ---------------------------------------------------------------------------

VOICE_COMMANDS = {
    r"^(play\s+)?next(\s+message)?$": ("tts-next", "Playing next message"),
    r"^skip(\s+(this|current|audio))?$": ("tts-skip", "Skipping"),
    r"^stop(\s+(all|audio|everything))?$": ("tts-stop", "Stopping all audio"),
    r"^(toggle\s+)?mute$": ("tts-mute", "Toggling mute"),
    r"^replay(\s+last)?$": ("tts-replay", "Replaying last message"),
    # Verbosity voice commands
    r"^be\s+quiet$": ("verbosity-short", "First sentence mode"),
    r"^be\s+brief$": ("verbosity-short", "First sentence mode"),
    r"^(be\s+)?verbose$": ("verbosity-full", "Speak all"),
    r"^full\s+verbosity$": ("verbosity-full", "Speak all"),
    r"^shut\s+up$": ("verbosity-skip", "Muted"),
    r"^(be\s+)?silent$": ("verbosity-skip", "Muted"),
    r"^speak\s+normally$": ("verbosity-full", "Speak all"),
}


def check_voice_command(text: str):
    """Check if a transcription string is a voice command."""
    clean = text.strip().lower().rstrip(".,!?")
    for pattern, (action, feedback) in VOICE_COMMANDS.items():
        if re.match(pattern, clean):
            return action, feedback
    return None


def execute_voice_command(action_key: str, feedback: str, tts_script_path: str = None, log_fn=None) -> None:
    """Execute a voice command by delegating to Herald CLI."""
    def _log(msg):
        if log_fn:
            log_fn(msg)
        else:
            print(msg, flush=True)

    _log(f"Voice command: {action_key} ({feedback})")

    # Verbosity voice commands
    verbosity_map = {
        "verbosity-full": "full",
        "verbosity-short": "short",
        "verbosity-skip": "skip",
    }
    if action_key in verbosity_map:
        level = verbosity_map[action_key]
        set_verbosity(level)
        _log(f"Verbosity set to {level}")
        return

    # Map action keys to Herald commands
    herald_cmds = {
        "tts-next": "skip",    # Herald skips to next queued item
        "tts-skip": "skip",
        "tts-stop": "stop",
        "tts-mute": "mute",
        "tts-replay": "replay",
    }

    cmd = herald_cmds.get(action_key)
    if cmd:
        result = _herald(cmd)
        if result.returncode != 0:
            _log(f"Herald '{cmd}' failed: {result.stderr.strip()}")
    else:
        _log(f"Unknown voice command: {action_key}")
