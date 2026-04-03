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
from enum import Enum
from typing import Optional

log = logging.getLogger(__name__)

HERALD_CMD = "herald"
_SUBPROCESS_TIMEOUT = 5


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
    """Filter text according to the given verbosity level."""
    if isinstance(verbosity, str):
        verbosity = Verbosity(verbosity)

    if verbosity == Verbosity.SKIP:
        return None
    if verbosity == Verbosity.FULL:
        return text
    if verbosity == Verbosity.SHORT:
        match = re.search(r'[.!?]', text)
        if match:
            sentence = text[:match.end()].strip()
            return sentence[:100]
        return text[:100]
    if verbosity == Verbosity.SUMMARY:
        if len(text) <= 150:
            return text
        truncated = text[:150]
        last_space = truncated.rfind(' ')
        if last_space > 0:
            truncated = truncated[:last_space]
        return truncated + "..."
    return text


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_muted: bool = False
_verbosity: Verbosity = Verbosity.FULL


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
    global _verbosity, _muted

    if config is not None:
        _verbosity = Verbosity(config.tts.verbosity)


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
    """Stop current TTS playback immediately."""
    _herald("skip")


def skip_current() -> None:
    """Stop current TTS item; Herald picks up the next item in queue."""
    _herald("skip")


def stop_all() -> None:
    """Stop current playback and clear the entire queue."""
    _herald("stop")


def clear_queue() -> None:
    """Clear queued messages without stopping current playback."""
    # Herald doesn't have a separate clear-queue command; stop clears everything
    _herald("stop")


def set_muted(muted: bool) -> None:
    """Mute or unmute TTS output."""
    global _muted
    _muted = muted
    if muted:
        stop_all()


def is_muted() -> bool:
    """Return current mute state (in-memory flag OR file flag from HUD toggle)."""
    return _muted or os.path.exists("/tmp/claude-tts-mute")


def set_verbosity(level: str) -> None:
    """Set verbosity mode (persisted to file for cross-process access)."""
    global _verbosity
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
    r"^be\s+quiet$": ("verbosity-short", "Short mode"),
    r"^be\s+brief$": ("verbosity-short", "Short mode"),
    r"^(be\s+)?verbose$": ("verbosity-full", "Full mode"),
    r"^full\s+verbosity$": ("verbosity-full", "Full mode"),
    r"^shut\s+up$": ("verbosity-skip", "Silent mode"),
    r"^(be\s+)?silent$": ("verbosity-skip", "Silent mode"),
    r"^summary(\s+mode)?$": ("verbosity-summary", "Summary mode"),
    r"^speak\s+normally$": ("verbosity-full", "Full mode"),
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
        "verbosity-summary": "summary",
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
