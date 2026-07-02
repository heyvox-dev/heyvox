---
phase: quick-260702-cdg-wake-word-training-data-quality-gate
plan: 01
subsystem: training
tags: [wake-word, whisper, mlx_whisper, training-data, quality-gate, openwakeword]

# Dependency graph
requires:
  - phase: none
    provides: n/a (quick task, no phase dependency)
provides:
  - "TrainingCollector with retroactive relabelers deleted (reclassify_tp_start_as_fp, reclassify_fn_start)"
  - "tools/quality_gate.py: mandatory, resumable, timeout-hardened batch Whisper quality gate"
  - "tools/collect_personal_features.py gated by default (--gate-only / --skip-gate escape hatches)"
  - "tools/fp_rate_eval.py --history-file append-only eval log"
  - ".planning/DEFECT-LOG.md DEF-167 entry + P-retroactive-labeling pattern"
affects: [wake-word-retraining, training-data-collection, whisper-audit-tooling]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Evidence-based label correction via STT re-transcription instead of timing/outcome heuristics"
    - "Parent-process RMS pre-gate before spawning an expensive transcription subprocess"
    - "Persistent, path-keyed, resumable JSONL state directory surviving across script invocations"
    - "subprocess.run(timeout=...) as the only reliable way to hard-kill a stuck native MLX/Metal call"

key-files:
  created:
    - tools/quality_gate.py
  modified:
    - heyvox/audio/training_collector.py
    - heyvox/recording.py
    - heyvox/main.py
    - heyvox/app_context.py
    - tests/test_training_collector.py
    - tools/collect_personal_features.py
    - tools/fp_rate_eval.py
    - .planning/DEFECT-LOG.md

key-decisions:
  - "RMS silence pre-gate moved to the PARENT process (not the worker subprocess) so silent clips never pay the ~3.7s transcription-subprocess spawn cost"
  - "Gate state (results.jsonl) persists ACROSS runs under ~/.config/heyvox/training/.gate_state/ by default, keyed by absolute clip path, so only the first full corpus pass is expensive"
  - "Per-clip hard timeout enforced via subprocess.run(timeout=...) SIGKILL, not an external timeout wrapper -- a Python thread cannot interrupt a stuck native MLX/Metal call, an OS process kill can"
  - "quality_gate.py does a deferred import of collect_personal_features inside main() (not at module level) to avoid a circular import, since collect_personal_features.py imports run_gate from quality_gate.py at module level"
  - "Quarantine-only moves, never deletes -- every move recorded in a timestamped, reversible manifest matching the existing recfp_cleanup_manifest_*.json precedent"

patterns-established:
  - "P-retroactive-labeling (DEFECT-LOG.md): any training/label-correction mechanism inferring a label from outcome/timing rather than re-examining evidence risks manufacturing the exact error class it claims to fix -- future auto-labeling paths must be evidence-based or route through tools/quality_gate.py"

requirements-completed: [CDG-01, CDG-02, CDG-03, CDG-04, CDG-05]

# Metrics
duration: ~25min
completed: 2026-07-02
---

# Quick Task 260702-cdg: Wake-Word Training Data Quality Gate Summary

**Removed two evidence-free retroactive label-correction methods from TrainingCollector (measured 11.5% real triggers mislabeled fp, 54% real true-negatives mislabeled fn) and replaced them with a mandatory, resumable, timeout-hardened batch Whisper quality gate (tools/quality_gate.py) that quarantines/recovers training clips based on actual transcript evidence.**

## Performance

- **Tasks:** 5/5 completed
- **Files modified:** 8 (1 created: tools/quality_gate.py; 7 modified)
- **Commits:** 5 task commits, all atomic

## Accomplishments

