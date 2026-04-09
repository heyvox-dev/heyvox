"""
HeyVox CLI — voice layer for AI coding agents.

Entry point: heyvox [command] [options]
"""

import argparse
import os
import sys


def _cmd_start(args):
    """Start heyvox — foreground mode by default, launchd daemon with --daemon.

    Requirement: CLI-01
    """
    if getattr(args, "daemon", False):
        from heyvox.setup.launchd import bootstrap
        success, msg = bootstrap()
        print(msg)
        if not success:
            sys.exit(1)
    else:
        # Foreground mode: run main loop directly (development/debug)
        from heyvox.main import run
        run()


def _cmd_stop(args):
    """Stop the running launchd heyvox service.

    Requirement: CLI-01
    """
    from heyvox.setup.launchd import bootout
    success, msg = bootout()
    print(msg)
    if not success:
        sys.exit(1)


def _cmd_restart(args):
    """Restart the heyvox launchd service (stop then start).

    Requirement: CLI-01
    """
    from heyvox.setup.launchd import restart
    success, msg = restart()
    print(msg)
    if not success:
        sys.exit(1)


def _cmd_status(args):
    """Show full HeyVox system status.

    Requirement: CLI-01
    """
    import glob
    from heyvox import __version__
    from heyvox.setup.launchd import get_status, PLIST_PATH

    status = get_status()

    # Service status
    if not PLIST_PATH.exists():
        svc = "Not installed (run: heyvox setup)"
    elif status["running"]:
        svc = f"Running (PID {status['pid']})"
    elif status["loaded"]:
        svc = f"Stopped (exit code {status['exit_code']})"
    else:
        svc = "Not loaded"
    print(f"HeyVox v{__version__} — {svc}")

    # TTS state
    from heyvox.audio.tts import is_muted, get_verbosity
    mute_str = "yes" if is_muted() else "no"
    print(f"  Verbosity:  {get_verbosity()}")
    print(f"  Muted:      {mute_str}")

    # Queue
    queue_files = glob.glob("/tmp/herald-queue/*.wav")
    hold_files = glob.glob("/tmp/herald-hold/*.wav")
    print(f"  Queue:      {len(queue_files)} queued, {len(hold_files)} held")

    # Daemons
    def _pid_alive(pidfile):
        try:
            with open(pidfile) as _f:
                pid = int(_f.read().strip())
            os.kill(pid, 0)
            return True
        except Exception:
            return False

    orch = "running" if _pid_alive("/tmp/herald-orchestrator.pid") else "stopped"
    kokoro = "running" if (os.path.exists("/tmp/kokoro-daemon.sock") and _pid_alive("/tmp/kokoro-daemon.pid")) else "stopped"
    hud = "running" if os.path.exists("/tmp/heyvox-hud.sock") else "stopped"
    print(f"  Orchestrator: {orch}")
    print(f"  Kokoro TTS:   {kokoro}")
    print(f"  HUD:          {hud}")


def _cmd_setup(args):
    """Run the interactive guided setup wizard.

    Requirement: CLI-02, CLI-03, CLI-04
    """
    from heyvox.config import load_config
    from heyvox.setup.wizard import run_setup
    config = load_config()
    run_setup(config)


def _cmd_logs(args):
    """Tail the heyvox service log file.

    Requirement: CLI-01
    """
    import subprocess
    from pathlib import Path

    from heyvox.constants import LOG_FILE
    log_path = LOG_FILE

    if not Path(log_path).exists():
        print("No log file found. Is the service running?")
        sys.exit(1)

    lines = getattr(args, "lines", 50)
    try:
        subprocess.run(["tail", f"-n{lines}", "-f", log_path])
    except KeyboardInterrupt:
        pass  # Clean exit on Ctrl+C


def _cmd_speak(args):
    """Synthesize and play text via Kokoro TTS, then exit.

    Starts the TTS worker, enqueues the text, waits for playback to finish,
    then shuts down. Designed as a fire-and-forget CLI command.

    Requirement: CLI-05
    """
    from heyvox.audio.tts import speak, start_worker, shutdown, _tts_queue
    from heyvox.config import load_config

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
    """Skip current TTS playback via Herald.

    Requirement: CLI-06
    """
    from heyvox.audio.tts import skip_current
    skip_current()
    print("Skipped current TTS.")


def _cmd_mute(args):
    """Toggle TTS mute on/off.

    Requirement: CLI-06
    """
    from heyvox.audio.tts import is_muted, set_muted
    new_state = not is_muted()
    set_muted(new_state)
    print("TTS muted." if new_state else "TTS unmuted.")


