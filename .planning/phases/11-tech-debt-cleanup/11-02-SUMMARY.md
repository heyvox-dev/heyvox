---
phase: 11-tech-debt-cleanup
plan: 02
subsystem: ipc
tags: [websockets, deprecation, tts_playing, state-file, echo-suppression, herald, ipc]

# Dependency graph
requires:
  - phase: 08-ipc-consolidation
    provides: atomic state file (heyvox-state.json) and ipc.state module
  - phase: 07-herald-python-port
    provides: HeraldOrchestrator Python class managing afplay lifecycle
provides:
  - Zero DeprecationWarning from websockets (uses websockets.asyncio.server API)
  - tts_playing field written atomically to heyvox-state.json during Herald playback
  - Legacy TTS_PLAYING_FLAG file kept as parallel write for backward compatibility
  - Echo suppression in main.py reads atomic state first, then flag file fallback
affects: [12-paste-injection-reliability, 11-tech-debt-cleanup]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - dual-write: atomic state file is primary, legacy flag file is parallel write
    - state-first read: main.py reads ipc state before checking legacy flag files

key-files:
  created: []
  modified:
    - heyvox/chrome/bridge.py
    - heyvox/herald/orchestrator.py
    - heyvox/main.py

key-decisions:
  - "Use websockets.asyncio.server.serve via __aenter__() to start server — avoids switching full class to context manager pattern while eliminating DeprecationWarning"
  - "Dual-write: state file is primary, legacy flag file is parallel (not removed yet) — safe rollout without breaking old readers"
  - "main.py reads atomic state first in echo suppression — if state says tts_playing:True, no need to check flag files"

patterns-established:
  - "Dual-write migration: new atomic state is written first, then legacy flag file, for gradual cutover"
  - "State-first reads: always check ipc.state before legacy flag files to prefer atomic source of truth"

requirements-completed: [DEBT-02, DEBT-03]

# Metrics
duration: 12min
completed: 2026-04-12
---

# Phase 11 Plan 02: Websockets Deprecation and tts_playing Dual-Write Summary

**Fixed websockets 13+ DeprecationWarning in ChromeBridge by switching to the asyncio API, and added tts_playing dual-write to atomic state file in Herald orchestrator so the state file is now the primary source of truth for TTS activity**

## Performance

- **Duration:** ~12 min
- **Started:** 2026-04-12T09:56:23Z
- **Completed:** 2026-04-12T10:08:00Z
- **Tasks:** 2
- **Files modified:** 3

## Accomplishments
- ChromeBridge no longer emits `DeprecationWarning: websockets.server.serve is deprecated` — uses `websockets.asyncio.server.serve` via `__aenter__()`
- Herald orchestrator writes `tts_playing: True` to atomic state when afplay starts and `tts_playing: False` when afplay ends, alongside existing `herald_playing_pid` writes
- Legacy `TTS_PLAYING_FLAG` (`/tmp/heyvox-tts-playing`) is now written/removed as a parallel write alongside the state update — not removed yet, backward-compatible
- main.py echo suppression reads atomic state first (fast, single JSON read) then falls back to legacy flag files for processes that don't write to state file yet
- 147 tests pass with no regressions

## Task Commits

Each task was committed atomically:

1. **Task 1: Fix websockets deprecation** - `3a7b629` (fix)
2. **Task 2: Add tts_playing dual-write to atomic state** - `67ffc68` (feat)

**Plan metadata:** (this commit)

## Files Created/Modified
- `heyvox/chrome/bridge.py` - Switched import from `websockets.server` to `websockets.asyncio.server`; use `__aenter__()` to start server without restructuring to context manager
- `heyvox/herald/orchestrator.py` - Added `TTS_PLAYING_FLAG` import; write `tts_playing: True/False` to state alongside `herald_playing_pid`; touch/unlink legacy flag file as parallel write
- `heyvox/main.py` - Echo suppression now reads `ipc.state.read_state()["tts_playing"]` first; falls back to legacy flag file check

## Decisions Made
- Used `__aenter__()` directly on the `serve()` context manager object rather than refactoring the whole `start()`/`stop()` method pair into async-with — preserves the existing API shape while eliminating the deprecation
- Kept legacy flag file writes alongside the state update rather than removing them — safer rollout, avoids breaking any reader not yet updated
- Added state-file read inline in the hot loop with a bare `except Exception` guard — if the state file read fails for any reason, we fall through to the existing flag file check

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None

## Next Phase Readiness
- Phase 11 complete: all 3 requirements satisfied (DEBT-01 via plan 01, DEBT-02 + DEBT-03 via plan 02)
- Phase 12 (Paste Injection Reliability) can begin
- The dual-write pattern is established; future work can remove flag-file parallel writes once all readers are confirmed to use the state file

## Self-Check: PASSED

- FOUND: heyvox/chrome/bridge.py
- FOUND: heyvox/herald/orchestrator.py
- FOUND: heyvox/main.py
- FOUND: .planning/phases/11-tech-debt-cleanup/11-02-SUMMARY.md
- FOUND: commit 3a7b629 (fix: websockets deprecation)
- FOUND: commit 67ffc68 (feat: tts_playing dual-write)

---
*Phase: 11-tech-debt-cleanup*
*Completed: 2026-04-12*
