---
phase: 13-audio-reliability
plan: "04"
subsystem: cli
tags: [calibrate, mic, pyaudio, calibration, noise-floor, silence-threshold, MicProfileManager]

# Dependency graph
requires:
  - phase: 13-audio-reliability
    provides: MicProfileManager with run_calibration() and save_calibration() methods
provides:
  - heyvox calibrate CLI command (--device, --duration, --show)
  - _calibrate_open_pa() and _calibrate_get_cache_dir() injectable helpers for testing
affects:
  - users can now run `heyvox calibrate` to measure per-device noise floors

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Injectable helper pattern: _calibrate_open_pa() and _calibrate_get_cache_dir() are module-level functions that can be patched in tests"
    - "CLI pattern: --show flag reads cache and exits early, never opens hardware"

key-files:
  created:
    - tests/test_calibrate_cmd.py
  modified:
    - heyvox/cli.py

key-decisions:
  - "_calibrate_open_pa() and _calibrate_get_cache_dir() separated from _cmd_calibrate so tests can mock hardware and filesystem without touching real PyAudio"
  - "--device uses case-insensitive substring matching (same as mic_priority in mic.py)"
  - "Chunk count computed as duration * sample_rate // chunk_size (16000 Hz, 1280 frames) — deterministic and testable"
  - "--show reads cache JSON directly without constructing MicProfileManager (simpler display path)"

patterns-established:
  - "CLI hardware commands: separate the PyAudio instance construction into injectable helpers"

requirements-completed:
  - AUDIO-01
  - D-04

# Metrics
duration: 8min
completed: 2026-04-13
---

# Phase 13 Plan 04: CLI Calibrate Command Summary

**`heyvox calibrate` CLI command that records ambient noise via PyAudio and persists per-device noise floor and silence threshold to the MicProfileManager cache**

## Performance

- **Duration:** ~8 min
- **Started:** 2026-04-13T10:44:54Z
- **Completed:** 2026-04-13T10:52:00Z
- **Tasks:** 2 (TDD RED + GREEN)
- **Files modified:** 2

## Accomplishments
- Added `_cmd_calibrate()` to `heyvox/cli.py` — records `--duration` seconds of ambient noise via PyAudio, calls `MicProfileManager.run_calibration()` + `save_calibration()`, prints noise floor and silence threshold
- Added `--device NAME`: case-insensitive substring device filter (e.g. `--device G435` matches "G435 Wireless Gaming Headset")
- Added `--show`: reads `~/.cache/heyvox/mic-profiles.json` and displays cached calibration data without opening any hardware
- Added injectable helpers `_calibrate_open_pa()` and `_calibrate_get_cache_dir()` so all 10 unit tests run without real hardware
- Registered `calibrate` subcommand in `main()` with `--device`, `--duration`, `--show` arguments

## Task Commits

Each task was committed atomically:

1. **Task 1 RED: Failing tests** - `96eef75` (test: add failing tests for heyvox calibrate command)
2. **Task 2 GREEN: Implementation** - `315132b` (feat: add heyvox calibrate command with --device, --duration, --show)

## Files Created/Modified
- `tests/test_calibrate_cmd.py` — 10 unit tests across TestCalibrateFlow, TestCalibrateShow, TestCalibrateSubparser (all mocked, no hardware)
- `heyvox/cli.py` — Added `_calibrate_open_pa()`, `_calibrate_get_cache_dir()`, `_cmd_calibrate()`, and registered `calibrate` subparser in `main()`

## Decisions Made
- Injectable helpers pattern: `_calibrate_open_pa()` and `_calibrate_get_cache_dir()` are module-level functions patched by tests — keeps `_cmd_calibrate` testable without real PyAudio or filesystem
- `--show` reads the cache JSON directly (no MicProfileManager construction) — simpler path that doesn't interact with config_profiles
- Chunk count = `duration * 16000 // 1280` — deterministic, matches `_CALIB_SAMPLE_RATE` and `_CALIB_CHUNK_SIZE` constants declared in cli.py
- Grace-handled edge cases: no input devices exits nonzero with helpful list; mismatched `--device` prints available devices

## Deviations from Plan

None — plan executed exactly as written.

## Issues Encountered
None.

## Known Stubs
None — `heyvox calibrate` is fully functional once MicProfileManager (plan 13-01) is available.

## Self-Check: PASSED

Files exist:
- heyvox/cli.py ✓
- tests/test_calibrate_cmd.py ✓

Commits exist:
- 96eef75 ✓
- 315132b ✓

All 10 tests pass (473 total passing) ✓

Acceptance criteria met:
- `heyvox calibrate --help` works ✓
- `heyvox calibrate` appears in `heyvox --help` ✓
- `_cmd_calibrate` is callable module-level function ✓
- `--show` flag skips PyAudio ✓
- `--device` substring match selects specific device ✓

## Next Phase Readiness
- `heyvox calibrate` is ready for user use once plan 13-03 (integration) wires MicProfileManager into the main loop
- The cache format (`noise_floor`, `silence_threshold`, `calibrated_at`) is already consumed by MicProfileManager.get_profile()
- No further CLI changes required for audio reliability phase

---
*Phase: 13-audio-reliability*
*Completed: 2026-04-13*
