"""
TTS voice command interception for vox.

Checks if a transcribed utterance is a TTS control command (skip, next, mute, etc.)
and executes it via the tts-ctl.sh hook script. If no TTS script is configured,
voice commands are logged and skipped gracefully.

Full TTS output (Kokoro) is implemented in Phase 4 via the MCP server.
"""

import re
import subprocess


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
    """Build the action dispatch table for a given tts-ctl script path."""
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

    Args:
        action_key: Action identifier from VOICE_COMMANDS (e.g. "tts-skip").
        feedback: Human-readable description for logging.
        tts_script_path: Absolute path to tts-ctl.sh hook script.
            If None, the command is logged but not executed.
        log_fn: Optional callable(str) for log output.
    """
    def _log(msg):
        if log_fn:
            log_fn(msg)
        else:
            print(msg, flush=True)

    _log(f"Voice command: {action_key} ({feedback})")

    if tts_script_path is None:
        _log(f"WARNING: tts_script_path not configured, skipping voice command execution")
        return

    try:
        actions = _make_actions(tts_script_path)
        actions[action_key]()
    except Exception as e:
        _log(f"Voice command error: {e}")
