---
phase: quick
plan: 260628-mhf
subsystem: audio/wakeword
tags: [latency, instrumentation, observability, wake-word]
dependency_graph:
  requires: []
  provides: [WW_LATENCY log tags, perf_counter timestamps t0/t1/t2]
  affects: [heyvox/main.py, heyvox/audio/cues.py, heyvox/recording.py]
tech_stack:
  added: []
  patterns: [perf_counter timing, module-level cross-module variable threading]
key_files:
  created: []
  modified:
    - heyvox/main.py
    - heyvox/audio/cues.py
    - heyvox/recording.py
decisions:
  - "t0 captured via perf_counter at first above-threshold frame (prev==0 and not _is_rec); _t0_wakeword parallel to existing _first_hit_time alias"
  - "t1/detect_ms threaded from _run_loop to recording.start() via module-level _ww_t1/_ww_detect_ms in heyvox.main (avoids RecordingStateMachine refactor)"
  - "t2 captured in audio_cue() via keyword-only params (t1, detect_ms); existing callers unchanged"
  - "WW_LATENCY feedback log written via print(flush=True) in cues.py — routes to LOG_FILE_DEFAULT via launchd stdout redirect"
metrics:
  duration: "~12 min"
  completed: "2026-06-28"
  tasks_completed: 2/3
  files_changed: 3
---

# Phase quick Plan 260628-mhf: WW_LATENCY Instrumentation Summary

**One-liner:** perf_counter timestamps at t0/t1/t2 wired through wake-word pipeline; [WW_LATENCY] detect + feedback log lines emitted per activation; baseline measurement pending manual collection.

## Tasks

| # | Name | Status | Commit |
|---|------|--------|--------|
| 1 | t0/t1 timestamps in wake-word trigger path (main.py) | Done | 2ef86947c |
| 2 | t2 timestamp in audio_cue() + feedback latency log (cues.py + recording.py) | Done | 2ef86947c |
| 3 | Collect baseline and emit summary to log | Pending — requires manual wake-word activations | — |

## What Was Built

### Task 1: main.py instrumentation

- `_t0_wakeword: float = 0.0` added alongside `_first_hit_time` (alias kept for stop-word paths)
- At `prev == 0 and not _is_rec`: `_t0_wakeword = time.perf_counter()` (replaces `time.time()` on the alias)
- At trigger commit (`if not _is_rec and _t0_wakeword > 0`):
  - `_t1_wakeword = time.perf_counter()`
  - `_detect_ms = (_t1_wakeword - _t0_wakeword) * 1000`
  - Emits `[WW_LATENCY] detect={detect_ms:.0f}ms frames={active_frames_required} score={s:.3f}`
  - Replaces prior `[TIMING] wake→trigger` line
- Module-level `_ww_t1` and `_ww_detect_ms` set just before `recording.start()` (both `use_separate_words` and shared-word paths), reset to 0.0 after consumption

### Task 2: cues.py + recording.py

`audio_cue()` new signature:
```python
def audio_cue(name, cues_dir=None, *, t1: float = 0.0, detect_ms: float = 0.0) -> None
```

- t2 = `time.perf_counter()` captured after file-existence check, before suppression window update and Popen/stream call
- Emits `[WW_LATENCY] feedback={feedback_ms:.0f}ms total={total_ms:.0f}ms cue={name}` when t1 > 0
- All existing callers (paused, ok, sending, error, device_change) unaffected — keyword-only defaults to 0.0

`recording.py` "listening" cue call:
- Reads `heyvox.main._ww_t1` and `heyvox.main._ww_detect_ms`
- Resets both to 0.0 immediately after reading (prevents leakage across activations)
- PTT/handsfree paths leave module globals at 0.0 — no timing reported for non-wake-word starts

## Deviations from Plan

**1. [Rule 2 - Extension] print() instead of log() in cues.py**
- cues.py has no access to main.py's `log()` (circular import: main → cues, cues → main)
- Used `print(flush=True)` instead — routes to `LOG_FILE_DEFAULT` via launchd's `StandardOutPath` redirect
- Consistent with existing `get_cues_dir()` warning pattern in cues.py

**2. [Rule 1 - Clarification] Parallel _t0_wakeword variable (not rename)**
- Plan offered "rename or add parallel variable" for `_first_hit_time`
- Chose parallel variable: `_t0_wakeword` set alongside `_first_hit_time` alias
- Avoids touching the stop-word path which uses `_first_hit_time` reset at line ~1826 (stop paths were excluded from scope)

## Task 3: Baseline Measurement (Pending)

The daemon is running with instrumentation active (PID 71581, started 2026-06-28 16:17). Restart was applied via `launchctl kickstart -k`.

**To collect the baseline, run:**
```
say -v Samantha "Starting wake word collection. Say hey vox 25 times."
```

Then say the wake word 25 times with ~3s gaps. After collection, parse the log:

```
LOG=$(python3 -c "import sys; sys.path.insert(0, '/Users/work/conductor/workspaces/vox-v2/seattle'); from heyvox.config import load_config; print(load_config().log_file)")
grep "WW_LATENCY" "$LOG" | python3 -c "
import sys, re, statistics
detect, feedback, total = [], [], []
for line in sys.stdin:
    m = re.search(r'detect=(\d+)', line); detect.append(int(m.group(1))) if m else None
    m = re.search(r'feedback=(\d+)', line); feedback.append(int(m.group(1))) if m else None
    m = re.search(r'total=(\d+)', line); total.append(int(m.group(1))) if m else None
def stats(label, vals):
    if not vals: return
    s = sorted(vals)
    print(f'{label}: n={len(s)} median={statistics.median(s):.0f}ms p95={s[int(len(s)*0.95)]:.0f}ms p99={s[min(len(s)-1,int(len(s)*0.99))]:.0f}ms min={s[0]}ms max={s[-1]}ms')
stats('detect (t1-t0)', detect)
stats('feedback (t2-t1)', feedback)
stats('total (t2-t0)', total)
"
```

**Expected log format after activations:**
```
[WW_LATENCY] detect=160ms frames=2 score=0.923
[WW_LATENCY] feedback=45ms total=205ms cue=listening
```

**Interpretation guide:**
- `detect` = model accumulation window (floor: frames_required × 80ms = 160ms for 2 frames)
- `feedback` = afplay/stream dispatch overhead from trigger commit to cue start
- If `total` p50 < 150ms: baseline met
- If `total` p50 150-300ms: expect ~160ms from accumulation floor + ~20-50ms afplay spawn
- If `feedback` dominates: afplay cold-start is bottleneck (USB keepalive path should reduce this)

## Self-Check

- [x] `heyvox/main.py` modified with perf_counter + [WW_LATENCY] log lines
- [x] `heyvox/audio/cues.py` modified with t1/detect_ms kwargs and t2 logging
- [x] `heyvox/recording.py` modified to pass _ww_t1/_ww_detect_ms to "listening" cue
- [x] Syntax check passed: `python3 -c "import heyvox.main; import heyvox.audio.cues; import heyvox.recording"` → OK
- [x] Commit 2ef86947c exists with all 3 files
- [ ] Task 3 baseline measurement — pending manual activation collection

## Self-Check: PARTIAL
Task 1+2 code complete and committed. Task 3 requires physical wake-word activations which cannot be performed by the executor agent. The instrumentation is live in the daemon; collection can proceed immediately.
