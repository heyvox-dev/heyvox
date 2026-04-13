---
phase: 13-audio-reliability
plan: 01
subsystem: audio
tags: [mic, calibration, device-profiles, silence-detection, bluetooth, pydantic]

# Dependency graph
requires:
  - phase: 12-paste-injection-reliability
    provides: injection config patterns and InjectionConfig model
provides:
  - MicProfileEntryConfig Pydantic model with 9 optional per-device fields
  - MicProfileManager class with get_profile, save_calibration, run_calibration
  - HeyvoxConfig.mic_profiles dict field for per-device config overrides
  - ~/.cache/heyvox/mic-profiles.json calibration cache with 30-day expiry
affects:
  - 13-audio-reliability (plans 03-04 will integrate MicProfileManager into main loop and DeviceManager)

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Config override wins: MicProfileManager merges cache then applies config overrides (config always wins)"
    - "Partial case-insensitive name matching: 'G435' matches 'G435 Wireless Gaming Headset'"
    - "Atomic cache write: tempfile.mkstemp + os.replace prevents partial writes"
    - "All-None dataclass: MicProfileEntry fields default None so callers can distinguish 'set' from 'missing'"

key-files:
  created:
    - heyvox/audio/profile.py
    - tests/test_mic_profile.py
  modified:
    - heyvox/config.py

key-decisions:
  - "MicProfileEntryConfig has all 9 fields optional (None) so callers always fall back to global defaults"
  - "Config overrides always win over calibration cache (D-03 from CONTEXT.md)"
  - "Calibration algorithm: median of per-chunk peaks * 3.5, capped at 500 (D-04, D-12)"
  - "Cache key is lowercased full device name; config lookup key is partial substring"
  - "Cache expiry 30 days: old calibration data silently ignored, falls back to global defaults"

patterns-established:
  - "MicProfileManager is constructed with config_profiles dict and cache_dir Path — injectable, testable"
  - "Calibration triggers save_calibration() externally (caller decides when to calibrate)"

requirements-completed:
  - D-01
  - D-02
  - D-03
  - D-04
  - D-12
  - AUDIO-01

# Metrics
duration: 4min
completed: 2026-04-13
---

# Phase 13 Plan 01: Silence Detection and Echo Suppression Summary

**MicProfileManager with per-device audio profiles, partial name matching, 30-day calibration cache, and median-based silence threshold algorithm**

## Performance

- **Duration:** ~4 min
- **Started:** 2026-04-13T10:34:33Z
- **Completed:** 2026-04-13T10:38:45Z
- **Tasks:** 2 (TDD: RED + GREEN for each)
- **Files modified:** 3

## Accomplishments
- Added `MicProfileEntryConfig` Pydantic model with 9 optional fields to `config.py`, enabling YAML-based per-device profile overrides
- Added `HeyvoxConfig.mic_profiles: dict[str, MicProfileEntryConfig]` field — zero config required, all devices fall back to global defaults
- Created `MicProfileManager` in `heyvox/audio/profile.py` with partial case-insensitive name matching, config-wins-over-cache merge logic, 30-day cache expiry, and atomic JSON writes
- 16 unit tests covering all key behaviors (name matching, config priority, cache expiry, calibration algorithm, atomic writes)

## Task Commits

Each task was committed atomically:

1. **Task 1 RED: Failing tests** - `1d084d2` (test: add failing tests for MicProfileEntryConfig and MicProfileManager)
2. **Task 1 GREEN: MicProfileEntryConfig model** - `6130293` (feat: add MicProfileEntryConfig model and mic_profiles to HeyvoxConfig)
3. **Task 2 GREEN: MicProfileManager implementation** - `2724da9` (feat: implement MicProfileManager with cache, lookup, and calibration)

_Note: RED tests for Task 2 were included in the initial RED commit. Task 2 had no separate test commit._

## Files Created/Modified
- `heyvox/audio/profile.py` — MicProfileEntry dataclass, MicProfileManager class (get_profile, run_calibration, save_calibration)
- `heyvox/config.py` — Added MicProfileEntryConfig model (9 fields), mic_profiles field to HeyvoxConfig
- `tests/test_mic_profile.py` — 16 unit tests for config model and MicProfileManager

## Decisions Made
- Config overrides always win over calibration cache (follows D-03)
- Calibration uses median of per-chunk peaks (not mean) — resistant to spike outliers like Bluetooth G435 bursts
- Cache key = lowercased full device name; config lookup = partial substring match (consistent with mic_priority matching in mic.py)
- MicProfileManager is not integrated into the main loop or DeviceManager in this plan — integration is plan 03's responsibility

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
- Pre-existing test failure in `tests/test_herald_orchestrator.py::TestOrchestratorConfig::test_default_poll_interval` (expects 0.3, actual 0.1). Pre-dates this plan (from orchestrator changes in a prior session). Logged to deferred items, not fixed here.

## Known Stubs

None — MicProfileManager is fully functional but not yet wired into the main loop (planned for phase 13-03).

## Next Phase Readiness
- `MicProfileManager` and `MicProfileEntryConfig` are ready for integration into `DeviceManager` (plan 13-03)
- Integration point: `DeviceManager.init()` should construct `MicProfileManager` and pass active device name to `get_profile()` to retrieve per-device silence threshold and echo_safe flag
- Plan 13-04 can wire calibration triggers into the health check and `heyvox calibrate` CLI command

---
*Phase: 13-audio-reliability*
*Completed: 2026-04-13*