def _cmd_quiet(args):
    """Set TTS verbosity to short (first sentence only).

    Requirement: CLI-06
    """
    from heyvox.audio.tts import set_verbosity, get_verbosity
    old = get_verbosity()
    set_verbosity("short")
    print(f"TTS verbosity set to short (was {old}).")


def _cmd_verbose(args):
    """Set TTS verbosity. Levels: full, summary, short, skip.

    Without arguments: show current level.
    With argument: set to that level.
    """
    from heyvox.audio.tts import set_verbosity, get_verbosity
    level = getattr(args, "level", None)
    if not level:
        print(f"TTS verbosity: {get_verbosity()}")
        return
    valid = {"full", "summary", "short", "skip"}
    if level not in valid:
        print(f"Invalid level '{level}'. Choose from: {', '.join(sorted(valid))}", file=sys.stderr)
        return
    old = get_verbosity()
    set_verbosity(level)
    print(f"TTS verbosity: {old} → {level}")


def _cmd_commands(args):
    """Show all available voice commands."""
    from heyvox.audio.tts import VOICE_COMMANDS
    print("Voice Commands (say these after the wake word):\n")

    # Group by category
    categories = {
        "Playback": ["tts-next", "tts-skip", "tts-stop", "tts-mute", "tts-replay"],
        "Verbosity": ["verbosity-full", "verbosity-summary", "verbosity-short", "verbosity-skip"],
    }
    action_to_patterns = {}
    for pattern, (action, feedback) in VOICE_COMMANDS.items():
        if action not in action_to_patterns:
            action_to_patterns[action] = []
        # Clean up regex for display
        display = pattern.lstrip("^").rstrip("$").replace(r"\s+", " ").replace("(", "").replace(")", "").replace("?", "").replace("|", "/")
        action_to_patterns[action].append(display)

    for cat, actions in categories.items():
        print(f"  {cat}:")
        for action in actions:
            if action in action_to_patterns:
                phrases = action_to_patterns[action]
                feedback = next(fb for _, (a, fb) in VOICE_COMMANDS.items() if a == action)
                print(f"    {' / '.join(phrases):40s} → {feedback}")
        print()


def _cmd_history(args):
    """Show recent transcription history.

    Displays the last N transcripts from the persistent log. Each entry
    was saved immediately after STT — even if paste failed, the text is here.
    """
    from heyvox.history import load, last, _HISTORY_FILE

    if getattr(args, "copy_last", False):
        entry = last()
        if not entry:
            print("No transcripts yet.")
            sys.exit(1)
        import subprocess
        subprocess.run(["pbcopy"], input=entry["text"].encode(), check=True)
        print(f"Copied to clipboard: {entry['text'][:80]}{'...' if len(entry['text']) > 80 else ''}")
        return

    if getattr(args, "path", False):
        print(_HISTORY_FILE)
        return

    limit = getattr(args, "limit", 20)
    entries = load(limit=limit)

    if not entries:
        print("No transcripts yet.")
        return

    for e in entries:
        ts = e.get("ts", "?")
        trigger = e.get("trigger", "?")
        dur = e.get("duration", 0)
        text = e.get("text", "")
        # Truncate long entries for display
        display = text if len(text) <= 120 else text[:117] + "..."
        print(f"[{ts}] ({trigger}, {dur}s) {display}")


def _cmd_chrome_bridge(args):
    """Start the Chrome companion WebSocket bridge.

    Runs a local WebSocket server that the HeyVox Chrome extension connects to
    for per-tab media state detection and control.

    Requirement: CHROME-01
    """
    from heyvox.chrome.bridge import run_bridge

    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 9285)
    run_bridge(host=host, port=port)


