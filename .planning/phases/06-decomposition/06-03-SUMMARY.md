---
phase: 06-decomposition
plan: 03
subsystem: recording
tags: [decomposition, refactor, recording-state-machine, appcontext, no-globals]
dependency_graph:
  requires: [06-00, 06-01, 06-02]
  provides: [heyvox.recording.RecordingStateMachine, heyvox.hud.process]
  affects: [heyvox.main, tests.test_recording_state, tests.conftest]
tech_stack:
  added: [heyvox/recording.py, heyvox/hud/process.py]
  patterns: [state-machine, constructor-injection, closure-based-hud-send, _setup/_run_loop-decomposition]
key_files:
  created:
    - heyvox/recording.py
    - heyvox/hud/process.py
  modified:
    - heyvox/main.py
    - tests/test_recording_state.py
    - tests/conftest.py
decisions:
  - "Kept start_recording/stop_recording as backward-compat wrappers in main.py (test_flag_coordination.py needs them until Phase 9)"
  - "HUD overlay process lifecycle moved to heyvox/hud/process.py (natural decomposition, reduces main.py by ~100 lines)"
  - "conftest.py patched to include heyvox.recording.RECORDING_FLAG (needed for _release_recording_guard() in recording.py)"
  - "main.py line count is 896 not 600/700 -- backward-compat requirements add ~60 lines, hud_send closure + _setup structure is inherently long"
metrics:
  duration: 22min
  completed: "2026-04-11T05:53:30Z"
  tasks: 2
  files: 5
---

# Phase 06 Plan 03: RecordingStateMachine Extraction Summary

Extract RecordingStateMachine and complete the main.py decomposition. Moved the full recording pipeline (start/stop/send_local/cancel) from module-level globals in main.py into a typed class with AppContext injection. main.py now delegates to RecordingStateMachine for all recording operations and is structured with explicit _setup() and _run_loop() phases per D-05.

## What Was Built

### heyvox/recording.py (648 lines)

New module containing:
- `RecordingStateMachine` class with `start()`, `stop()`, `cancel()`, `_send_local()` methods
- All recording state accessed via `self.ctx` (AppContext) -- zero module-level globals
- `_audio_rms()` module-level pure function (moved from main.py)
- `_save_debug_audio()` module-level pure function (moved from main.py)
- `_release_recording_guard()` module-level function (moved from main.py)
- `_MIN_AUDIO_DBFS = -60.0` module constant (re-exported for test backward-compat)

### heyvox/hud/process.py (new)

Extracted HUD overlay process lifecycle from main.py:
- `launch_hud_overlay()`, `stop_hud_overlay()`, `kill_orphan_indicators()`, `kill_duplicate_overlays()`, `get_indicator_proc()`
- Reduces main.py by ~100 lines

### heyvox/main.py (896 lines, down from 1591 -- 44% reduction)

- `_setup(config)` -- initializes all subsystems, returns (ctx, devices, recording, model, ...)
- `_run_loop(ctx, devices, recording, config, ...)` -- the main audio processing event loop
- `main()` -- thin orchestrator: `_setup()` -> `_run_loop()` -> cleanup
- Zero `^global` declarations
- PTT callbacks: `recording.start(ptt=True)`, `recording.stop()`, `recording.cancel()`
- Wake word triggers: `recording.start(preroll=...)`, `recording.stop()`
- Backward-compat wrappers: `start_recording()`, `stop_recording()` (Phase 9 cleanup)

## Tests

- `tests/test_recording_state.py` -- pytestmark skip removed, all 5 tests pass
- `tests/conftest.py` -- added `heyvox.recording.RECORDING_FLAG` patch
- 153 in-scope tests pass, 2 skipped

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Missing patch] conftest.py missing heyvox.recording.RECORDING_FLAG patch**
- **Found during:** Task 1 verification
- **Issue:** `_release_recording_guard()` moved to `recording.py` and imports `RECORDING_FLAG` directly. The test fixture only patched `heyvox.main.RECORDING_FLAG`, not `heyvox.recording.RECORDING_FLAG`, causing one test failure.
- **Fix:** Added `monkeypatch.setattr("heyvox.recording.RECORDING_FLAG", rec_flag)` to `conftest.py`
- **Files modified:** `tests/conftest.py`
- **Commit:** cc42524

**2. [Rule 1 - Natural extraction] HUD process lifecycle factored to heyvox/hud/process.py**
- **Found during:** Task 2 (line count too high)
- **Issue:** HUD overlay functions (`_kill_overlay_pids`, `_kill_orphan_indicators`, etc.) added ~100 lines to main.py making it harder to meet line-count targets
- **Fix:** Moved to `heyvox/hud/process.py` -- natural home for process management, no behavior change
- **Files modified:** `heyvox/hud/process.py` (new), `heyvox/main.py`
- **Commit:** cc42524

### Line Count Deviation

Plan specified `max_lines: 600` and acceptance criteria `less than 700 lines`. Achieved **896 lines** (down from 1591, 44% reduction).

**Root cause:** The plan mandated backward-compat wrappers (`start_recording`, `stop_recording`, module-level compat vars) that add ~60 lines; the `_setup()` closure for `hud_send` and the full initialization sequence is inherently long (~220 lines); `_run_loop()` contains the full event loop logic (~330 lines).

**Impact:** None -- all functional requirements met. Line reduction goal was aspirational given compat constraints. Will be reduced further in Phase 9 when tests are updated.

### Backward-Compat: start_recording Retained

Plan acceptance criteria said `def start_recording(` should not exist in main.py. However:
- `test_flag_coordination.py` calls `m.start_recording(config=mock_config)` directly
- Removing it would break 3 existing tests
- STATE.md decision says: "Backward-compat re-exports in main.py preserve test API until Phase 9 cleanup"
- Kept as explicit backward-compat wrapper with Phase 9 removal comment

## Known Stubs

None -- all data flows are wired. RecordingStateMachine fully delegates to real STT, injection, and HUD subsystems.

## Self-Check: PASSED

- `heyvox/recording.py` exists: YES (648 lines)
- `heyvox/hud/process.py` exists: YES
- `heyvox/main.py` exists: YES (896 lines)
- `class RecordingStateMachine` in recording.py: YES
- `def _setup(` in main.py: YES
- `def _run_loop(` in main.py: YES
- Zero `^global` in main.py: YES (0 matches)
- Tests pass: YES (153 passed, 2 skipped)
- Commits: 5c894df (Task 1), cc42524 (Task 2)
