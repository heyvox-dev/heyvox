"""Herald CLI — Python commands for TTS orchestration.

Replaces the bash herald CLI with direct Python calls.
"""

import os
import sys
import logging

log = logging.getLogger(__name__)


def dispatch(args: list[str]) -> int:
    """Dispatch Herald CLI command. Returns exit code."""
    if not args:
        print("Usage: herald <command> [args]", file=sys.stderr)
        return 1

    cmd = args[0]
    rest = args[1:]

    if cmd == "speak":
        return _cmd_speak(rest)
    elif cmd == "pause":
        return _cmd_pause()
    elif cmd == "resume":
        return _cmd_resume()
    elif cmd == "skip":
        return _cmd_skip()
    elif cmd == "stop":
        return _cmd_stop()
    elif cmd == "interrupt":
        return _cmd_interrupt()
    elif cmd == "mute":
        return _cmd_mute()
    elif cmd == "status":
        return _cmd_status()
    elif cmd == "queue":
        return _cmd_queue()
    elif cmd == "orchestrator":
        return _cmd_orchestrator()
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        return 1


def _cmd_speak(args: list[str]) -> int:
    """Speak text via Herald worker."""
    text = " ".join(args) if args else sys.stdin.read()
    if not text.strip():
        return 1
    from heyvox.herald.worker import HeraldWorker
    worker = HeraldWorker()
    # Wrap in <tts> if not already wrapped
    if "<tts>" not in text:
        text = f"<tts>{text}</tts>"
    ok = worker.process_response(text)
    return 0 if ok else 1


def _cmd_pause() -> int:
    """Create pause flag."""
    from heyvox.constants import HERALD_PAUSE_FLAG
    from pathlib import Path
    Path(HERALD_PAUSE_FLAG).touch()
    try:
        from heyvox.ipc import update_state
        update_state({"paused": True})
    except Exception:
        pass
    return 0


def _cmd_resume() -> int:
    """Remove pause flag."""
    from heyvox.constants import HERALD_PAUSE_FLAG
    try:
        os.unlink(HERALD_PAUSE_FLAG)
    except FileNotFoundError:
        pass
    try:
        from heyvox.ipc import update_state
        update_state({"paused": False})
    except Exception:
        pass
    return 0


def _cmd_skip() -> int:
    """Clear queue directory."""
    import glob
    from heyvox.constants import HERALD_QUEUE_DIR
    for f in glob.glob(os.path.join(HERALD_QUEUE_DIR, "*")):
        try:
            os.unlink(f)
        except OSError:
            pass
    return 0


def _cmd_stop() -> int:
    """Kill current afplay and clear entire queue (D-07: Escape behavior).

    Clears TTS state immediately so echo suppression doesn't stay muted.
    """
    _kill_afplay()
    _clear_tts_state()
    return _cmd_skip()  # Clear all queue files


def _cmd_interrupt() -> int:
    """Kill current afplay; let orchestrator purge current message parts only (D-06).

    The orchestrator detects non-zero afplay exit, then calls _purge_message_parts()
    which only removes files matching the current message prefix. Unrelated queued
    messages survive.
    """
    _kill_afplay()
    _clear_tts_state()
    return 0


def _kill_afplay() -> None:
    """Kill afplay process by reading HERALD_PLAYING_PID."""
    import signal
    from heyvox.constants import HERALD_PLAYING_PID
    if not os.path.exists(HERALD_PLAYING_PID):
        return
    try:
        pid = int(open(HERALD_PLAYING_PID).read().strip())
        os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError):
        pass


def _clear_tts_state() -> None:
    """Clear TTS flag and IPC state immediately (don't wait for orchestrator)."""
    from heyvox.constants import TTS_PLAYING_FLAG
    try:
        os.unlink(TTS_PLAYING_FLAG)
    except FileNotFoundError:
        pass
    try:
        from heyvox.ipc import update_state
        update_state({"tts_playing": False})
    except Exception:
        pass


def _cmd_mute() -> int:
    """Toggle mute flag."""
    from heyvox.constants import HERALD_MUTE_FLAG
    from pathlib import Path
    if os.path.exists(HERALD_MUTE_FLAG):
        os.unlink(HERALD_MUTE_FLAG)
        muted = False
    else:
        Path(HERALD_MUTE_FLAG).touch()
        muted = True
    try:
        from heyvox.ipc import update_state
        update_state({"muted": muted})
    except Exception:
        pass
    return 0


def _cmd_status() -> int:
    """Print Herald status."""
    from heyvox.constants import HERALD_ORCH_PID, HERALD_QUEUE_DIR, HERALD_HOLD_DIR, HERALD_MUTE_FLAG, HERALD_PAUSE_FLAG
    orch_running = False
    if os.path.exists(HERALD_ORCH_PID):
        try:
            pid = int(open(HERALD_ORCH_PID).read().strip())
            os.kill(pid, 0)
            orch_running = True
        except (OSError, ValueError):
            pass
    queue_count = len([f for f in os.listdir(HERALD_QUEUE_DIR) if f.endswith(".wav")]) if os.path.isdir(HERALD_QUEUE_DIR) else 0
    hold_count = len([f for f in os.listdir(HERALD_HOLD_DIR) if f.endswith(".wav")]) if os.path.isdir(HERALD_HOLD_DIR) else 0
    print(f"orchestrator: {'running' if orch_running else 'stopped'}")
    print(f"queue: {queue_count} WAVs")
    print(f"held: {hold_count} WAVs")
    print(f"muted: {os.path.exists(HERALD_MUTE_FLAG)}")
    print(f"paused: {os.path.exists(HERALD_PAUSE_FLAG)}")
    return 0


def _cmd_queue() -> int:
    """List queue contents."""
    from heyvox.constants import HERALD_QUEUE_DIR
    if not os.path.isdir(HERALD_QUEUE_DIR):
        print("Queue empty (directory does not exist)")
        return 0
    wavs = sorted(f for f in os.listdir(HERALD_QUEUE_DIR) if f.endswith(".wav"))
    if not wavs:
        print("Queue empty")
    else:
        for w in wavs:
            print(w)
    return 0


def _cmd_orchestrator() -> int:
    """Start orchestrator daemon (blocking)."""
    # Own process group so kokoro-daemon restarts don't kill us
    try:
        os.setpgrp()
    except OSError:
        pass
    from heyvox.herald.orchestrator import HeraldOrchestrator
    orch = HeraldOrchestrator()
    orch.run()
    return 0


def main() -> None:
    """Entry point for 'herald' console script."""
    sys.exit(dispatch(sys.argv[1:]))


if __name__ == "__main__":
    main()
