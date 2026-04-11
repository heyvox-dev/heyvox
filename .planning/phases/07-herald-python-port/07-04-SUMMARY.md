---
phase: 07-herald-python-port
plan: "04"
subsystem: herald
tags: [herald, tts, python-port, integration, cleanup]
dependency_graph:
  requires: ["07-02", "07-03"]
  provides: ["fully-wired Herald Python pipeline"]
  affects: ["heyvox/herald/__init__.py", "heyvox/herald/cli.py", "heyvox/herald/hooks/"]
tech_stack:
  added: []
  patterns: ["thin bash shims calling python3 -m module", "Python CLI dispatch pattern"]
key_files:
  created: []
  modified:
    - heyvox/herald/__init__.py
    - heyvox/herald/cli.py
    - heyvox/herald/hooks/on-response.sh
    - heyvox/herald/hooks/on-notify.sh
    - heyvox/herald/hooks/on-ambient.sh
    - heyvox/herald/hooks/on-session-start.sh
    - heyvox/herald/hooks/on-session-end.sh
  deleted:
    - heyvox/herald/lib/orchestrator.sh
    - heyvox/herald/lib/worker.sh
    - heyvox/herald/lib/config.sh
    - heyvox/herald/lib/speak.sh
    - heyvox/herald/lib/media.sh
    - heyvox/herald/bin/herald
decisions:
  - "D-03 applied: thin bash shims (5 lines) retained for hook entry points; Python handles all logic"
  - "coreaudio.py (not volume.py) is the CoreAudio module — plan referred to volume.py but actual file is coreaudio.py"
metrics:
  duration: "~5 min"
  completed_date: "2026-04-11"
  tasks_completed: 2
  tasks_total: 3
  files_modified: 12
---

# Phase 07 Plan 04: Herald Python Port — Wiring and Cleanup Summary

Wire Herald Python modules into entry points, delete old bash scripts. Hook shims updated to `exec python3 -m heyvox.herald.worker`, CLI rewritten in Python, `run_herald()` delegates to Python dispatch.

## Tasks Completed

| # | Name | Commit | Files |
|---|------|--------|-------|
| 1 | Update hooks, CLI, and __init__.py to use Python modules | 0f26b7e | 7 files (5 hooks + __init__.py + cli.py) |
| 2 | Delete old bash scripts (D-02) | b748e3c | 6 files deleted (lib/*.sh, bin/herald) |

## Task 3: Checkpoint (Pending Human Verification)

Task 3 is a `checkpoint:human-verify` gate — awaiting human verification of end-to-end TTS pipeline.

## What Was Built

**Task 1 — Entry Point Wiring:**
- Hook shims (`on-response.sh`, `on-notify.sh`, `on-ambient.sh`, `on-session-start.sh`, `on-session-end.sh`): Each is now a 5-line bash script that sets `HERALD_HOOK_TYPE` and `exec python3 -m heyvox.herald.worker "$@"`. No more `speak.sh`, `modes/notify.sh` etc delegation.
- `__init__.py`: Removed `HERALD_BIN`, `HERALD_LIB`, `HERALD_DAEMON`, `HERALD_MODES`. Added `start_orchestrator()`. `run_herald()` now calls `cli.dispatch()` directly.
- `cli.py`: Full Python CLI dispatch — speak/pause/resume/skip/mute/status/queue/orchestrator. All implemented via Python constants and modules, no bash subprocess delegation.

**Task 2 — Clean Break (D-02):**
- Deleted: `heyvox/herald/lib/orchestrator.sh`, `worker.sh`, `config.sh`, `speak.sh`, `media.sh`
- Deleted: `heyvox/herald/bin/herald` (bash CLI)
- Python modules are the sole implementation

## Verification Results

- 110 tests pass: `tests/test_herald_worker.py` + `tests/test_herald_orchestrator.py`
- `python -m heyvox.herald.cli status` outputs correct orchestrator status
- All herald imports resolve: `HeraldOrchestrator`, `HeraldWorker`, `get_system_volume`
- No bash remnants in lib/ or bin/ (directories removed)

## Deviations from Plan

**1. [Rule 0 - Adaptation] coreaudio.py vs volume.py**
- **Found during:** Task 2 verification
- **Issue:** Plan verification script checked for `heyvox/herald/volume.py` and imports `get_volume`, `set_volume`, `is_muted`, `CachedVolumeState` — but the actual module from Plan 01 is `heyvox/herald/coreaudio.py` with different function names (`get_system_volume`, `set_system_volume`, `is_system_muted`)
- **Fix:** Adapted verification to use correct module name and function names. No code change needed — existing module is correct.

## Known Stubs

None — all entry points fully wired to Python implementations.

## Self-Check: PASSED

- heyvox/herald/__init__.py: EXISTS
- heyvox/herald/cli.py: EXISTS
- heyvox/herald/hooks/on-response.sh: EXISTS, contains `python3 -m heyvox.herald.worker`
- heyvox/herald/lib/orchestrator.sh: DELETED (confirmed)
- Commits 0f26b7e, b748e3c: CONFIRMED in git log
- 110 tests: PASSED
