---
phase: quick
plan: 260628-mhf
type: execute
wave: 1
depends_on: []
files_modified:
  - heyvox/main.py
  - heyvox/audio/cues.py
autonomous: true
requirements: []

must_haves:
  truths:
    - "Every wake-word activation logs t0 (last frame entering model), t1 (accumulation complete = trigger), t2 (cue playback started)"
    - "Log lines use tag [WW_LATENCY] so they are greppable"
    - "After 20+ activations, grep output shows median/p95/p99 across the three intervals"
    - "afplay cold-start overhead is measured and visible in the log"
  artifacts:
    - path: "heyvox/main.py"
      provides: "t0 + t1 timestamps via perf_counter, [WW_LATENCY] log lines"
      contains: "WW_LATENCY"
    - path: "heyvox/audio/cues.py"
      provides: "t2 timestamp logged at moment cue starts (before Popen/stream call)"
      contains: "WW_LATENCY"
  key_links:
    - from: "heyvox/main.py (_run_loop)"
      to: "heyvox/audio/cues.audio_cue"
      via: "recording.start() → audio_cue('listening')"
      pattern: "audio_cue.*listening"
---

<objective>
Instrument the wake-word pipeline with precise perf_counter timestamps at t0, t1,
and t2 to produce greppable latency data. The goal is a baseline of median + p95 +
p99 over 20-30 activations that exposes where time is lost between "frame enters
model" and "cue starts playing".

Purpose: without measured numbers we cannot know whether the bottleneck is the
openwakeword accumulation window (chunk × frames_required), the
recording.start() call path, or afplay spawn latency. Measurement first.

Output:
- Instrumented main.py and cues.py
- Sample log output showing [WW_LATENCY] lines after 20-30 activations
- Summary of median/p95/p99 for detection latency (t1-t0), feedback latency
  (t2-t1), and total (t2-t0)
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/STATE.md
@heyvox/audio/cues.py
@heyvox/main.py
</context>

<tasks>

<task type="auto">
  <name>Task 1: Add t0/t1 timestamps in the wake-word trigger path (main.py)</name>
  <files>heyvox/main.py</files>
  <action>
The run loop already logs `[TIMING] wake→trigger` at line ~1824 using `time.time()`.
Replace/extend this with `time.perf_counter()` and capture t0 separately.

t0 definition: the timestamp of the LAST audio frame that caused the model score to
cross threshold in the FIRST hit of the current accumulation run. This is the best
proxy for "last frame containing the wake word" available without hardware-level
timestamping. Record it when `prev == 0 and not _is_rec` (line ~1675, where
`_first_hit_time` is already set). Rename `_first_hit_time` to `_t0_wakeword` to
make the intent explicit, or add a parallel `_t0_wakeword` variable — whichever is
less disruptive.

t1 definition: `time.perf_counter()` recorded immediately BEFORE the call to
`recording.start()` (line ~1888 in the `elif use_separate_words` branch and line
~1899 in the shared-word branch). This is when the trigger is committed and the cue
will be fired.

At the t1 capture point, emit a log line:
  `[WW_LATENCY] detect={detect_ms:.0f}ms frames={active_frames_required} score={s:.3f}`
where `detect_ms = (t1 - t0) * 1000`.

Use `time.perf_counter()` for both t0 and t1 (monotonic, sub-millisecond precision).
`_t0_wakeword` should be reset to 0.0 after use (same as existing `_first_hit_time`
reset at line ~1826).

Do not change the cooldown logic, accumulator logic, or any gate — this is
measurement only. The existing `[TIMING] wake→trigger` log line at ~1824 covers
the case when `_first_hit_time > 0 and not _is_rec`; update that line to use
`perf_counter` and rename it to `[WW_LATENCY]` as well, or emit both (new tag
takes precedence for grepping). Keep the stop-word path untouched (stop latency
is not the target of this task).
  </action>
  <verify>
    <automated>grep -n "WW_LATENCY\|_t0_wakeword\|perf_counter" /Users/work/conductor/workspaces/vox-v2/seattle/heyvox/main.py | head -20</automated>
  </verify>
  <done>
main.py contains at least two `perf_counter` calls (t0 capture + t1 capture) and
emits `[WW_LATENCY] detect=...ms` log lines. The existing `[TIMING] wake→trigger`
either updated or accompanied by the new tag. No functional change to trigger logic.
  </done>
</task>

<task type="auto">
  <name>Task 2: Add t2 timestamp in audio_cue() and log feedback latency</name>
  <files>heyvox/audio/cues.py</files>
  <action>
`audio_cue()` in cues.py currently fires afplay via subprocess.Popen (or
`play_cue_via_stream` for USB keep-alive path). We need t2 = the moment the cue
playback is dispatched.

Add an optional `t1_wakeword: float = 0.0` parameter to `audio_cue()`. When
`t1_wakeword > 0`, capture `t2 = time.perf_counter()` at the top of the function
body — after the file-existence check but before the suppression-window update and
before the Popen/stream call. Then emit:
  `[WW_LATENCY] feedback={feedback_ms:.0f}ms total={total_ms:.0f}ms cue={name}`
where `feedback_ms = (t2 - t1_wakeword) * 1000` and `total_ms` is passed in as
a separate kwarg `detect_ms: float = 0.0` so the log line can print the full
end-to-end number without needing t0 here.

Full signature addition:
  `def audio_cue(name: str, cues_dir: str | None = None, *, t1: float = 0.0, detect_ms: float = 0.0) -> None`

The existing callers pass only `name` and `cues_dir` — the new kwargs are
keyword-only so no existing call site breaks. No default argument change needed
for any existing call.

