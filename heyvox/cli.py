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

    from heyvox.constants import (
        HERALD_QUEUE_DIR, HERALD_HOLD_DIR, HERALD_ORCH_PID,
        KOKORO_DAEMON_SOCK, KOKORO_DAEMON_PID, HUD_SOCKET_PATH,
    )
    # Queue
    queue_files = glob.glob(HERALD_QUEUE_DIR + "/*.wav")
    hold_files = glob.glob(HERALD_HOLD_DIR + "/*.wav")
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

    orch = "running" if _pid_alive(HERALD_ORCH_PID) else "stopped"
    kokoro = "running" if (os.path.exists(KOKORO_DAEMON_SOCK) and _pid_alive(KOKORO_DAEMON_PID)) else "stopped"
    hud = "running" if os.path.exists(HUD_SOCKET_PATH) else "stopped"
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

    Starts the TTS worker, enqueues the text, waits for Herald to finish
    playing all queued WAVs, then shuts down. Designed as a fire-and-forget
    CLI command.

    Requirement: CLI-05
    """
    import os
    import time

    from heyvox.audio.tts import speak, start_worker, shutdown
    from heyvox.config import load_config
    from heyvox.constants import HERALD_QUEUE_DIR, HERALD_PLAYING_PID

    config = load_config()
    start_worker(config)

    text = " ".join(args.text)
    speak(
        text=text,
        voice=args.voice,
        speed=args.speed,
        verbosity=args.verbosity,
    )

    # Herald is a separate process — poll queue + playing PID until drained.
    # Cap at 120s so a stuck queue doesn't hang the CLI forever.
    deadline = time.time() + 120.0
    # Brief grace so the speak() call's enqueue hits Herald before we check.
    time.sleep(0.3)
    while time.time() < deadline:
        queue_empty = True
        if os.path.isdir(HERALD_QUEUE_DIR):
            queue_empty = not any(
                f.endswith(".wav") for f in os.listdir(HERALD_QUEUE_DIR)
            )
        playing = False
        if os.path.exists(HERALD_PLAYING_PID):
            try:
                pid = int(open(HERALD_PLAYING_PID).read().strip())
                os.kill(pid, 0)
                playing = True
            except (OSError, ValueError):
                playing = False
        if queue_empty and not playing:
            break
        time.sleep(0.2)

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


def _cmd_log_health(args):
    """Daily digest of wake-word, STT, and Herald log health.

    Aggregates counts of wake triggers, VAD-killed triggers (WAKE_VAD_DROP),
    sub-threshold near misses (NEAR_MISS), recording sessions where the user
    had to repeat themselves (USER_EFFORT), STT latencies, Herald violations,
    workspace-switch outcomes, and Hammerspoon skips.

    Designed to be run daily — surfaces patterns that no single log line shows.
    """
    import datetime
    import re
    from heyvox.constants import (
        LOG_FILE,
        STT_DEBUG_LOG,
        HERALD_DEBUG_LOG,
        HERALD_VIOLATIONS_LOG,
    )

    # Resolve the active log file from config — it can be overridden in
    # config.yaml (default ships as /tmp/heyvox.log to match the launchd plist
    # redirect). Reading the constant alone misses the live data.
    try:
        from heyvox.config import load_config
        active_log_file = load_config().log_file or LOG_FILE
    except Exception:
        active_log_file = LOG_FILE

    target_date = getattr(args, "date", None) or datetime.date.today().isoformat()
    json_mode = getattr(args, "json", False)

    def _say(*a, **kw) -> None:
        """Suppress human-readable output when --json is requested."""
        if not json_mode:
            print(*a, **kw)

    # Build a set of substring matchers covering the formats found in our logs:
    #   ISO     "2026-04-21"        — herald-debug, herald-violations, hs lines
    #   Short   "Apr 21"            — bash `date` default in conductor-switch-workspace
    #   Ordinal "21 Apr"            — locale variant
    # A line is "today" if any matcher hits.
    try:
        _dt = datetime.date.fromisoformat(target_date)
        _short = _dt.strftime("%b %d").replace(" 0", " ")  # "Apr 21" not "Apr 21" with zero-pad
        _short_alt = _dt.strftime("%b %d")                  # "Apr 21" with possible zero-pad
        _ordinal = _dt.strftime("%d %b").lstrip("0")        # "21 Apr"
    except ValueError:
        _short = _short_alt = _ordinal = ""
    _date_matchers = {target_date, _short, _short_alt, _ordinal}
    _date_matchers.discard("")

    def _read(path: str) -> list[str]:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.readlines()
        except FileNotFoundError:
            return []

    def _today(lines: list[str], date: str) -> list[str]:
        # Multi-format match — covers ISO and bash-date timestamps that appear
        # in the various log files. Lines from heyvox.log carry only HH:MM:SS,
        # so callers that need that file pass it directly without this filter.
        return [ln for ln in lines if any(m in ln for m in _date_matchers)]

    main_lines_all = _read(active_log_file)
    stt_lines_all = _read(STT_DEBUG_LOG)
    herald_lines = _today(_read(HERALD_DEBUG_LOG), target_date)
    violation_lines = _today(_read(HERALD_VIOLATIONS_LOG), target_date)
    claude_log_lines = _today(_read("/tmp/claude-tts-debug.log"), target_date)

    _say(f"HeyVox log-health — {target_date}")
    _say("=" * 60)
    _say("Sources scanned:")
    _say(f"  {active_log_file} ({len(main_lines_all)} lines, current rotation)")
    _say(f"  {HERALD_DEBUG_LOG} ({len(herald_lines)} lines today)")
    _say(f"  {HERALD_VIOLATIONS_LOG} ({len(violation_lines)} entries today)")

    # ----- Wake word -----
    triggers = sum(1 for ln in main_lines_all if ">>> TRIGGER" in ln)
    vad_drops = [ln for ln in main_lines_all if "[WAKE_VAD_DROP]" in ln]
    near_misses = [ln for ln in main_lines_all if "[NEAR_MISS]" in ln]
    user_efforts = [ln for ln in main_lines_all if "[USER_EFFORT]" in ln]

    _say("\n## Wake word (current rotation of heyvox.log)")
    _say(f"  Triggers fired:        {triggers}")
    _say(f"  VAD drops (lost):      {len(vad_drops)}")
    _say(f"  Near-misses (sub-thr): {len(near_misses)}")
    _say(f"  USER_EFFORT events:    {len(user_efforts)}")

    if user_efforts:
        _say("\n  Recent USER_EFFORT (user had to repeat 'Hey Vox'):")
        for ln in user_efforts[-5:]:
            ts_match = re.search(r"\[(\d{2}:\d{2}:\d{2})\]", ln)
            n_match = re.search(r"attempts=(\d+) window=([\d.]+)s", ln)
            ts = ts_match.group(1) if ts_match else "??:??:??"
            if n_match:
                _say(f"    {ts}  {n_match.group(1)} attempts in {n_match.group(2)}s")

    if vad_drops:
        _say("\n  Recent WAKE_VAD_DROP (model heard it, VAD killed it):")
        for ln in vad_drops[-5:]:
            ts_match = re.search(r"\[(\d{2}:\d{2}:\d{2})\]", ln)
            score_match = re.search(r"score=([\d.]+)", ln)
            ts = ts_match.group(1) if ts_match else "??:??:??"
            score = score_match.group(1) if score_match else "?"
            _say(f"    {ts}  score={score}")

    # ----- STT -----
    stt_finals = [ln for ln in stt_lines_all if '"label": "_final"' in ln or '"label":"_final"' in ln]
    stt_durations: list[float] = []
    stt_times: list[float] = []
    for ln in stt_lines_all:
        if '"label": "_stt_result"' in ln or '"label":"_stt_result"' in ln:
            d = re.search(r'"stt_time_s":\s*([\d.]+)', ln)
            if d:
                stt_times.append(float(d.group(1)))
        d = re.search(r'"duration_s":\s*([\d.]+)', ln)
        if d:
            stt_durations.append(float(d.group(1)))

    _say("\n## STT (current rotation of heyvox-stt-debug.log)")
    _say(f"  Finals logged:         {len(stt_finals)}")
    stt_p50 = stt_p99 = None
    if stt_times:
        stt_times.sort()
        stt_p50 = stt_times[len(stt_times) // 2]
        stt_p99 = stt_times[min(len(stt_times) - 1, int(len(stt_times) * 0.99))]
        _say(f"  STT time p50/p99:      {stt_p50:.2f}s / {stt_p99:.2f}s")
    if stt_durations:
        stt_durations.sort()
        p50 = stt_durations[len(stt_durations) // 2]
        p99 = stt_durations[min(len(stt_durations) - 1, int(len(stt_durations) * 0.99))]
        _say(f"  Audio duration p50/p99: {p50:.2f}s / {p99:.2f}s")

    # ----- Herald -----
    _say("\n## Herald (TTS playback)")
    _say(f"  Lines today:           {len(herald_lines)}")
    _say(f"  Violations today:      {len(violation_lines)}")
    if violation_lines:
        _say("\n  Recent violations:")
        for ln in violation_lines[-3:]:
            _say(f"    {ln.strip()}")

    # ----- Workspace switching -----
    sw_skip_hs = sum(1 for ln in claude_log_lines if "Hammerspoon not running" in ln)
    sw_skip_idle = sum(1 for ln in claude_log_lines if "SKIP switch" in ln and "idle=" in ln)
    sw_done = sum(1 for ln in claude_log_lines if "Switching to:" in ln)
    sw_fail = sum(1 for ln in claude_log_lines if "SWITCH FAILED" in ln)

    _say("\n## Workspace switch (today)")
    _say(f"  Switches done:         {sw_done}")
    _say(f"  Skips (HS not running):{sw_skip_hs}")
    _say(f"  Skips (user busy):     {sw_skip_idle}")
    _say(f"  Failures (no DB match):{sw_fail}")

    # --- Phase 15-07: Paste section ------------------------------------------
    import re as _re
    _paste_tier_re = _re.compile(
        r"\[PASTE\]\s+tier_used=(\w+)\s+reason=(\S+)\s+elapsed_ms=(\d+)"
    )
    _paste_verify_re = _re.compile(
        r"\[PASTE\]\s+verified=(true|false)\s+retried=(true|false)\s+drift=(true|false)"
    )

    paste_lines = [ln for ln in main_lines_all if "[PASTE]" in ln]

    tier_counts = {"1": 0, "2": 0, "fail_closed": 0}
    fail_reasons = {
        "no_text_field_at_start": 0,
        "multi_field_no_shortcut": 0,
        "target_unreachable": 0,
    }
    elapsed_by_tier = {"1": [], "2": []}
    for ln in paste_lines:
        m = _paste_tier_re.search(ln)
        if m:
            tier, reason, ms = m.group(1), m.group(2), int(m.group(3))
            if tier in tier_counts:
                tier_counts[tier] += 1
            if tier == "fail_closed" and reason in fail_reasons:
                fail_reasons[reason] += 1
            if tier in elapsed_by_tier:
                elapsed_by_tier[tier].append(ms)

    verify_total = 0
    verify_drift = 0
    verify_retried = 0
    for ln in paste_lines:
        m = _paste_verify_re.search(ln)
        if m:
            verify_total += 1
            if m.group(3) == "true":
                verify_drift += 1
            if m.group(2) == "true":
                verify_retried += 1

    total_resolves = sum(tier_counts.values())
    non_fail_resolves = tier_counts["1"] + tier_counts["2"]

    def _paste_pct(num, den):
        return (num / den * 100) if den > 0 else 0.0

    def _paste_p95(values):
        if not values:
            return None
        s = sorted(values)
        return s[min(len(s) - 1, int(len(s) * 0.95))]

    tier_1_hit_rate = (
        _paste_pct(tier_counts["1"], non_fail_resolves) if non_fail_resolves else 0.0
    )
    tier_2_hit_rate = (
        _paste_pct(tier_counts["2"], non_fail_resolves) if non_fail_resolves else 0.0
    )
    fail_closed_rate = (
        _paste_pct(tier_counts["fail_closed"], total_resolves) if total_resolves else 0.0
    )
    drift_rate = _paste_pct(verify_drift, verify_total) if verify_total else 0.0
    # B6: canonical names match JSON keys (no `_elapsed_` infix).
    tier_1_p95_ms = _paste_p95(elapsed_by_tier["1"])
    tier_2_p95_ms = _paste_p95(elapsed_by_tier["2"])

    _say("\n## Paste (current rotation of heyvox.log)")
    if total_resolves == 0 and verify_total == 0:
        _say("  (no [PASTE] events in current rotation)")
    else:
        _say(f"  Total resolves:        {total_resolves}")
        _say(
            f"  Tier 1 hit rate:       {tier_1_hit_rate:.1f}%   "
            f"({tier_counts['1']}/{non_fail_resolves} non-fail)"
        )
        _say(
            f"  Tier 2 hit rate:       {tier_2_hit_rate:.1f}%   "
            f"({tier_counts['2']}/{non_fail_resolves} non-fail)"
        )
        _say(
            f"  Fail-closed rate:      {fail_closed_rate:.1f}%   "
            f"({tier_counts['fail_closed']}/{total_resolves} total)"
        )
        if any(fail_reasons.values()):
            _say("    by reason:")
            for reason_k, n in fail_reasons.items():
                if n > 0:
                    _say(f"      {reason_k}: {n}")
        if verify_total > 0:
            _say(
                f"  Verify-drift rate:     {drift_rate:.1f}%   "
                f"({verify_drift}/{verify_total} verifies)"
            )
            _say(f"  Verify retried (1/N):  {verify_retried}/{verify_total}")
        if tier_1_p95_ms is not None:
            _say(f"  Tier 1 elapsed p95:    {tier_1_p95_ms}ms")
        if tier_2_p95_ms is not None:
            _say(f"  Tier 2 elapsed p95:    {tier_2_p95_ms}ms")

    _say()
    if not json_mode:
        _say("Tip: 'heyvox log-health --date YYYY-MM-DD' to inspect a previous day.")
        _say("Tip: 'heyvox log-health --json' for machine-readable output.")
    else:
        # Re-emit minimal counters as JSON for piping into other tools.
        import json as _json
        payload = {
            "date": target_date,
            "wake": {
                "triggers": triggers,
                "vad_drops": len(vad_drops),
                "near_misses": len(near_misses),
                "user_efforts": len(user_efforts),
            },
            "stt": {
                "finals": len(stt_finals),
                "stt_time_p50": stt_p50,
                "stt_time_p99": stt_p99,
            },
            "herald": {
                "lines": len(herald_lines),
                "violations": len(violation_lines),
            },
            "workspace_switch": {
                "done": sw_done,
                "skips_hs_dead": sw_skip_hs,
                "skips_user_busy": sw_skip_idle,
                "failures": sw_fail,
            },
            "paste": {
                "total_resolves": total_resolves,
                "tier_1_hit_count": tier_counts["1"],
                "tier_2_hit_count": tier_counts["2"],
                "fail_closed_count": tier_counts["fail_closed"],
                "tier_1_hit_rate_pct": round(tier_1_hit_rate, 2),
                "tier_2_hit_rate_pct": round(tier_2_hit_rate, 2),
                "fail_closed_rate_pct": round(fail_closed_rate, 2),
                "fail_closed_by_reason": dict(fail_reasons),
                "verify_total": verify_total,
                "verify_drift_count": verify_drift,
                "verify_drift_rate_pct": round(drift_rate, 2),
                "verify_retried_count": verify_retried,
                "tier_1_p95_ms": tier_1_p95_ms,   # B6 canonical (no _elapsed_ infix)
                "tier_2_p95_ms": tier_2_p95_ms,   # B6 canonical (no _elapsed_ infix)
            },
        }
        print(_json.dumps(payload, indent=2))


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


# ---------------------------------------------------------------------------
# Calibrate helpers (injectable for testing)
# ---------------------------------------------------------------------------

def _calibrate_open_pa():
    """Open a PyAudio instance. Separated for testability."""
    import pyaudio
    return pyaudio.PyAudio()


def _calibrate_get_cache_dir():
    """Return the heyvox cache directory Path. Separated for testability."""
    from pathlib import Path
    try:
        from platformdirs import user_cache_dir
        return Path(user_cache_dir("heyvox"))
    except ImportError:
        return Path.home() / ".cache" / "heyvox"


# ---------------------------------------------------------------------------
# Calibrate command
# ---------------------------------------------------------------------------

# Default sample rate and chunk size for calibration (matches mic.py defaults)
_CALIB_SAMPLE_RATE = 16000
_CALIB_CHUNK_SIZE = 1280


def _cmd_calibrate(args):
    """Calibrate microphone noise floor and silence threshold.

    Records ambient noise for ``--duration`` seconds and computes
    per-device silence detection thresholds using MicProfileManager.
    Results are persisted to ``~/.cache/heyvox/mic-profiles.json``.

    With ``--show``: display cached calibration data without recording.

    Requirement: AUDIO-01, D-04
    """
    import json
    import time

    import numpy as np

    from heyvox.audio.profile import MicProfileManager

    cache_dir = _calibrate_get_cache_dir()

    # --show: display cached profiles and exit
    if getattr(args, "show", False):
        cache_file = cache_dir / "mic-profiles.json"
        if not cache_file.exists():
            print("No calibration cache found. Run 'heyvox calibrate' to calibrate your mic.")
            return

        try:
            data = json.loads(cache_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"Error reading cache: {e}", file=sys.stderr)
            sys.exit(1)

        if not data:
            print("Calibration cache is empty. Run 'heyvox calibrate' to calibrate your mic.")
            return

        print("Calibration cache:")
        for dev_name, entry in data.items():
            calibrated_at = entry.get("calibrated_at", 0)
            age_hours = (time.time() - calibrated_at) / 3600
            expires_days = 30 - age_hours / 24
            noise_floor = entry.get("noise_floor", "?")
            silence_threshold = entry.get("silence_threshold", "?")
            print(
                f"  {dev_name}:\n"
                f"    noise_floor:        {noise_floor}\n"
                f"    silence_threshold:  {silence_threshold}\n"
                f"    age:                {age_hours:.1f}h (expires in {expires_days:.1f} days)\n"
            )
        return

    # --- Find target device ---
    pa = _calibrate_open_pa()
    device_filter = getattr(args, "device", None)
    duration = getattr(args, "duration", 3)

    target_index = None
    target_name = None

    try:
        if device_filter:
            # Find first input device matching the filter substring
            for i in range(pa.get_device_count()):
                d = pa.get_device_info_by_index(i)
                if d["maxInputChannels"] <= 0:
                    continue
                if device_filter.lower() in d["name"].lower():
                    target_index = d.get("index", i)
                    target_name = d["name"]
                    break

            if target_index is None:
                print(
                    f"ERROR: No input device matching '{device_filter}' found.",
                    file=sys.stderr,
                )
                print("Available input devices:", file=sys.stderr)
                for i in range(pa.get_device_count()):
                    d = pa.get_device_info_by_index(i)
                    if d["maxInputChannels"] > 0:
                        print(f"  [{i}] {d['name']}", file=sys.stderr)
                sys.exit(1)
        else:
            # Use the default input device
            try:
                default = pa.get_default_input_device_info()
                target_index = default.get("index", 0)
                target_name = default["name"]
            except OSError:
                # No default input device — try any input device
                found = False
                for i in range(pa.get_device_count()):
                    d = pa.get_device_info_by_index(i)
                    if d["maxInputChannels"] > 0:
                        target_index = d.get("index", i)
                        target_name = d["name"]
                        found = True
                        break
                if not found:
                    print(
                        "ERROR: No input devices found. Connect a microphone and try again.",
                        file=sys.stderr,
                    )
                    sys.exit(1)

        # --- Record ambient noise ---
        chunk_count = duration * _CALIB_SAMPLE_RATE // _CALIB_CHUNK_SIZE
        print(f"Calibrating: {target_name}")
        print(f"Recording {duration}s of ambient noise ({chunk_count} chunks)...")
        print("Please stay quiet during calibration.", flush=True)

        chunks = []
        import pyaudio as _pyaudio
        stream = pa.open(
            format=_pyaudio.paInt16,
            channels=1,
            rate=_CALIB_SAMPLE_RATE,
            input=True,
            input_device_index=target_index,
            frames_per_buffer=_CALIB_CHUNK_SIZE,
        )
        try:
            for _ in range(chunk_count):
                raw = stream.read(_CALIB_CHUNK_SIZE, exception_on_overflow=False)
                chunk = np.frombuffer(raw, dtype=np.int16)
                chunks.append(chunk)
        finally:
            stream.stop_stream()
            stream.close()

        # --- Run calibration ---
        mgr = MicProfileManager(config_profiles={}, cache_dir=cache_dir)
        noise_floor, silence_threshold = mgr.run_calibration(chunks)
        mgr.save_calibration(target_name, noise_floor, silence_threshold)

        print("\nCalibration complete:")
        print(f"  Device:             {target_name}")
        print(f"  Noise floor:        {noise_floor}")
        print(f"  Silence threshold:  {silence_threshold}")
        print(f"  Cache:              {cache_dir / 'mic-profiles.json'}")
        print()
        print("Restart heyvox to apply the new silence threshold.")

    finally:
        pa.terminate()


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

    # log-health — daily digest of wake/STT/Herald log signals
    sub_loghealth = subparsers.add_parser(
        "log-health",
        help="Daily digest of wake/STT/Herald log signals (regression spotter)",
    )
    sub_loghealth.add_argument(
        "--date",
        default=None,
        help="ISO date YYYY-MM-DD (default: today). Filters herald + workspace logs.",
    )
    sub_loghealth.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON payload instead of human-readable digest",
    )
    sub_loghealth.set_defaults(func=_cmd_log_health)

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

    # calibrate -- calibrate mic noise floor and silence threshold (AUDIO-01, D-04)
    sub_calibrate = subparsers.add_parser(
        "calibrate",
        help="Calibrate microphone noise floor and silence threshold",
    )
    sub_calibrate.add_argument(
        "--device", "-d",
        default=None,
        metavar="NAME",
        help="Device name substring to calibrate (e.g. 'G435'). Default: system default input.",
    )
    sub_calibrate.add_argument(
        "--duration", "-t",
        type=int,
        default=3,
        metavar="SECS",
        help="Duration of ambient noise recording in seconds (default: 3)",
    )
    sub_calibrate.add_argument(
        "--show", "-s",
        action="store_true",
        help="Show current calibration cache without recording",
    )
    sub_calibrate.set_defaults(func=_cmd_calibrate)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()
