---
phase: 10-test-stability
plan: "03"
subsystem: testing
tags: [pytest, pytest-mock, pytest-subprocess, injection, markers]

# Dependency graph
requires:
  - phase: 10-test-stability/10-01
    provides: Repaired 6 stale test failures, established CI ignore flags
  - phase: 10-test-stability/10-02
    provides: Updated CI workflow with --ignore flags

provides:
  - pytest-mock and pytest-subprocess installed as dev dependencies (TEST-03)
  - requires_audio marker registered in pyproject.toml and defined in conftest.py (TEST-02)
  - Injection tests converted from call_count assertions to intent-based pbcopy/osascript filtering (TEST-01)

affects: [ci, tests]

# Tech tracking
tech-stack:
  added:
    - pytest-mock
    - pytest-subprocess
  patterns:
    - Intent-based subprocess assertion pattern (filter by command name, not count)
    - requires_audio skip marker for CI audio-hardware gating

key-files:
  created: []
  modified:
    - pyproject.toml
    - tests/conftest.py
    - tests/test_injection.py

key-decisions:
  - "Use call_args_list filtering by command name (pbcopy/osascript) rather than call_count for subprocess assertions — decoupled from internal implementation details"
  - "requires_audio marker uses pyaudio device enumeration at collection time to skip on CI"

patterns-established:
  - "Intent-based subprocess assertions: filter call_args_list by cmd[0] name, assert presence of specific commands"
  - "Audio hardware markers: _audio_device_available() at conftest load time, mark.skipif wraps the result"

requirements-completed:
  - TEST-01
  - TEST-02
  - TEST-03

# Metrics
duration: 5min
completed: 2026-04-12
---

# Phase 10 Plan 03: Gap Closure Summary

**pytest-mock + pytest-subprocess added as dev deps, requires_audio CI marker defined, injection tests rewritten with intent-based pbcopy/osascript assertions — all 3 Phase 10 verification gaps closed**

## Performance

- **Duration:** ~5 min
- **Started:** 2026-04-12T10:02:43Z
- **Completed:** 2026-04-12T10:07:30Z
- **Tasks:** 2
- **Files modified:** 3

## Accomplishments

- Added `pytest-mock` and `pytest-subprocess` to `[project.optional-dependencies].dev` and installed them
- Defined `requires_audio` skip marker backed by `_audio_device_available()` pyaudio device scan, registered in `pyproject.toml` markers
- Replaced `call_count == 2` in `test_basic_paste` and `test_no_clipboard_restore` with intent-based filtering on `pbcopy` and `osascript` command names
- `test_no_clipboard_restore` now additionally verifies no `pbcopy` call occurs after the `osascript` paste (no clipboard restore)
- Full suite: 383 passed, 2 skipped, 0 failed — no regressions

## Task Commits

1. **Task 1: Add dev dependencies and requires_audio marker** - `7af5942` (feat)
2. **Task 2: Convert injection tests to intent-based assertions** - `f58a803` (fix)

## Files Created/Modified

- `pyproject.toml` - Added pytest-mock, pytest-subprocess to dev deps; added requires_audio marker registration
- `tests/conftest.py` - Added `_audio_device_available()` helper and `requires_audio = pytest.mark.skipif(...)` after `vox_running` marker
- `tests/test_injection.py` - Replaced `call_count == 2` assertions with `pbcopy_calls`/`paste_calls` filtering in `test_basic_paste` and `test_no_clipboard_restore`

## Decisions Made

- Intent-based assertions (filter by command name) are more resilient than call_count: they remain valid if the implementation adds diagnostic subprocess calls, and they document intent clearly in the test body.
- `requires_audio` evaluates at collection time using pyaudio device enumeration — fast, no side effects, works reliably in CI environments with no audio hardware.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- All 3 Phase 10 verification gaps closed (TEST-01, TEST-02, TEST-03)
- Test suite remains at 383 passed, 2 skipped, 0 failed
- Phase 10 (test-stability) is complete — no further plans defined

---
*Phase: 10-test-stability*
*Completed: 2026-04-12*