- Deleted `TrainingCollector.reclassify_tp_start_as_fp` and `reclassify_fn_start` (and their `_recent_tp_start`/`_recent_tn` tracking state, the now-unused `fn_reclassify_window_secs` constructor param) along with all six call sites across `heyvox/recording.py` and `heyvox/main.py`. `save_fn_stop` (backed by the evidence-based `classify_stop_outcome`) remains the sole automatic fn-labeling path.
- Built `tools/quality_gate.py`: absorbs `whisper_sanity_pass.py`'s proven `_WW_RE`/`_HALLUCINATION_RE` matcher (imported, not duplicated) and `REFERENCE-fp_check_step0.py`'s resumable write-ahead-JSONL driver. Positive-dir clips without the wake word get quarantined; negative-dir clips WITH the wake word get recovered into `positives/` (<=2.5s) or `quarantine/` (>2.5s). Never deletes -- every move is recorded in a timestamped, reversible manifest.
- Wired the gate into `tools/collect_personal_features.py` as the mandatory first step of `main()`, with `--gate-only` (audit without featurizing) and `--skip-gate` (explicit bypass, prints a visible warning) as the only two ways to deviate.
- Added `--history-file` to `tools/fp_rate_eval.py`: append-only JSONL log of every eval run (timestamp, model, corpus sizes, per-threshold results, best-passing-threshold), always-on, opens with `"a"` only.
- Logged DEF-167 in `.planning/DEFECT-LOG.md` with all 8 required fields, matching the DEF-165/166 structural format exactly, plus a new `P-retroactive-labeling` entry in Patterns & Process Gaps.

## Task Commits

Each task was committed atomically:

1. **Task 1: Remove retroactive relabelers from TrainingCollector and all call sites** - `31b3a0ddb` (fix)
2. **Task 2: Build the mandatory batch Whisper quality gate** - `e5c985947` (feat)
3. **Task 3: Wire the gate into collect_personal_features.py** - `59196d58d` (feat)
4. **Task 4: Add --history-file to fp_rate_eval.py** - `d77cd4a0e` (feat)
5. **Task 5: Log DEF-167 in DEFECT-LOG.md** - `04583e455` (docs)

_No plan-metadata commit -- per this session's constraints, the orchestrator commits STATE.md/PLAN.md/SUMMARY.md separately after this executor returns._

## Files Created/Modified

- `tools/quality_gate.py` - New module: `run_gate()`, `main()`, `_transcribe_worker()`, `transcribe_with_timeout()`, resumable JSONL state driver
- `heyvox/audio/training_collector.py` - Deleted `reclassify_tp_start_as_fp`, `reclassify_fn_start`, their tracking lists, the `fn_reclassify_window_secs` param; docstring updated
- `heyvox/recording.py` - Removed 4 call sites (post-trim-short, low-energy, empty-stt, user-cancelled); the `save_fp` calls that followed each are unchanged
- `heyvox/main.py` - Removed 2 call sites (no-speech cancel branch, TP-start/TN retry-pattern branch); comment updated to point at the new gate
- `heyvox/app_context.py` - Reworded a docstring sentence that named the removed `reclassify_tp_start_as_fp` method (orchestrator amendment 1)
- `tests/test_training_collector.py` - Added 4 new tests proving the negative behavior (no reclassify methods exist; aborted trigger doesn't fp-relabel; tn save doesn't get fn-relabeled by a later trigger)
- `tools/collect_personal_features.py` - Added `--gate-only`/`--skip-gate` flags, gate invocation as the mandatory first step of `main()`, spot-check log for negative-side recoveries
- `tools/fp_rate_eval.py` - Added `--history-file` flag, history-record building, local-vs-remote retrain/eval boundary documented in the module docstring
- `.planning/DEFECT-LOG.md` - New DEF-167 entry + `P-retroactive-labeling` Patterns bullet (force-added; `.planning/` is gitignored but this file is an explicit task deliverable)

## Decisions Made

