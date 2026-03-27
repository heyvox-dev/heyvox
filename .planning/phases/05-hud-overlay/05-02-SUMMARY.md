---
phase: 05-hud-overlay
plan: 02
subsystem: hud
tags: [hud, ipc, unix-socket, overlay, audio-level, tts-events, state-machine]

# Dependency graph
requires:
  - phase: 05-01
    provides: HUDClient/HUDServer IPC layer and overlay.py process
  - phase: 04-02
    provides: tts.py worker architecture (start_worker/shutdown/speak)

provides:
  - HUD_SOCKET_PATH constant in vox/constants.py (single source of truth)
  - HUDClient wired into main.py sending state/audio_level/transcript
  - HUDClient wired into tts.py sending tts_start/tts_end/queue_update
  - Periodic reconnect logic for HUD restart resilience
  - Full live feedback loop: every state transition visible in HUD overlay

affects: [05-hud-overlay, hud, main-loop, tts-worker]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "_hud_send() wrapper: all HUD sends go through a try/except helper, never crash callers"
    - "HUDClient lazy import in tts.py start_worker() with try/except ImportError for testability"
    - "Periodic reconnect via _hud_ensure_connected() in idle section of main loop (5s interval)"
    - "Audio level throttle: _HUD_LEVEL_INTERVAL = 0.05s (20fps) to avoid overwhelming socket"

key-files:
  created: []
  modified:
    - vox/constants.py
    - vox/main.py
    - vox/audio/tts.py

key-decisions:
  - "HUD_SOCKET_PATH defined in constants.py (not ipc.py) — ipc.py keeps DEFAULT_SOCKET_PATH as module fallback for standalone use"
  - "_hud_send() helper pattern in both main.py and tts.py — single try/except point, callers never guard"
  - "Lazy HUDClient import in tts.py start_worker() wrapped in try/except ImportError — avoids failure in test/CI environments without full package"
  - "Periodic reconnect in idle health-check section (not a separate timer) — reuses existing idle gate with 5s internal throttle"

patterns-established:
  - "Optional IPC pattern: create client → connect (silent fail) → send via _hud_send() wrapper → close in finally"

# Metrics
duration: 3min
completed: 2026-03-27
---

# Phase 5 Plan 02: HUD Integration Wiring Summary

**HUDClient wired into main.py and tts.py: real-time state/audio_level/transcript/tts_start/tts_end/queue_update messages over Unix socket, with periodic reconnect and zero-crash guarantees**

## Performance

- **Duration:** 3 min
- **Started:** 2026-03-27T10:15:26Z
- **Completed:** 2026-03-27T10:18:58Z
- **Tasks:** 2
- **Files modified:** 3

## Accomplishments

- `HUD_SOCKET_PATH` added to `constants.py` as the single canonical socket path, referenced by both main.py and tts.py
- `main.py` now sends `state=listening` on start, `state=processing` on stop, `state=idle` on completion and both cancel paths, `transcript` after STT, and `audio_level` at 20fps during recording
- `tts.py` now sends `tts_start`/`state=speaking` on dequeue, `tts_end`/`state=idle`/`queue_update` in the finally block, and `queue_update` on every `speak()` call

## Task Commits

Each task was committed atomically:

1. **Task 1: Add HUD_SOCKET_PATH constant and wire HUDClient into main.py** - `3d194a5` (feat)
2. **Task 2: Wire HUDClient into tts.py for TTS event messages** - `2a7831b` (feat)

**Plan metadata:** (docs commit)

## Files Created/Modified

- `vox/constants.py` — Added `HUD_SOCKET_PATH = "/tmp/vox-hud.sock"` with Phase 5 / HUD-08 annotation
- `vox/main.py` — Added `_hud_client`, `_hud_send()`, `_hud_ensure_connected()`, HUDClient init/close, 8 `_hud_send` call sites
- `vox/audio/tts.py` — Added `_hud_client`, `_hud_send()`, HUDClient init/close in `start_worker`/`shutdown`, 7 `_hud_send` call sites

## Decisions Made

- `HUD_SOCKET_PATH` lives in `constants.py`, not `ipc.py` — keeps `ipc.py` standalone-usable without circular import
- `_hud_send()` wrapper pattern in both files — single exception boundary, callers can call inline without try/except boilerplate
- Lazy `HUDClient` import in `tts.py` wrapped in `try/except ImportError` — allows `tts.py` to load in CI or partial installs without the HUD module
- Reconnect logic reuses the existing `not _is_rec and not _is_busy` idle gate rather than a separate timer thread — lower complexity

## Deviations from Plan

None — plan executed exactly as written.

## Issues Encountered

None.

## User Setup Required

None — no external service configuration required.

## Next Phase Readiness

- Phase 5 (HUD Overlay) is now complete: IPC layer (Plan 01) + integration wiring (Plan 02)
- The HUD overlay provides live visual feedback for all voice pipeline states
- All 5 phases complete — Vox v1 MVP is fully built

---
*Phase: 05-hud-overlay*
*Completed: 2026-03-27*
