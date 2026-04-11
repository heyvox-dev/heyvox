---
phase: 06-decomposition
plan: 00
subsystem: testing
tags: [pytest, test-scaffolding, tdd, nyquist, app-context, device-manager, recording-state]

# Dependency graph
requires: []
provides:
  - "tests/test_app_context.py: behavioral stubs for AppContext dataclass (skipped)"
  - "tests/test_device_manager.py: behavioral stubs for DeviceManager class (skipped)"
  - "tests/test_recording_state.py: behavioral stubs for RecordingStateMachine (skipped)"
affects: [06-01, 06-02, 06-03]

# Tech tracking
tech-stack:
  added: []
  patterns: ["Nyquist compliance: test scaffolds precede production code", "pytestmark module-level skip until module is created"]

key-files:
  created:
    - tests/test_app_context.py
    - tests/test_device_manager.py
    - tests/test_recording_state.py
  modified: []

key-decisions:
  - "Test scaffolds use pytestmark module-level skip so unskipping is a single line removal"
  - "Each scaffold documents the behavioral contract the extracted module must fulfill"

patterns-established:
  - "pytestmark = pytest.mark.skip(reason='module not yet created — Plan 0X will unskip')"
  - "Plan that creates module must remove pytestmark as part of its verification step"

requirements-completed: [DECOMP-01, DECOMP-02, DECOMP-04]

# Metrics
duration: 1min
completed: 2026-04-11
---

# Phase 06 Plan 00: Decomposition Test Scaffolds Summary

**Behavioral test stubs for AppContext, DeviceManager, and RecordingStateMachine — all skipped via pytestmark until Plans 01-03 create their modules**

## Performance

- **Duration:** 1 min
- **Started:** 2026-04-11T05:09:44Z
- **Completed:** 2026-04-11T05:10:49Z
- **Tasks:** 1
- **Files modified:** 3

## Accomplishments
- Created test/test_app_context.py with 3 behavioral stubs for the AppContext dataclass
- Created tests/test_device_manager.py with 4 behavioral stubs for DeviceManager class
- Created tests/test_recording_state.py with 5 behavioral stubs for RecordingStateMachine
- All 12 tests run and skip cleanly (0 errors, 0 failures)

## Task Commits

Each task was committed atomically:

1. **Task 1: Create test scaffolds for AppContext, DeviceManager, RecordingStateMachine** - `7f0e964` (test)

## Files Created/Modified
- `tests/test_app_context.py` - 3 behavioral stubs: defaults, independent instances, all state fields
- `tests/test_device_manager.py` - 4 behavioral stubs: import, constructor, required methods, initial state
- `tests/test_recording_state.py` - 5 behavioral stubs: import, constructor, methods, _audio_rms, ctx usage

## Decisions Made
- Used module-level `pytestmark = pytest.mark.skip(...)` rather than per-test decorators so Plan 01/02/03 only need to remove one line to unskip all tests in the file
- Scaffold stubs reference the exact module paths (heyvox.app_context, heyvox.device_manager, heyvox.recording) that Plans 01-03 will create

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Test scaffolds are in place before any extraction work begins (Nyquist compliance)
- Plan 01 must create heyvox/app_context.py and remove pytestmark from tests/test_app_context.py
- Plan 02 must create heyvox/device_manager.py and remove pytestmark from tests/test_device_manager.py
- Plan 03 must create heyvox/recording.py and remove pytestmark from tests/test_recording_state.py

---
*Phase: 06-decomposition*
*Completed: 2026-04-11*

## Self-Check: PASSED
- tests/test_app_context.py: FOUND
- tests/test_device_manager.py: FOUND
- tests/test_recording_state.py: FOUND
- 06-00-SUMMARY.md: FOUND
- Commit 7f0e964: FOUND
