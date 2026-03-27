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

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()
