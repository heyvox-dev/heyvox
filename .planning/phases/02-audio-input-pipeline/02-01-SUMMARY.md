---
phase: 02-audio-input-pipeline
plan: 01
subsystem: audio
tags: [pyaudio, openwakeword, echo-suppression, health-check, headset-detection]

# Dependency graph
requires:
  - phase: 01-foundation
    provides: VoxConfig pydantic system, mic.py find_best_mic/open_mic_stream, main.py event loop

provides:
  - detect_headset() in vox/audio/mic.py with partial case-insensitive name matching (AUDIO-10)
  - TTS_PLAYING_FLAG and TTS_PLAYING_MAX_AGE_SECS constants in vox/constants.py (AUDIO-09)
  - EchoSuppressionConfig nested model in vox/config.py, integrated into VoxConfig (AUDIO-09)
  - Echo suppression check in main loop (AUDIO-09, AUDIO-10)
  - Silent-mic health check loop with 3-strike reinit in main.py (AUDIO-08)

affects: [03-hud, 04-mcp-server, main-loop-extensions]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Flag-file echo suppression: TTS writes /tmp/vox-tts-playing; main loop skips model.predict() while flag is fresh"
    - "Stale-flag guard: os.path.getmtime() age check prevents permanent mute if TTS crashes without cleanup"
    - "3-strike health check: 30s interval probes, after 3 consecutive zero readings triggers full PyAudio reinit"
    - "Headset detection: partial case-insensitive substring matching handles BT/USB device name variations"

key-files:
  created: []
  modified:
    - vox/audio/mic.py
    - vox/constants.py
    - vox/config.py
    - vox/main.py

key-decisions:
  - "Echo suppression uses a file flag (/tmp/vox-tts-playing) rather than in-process signaling — TTS runs out-of-process, flag is the IPC boundary"
  - "Stale flag guard is 60 seconds — generous enough for long TTS responses, tight enough to recover from crashes quickly"
  - "Health check interval is 30 seconds with 3 strikes required (90s minimum before reinit) to avoid false positives from brief silence"
  - "detect_headset() uses bidirectional substring matching to handle asymmetric Bluetooth/USB device name reporting on macOS"
  - "detect_headset() re-runs after health check reinit because device index may change after PyAudio restart"

patterns-established:
  - "EchoSuppressionConfig: nested pydantic model pattern established for future feature-scoped config blocks"

# Metrics
duration: 2min
completed: 2026-03-27
---

# Phase 2 Plan 1: Echo Suppression and Silent-Mic Health Check Summary

**PyAudio health check (30s/3-strike auto-reinit), TTS flag-based echo suppression (speaker mode only), and Bluetooth/USB headset detection via bidirectional name matching**

## Performance

- **Duration:** ~2 min
- **Started:** 2026-03-27T09:05:59Z
- **Completed:** 2026-03-27T09:08:05Z
- **Tasks:** 2
- **Files modified:** 4

## Accomplishments

- `detect_headset()` added to `vox/audio/mic.py` — partial case-insensitive name matching identifies paired output devices, handles BT vs USB name variations (AUDIO-10)
- Echo suppression wired into main loop — when in speaker mode and TTS_PLAYING_FLAG is fresh, `model.predict()` is skipped, preventing TTS audio from triggering false wake words (AUDIO-09)
- Silent-mic health check loop added — every 30s during idle state, checks audio level; after 3 consecutive zero readings triggers full PyAudio reinit with fresh `detect_headset()` call (AUDIO-08)
- `EchoSuppressionConfig` pydantic model added to `config.py` with `generate_default_config()` YAML section

## Task Commits

Each task was committed atomically:

1. **Task 1: Add headset detection and echo suppression infrastructure** - `b0d5f48` (feat)
2. **Task 2: Wire echo suppression and health check into main loop** - `ebbdd64` (feat)

**Plan metadata:** (docs commit — this summary)

## Files Created/Modified

- `vox/audio/mic.py` - Added `detect_headset(pa, selected_input_index) -> bool` function
- `vox/constants.py` - Added `TTS_PLAYING_FLAG = "/tmp/vox-tts-playing"` and `TTS_PLAYING_MAX_AGE_SECS = 60.0`
- `vox/config.py` - Added `EchoSuppressionConfig` model, `echo_suppression` field on `VoxConfig`, YAML template section
- `vox/main.py` - Added headset detection at startup, echo suppression check before `model.predict()`, health check loop with reinit

## Decisions Made

- Echo suppression uses file flag IPC rather than in-process signaling because TTS runs out-of-process; the flag is the natural IPC boundary
- 60-second stale flag threshold — generous for long TTS responses, fast enough to recover from crash without cleanup
- 30-second health check interval with 3 consecutive strikes required (90s minimum before reinit) avoids false positives from natural silence
- Bidirectional substring matching in `detect_headset()` handles asymmetric macOS device naming for BT headsets (input and output names can differ)
- `detect_headset()` is re-run after reinit because the device index may change on a fresh `PyAudio()` instance

## Deviations from Plan

None — plan executed exactly as written.

## Issues Encountered

None.

## User Setup Required

None — no external service configuration required.

## Next Phase Readiness

- AUDIO-08, AUDIO-09, AUDIO-10 all complete
- Audio pipeline fully hardened against real-world failure modes
- TTS process can write `/tmp/vox-tts-playing` to activate echo suppression (expected to be wired in Phase 3/4 TTS implementation)
- Ready for Phase 2 Plan 02 (push-to-talk and text injection pipeline)

---
*Phase: 02-audio-input-pipeline*
*Completed: 2026-03-27*

## Self-Check: PASSED

- vox/audio/mic.py — FOUND
- vox/constants.py — FOUND
- vox/config.py — FOUND
- vox/main.py — FOUND
- 02-01-SUMMARY.md — FOUND
- Commit b0d5f48 — FOUND
- Commit ebbdd64 — FOUND
