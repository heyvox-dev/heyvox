---
phase: 06-decomposition
plan: 02
subsystem: audio
tags: [pyaudio, microphone, hotplug, zombie-detection, health-check, device-manager]

# Dependency graph
requires:
  - phase: 06-01
    provides: AppContext dataclass with zombie_mic_reinit, last_good_audio_time, is_recording, busy fields
  - phase: 06-00
    provides: test scaffolds including tests/test_device_manager.py stubs

provides:
  - DeviceManager class in heyvox/device_manager.py with full device lifecycle management
  - main.py reduced by ~253 lines, delegating all device operations to DeviceManager
  - tests/test_device_manager.py unskipped and passing (4 behavioral tests)

affects: [06-03, main-event-loop-refactor]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Constructor injection for device state: ctx, config, log_fn, hud_send"
    - "Bridge pattern: ctx.is_recording/busy synced from module globals each loop iteration (temporary, removed in Plan 03)"
    - "Local alias refresh after DeviceManager operations: pa/stream/dev_index/dev_name/headset_mode updated from devices.*"

key-files:
  created:
    - heyvox/device_manager.py
  modified:
    - heyvox/main.py
    - tests/test_device_manager.py

key-decisions:
  - "Device-private state (pa, stream, dev_index, headset_mode, _mic_pinned, cv_history, zero_streak) lives on DeviceManager, not AppContext"
  - "Bridge pattern for recording globals: ctx.is_recording = is_recording at top of loop (temporary glue, Plan 03 removes)"
  - "hud_send passed as callable to DeviceManager constructor — avoids circular import, stable interface across Plans 02-03"
  - "health_check performs inline silent-mic recovery (not just flag-setting) to preserve the backoff and recovery logic exactly"

patterns-established:
  - "DeviceManager.scan() guards its own interval check — callers don't need to gate on timing"
  - "DeviceManager methods return bool (True=success) for callers to handle failure; no exceptions raised"

requirements-completed:
  - DECOMP-02

# Metrics
duration: 12min
completed: 2026-04-11
---

# Phase 06 Plan 02: DeviceManager Extraction Summary

**DeviceManager class extracted from main.py: ~300 lines of mic init, hotplug scan, zombie detection, and health checks now live in heyvox/device_manager.py with constructor injection**

## Performance

- **Duration:** ~12 min
- **Started:** 2026-04-11T05:25:00Z
- **Completed:** 2026-04-11T05:37:00Z
- **Tasks:** 2
- **Files modified:** 3

## Accomplishments

- Created `heyvox/device_manager.py` (648 lines) with DeviceManager class covering all device lifecycle operations
- Reduced `heyvox/main.py` from ~1843 to 1590 lines (~253 lines removed) by replacing inline device code with delegation calls
- Unskipped `tests/test_device_manager.py` — 4 behavioral tests now passing (import, constructor, methods, initial state)
- Removed now-redundant imports from main.py: `find_best_mic`, `open_mic_stream`, `detect_headset`, `get_dead_input_device_names`, `add_device_cooldown`, `is_device_cooled_down`, `device_change_cue`

## Task Commits

1. **Task 1: Create DeviceManager class** - `77cf15b` (feat)
2. **Task 2: Wire DeviceManager into main.py** - `b9071cf` (refactor)

## Files Created/Modified

- `heyvox/device_manager.py` — New: DeviceManager class with init, reinit, scan, health_check, check_dead_mic_timeout, handle_io_error, cleanup methods
- `heyvox/main.py` — Updated: imports AppContext + DeviceManager, replaces ~5 inline device management blocks with delegation calls, removes stale local variables
- `tests/test_device_manager.py` — Updated: removed pytestmark skip, 4 behavioral tests now active

## Decisions Made

- **Device-private state on DeviceManager, not AppContext**: `pa`, `stream`, `dev_index`, `headset_mode`, `_mic_pinned`, `_health_cv_history`, `_zero_streak` live on DeviceManager. AppContext only holds cross-concern state used by multiple subsystems.
- **Bridge pattern for recording globals**: `ctx.is_recording = is_recording` at top of each loop iteration. Temporary glue — Plan 03 moves recording state fully to AppContext.
- **health_check performs inline recovery**: The silent-mic recovery (close/sleep/reopen) happens inside health_check rather than just setting a flag. This preserves the exponential backoff logic and exact recovery behavior from main.py.
- **hud_send as constructor parameter**: Avoids circular import (main.py → DeviceManager → main.py). The callable interface accepting a dict is stable across Plans 02-03.

## Deviations from Plan

None — plan executed exactly as written.

## Issues Encountered

None — the extraction was straightforward. Pre-existing test failures in `test_adapters.py` and `test_injection.py` were not caused by this plan's changes (verified by checking base branch state).

## Next Phase Readiness

- Plan 03 (RecordingStateMachine) can proceed: DeviceManager is wired, AppContext exists, bridge pattern is documented
- `_ctx.is_recording/busy` bridge should be removed in Plan 03 when recording state moves to AppContext
- All device operations are now independently testable via DeviceManager constructor injection

---
*Phase: 06-decomposition*
*Completed: 2026-04-11*
