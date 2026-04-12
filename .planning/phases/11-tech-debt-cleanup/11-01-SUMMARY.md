---
phase: 11-tech-debt-cleanup
plan: 01
subsystem: testing
tags: [pytest, AppContext, RecordingStateMachine, shim-removal, tech-debt]

# Dependency graph
requires:
  - phase: 06-decomposition
    provides: AppContext, RecordingStateMachine modules replacing main.py globals
  - phase: 09-test-suite
    provides: test_flag_coordination.py with shim-based tests
provides:
  - Clean main.py with zero backward-compat shim variables or wrapper functions
  - test_flag_coordination.py fully migrated to AppContext/RecordingStateMachine API
affects: [11-tech-debt-cleanup]

# Tech tracking
tech-stack:
  added: []
  patterns: [AppContext constructor injection, direct import instead of module-level shims]

key-files:
  created: []
  modified:
    - tests/test_flag_coordination.py
    - heyvox/main.py

key-decisions:
  - "Migrate tests before deleting shims (never same commit) per STATE.md decision"
  - "Remove threading import from main.py since _state_lock was its only consumer"

patterns-established:
  - "Tests use AppContext and RecordingStateMachine directly instead of main module shims"

requirements-completed: [DEBT-01]

# Metrics
duration: 8min
completed: 2026-04-12
---

# Phase 11 Plan 01: Shim Removal Summary

**Removed 7 backward-compat shim variables and 2 wrapper functions from main.py after migrating test_flag_coordination.py to use AppContext and RecordingStateMachine directly**

## Performance

- **Duration:** ~8 min
- **Started:** 2026-04-12T09:42:00Z
- **Completed:** 2026-04-12T09:50:55Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments

- Rewrote test_flag_coordination.py to import AppContext and RecordingStateMachine directly — zero references to `from heyvox import main as m`
- Deleted module-level shims from main.py: `is_recording`, `busy`, `recording_start_time`, `_audio_buffer`, `_triggered_by_ptt`, `_recording_target`, `_state_lock`, `_recording`
- Deleted `start_recording()` and `stop_recording()` backward-compat wrapper functions
- Removed `import threading` from main.py (was only used by `_state_lock`)
- Full test suite (383 tests) passes with zero regressions

## Task Commits

Each task was committed atomically:

1. **Task 1: Migrate test_flag_coordination.py to AppContext API** - `126ad39` (refactor)
2. **Task 2: Remove shim variables and wrapper functions from main.py** - `4f833b1` (feat)

## Files Created/Modified

- `tests/test_flag_coordination.py` - Rewritten to use AppContext/RecordingStateMachine; added monkeypatching for audio cues and TTS side effects
- `heyvox/main.py` - Removed ~70 lines: all shim vars, wrapper functions, threading import, and `_recording = recording` assignment

## Decisions Made

- Migrate tests before deleting shims (separate commits) per the plan's stated ordering requirement — ensures tests never reference code that no longer exists
- Remove `import threading` from main.py since `_state_lock` was its only consumer (checked all usages with grep before removing)

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None.

## Next Phase Readiness

- DEBT-01 (zero shim vars in main.py) is satisfied
- Ready for 11-02 (next tech debt plan)

---
*Phase: 11-tech-debt-cleanup*
*Completed: 2026-04-12*

## Self-Check: PASSED

- tests/test_flag_coordination.py: FOUND
- heyvox/main.py: FOUND
- 11-01-SUMMARY.md: FOUND
- Commit 126ad39: FOUND
- Commit 4f833b1: FOUND
