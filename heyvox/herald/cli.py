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
    return 0


def _cmd_resume() -> int:
    """Remove pause flag."""
    from heyvox.constants import HERALD_PAUSE_FLAG
    try:
        os.unlink(HERALD_PAUSE_FLAG)
    except FileNotFoundError:
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


def _cmd_mute() -> int:
    """Toggle mute flag."""
    from heyvox.constants import HERALD_MUTE_FLAG
    from pathlib import Path
    if os.path.exists(HERALD_MUTE_FLAG):
        os.unlink(HERALD_MUTE_FLAG)
    else:
        Path(HERALD_MUTE_FLAG).touch()
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
    from heyvox.herald.orchestrator import HeraldOrchestrator
    orch = HeraldOrchestrator()
    orch.run()
    return 0


def main() -> None:
    """Entry point for 'herald' console script."""
    sys.exit(dispatch(sys.argv[1:]))


if __name__ == "__main__":
    main()
