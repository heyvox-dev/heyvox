---
phase: 08-ipc-consolidation
plan: 02
subsystem: ipc
tags: [ipc, state-file, atomic-write, cross-process, threading]

# Dependency graph
requires:
  - phase: 08-01
    provides: IPC path constants in heyvox.constants (HEYVOX_STATE_FILE and 24 others)
provides:
  - heyvox/ipc/__init__.py — public API (read_state, write_state, update_state, reset_transient_state)
  - heyvox/ipc/state.py — atomic state file implementation using tmp+os.rename, thread-safe lock
  - Dual-write wiring in main.py, recording.py, herald/orchestrator.py, herald/cli.py, mcp/server.py
affects:
  - 08-03 (will migrate readers from flag files to state file as sole source of truth)

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Atomic write: write to .tmp sibling, then os.rename (POSIX-atomic, no partial reads)"
    - "Thread-safe state: threading.Lock within process, os.rename across processes"
    - "Dual-write migration: write both old flag files AND state file during transition"
    - "Best-effort reads: return {} on missing/corrupt state file, never crash"
    - "Transient fields: recording/tts_playing/herald_playing_pid/paused reset on startup"

key-files:
  created:
    - heyvox/ipc/__init__.py
    - heyvox/ipc/state.py
    - tests/test_ipc_state.py (unskipped and implemented)
  modified:
    - heyvox/main.py
    - heyvox/recording.py
    - heyvox/herald/orchestrator.py
    - heyvox/herald/cli.py
    - heyvox/mcp/server.py

key-decisions:
  - "Dual-write strategy: old flag files kept intact; state file populated alongside for safe migration"
  - "Module-level _state_path/_tmp_path enables monkeypatching in tests without import side-effects"
  - "TRANSIENT_FIELDS as a set constant makes reset_transient_state() self-documenting and extensible"
  - "Inline try/except around all update_state() calls prevents any IPC failure from crashing callers"
  - "recording.py is the canonical write point for recording state (not main.py compat shims)"

patterns-established:
  - "IPC dual-write: always wrap update_state() in try/except; dual-write is additive, never blocking"
  - "Atomic state: write DEFAULTS-keyed transient resets via update_state(), preserving persistent fields"

requirements-completed:
  - IPC-02

# Metrics
duration: 12min
completed: 2026-04-11
---

# Phase 08 Plan 02: IPC State File Summary

**Atomic /tmp/heyvox-state.json replaces 8 flag files for cross-process state coordination, using tmp+os.rename writes, thread-safe locking, and dual-write migration across 5 callers**

## Performance

- **Duration:** ~12 min
- **Started:** 2026-04-11T17:25:00Z
- **Completed:** 2026-04-11T17:37:00Z
- **Tasks:** 2
- **Files modified:** 7 (2 created, 5 modified + 1 test unskipped)

## Accomplishments

- Created heyvox/ipc/ module with atomic read/write/update/reset_transient_state API
- Wrote temp+os.rename atomic write pattern, thread-safe with threading.Lock
- Implemented and unskipped all 6 unit tests (write, read missing, atomic rename, corrupt JSON, merge, startup reset)
- Added dual-write calls to recording.py (canonical write point), main.py (standalone fallback), herald/orchestrator.py (herald_playing_pid + last_play_ts), herald/cli.py (paused + muted), mcp/server.py (supplementary read in voice_status)
- reset_transient_state() called in _acquire_singleton() to clear stale state after crash recovery

## Task Commits

1. **Task 1: Create heyvox/ipc/ module with atomic state file** - `f4a4a10` (feat + test TDD)
2. **Task 2: Wire state file into callers (dual-write migration)** - `5063785` (feat)

## Files Created/Modified

- `heyvox/ipc/__init__.py` - Public API: read_state, write_state, update_state, reset_transient_state
- `heyvox/ipc/state.py` - Atomic state file with DEFAULTS, TRANSIENT_FIELDS, thread lock
- `tests/test_ipc_state.py` - 6 unit tests, all passing
- `heyvox/main.py` - reset_transient_state() in _acquire_singleton(); dual-write in standalone start_recording
- `heyvox/recording.py` - dual-write recording=True on start, recording=False on _release_recording_guard
- `heyvox/herald/orchestrator.py` - dual-write herald_playing_pid on play start/stop/cleanup; last_play_ts on play end
- `heyvox/herald/cli.py` - dual-write paused on pause/resume; muted on mute-toggle
- `heyvox/mcp/server.py` - read_state() supplementary check in voice_status()

## Decisions Made

- Dual-write strategy chosen over immediate cutover: old flag files continue working; state file populated in parallel. This allows 08-03 to migrate readers safely with zero risk of broken state.
- recording.py identified as canonical write point for recording flag (RecordingStateMachine.start()); main.py standalone fallback also gets dual-write for test compatibility.
- inline try/except around all update_state() calls: IPC failures must never crash callers (TTS, recording, CLI).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Missing Critical] Added dual-write to recording.py (not just main.py)**
- **Found during:** Task 2 (Wire callers)
- **Issue:** Plan said to add dual-writes to main.py RecordingStateMachine.start()/stop(), but the actual RECORDING_FLAG write is in recording.py:RecordingStateMachine.start() and _release_recording_guard(). main.py only has a compat shim that rarely runs.
- **Fix:** Added dual-write to both recording.py (canonical) and main.py standalone fallback
- **Files modified:** heyvox/recording.py
- **Verification:** grep confirms update_state in recording.py; tests pass
- **Committed in:** 5063785

**2. [Rule 2 - Missing Critical] Added dual-write to herald/cli.py for paused/muted**
- **Found during:** Task 2 (Wire callers)
- **Issue:** Plan said to add pause/mute dual-writes in orchestrator.py, but those flags are only READ there — they are WRITTEN by herald/cli.py commands. Orchestrator never creates the flag files.
- **Fix:** Added update_state paused/muted in _cmd_pause, _cmd_resume, _cmd_mute in herald/cli.py
- **Files modified:** heyvox/herald/cli.py
- **Verification:** grep confirms update_state calls in cli.py
- **Committed in:** 5063785

---

**Total deviations:** 2 auto-fixed (both Rule 2 — missing critical write points identified by tracing actual flag file writers)
**Impact on plan:** Both fixes required for complete state file population. No scope creep.

## Issues Encountered

- test_adapters.py::test_inject_text_pastes_directly has a pre-existing failure (AttributeError: `heyvox.adapters.generic` has no attribute `type_text`). This failure pre-dates this plan (confirmed via git stash). Logged to deferred items — out of scope for 08-02.

## Next Phase Readiness

- heyvox/ipc/ module complete and tested
- State file is now populated from all write paths (dual-write mode)
- 08-03 can safely migrate readers to use read_state() as primary source, removing flag file reads
- Pre-existing test_adapters.py failure needs attention before or during 08-03

---
*Phase: 08-ipc-consolidation*
*Completed: 2026-04-11*
