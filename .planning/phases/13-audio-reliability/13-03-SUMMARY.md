---
phase: 13-audio-reliability
plan: "03"
subsystem: audio
tags: [echo-suppression, headset, wake-word, tts-interrupt, calibration, device-profiles, silence-threshold]

# Dependency graph
requires:
  - phase: 13-audio-reliability
    plan: "01"
    provides: MicProfileManager with per-device profiles, silence thresholds, calibration cache
  - phase: 13-audio-reliability
    plan: "02"
    provides: tts.interrupt() for selective TTS kill, herald stop/interrupt commands
provides:
  - Echo suppression gated on headset_mode (D-08/D-09): headset users get wake word during TTS
  - Per-device grace period: 0.5s headset, 2.0s speaker (D-10)
  - force_disabled config flag bypasses echo suppression (D-11)
  - Wake word during TTS interrupts playback and starts recording (D-05)
  - Auto-calibration of silence threshold from first 50 audio chunks per new device (D-04)
  - Profile-aware silence threshold updates on every device switch (D-13)
  - DeviceManager.active_profile updated on every detect_headset call
affects:
  - 13-audio-reliability (plan 04 can use active_profile and calibration for CLI calibrate command)

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "echo_safe = headset_mode OR profile.echo_safe OR force_disabled (D-08/D-09/D-11)"
    - "Grace period is device-aware: 0.5s headset, 2.0s speaker (D-10)"
    - "Calibration runs in parallel with normal loop — do NOT gate wake word (Pitfall 4)"
    - "RECORDING_FLAG written BEFORE tts.interrupt() so orchestrator sees is_paused (Pitfall 3)"

key-files:
  created:
    - tests/test_echo_suppression.py (new test classes TestEchoSafeGating, TestEchoSuppressionConfig)
  modified:
    - heyvox/device_manager.py
    - heyvox/main.py
    - heyvox/config.py

key-decisions:
  - "echo_safe logic: headset_mode > profile.echo_safe > force_disabled (last wins, all can override)"
  - "Grace period constants removed from config: 0.5s headset / 2.0s speaker are fixed per D-10"
  - "TTS interrupt: RECORDING_FLAG written first (Pitfall 3: orchestrator checks _is_paused before purging)"
  - "Auto-calibration does NOT block wake word: runs in parallel collecting 50 chunks then computes"
  - "profile_manager passed through _run_loop signature (not module-level global) for testability"

patterns-established:
  - "DeviceManager.active_profile always reflects current device — callers read it directly"
  - "Silence threshold re-read after every devices.scan() and zombie reinit"

requirements-completed:
  - D-08
  - D-09
  - D-10
  - D-11
  - AUDIO-02
  - AUDIO-04

# Metrics
duration: 4min
completed: 2026-04-13
---

# Phase 13 Plan 03: Audio Loop Integration Summary

**Echo suppression gated on headset detection (D-08/D-09), 0.5s/2.0s grace periods (D-10), wake-word-interrupts-TTS (D-05), and per-device silence threshold auto-calibration wired into main event loop**

## Performance

- **Duration:** ~4 min
- **Started:** 2026-04-13T10:42:30Z
- **Completed:** 2026-04-13T10:46:34Z
- **Tasks:** 2 (Task 2 was TDD: RED then GREEN)
- **Files modified:** 4

## Accomplishments
- DeviceManager now holds `profile_manager` and `active_profile`; active_profile updated on every `detect_headset()` call across all code paths (init, reinit, recover_silent_mic, scan hotplug, scan manual)
- Echo suppression block rewired: headset mode = echo_safe → wake word stays active during TTS; profile echo_safe override and force_disabled config flag supported (D-08/D-09/D-11)
- Grace period changed from config-static 0.6s to device-aware 0.5s headset / 2.0s speaker (D-10)
- Wake word during TTS with headset: writes RECORDING_FLAG then calls tts.interrupt() — orchestrator sees recording before purging (D-05, Pitfall 3)
- Auto-calibration collects 50 chunks from first new device without blocking wake word, then calls run_calibration + save_calibration (D-04)
- silence_threshold re-read from active_profile after every device switch (scan + zombie reinit)
- 10 new unit tests covering all echo_safe gating scenarios and config field

## Task Commits

Each task was committed atomically:

1. **Task 1: Wire MicProfileManager into DeviceManager and main loop** - `18e41e8` (feat)
2. **Task 2 RED: Failing echo suppression gate tests** - `69b00e4` (test)
3. **Task 2 GREEN: Gate echo suppression + interrupt TTS + fix grace periods** - `a97460a` (feat)

## Files Created/Modified
- `heyvox/device_manager.py` — Added profile_manager param, active_profile attr, profile update after every detect_headset call
- `heyvox/main.py` — MicProfileManager in _setup, profile-aware silence_threshold, auto-calibration, echo_safe gate, 0.5/2.0s grace, TTS interrupt on wake word
- `heyvox/config.py` — Added force_disabled: bool = False to EchoSuppressionConfig
- `tests/test_echo_suppression.py` — Added TestEchoSafeGating (8 tests) and TestEchoSuppressionConfig (2 tests)

## Decisions Made
- Grace period made constant (0.5s/2.0s) rather than configurable — D-10 specifies exact values, no user config needed
- Profile override wins over headset detection, force_disabled wins over both — allows emergency bypass without hardware change
- TTS interrupt path only runs when echo_safe=True (headset mode) and TTS is active and not recording — prevents double-interrupt
- Auto-calibration does NOT suppress wake word during collection (Pitfall 4 from CONTEXT.md)

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

- 5 pre-existing test failures in unrelated files (test_herald_orchestrator, test_herald_worker, test_media) — these predate plan 13-03 and are caused by other agents' concurrent modifications. Logged to deferred items, not fixed here.

## Known Stubs

None — all features fully implemented and wired.

## Self-Check: PASSED

Files exist:
- heyvox/device_manager.py (contains `self.profile_manager`, `self.active_profile`) ✓
- heyvox/main.py (contains `MicProfileManager`, `_calibrating`, `not _echo_safe`, `0.5 if _echo_safe`) ✓
- heyvox/config.py (contains `force_disabled: bool = False`) ✓
- tests/test_echo_suppression.py (contains `test_wake_word_active_during_tts_with_headset`) ✓

Commits:
- 18e41e8 (feat: wire MicProfileManager) ✓
- 69b00e4 (test: failing echo suppression tests) ✓
- a97460a (feat: gate echo suppression) ✓

All 36 echo suppression tests pass ✓

## Next Phase Readiness
- Plan 13-04 can wire `heyvox calibrate` CLI command using `DeviceManager.active_profile` and `profile_manager.run_calibration`
- `devices.active_profile` is always available for any subsystem that needs per-device settings

---
*Phase: 13-audio-reliability*
*Completed: 2026-04-13*
