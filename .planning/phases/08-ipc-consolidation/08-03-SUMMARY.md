---
phase: 08-ipc-consolidation
plan: "03"
subsystem: infra
tags: [herald, gc, queue, orchestrator, tts, ipc]

requires:
  - phase: 08-01
    provides: HERALD_QUEUE_DIR, HERALD_HOLD_DIR, HERALD_HISTORY_DIR, HERALD_CLAIM_DIR, HERALD_WATCHER_HANDLED_DIR constants in heyvox.constants

provides:
  - _gc_queue_dirs() module-level function in heyvox/herald/orchestrator.py with frequency gate
  - Automatic periodic cleanup of orphaned WAV/txt/workspace files in queue/hold/history/claim/watcher-handled dirs
  - Per-directory age thresholds: queue 1h, hold 4h, history 24h, claim 1h, watcher-handled 1h
  - 5 unit tests covering all GC behaviors

affects: [09-tests, herald-orchestrator, ipc-consolidation]

tech-stack:
  added: []
  patterns:
    - "Module-level _last_gc float + _GC_INTERVAL guard for rate-limited side-effect functions"
    - "Single _gc_queue_dirs function covers all queue dirs via dir_thresholds list — easy to extend"

key-files:
  created:
    - tests/test_queue_gc.py
  modified:
    - heyvox/herald/orchestrator.py

key-decisions:
  - "claim_dir added to dir_thresholds (same 1h threshold) so old inline claim GC block is eliminated — one place for all dir cleanup"
  - "HERALD_WATCHER_HANDLED_DIR cleaned via separate loop (it lives in /tmp, not in OrchestratorConfig) — avoids adding it to the config dataclass"

patterns-established:
  - "TDD: failing import error as RED (function not yet defined), then implement to GREEN"

requirements-completed:
  - IPC-03
  
duration: 7min
completed: 2026-04-11
---

# Phase 8 Plan 03: Queue GC Summary

**Periodic garbage collection for Herald queue dirs — _gc_queue_dirs() removes orphaned WAV/sidecar files with per-directory age thresholds (1h queue, 4h hold, 24h history) wired into the orchestrator idle loop.**

## Performance

- **Duration:** 7 min
- **Started:** 2026-04-11T17:24:31Z
- **Completed:** 2026-04-11T17:31:10Z
- **Tasks:** 1 (TDD: RED commit + GREEN commit)
- **Files modified:** 2

## Accomplishments
- Implemented `_gc_queue_dirs(cfg, debug_log)` as a module-level function — keeps it testable outside `HeraldOrchestrator`
- Added `_GC_INTERVAL = 60` and `_last_gc = 0.0` module-level state for frequency gating
- Per-directory age thresholds: queue_dir 1h, hold_dir 4h, history_dir 24h, claim_dir 1h, watcher-handled 1h
- Replaced the inline claim-file GC block (lines 738-745) with a single `_gc_queue_dirs(cfg, cfg.debug_log)` call
- Imported `HERALD_WATCHER_HANDLED_DIR` from `heyvox.constants` (added in 08-01)
- 5 unit tests covering old-file removal, recent-file skip, orphaned sidecar, hold-dir threshold, and frequency gate — all passing

## Task Commits

1. **Task 1 (RED): Queue GC tests** - `1d7a4e4` (test)
2. **Task 1 (GREEN): Implement _gc_queue_dirs** - `0da808e` (feat)

## Files Created/Modified
- `tests/test_queue_gc.py` - 5 unit tests for queue GC behavior (uses tmp_path, os.utime for mtime control)
- `heyvox/herald/orchestrator.py` - Added _GC_INTERVAL, _last_gc, _gc_queue_dirs(); updated import; replaced inline claim GC

## Decisions Made
- claim_dir added to dir_thresholds at 1h threshold, eliminating the old inline GC block (one place for all cleanup)
- HERALD_WATCHER_HANDLED_DIR handled separately (it's a raw path string, not a Path in OrchestratorConfig) — avoids dataclass change
- Function is module-level (not a method) to keep it importable and testable without instantiating HeraldOrchestrator

## Deviations from Plan

None - plan executed exactly as written. The implementation matches the specification in the plan's `<action>` block.

## Issues Encountered
- Pre-existing `test_adapters.py` failure (AttributeError: `heyvox.adapters.generic` has no `type_text`) was present before this plan's changes and is out of scope. All 163 tests in relevant modules pass.

## Next Phase Readiness
- IPC-03 complete: /tmp/herald-queue/ and related directories will not grow unbounded
- Ready for 08-04 or the verification/integration phase

---
*Phase: 08-ipc-consolidation*
*Completed: 2026-04-11*