Update the single call site in recording.py line ~396 (`audio_cue("listening", cues_dir)`)
to pass `t1=_t1_wakeword, detect_ms=_detect_ms` — but only for the "listening" cue,
which is the wake-word feedback cue. All other `audio_cue` calls (paused, ok, sending,
error) omit the timing kwargs and continue unchanged.

To thread `_t1_wakeword` and `_detect_ms` from main.py into recording.start(), the
simplest approach is: store them as short-lived module-level variables in main.py set
just before calling `recording.start()` and consumed once in `RecordingStateMachine.start()`.
Alternatively, pass them as keyword arguments to `recording.start()` if that method
already accepts `**kwargs` or can be extended without touching the PTT path. Choose
whichever approach touches fewer lines — do not refactor the RecordingStateMachine
constructor or AppContext.

Note: `play_cue_via_stream` (the USB keepalive fast path in cues.py) does not need
its own timing — the overhead difference between the two paths is captured by t2
landing before either branch, so the log reflects the dispatch cost regardless.
  </action>
  <verify>
    <automated>grep -n "WW_LATENCY\|t1_wakeword\|t1: float\|feedback_ms\|total_ms" /Users/work/conductor/workspaces/vox-v2/seattle/heyvox/audio/cues.py | head -15</automated>
  </verify>
  <done>
cues.py emits `[WW_LATENCY] feedback=...ms total=...ms cue=listening` when called
from the wake-word path. All other callers unaffected (no new required args).
recording.py "listening" call updated to pass t1 + detect_ms. Daemon can be
restarted and the next 20 activations produce greppable [WW_LATENCY] pairs.
  </done>
</task>

<task type="auto">
  <name>Task 3: Collect baseline and emit summary to log</name>
  <files>heyvox/main.py</files>
  <action>
After the instrumentation is in place, restart the daemon and collect a baseline
sample of 20-30 activations. Run:

  launchctl kickstart "gui/$UID/com.heyvox.listener"

Then say the wake word 25 times with ~3s gaps. After the session, parse the log
with a one-liner to extract the numbers:

  grep "WW_LATENCY" $(python3 -c "from heyvox.config import load_config; print(load_config().log_file)") \
    | grep "detect=" | python3 -c "
import sys, re, statistics
detect, feedback, total = [], [], []
for line in sys.stdin:
    m = re.search(r'detect=(\d+)', line); detect.append(int(m.group(1))) if m else None
    m = re.search(r'feedback=(\d+)', line); feedback.append(int(m.group(1))) if m else None
    m = re.search(r'total=(\d+)', line); total.append(int(m.group(1))) if m else None
def stats(label, vals):
    if not vals: return
    s = sorted(vals)
    print(f'{label}: n={len(s)} median={statistics.median(s):.0f}ms p95={s[int(len(s)*0.95)]:.0f}ms p99={s[min(len(s)-1, int(len(s)*0.99))]:.0f}ms min={s[0]}ms max={s[-1]}ms')
stats('detect (t1-t0)', detect)
stats('feedback (t2-t1)', feedback)
stats('total (t2-t0)', total)
"

Run the one-liner, paste the output here as a log entry with prefix `[WW_LATENCY_SUMMARY]`,
and append it to the heyvox log file:

  echo "[WW_LATENCY_SUMMARY] $(date '+%H:%M:%S') <paste output here>" >> <log_file>

The executor should run the 25-activation collection, then run the parse script,
then paste the result as a `[WW_LATENCY_SUMMARY]` log entry. No code change in
this task — it is the measurement execution step.

If total p50 is under 150ms already: note it and declare baseline met. Done.
If total p50 is 150-300ms: the bottleneck is likely afplay cold-start or the
  frame accumulation window (2 × 80ms = 160ms floor). Note which interval dominates.
If total p50 is over 300ms: look at feedback_ms — if that is high, afplay startup
  is the culprit; if detect_ms is high, the model scoring or accumulator is slow.

Report the breakdown in the SUMMARY.md.
  </action>
  <verify>
    <automated>grep "WW_LATENCY_SUMMARY" "$(python3 -c 'import sys; sys.path.insert(0, "/Users/work/conductor/workspaces/vox-v2/seattle"); from heyvox.config import load_config; print(load_config().log_file)' 2>/dev/null || echo '/tmp/heyvox.log')" 2>/dev/null | tail -5 || echo "SUMMARY_PENDING — run activations first"</automated>
  </verify>
  <done>
At least one `[WW_LATENCY_SUMMARY]` line exists in the heyvox log with n >= 10
activations. The SUMMARY.md records median/p95/p99 for detect, feedback, and total,
plus a one-sentence assessment of where time is spent.
  </done>
</task>

</tasks>

<threat_model>
No new trust boundaries introduced. Timing instrumentation is read-only on the audio
path; the new `t1`/`detect_ms` kwargs are keyword-only and cannot be triggered by
external input. No security implications.
</threat_model>

<verification>
After all three tasks:
- grep [WW_LATENCY] heyvox.log shows paired detect + feedback lines per activation
- grep [WW_LATENCY_SUMMARY] heyvox.log shows aggregated stats
- Daemon restarts cleanly (no import errors from signature change)
- All existing audio_cue callers still work (keyword-only args, defaults to 0.0)
</verification>

<success_criteria>
- [WW_LATENCY] lines appear in heyvox.log for every wake-word activation
- Baseline report: median/p95/p99 for detect, feedback, total over >= 20 samples
- Dominant latency interval identified (detect vs feedback)
- No regression in existing functionality (cue still fires, recording still starts)
</success_criteria>

<output>
Create `.planning/quick/260628-mhf-wakeword-latency/260628-mhf-SUMMARY.md` when done.
Include the WW_LATENCY_SUMMARY output and a one-paragraph assessment of where
optimization effort should focus based on the measured breakdown.
</output>
