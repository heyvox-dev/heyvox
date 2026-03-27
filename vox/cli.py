"""
Vox CLI — voice layer for AI coding agents.

Entry point: vox [command] [options]
"""

import argparse
import sys


def _cmd_start(args):
    # Lazy import to avoid loading heavy audio deps at import time
    from vox.main import run
    run()


def _cmd_stop(args):
    print("Service management not yet implemented (Phase 3).")


def _cmd_restart(args):
    print("Service management not yet implemented (Phase 3).")


def _cmd_status(args):
    from vox import __version__
    print(f"Vox v{__version__} — service management not yet implemented (Phase 3).")


def _cmd_setup(args):
    print("Setup not yet implemented (Phase 3).")


def _cmd_logs(args):
    from vox.constants import LOG_FILE
    import subprocess

    if args.follow:
        subprocess.run(["tail", "-f", LOG_FILE])
    else:
        subprocess.run(["tail", "-n", "100", LOG_FILE])


def _cmd_speak(args):
    """Synthesize and play text via Kokoro TTS, then exit.

    Starts the TTS worker, enqueues the text, waits for playback to finish,
    then shuts down. Designed as a fire-and-forget CLI command.

    Requirement: CLI-05
    """
    from vox.audio.tts import speak, start_worker, shutdown, _tts_queue
    from vox.config import load_config

    config = load_config()
    start_worker(config)

    text = " ".join(args.text)
    speak(
        text=text,
        voice=args.voice,
        speed=args.speed,
        verbosity=args.verbosity,
    )

    # Wait for all enqueued items to finish playing
    _tts_queue.join()
    shutdown()


def _cmd_skip(args):
    """Write a skip command to the TTS command file.

    The running vox TTS worker reads this file between synthesis chunks and
    stops current playback.

    Requirement: CLI-06
    """
    from vox.constants import TTS_CMD_FILE
    try:
        with open(TTS_CMD_FILE, "w") as f:
            f.write("skip\n")
        print("Skipped current TTS.")
    except Exception as e:
        print(f"Error writing TTS command: {e}", file=sys.stderr)


def _cmd_mute(args):
    """Toggle TTS mute state via command file IPC.

    Requirement: CLI-06
    """
    from vox.constants import TTS_CMD_FILE
    try:
        with open(TTS_CMD_FILE, "w") as f:
            f.write("mute-toggle\n")
        print("TTS mute toggled.")
    except Exception as e:
        print(f"Error writing TTS command: {e}", file=sys.stderr)


def _cmd_quiet(args):
    """Set TTS verbosity to short for the session via command file IPC.

    Requirement: CLI-06
    """
    from vox.constants import TTS_CMD_FILE
    try:
        with open(TTS_CMD_FILE, "w") as f:
            f.write("quiet\n")
        print("TTS verbosity set to short.")
    except Exception as e:
        print(f"Error writing TTS command: {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        prog="vox",
        description="Vox — voice layer for AI coding agents",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="command")

    # start
    sub_start = subparsers.add_parser("start", help="Start the vox listener")
    sub_start.set_defaults(func=_cmd_start)

    # stop
    sub_stop = subparsers.add_parser("stop", help="Stop the vox listener")
    sub_stop.set_defaults(func=_cmd_stop)

    # restart
    sub_restart = subparsers.add_parser("restart", help="Restart the vox listener")
    sub_restart.set_defaults(func=_cmd_restart)

    # status
    sub_status = subparsers.add_parser("status", help="Show vox status")
    sub_status.set_defaults(func=_cmd_status)

    # setup
    sub_setup = subparsers.add_parser("setup", help="Run initial setup")
    sub_setup.set_defaults(func=_cmd_setup)

    # logs
    sub_logs = subparsers.add_parser("logs", help="Show vox logs")
    sub_logs.add_argument("-f", "--follow", action="store_true", help="Follow log output")
    sub_logs.set_defaults(func=_cmd_logs)

    # speak — synthesize and play text (CLI-05)
    sub_speak = subparsers.add_parser("speak", help="Speak text via Kokoro TTS")
    sub_speak.add_argument(
        "text",
        nargs="+",
        help="Text to speak (multiple words joined with spaces)",
    )
    sub_speak.add_argument(
        "--voice",
        default=None,
        help="Kokoro voice name (default: from config, e.g. af_heart)",
    )
    sub_speak.add_argument(
        "--speed",
        type=float,
        default=None,
        help="Playback speed multiplier (default: from config, e.g. 1.0)",
    )
    sub_speak.add_argument(
        "--verbosity",
        choices=["full", "summary", "short", "skip"],
        default=None,
        help="Verbosity mode: full (default) | summary | short | skip",
    )
    sub_speak.set_defaults(func=_cmd_speak)

    # skip — stop current TTS playback (CLI-06)
    sub_skip = subparsers.add_parser("skip", help="Skip current TTS playback")
    sub_skip.set_defaults(func=_cmd_skip)

    # mute — toggle TTS mute (CLI-06)
    sub_mute = subparsers.add_parser("mute", help="Toggle TTS mute on/off")
    sub_mute.set_defaults(func=_cmd_mute)

    # quiet — set verbosity to short for the session (CLI-06)
    sub_quiet = subparsers.add_parser("quiet", help="Set TTS verbosity to short for this session")
    sub_quiet.set_defaults(func=_cmd_quiet)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()