def _cmd_debug(args):
    """Show recent STT debug recordings and pipeline info."""
    import json
    from heyvox.constants import STT_DEBUG_DIR, STT_DEBUG_LOG

    if args.enable:
        os.makedirs(STT_DEBUG_DIR, exist_ok=True)
        print(f"Debug capturing enabled. Audio saved to: {STT_DEBUG_DIR}")
        print(f"Pipeline log: {STT_DEBUG_LOG}")
        print("Restart heyvox for changes to take effect.")
        return

    if args.disable:
        import shutil
        if os.path.isdir(STT_DEBUG_DIR):
            shutil.rmtree(STT_DEBUG_DIR)
            print(f"Debug directory removed: {STT_DEBUG_DIR}")
        try:
            os.remove(STT_DEBUG_LOG)
            print(f"Debug log removed: {STT_DEBUG_LOG}")
        except FileNotFoundError:
            pass
        return

    if not os.path.isdir(STT_DEBUG_DIR):
        print("Debug capturing is OFF. Enable with: heyvox debug --enable")
        print("Then restart heyvox to start saving raw audio.")
        return

    # Read and display recent debug log entries
    if not os.path.exists(STT_DEBUG_LOG):
        print("No debug entries yet. Record something and check again.")
        return

    with open(STT_DEBUG_LOG) as f:
        lines = f.readlines()

    # Group entries by timestamp (raw, trimmed, _stt_result, _final share same ts)
    recordings = {}
    for line in lines:
        try:
            entry = json.loads(line.strip())
        except json.JSONDecodeError:
            continue
        ts = entry.get("timestamp", "unknown")
        label = entry.get("label", "")
        if label == "raw":
            recordings[ts] = {"raw": entry}
        elif ts in recordings:
            recordings[ts][label] = entry

    # Show most recent N recordings
    recent = list(recordings.items())[-args.n:]

    if not recent:
        print("No recordings captured yet.")
        return

    for ts, group in recent:
        raw = group.get("raw", {})
        trimmed = group.get("trimmed", {})
        stt = group.get("_stt_result", {})
        final = group.get("_final", {})

        print(f"\n{'='*60}")
        print(f"  Recording: {ts}")
        print(f"  Raw:     {raw.get('duration_s', '?')}s, {raw.get('rms_dbfs', '?')} dBFS, {raw.get('num_chunks', '?')} chunks")
        if trimmed:
            print(f"  Trimmed: {trimmed.get('duration_s', '?')}s, {trimmed.get('rms_dbfs', '?')} dBFS, {trimmed.get('num_chunks', '?')} chunks")
        if stt:
            print(f"  STT raw: \"{stt.get('stt_raw', '')}\"  ({stt.get('stt_engine', '?')}, {stt.get('stt_time_s', '?')}s)")
        if final:
            print(f"  Echo filtered: {final.get('echo_filtered', False)}")
            print(f"  WW stripped:   {final.get('wake_word_stripped', False)}")
            print(f"  Final text:    \"{final.get('final_text', '')}\"")

        # List WAV files for this timestamp
        wav_files = [f for f in os.listdir(STT_DEBUG_DIR) if f.startswith(ts) and f.endswith('.wav')]
        if wav_files:
            print(f"  Files: {', '.join(sorted(wav_files))}")

    print(f"\n  Debug dir: {STT_DEBUG_DIR}")
    print(f"  Log file:  {STT_DEBUG_LOG}")


def _cmd_doctor(args):
    """Run system diagnostics to check HeyVox health."""
    from heyvox.doctor import run_doctor
    print(run_doctor())


def _cmd_bugreport(args):
    """Generate a structured bug report for GitHub Issues."""
    from heyvox.doctor import run_bugreport
    report = run_bugreport()
    if getattr(args, "clipboard", True):
        try:
            import subprocess
            subprocess.run(["pbcopy"], input=report.encode(), check=True)
            print("Bug report copied to clipboard. Paste it into a GitHub Issue.")
            print(f"({len(report)} characters)")
        except Exception:
            print(report)
    else:
        print(report)


def _cmd_register(args):
    """Register (or re-register) HeyVox MCP server with AI coding agents."""
    from heyvox.setup.wizard import _detect_mcp_agents, _register_mcp_agent

    mcp_entry = {
        "command": sys.executable,
        "args": ["-m", "heyvox.mcp.server"],
    }

    agents = _detect_mcp_agents()
    if not agents:
        print("No supported AI coding agents detected.")
        print("Supported: Claude Code, Cursor, Windsurf, Continue.dev")
        sys.exit(1)

    agent_filter = getattr(args, "agent", None)

    registered = 0
    for agent in agents:
        if agent_filter and agent_filter.lower() not in agent["name"].lower():
            continue
        ok, msg = _register_mcp_agent(agent, mcp_entry)
        print(f"{'✓' if ok else '✗'} {msg}")
        if ok:
            registered += 1

    if registered == 0 and agent_filter:
        print(f"No agent matching '{agent_filter}' found.")
        print(f"Available: {', '.join(a['name'] for a in agents)}")


