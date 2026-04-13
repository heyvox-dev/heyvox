---
phase: quick
plan: 260413-j7m
subsystem: tests/audio
tags: [integration-tests, blackhole, audio, calibration, herald, echo-suppression]
dependency_graph:
  requires: [heyvox/audio/profile.py, heyvox/herald/cli.py, heyvox/cli.py, heyvox/constants.py]
  provides: [tests/test_integration_audio.py]
  affects: [tests/test_calibrate_cmd.py, heyvox/cli.py]
tech_stack:
  added: []
  patterns: [BlackHole loopback, PyAudio non-blocking callbacks, afplay subprocess, pytest.mark.integration]
key_files:
  created:
    - tests/test_integration_audio.py
  modified:
    - heyvox/cli.py
    - tests/test_calibrate_cmd.py
decisions:
  - Use single PyAudio instance with non-blocking callbacks for tone round-trip (avoids CoreAudio device-busy error when opening separate input+output streams)
  - Use afplay subprocess for playback in calibration tone test (avoids PortAudio conflict with pyaudio input stream)
  - Fix PyAudio Stream context manager bug in heyvox/cli.py (Rule 1 auto-fix)
metrics:
  duration: ~25 minutes
  completed: 2026-04-13
  tasks_completed: 2
  files_created: 1
  files_modified: 2
---

# Phase quick Plan 260413-j7m: BlackHole Integration Tests Summary

**One-liner:** 11 BlackHole-based component integration tests for mic calibration, herald stop/interrupt, calibrate CLI, and echo suppression flag coordination — plus auto-fix of PyAudio context manager bug in heyvox/cli.py.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Create BlackHole component integration test file | 791ff19 | tests/test_integration_audio.py (created), heyvox/cli.py, tests/test_calibrate_cmd.py |
| 2 | Ensure integration marker covers new test file | — | No changes needed (blackhole_installed marker sufficient) |

## What Was Built

`tests/test_integration_audio.py` — 11 integration tests across 5 classes:

1. **TestBlackHoleLoopback** (2 tests): Loopback sanity — tone round-trip using PyAudio non-blocking callbacks (single instance, input+output streams), silence verification.

2. **TestMicProfileCalibrationIntegration** (3 tests): `MicProfileManager.run_calibration()` with real BlackHole audio — silence chunks produce low noise_floor, tone produces non-zero noise_floor with correct threshold formula, save/reload round-trip.

3. **TestHeraldStopInterruptIntegration** (2 tests): Real `afplay` processes killed by `_cmd_stop()` and `_cmd_interrupt()` — stop clears queue, interrupt preserves queue files.

4. **TestCalibrateCommandIntegration** (1 test): `_cmd_calibrate` with real PyAudio on BlackHole device — writes cache with blackhole entry containing noise_floor and silence_threshold.

5. **TestEchoSuppressionGatingIntegration** (3 tests): Flag coordination — TTS_PLAYING_FLAG cleared by stop and interrupt, RECORDING_FLAG survives interrupt.

All tests marked `pytestmark = pytest.mark.integration` and `@blackhole_installed` — excluded from default `pytest` run, skip gracefully without BlackHole.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed PyAudio Stream context manager TypeError in heyvox/cli.py**
- **Found during:** Task 1 (running test_calibrate_with_blackhole_device against real hardware)
- **Issue:** `_cmd_calibrate` used `with pa.open(...) as stream:` but `pyaudio.Stream` does not implement the context manager protocol (`__enter__`/`__exit__`). Unit tests masked this with MagicMock context manager setup — real PyAudio raised `TypeError: 'Stream' object does not support the context manager protocol`.
- **Fix:** Changed to `stream = pa.open(...)` with explicit `try/finally: stream.stop_stream(); stream.close()`.
- **Files modified:** `heyvox/cli.py` (lines 624-636), `tests/test_calibrate_cmd.py` (3 test mock setups updated to `mock_pa.open.return_value = mock_stream` instead of `__enter__` pattern)
- **Commit:** 791ff19

**2. [Rule 1 - Bug] Replaced sounddevice+pyaudio concurrent stream approach in loopback test**
- **Found during:** Task 1 test run (full suite)
- **Issue:** Opening sounddevice (output) and pyaudio (input) simultaneously on BlackHole caused CoreAudio error `-10863` ("cannot do in current context") — CoreAudio virtual devices reject concurrent PortAudio contexts.
- **Fix (tone_round_trip):** Used single `pyaudio.PyAudio()` instance with two non-blocking callback streams (output + input) — same PortAudio context handles both sides without conflict.
- **Fix (calibration_with_blackhole_tone):** Used `afplay` subprocess for playback (CoreAudio AUHAL, separate from PortAudio) while pyaudio records from BlackHole input.

## Verification Results

```
pytest tests/test_integration_audio.py -v -m integration
→ 11 passed in 11.71s

pytest tests/ --co -q | grep integration_audio
→ 0 (excluded from default run)

pytest tests/test_mic_profile.py tests/test_herald_cli.py tests/test_calibrate_cmd.py tests/test_echo_suppression.py
→ 77 passed, 2 skipped
```

## Known Stubs

None.

## Self-Check

### Files created/modified

- [x] `tests/test_integration_audio.py` — exists, 320+ lines, 11 tests
- [x] `heyvox/cli.py` — PyAudio context manager bug fixed
- [x] `tests/test_calibrate_cmd.py` — mock pattern updated

### Commits

- [x] 791ff19 — feat(quick-260413-j7m): add BlackHole integration tests

## Self-Check: PASSED
