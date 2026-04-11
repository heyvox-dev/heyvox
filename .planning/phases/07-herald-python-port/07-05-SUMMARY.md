---
phase: 07-herald-python-port
plan: 05
subsystem: audio
tags: [kokoro, tts, normalization, media-control, herald]

requires:
  - phase: 07-herald-python-port/03
    provides: "orchestrator.py with normalize_wav and media functions"
  - phase: 07-herald-python-port/04
    provides: "Python wiring of Herald entry points, bash script deletion"
provides:
  - "normalize_samples() in kokoro-daemon.py for generation-time WAV normalization"
  - "Python media pause/resume via heyvox.audio.media in orchestrator"
affects: [herald, tts, media-control]

tech-stack:
  added: []
  patterns: ["float32 normalization before int16 conversion", "lazy import for media modules"]

key-files:
  created: []
  modified:
    - heyvox/herald/daemon/kokoro-daemon.py
    - heyvox/herald/orchestrator.py
    - tests/test_herald_orchestrator.py

key-decisions:
  - "normalize_samples operates in float32 space (pre-int16) matching orchestrator int16-scale constants"
  - "Inline import of pause_media/resume_media in _media_pause/_media_resume to avoid circular imports"

patterns-established:
  - "Generation-time normalization: normalize audio before write, not at playback"

requirements-completed: [HERALD-01, HERALD-02]

duration: 5min
completed: 2026-04-11
---

# Plan 07-05: Gap Closure Summary

**WAV normalization moved to Kokoro daemon generation-time, media pause/resume wired to Python API replacing deleted bash scripts**

## Performance

- **Duration:** ~5 min
- **Started:** 2026-04-11T16:22:00Z
- **Completed:** 2026-04-11T16:27:00Z
- **Tasks:** 2
- **Files modified:** 3

## Accomplishments
- Added `normalize_samples()` to kokoro-daemon.py, called before every `write_wav()` in both `generate_mlx()` (3 sites) and `generate_onnx()` (3 sites)
- Removed playback-time `normalize_wav()` call from orchestrator `_play_wav()`, kept function as legacy fallback
- Replaced bash subprocess calls to deleted `media.sh` with Python `pause_media`/`resume_media` from `heyvox.audio.media`
- Added 4 unit tests for media pause/resume wiring; all 69 orchestrator tests pass

## Task Commits

1. **Task 1: Move WAV normalization into Kokoro daemon** - `9a4b933` (feat)
2. **Task 2: Fix media pause/resume to use Python API** - `02997e0` (fix)

**Plan metadata:** `e4bceaa` (docs: complete plan)

## Files Created/Modified
- `heyvox/herald/daemon/kokoro-daemon.py` - Added normalize_samples(), called before all write_wav() sites
- `heyvox/herald/orchestrator.py` - Removed playback normalization, replaced bash media calls with Python API
- `tests/test_herald_orchestrator.py` - 4 new tests for media pause/resume wiring

## Decisions Made
- normalize_samples works in float32 space, converting to int16 scale only for RMS calculation (consistent with existing orchestrator constants)
- Used inline imports for media modules to avoid circular import issues
- Kept normalize_wav() function as legacy fallback for externally-generated WAVs

## Deviations from Plan
None - plan executed as specified.

## Issues Encountered
None.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- All Herald Python port verification gaps closed
- Phase 07 ready for final verification

---
*Phase: 07-herald-python-port*
*Completed: 2026-04-11*