def main():
    from heyvox import __version__

    parser = argparse.ArgumentParser(
        prog="heyvox",
        description="HeyVox — voice layer for AI coding agents",
    )
    parser.add_argument(
        "--version", "-V",
        action="version",
        version=f"heyvox {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="command")

    # start
    sub_start = subparsers.add_parser("start", help="Start the heyvox listener")
    sub_start.add_argument(
        "--daemon", "-d",
        action="store_true",
        help="Start as launchd service (background daemon)",
    )
    sub_start.set_defaults(func=_cmd_start)

    # stop
    sub_stop = subparsers.add_parser("stop", help="Stop the heyvox listener")
    sub_stop.set_defaults(func=_cmd_stop)

    # restart
    sub_restart = subparsers.add_parser("restart", help="Restart the heyvox listener")
    sub_restart.set_defaults(func=_cmd_restart)

    # status
    sub_status = subparsers.add_parser("status", help="Show heyvox status")
    sub_status.set_defaults(func=_cmd_status)

    # setup
    sub_setup = subparsers.add_parser("setup", help="Run initial setup")
    sub_setup.set_defaults(func=_cmd_setup)

    # logs
    sub_logs = subparsers.add_parser("logs", help="Tail the heyvox service log file")
    sub_logs.add_argument(
        "--lines", "-n",
        type=int,
        default=50,
        help="Number of lines to show before following (default: 50)",
    )
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

    # quiet — set verbosity to short (CLI-06)
    sub_quiet = subparsers.add_parser("quiet", help="Set TTS verbosity to short (first sentence only)")
    sub_quiet.set_defaults(func=_cmd_quiet)

    # verbose — get/set verbosity level
    sub_verbose = subparsers.add_parser("verbose", help="Get or set TTS verbosity level")
    sub_verbose.add_argument(
        "level",
        nargs="?",
        choices=["full", "summary", "short", "skip"],
        default=None,
        help="Verbosity level (omit to show current)",
    )
    sub_verbose.set_defaults(func=_cmd_verbose)

    # commands — show available voice commands
    sub_commands = subparsers.add_parser("commands", help="Show available voice commands")
    sub_commands.set_defaults(func=_cmd_commands)

    # history — show recent transcripts
    sub_history = subparsers.add_parser("history", help="Show recent transcription history")
    sub_history.add_argument(
        "--limit", "-n",
        type=int,
        default=20,
        help="Number of entries to show (default: 20, newest first)",
    )
    sub_history.add_argument(
        "--copy-last", "-c",
        action="store_true",
        help="Copy the most recent transcript to clipboard",
    )
    sub_history.add_argument(
        "--path",
        action="store_true",
        help="Print the transcript file path",
    )
    sub_history.set_defaults(func=_cmd_history)

    # chrome-bridge — start WebSocket bridge for Chrome extension (CHROME-01)
    sub_chrome = subparsers.add_parser(
        "chrome-bridge",
        help="Start Chrome companion WebSocket bridge for per-tab media control",
    )
    sub_chrome.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address (default: 127.0.0.1, localhost only)",
    )
    sub_chrome.add_argument(
        "--port",
        type=int,
        default=9285,
        help="WebSocket port (default: 9285)",
    )
    sub_chrome.set_defaults(func=_cmd_chrome_bridge)

    # debug — show recent STT debug info
    sub_debug = subparsers.add_parser("debug", help="Show recent STT recordings and debug info")
    sub_debug.add_argument(
        "-n",
        type=int,
        default=10,
        help="Number of recent entries to show (default: 10)",
    )
    sub_debug.add_argument(
        "--enable",
        action="store_true",
        help="Create the debug directory to start capturing",
    )
    sub_debug.add_argument(
        "--disable",
        action="store_true",
        help="Remove the debug directory to stop capturing",
    )
    sub_debug.set_defaults(func=_cmd_debug)

    # doctor — system diagnostics
    sub_doctor = subparsers.add_parser("doctor", help="Run system diagnostics")
    sub_doctor.set_defaults(func=_cmd_doctor)

    # bugreport — generate structured bug report
    sub_bugreport = subparsers.add_parser("bugreport", help="Generate bug report for GitHub Issues")
    sub_bugreport.add_argument(
        "--no-clipboard",
        dest="clipboard",
        action="store_false",
        default=True,
        help="Print to stdout instead of copying to clipboard",
    )
    sub_bugreport.set_defaults(func=_cmd_bugreport)

    # register — register MCP server with AI agents
    sub_register = subparsers.add_parser("register", help="Register HeyVox MCP server with AI coding agents")
    sub_register.add_argument(
        "agent",
        nargs="?",
        default=None,
        help="Filter by agent name (e.g. 'cursor'). Registers all detected if omitted.",
    )
    sub_register.set_defaults(func=_cmd_register)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()
