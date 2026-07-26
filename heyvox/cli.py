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
    # DEF-228: `heyvox speak` is a one-shot process; it must not reset the
    # verbosity/style the user set at runtime just by speaking one line.
    start_worker(config, seed_only=True)

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
    """Set TTS verbosity. Levels: full, short, skip.

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
        "Verbosity": ["verbosity-full", "verbosity-short", "verbosity-skip"],
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
    # DEF-151: pre-dedup logs double-counted trigger-frames of one utterance
    # as attempts=2 window<0.3s. Split those legacy artifacts out so the
    # headline number reflects real "user had to repeat" events.
    real_efforts = []
    for ln in user_efforts:
        m = re.search(r"attempts=(\d+) window=([\d.]+)s", ln)
        if m and int(m.group(1)) == 2 and float(m.group(2)) < 0.3:
            continue
        real_efforts.append(ln)
    # DEF-117/118 forensic tags: stop-wake at full confidence rejected by the
    # pre-silence gate. last_silent <= 2s is the suspect band (a real
    # "...pause. Hey Vox" whose pause fell just outside the window — the
    # DEF-149 class); larger ages are speech-flow FPs blocked as designed.
    gate_blocks = [
        ln for ln in main_lines_all
        if "[NEAR_MISS_FAST_BLOCKED]" in ln or "[NEAR_MISS_WINDOW_BLOCKED]" in ln
    ]
    suspect_blocks = []
    for ln in gate_blocks:
        m = re.search(r"last_silent=([\d.]+)s ago", ln)
        if m and float(m.group(1)) <= 2.0:
            suspect_blocks.append(ln)
    cooldown_drops = [ln for ln in main_lines_all if "[WAKE_COOLDOWN_DROP]" in ln]
    stop_missed = [ln for ln in main_lines_all if "[STOP_MISSED]" in ln]

    _say("\n## Wake word (current rotation of heyvox.log)")
    _say(f"  Triggers fired:        {triggers}")
    _say(f"  VAD drops (lost):      {len(vad_drops)}")
    _say(f"  Near-misses (sub-thr): {len(near_misses)}")
    _say(
        f"  USER_EFFORT events:    {len(real_efforts)}"
        + (
            f"  ({len(user_efforts) - len(real_efforts)} frame-doubling artifacts filtered)"
            if len(user_efforts) != len(real_efforts) else ""
        )
    )
    _say(
        f"  Stop-gate blocks:      {len(gate_blocks)}"
        f"  ({len(suspect_blocks)} suspect: last_silent <= 2s)"
    )
    _say(f"  Cooldown drops:        {len(cooldown_drops)}")
    _say(f"  Missed stops (STT-confirmed): {len(stop_missed)}")

    if real_efforts:
        _say("\n  Recent USER_EFFORT (user had to repeat 'Hey Vox'):")
        for ln in real_efforts[-5:]:
            ts_match = re.search(r"\[(\d{2}:\d{2}:\d{2})\]", ln)
            n_match = re.search(r"attempts=(\d+) window=([\d.]+)s", ln)
            ts = ts_match.group(1) if ts_match else "??:??:??"
            if n_match:
                _say(f"    {ts}  {n_match.group(1)} attempts in {n_match.group(2)}s")

    if stop_missed:
        _say("\n  Recent STOP_MISSED (user said it, STT heard it, detector didn't):")
        for ln in stop_missed[-5:]:
            ts_match = re.search(r"\[(\d{2}:\d{2}:\d{2})\]", ln)
            detail = re.search(r"reason=(\S+) tail='([^']*)'", ln)
            ts = ts_match.group(1) if ts_match else "??:??:??"
            if detail:
                _say(f"    {ts}  ended_by={detail.group(1)}  tail='{detail.group(2)}'")

    if suspect_blocks:
        _say("\n  Recent suspect stop-gate blocks (full score, pause just outside window):")
        for ln in suspect_blocks[-5:]:
            ts_match = re.search(r"\[(\d{2}:\d{2}:\d{2})\]", ln)
            sc = re.search(r"score=([\d.]+)", ln)
            ls = re.search(r"last_silent=([\d.]+)s", ln)
            ts = ts_match.group(1) if ts_match else "??:??:??"
            _say(
                f"    {ts}  score={sc.group(1) if sc else '?'} "
                f"last_silent={ls.group(1) if ls else '?'}s"
            )

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
    stt_models: dict[str, int] = {}
    stt_cold = 0
    for ln in stt_lines_all:
        if '"label": "_stt_result"' in ln or '"label":"_stt_result"' in ln:
            d = re.search(r'"stt_time_s":\s*([\d.]+)', ln)
            if d:
                stt_times.append(float(d.group(1)))
            m = re.search(r'"stt_model":\s*"([^"]+)"', ln)
            if m:
                stt_models[m.group(1)] = stt_models.get(m.group(1), 0) + 1
            if re.search(r'"stt_warm":\s*false', ln):
                stt_cold += 1
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
    if stt_models:
        models_str = ", ".join(
            f"{k}×{v}" for k, v in sorted(stt_models.items(), key=lambda kv: -kv[1])
        )
        _say(f"  STT model(s):          {models_str}")
        if len(stt_models) > 1:
            _say("  WARN: multiple STT models in window — a model swap shifts p50/p99 (see DEF-137)")
    if stt_times:
        _say(f"  Cold loads (warm=false): {stt_cold}  (each pays full model-load latency)")
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
                "user_efforts": len(real_efforts),
                "user_effort_artifacts": len(user_efforts) - len(real_efforts),
                "stop_gate_blocks": len(gate_blocks),
                "stop_gate_blocks_suspect": len(suspect_blocks),
                "cooldown_drops": len(cooldown_drops),
                "stop_missed": len(stop_missed),
            },
            "stt": {
                "finals": len(stt_finals),
                "stt_time_p50": stt_p50,
                "stt_time_p99": stt_p99,
                "models": stt_models,
                "cold_loads": stt_cold,
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
    """Generate a structured bug report for GitHub Issues.

    Two modes:
    * default — Markdown text summary to clipboard (paste into issue body).
    * ``--bundle`` — full zip with logs + config + diagnostics in
      ``~/Downloads/``; optionally opens a pre-filled GitHub Issue.
    """
    from heyvox.reporting.text_report import run_bugreport
    from heyvox.reporting.bundle import (
        BundleOptions,
        build_bundle,
        summarize_bundle,
    )
    from heyvox.reporting.issue import (
        build_issue_url,
        open_in_browser,
        reveal_in_finder,
    )

    comment = getattr(args, "comment", "") or ""

    if getattr(args, "bundle", False):
        opts = BundleOptions(
            comment=comment,
            include_transcripts=getattr(args, "include_transcripts", False),
        )
        zip_path = build_bundle(opts)
        print(f"Bundle written: {zip_path}")
        print(summarize_bundle(zip_path))

        if getattr(args, "open_issue", False):
            body = run_bugreport(comment)
            title = "[Bug] " + (comment.splitlines()[0][:80] if comment else "")
            url = build_issue_url(title, body, bundle_path=zip_path)
            open_in_browser(url)
            reveal_in_finder(zip_path)
            print("Opened GitHub Issue in your browser and revealed the bundle in Finder.")
            print("Drag the .zip into the issue comment box before submitting.")
        return

    # Default: text report → clipboard
    report = run_bugreport(comment)
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


def _cmd_telemetry(args):
    """Inspect or toggle anonymous telemetry.

    Subcommands: status (default), enable, disable, preview, reset-id.
    """
    from heyvox.telemetry import consent
    from heyvox.telemetry import events as evmod

    action = getattr(args, "telemetry_action", None) or "status"

    if action == "enable":
        consent.enable()
        print(f"Telemetry enabled. Anonymous ID: {consent.get_anon_id()}")
        print("Run `heyvox telemetry preview` to see what gets sent.")
        return

    if action == "disable":
        consent.disable()
        print("Telemetry disabled.")
        return

    if action == "reset-id":
        new_id = consent.reset_anon_id()
        print(f"New anonymous ID: {new_id}")
        return

    if action == "preview":
        print(evmod.preview())
        return

    # status (default)
    from pathlib import Path
    from heyvox.constants import (
        TELEMETRY_QUEUE_DIR,
        TELEMETRY_LAST_BATCH_FILE,
    )

    print(f"Telemetry enabled : {consent.is_enabled()}")
    aid = consent.get_anon_id(create_if_missing=False)
    print(f"Anonymous ID      : {aid or '(not yet generated)'}")
    try:
        from heyvox.config import load_config
        cfg = load_config().telemetry
        print(f"Endpoint          : {cfg.endpoint}")
        print(f"Batch interval    : {cfg.batch_secs}s")
    except Exception as exc:
        print(f"Config read failed: {exc}")
    queued = list(Path(TELEMETRY_QUEUE_DIR).glob("batch-*.json")) if Path(TELEMETRY_QUEUE_DIR).exists() else []
    print(f"Queued batches    : {len(queued)}")
    last = Path(TELEMETRY_LAST_BATCH_FILE)
    if last.exists():
        import time as _t
        age = int(_t.time() - last.stat().st_mtime)
        print(f"Last attempt      : {age}s ago")
    else:
        print("Last attempt      : never")


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


def _cmd_learn_vocab(args):
    """Learn the STT vocabulary glossary from transcript history (Phase 16).

    Off the hot path: runs the offline extractor over ~/.local/share/heyvox/transcripts.jsonl,
    merges into vocab_store.json, renders the top-N (<=223 whisper tokens) into
    stt.local.initial_prompt, and writes it to config.yaml. Manual or nightly (launchd).
    Requires config.vocab_learner.enabled = true (opt-in, default off).
    """
    from heyvox.config import load_config, update_config
    from heyvox.audio import vocab_learner

    config = load_config()
    summary = vocab_learner.learn_vocab(
        cfg=config.vocab_learner,
        dry_run=getattr(args, "dry_run", False),
        run_eval=getattr(args, "eval", False),
        model_override=getattr(args, "model", None),
        max_terms_override=getattr(args, "max_terms", None),
        min_frequency_override=getattr(args, "min_frequency", None),
        reset=getattr(args, "reset", False),
    )

    if not summary.get("enabled", False):
        print("vocab_learner is disabled. Enable it in config.yaml:")
        print("  vocab_learner:\n    enabled: true")
        return

    # Monitoring summary (AI-SPEC §7): surface counts + token usage vs the 223 cap.
    print(f"Extracted {summary.get('extracted', 0)} items, "
          f"dropped {summary.get('dropped', 0)} malformed, "
          f"skipped {summary.get('skipped_batches', 0)} batches.")
    print(f"Promoted {summary.get('promoted', 0)} terms "
          f"({summary.get('token_count', 0)}/223 whisper tokens).")
    if summary.get("promoted", 0) == 0:
        print("WARNING: 0 terms promoted — extractor may be broken or all entries gated.")
    if summary.get("token_count", 0) >= 220:
        print("WARNING: glossary hit the token cap — lower-frequency terms were dropped.")

    # --eval: report deterministic recall/precision of the promoted glossary against
    # the bundled ground-truth fixture (key present only when --eval was passed).
    ev = summary.get("eval")
    if ev is not None:
        if ev.get("available") is False:
            print(f"[eval] reference fixture unavailable — eval skipped ({ev.get('reason', '')}).")
        elif "error" in ev:
            print(f"[eval] scoring failed: {ev['error']}")
        else:
            print(f"[eval] vs ground_truth: recall={ev['recall']:.2f} precision={ev['precision']:.2f} "
                  f"(content={ev['content']}, fp={ev['fp']}, wake={ev['wake']}).")

    rendered = summary.get("prompt", "")
    if getattr(args, "dry_run", False):
        print(f"\n[dry-run] would write initial_prompt:\n  {rendered!r}")
        return

    # Persist the rendered glossary where init_local_stt reads it. update_config takes
    # **kwargs; the dotted key has dots so splat it from a dict (NOT a positional arg).
    # It returns False if the write was skipped (e.g. config.yaml lacks an stt.local
    # section), so only claim success when it actually wrote (WR-04).
    wrote = update_config(**{"stt.local.initial_prompt": rendered})
    if wrote:
        print("Wrote stt.local.initial_prompt to config.yaml. Restart heyvox to apply.")
    else:
        print("WARNING: could not write stt.local.initial_prompt — config.yaml is missing the "
              "'stt:' / 'local:' section.")
        print("  Add a 'local:' block under 'stt:' and re-run, or STT biasing stays off.")


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
        help="Verbosity mode: full (default) | short | skip",
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
        help="Print to stdout instead of copying to clipboard (text mode only)",
    )
    sub_bugreport.add_argument(
        "--bundle",
        action="store_true",
        help="Build a full zip bundle (logs + config + diagnostics) in ~/Downloads/",
    )
    sub_bugreport.add_argument(
        "--open-issue",
        dest="open_issue",
        action="store_true",
        help="With --bundle: also open a pre-filled GitHub Issue in your browser",
    )
    sub_bugreport.add_argument(
        "--include-transcripts",
        dest="include_transcripts",
        action="store_true",
        help="With --bundle: also include the last 20 transcripts (private text)",
    )
    sub_bugreport.add_argument(
        "--comment", "-m",
        default="",
        help="Short description of the problem (included in the report body)",
    )
    sub_bugreport.set_defaults(func=_cmd_bugreport)


    # telemetry — opt-in anonymous telemetry
    sub_telemetry = subparsers.add_parser(
        "telemetry",
        help="Inspect or toggle anonymous telemetry (opt-in)",
    )
    sub_telemetry.add_argument(
        "telemetry_action",
        nargs="?",
        default="status",
        choices=["status", "enable", "disable", "preview", "reset-id"],
        help="Action: status (default), enable, disable, preview, reset-id",
    )
    sub_telemetry.set_defaults(func=_cmd_telemetry)

    # register — register MCP server with AI agents
    sub_register = subparsers.add_parser("register", help="Register HeyVox MCP server with AI coding agents")
    sub_register.add_argument(
        "agent",
        nargs="?",
        default=None,
        help="Filter by agent name (e.g. 'cursor'). Registers all detected if omitted.",
    )
    sub_register.set_defaults(func=_cmd_register)

    # learn-vocab — batch vocabulary extractor for STT initial_prompt (Phase 16)
    sub_learn_vocab = subparsers.add_parser(
        "learn-vocab",
        help="Learn vocabulary glossary from dictation history (STT biasing, Phase 16)",
    )
    sub_learn_vocab.add_argument(
        "--dry-run",
        action="store_true",
        help="Run extraction but do not write the store or initial_prompt file",
    )
    sub_learn_vocab.add_argument(
        "--reset",
        action="store_true",
        help="Start with an empty store (discards accumulated vocabulary)",
    )
    sub_learn_vocab.add_argument(
        "--eval",
        action="store_true",
        help="Run the post-extraction evaluation harness",
    )
    sub_learn_vocab.add_argument(
        "--model",
        default=None,
        metavar="MODEL",
        help="Override the extraction model (e.g. claude-opus-4-8)",
    )
    sub_learn_vocab.add_argument(
        "--max-terms",
        type=int,
        default=None,
        metavar="N",
        help="Cap how many terms enter initial_prompt (default: from config)",
    )
    sub_learn_vocab.add_argument(
        "--min-frequency",
        type=int,
        default=None,
        metavar="N",
        help="Minimum corpus frequency to include a term (default: from config)",
    )
    sub_learn_vocab.set_defaults(func=_cmd_learn_vocab)

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