- **RMS pre-gate placement (orchestrator amendment 2):** moved from the worker subprocess (as the original plan specified) into the parent process, before deciding whether to spawn a transcription subprocess at all. Measured this session: a cold transcription subprocess costs ~3.7s; the RMS-only path returns in ~0.3s. This means silent clips (a meaningful fraction of `tn/`, which is specifically "confusable ambient that almost fired") never pay the subprocess-spawn cost.
- **Persistent state dir across runs (orchestrator amendment 3):** `~/.config/heyvox/training/.gate_state/` is never wiped between invocations. `results.jsonl` is keyed by absolute clip path; a clip already in it is skipped on every later run. Made this explicit in the module docstring since it's the difference between "run once, ~1-2h" and "run every collect, ~1-2h forever."
- **Deferred import for the circular-import risk:** `quality_gate.py`'s CLI `main()` needs `collect_personal_features.POSITIVE_DIRS`/`HARD_NEGATIVE_DIRS` as defaults, but `collect_personal_features.py` imports `run_gate` FROM `quality_gate.py` at module level. Resolved by doing `import collect_personal_features as cpf` lazily inside `quality_gate.main()`, never at module level in `quality_gate.py`. Verified: `python3 -c "import quality_gate, collect_personal_features"` imports both cleanly with no circular-import error.
- **`save_tp_start`/`save_tn` kept the `return_path=True` call shape** even though the tracking-list side effect using that return value is gone -- matches the plan's literal instruction and preserves the existing `_save()` internal contract (return `Path | None` vs `bool`) without touching `_save()` itself.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking, orchestrator amendment 1] Two extra docstring references to the removed method name**
- **Found during:** Task 1 (verify step -- the plan's own negative grep would have caught these on the first verify run)
- **Issue:** `heyvox/app_context.py:113` and (originally) `heyvox/audio/training_collector.py:152` both referenced `reclassify_tp_start_as_fp` by name in prose docstrings, which the plan's verify grep (`grep -rn "reclassify_fn_start\|reclassify_tp_start_as_fp" heyvox/`) would have matched even after the method itself was deleted.
- **Fix:** Reworded `app_context.py:113`'s docstring sentence to drop the method-name reference. The `training_collector.py:152` reference was resolved as a side effect of rewriting `save_tp_start`'s docstring during the method-body edit (the docstring line that mentioned it no longer exists in the new, shorter docstring).
- **Files modified:** `heyvox/app_context.py`, `heyvox/audio/training_collector.py`
- **Verification:** `grep -rn "reclassify_fn_start\|reclassify_tp_start_as_fp" heyvox/ --include="*.py"` returns zero matches (confirmed as part of Task 1's verify).
- **Committed in:** `31b3a0ddb` (Task 1 commit)

**2. [Rule 1 - minor accuracy, self-identified while editing the same file] Stale "(retroactive)" label in collect_personal_features.py's docstring**
- **Found during:** Task 3 (module docstring describes `~/.config/heyvox/training/fn` as "runtime false negatives (retroactive)")
- **Issue:** The word "(retroactive)" became misleading after Task 1 removed the retroactive `reclassify_fn_start` relabeler -- `fn/` is still populated, but exclusively via the evidence-based `save_fn_stop` path now, which was already true before this plan (the retroactive mechanism added `fn_start_*` clips as a SEPARATE relabeling side effect, not the primary way `fn/` was populated).
- **Fix:** Changed the docstring line to "runtime false negatives (evidence-based, save_fn_stop)".
- **Files modified:** `tools/collect_personal_features.py`
- **Verification:** Visual read of the updated docstring; no test covers docstring prose.
- **Committed in:** `59196d58d` (Task 3 commit)

**3. [Rule 3 - Blocking, environment-specific] `.planning/DEFECT-LOG.md` is gitignored; required `git add -f`**
- **Found during:** Task 5 (attempting to stage the file for commit -- `git status --short` showed nothing after editing it)
- **Issue:** `.gitignore` has a blanket `.planning/` rule (public-release repo hygiene -- internal planning docs excluded). `DEFECT-LOG.md` had never been force-added before (unlike `MILESTONES.md`/`PROJECT.md`/`REQUIREMENTS.md`/`RETROSPECTIVE.md`/`ROADMAP.md`, which ARE tracked in this repo's history despite the same gitignore rule), so my edit produced no visible `git status` change until I checked `git check-ignore -v`.
- **Fix:** `git add -f .planning/DEFECT-LOG.md`, consistent with the explicit task constraint ("Task 5 modifies .planning/DEFECT-LOG.md — DO commit that as Task 5's commit (it is a task deliverable, not a gsd-quick meta-artifact)"). This is the first time DEFECT-LOG.md has ever been committed to this repo's git history -- prior DEF-163 through DEF-166 entries existed only on-disk in this working tree before this commit.
- **Files modified:** `.planning/DEFECT-LOG.md` (force-added)
- **Verification:** `git log --oneline -- .planning/DEFECT-LOG.md` now shows the commit; `git show 04583e455 --stat` confirms it as a new file addition containing the full existing DEF-163..166 history plus the new DEF-167 entry.
- **Committed in:** `04583e455` (Task 5 commit)

---

**Total deviations:** 3 auto-fixed (2 Rule 3 blocking, 1 Rule 1 minor accuracy)
**Impact on plan:** All three were necessary to satisfy the plan's own verify criteria (deviation 1) or were flagged in advance by the orchestrator amendments (deviations 1 and 3 were explicitly anticipated). No scope creep -- deviation 2 is a one-line docstring wording fix in a file already being edited for this task.

## Issues Encountered

None beyond the deviations above. All 5 tasks' `<verify>` blocks and the plan's full 9-item `<verification>` section (items 1-8; item 9 is a user-executed daemon restart, not part of automated verify) passed on the first attempt after the deviation fixes.

## Real Corpus Safety

Per this session's explicit constraint, the FULL multi-hundred-clip gate corpus pass was NOT run. All Task 2/3 verification used either the plan's own fast checks (import, `--help`, single `--_worker` smoke test) or isolated `/tmp/` fixture directories with copied (not moved) real clips. Confirmed before and after this session's work: `~/.config/heyvox/training/fp/` = 304 clips, `positives/` = 403 clips (unchanged); no `~/.config/heyvox/training/.gate_state/` directory was created (confirming the real gate never ran against the live corpus).

## User Setup Required

None - no external service configuration required.

**Daemon restart required (per plan verification item 9, NOT executed by this session per explicit instruction):** `heyvox/audio/training_collector.py`, `heyvox/recording.py`, and `heyvox/main.py` are live-daemon hot-path files. Run this after reviewing the changes, before relying on the new no-retroactive-relabeling behavior in a live session:

```
launchctl kickstart -k "gui/$UID/com.heyvox.listener"
```

Tasks 2-4 (`tools/quality_gate.py`, `tools/collect_personal_features.py`, `tools/fp_rate_eval.py`) are offline scripts with zero live-daemon impact and need no restart.

## Next Phase Readiness

- The training pipeline no longer manufactures mislabeled clips via timing/outcome heuristics -- new `fp_start_*`/`fn_start_*` files with those specific `_start_` suffixes will stop appearing (they were exclusively produced by the two deleted `reclassify_*` methods).
- `tools/quality_gate.py` is ready to run against the real corpus whenever the user chooses -- expected first-run cost is ~1-2 hours over the full existing corpus (per the plan's stated estimate), with every subsequent run being incremental (only new clips since the last run get transcribed).
- Deferred (explicitly out of scope for this plan, confirmed still open): an env-diversity recording guide (music/distance/outdoor conditions) -- no task in this plan addressed it.
- No blockers for running the real gate pass or the next retrain cycle.

---
*Phase: quick-260702-cdg-wake-word-training-data-quality-gate*
*Completed: 2026-07-02*

## Self-Check: PASSED

All 10 claimed files confirmed present on disk; all 5 claimed commit hashes confirmed present in `git log --oneline --all`.
