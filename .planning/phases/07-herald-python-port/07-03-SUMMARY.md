---
phase: "07-herald-python-port"
plan: "07-03"
subsystem: herald
tags: [tts, orchestrator, coreaudio, tests, unit-tests]
dependency_graph:
  requires: [heyvox.herald.orchestrator, heyvox.herald.coreaudio]
  provides: [tests.test_herald_orchestrator]
  affects: []
tech_stack:
  added: []
  patterns: [pytest, unittest.mock, tmp_path isolation, inline-import patching]
key_files:
  created:
    - tests/test_herald_orchestrator.py
  modified: []
decisions:
  - patch heyvox.herald.coreaudio directly (not heyvox.herald.orchestrator) because duck/restore use inline imports
  - use tmp_path fixture for all flag/queue dirs to avoid interfering with running HeyVox
  - mock subprocess.Popen at orchestrator level for lifecycle tests
metrics:
  duration_minutes: 4
  tasks_completed: 2
  files_created: 1
  files_modified: 0
  completed_date: "2026-04-11"
requirements: [HERALD-01, HERALD-02, HERALD-03, HERALD-04]
---

# Phase 7 Plan 3: Herald Orchestrator Tests Summary

## One-liner

65 unit tests for HeraldOrchestrator and CoreAudio volume module — all green, no real audio/CoreAudio calls, full tmp_path isolation.

## What Was Built

### tests/test_herald_orchestrator.py (new)

Comprehensive unit tests across 10 test classes (65 tests total):

| Class | Tests | Coverage |
|-------|-------|----------|
| TestOrchestratorConfig | 10 | Defaults, Path types, duck/media flags, cache TTL, normalize params |
| TestNormalizeWav | 6 | Scale-up, silent skip, cap enforcement, peak softclip, bad path, WAV params |
| TestIsPaused | 5 | Pause flag, recording flag, stale flag auto-removal, both flags |
| TestAudioDucking | 8 | Duck saves volume, sets duck level, disabled no-op, restart reuse, restore |
| TestVerbosity | 4 | Default full, file read, skip detection |
| TestUserIsActive | 4 | 15s window, expired, paused=active |
| TestViolationCheck | 4 | No violation, recording violation, pause violation, context in log |
| TestHeraldLog | 3 | Write, append, tolerates bad path |
| TestEnforceSingleton | 4 | No PID file, own PID, dead PID, corrupt PID |
| TestHeraldOrchestratorLifecycle | 8 | stop() terminates run(), idempotent stop, dir creation, PID file, mute/skip |
| TestCoreAudioModule | 9 | get/cached volume, TTL, cache update on set, clamp, is_muted, invalidation |

**Key testing patterns:**
- All filesystem I/O redirected to `tmp_path` via `_cfg()` helper
- CoreAudio patches target `heyvox.herald.coreaudio` directly (not orchestrator module)  
  because `_duck_audio`/`_restore_audio` use inline imports
- Lifecycle tests use `threading.Thread(daemon=True)` + `orch.stop()` for clean teardown
- Mock `subprocess.Popen` at orchestrator module level for mute/skip WAV deletion tests

## Verification Results

```
============================= test session starts ==============================
collected 65 items

tests/test_herald_orchestrator.py::TestOrchestratorConfig::test_defaults_are_path_objects PASSED
...
tests/test_herald_orchestrator.py::TestCoreAudioModule::test_invalidate_cache_clears_cached_value PASSED

============================== 65 passed in 3.03s ==============================
```

## Commits

| Task | Commit | Description |
|------|--------|-------------|
| 1 | d1bfb5e | feat(07-03): cherry-pick orchestrator + coreaudio from 07-01 |
| 2 | 5b2385b | test(07-03): add 65 unit tests for Herald orchestrator and CoreAudio |

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Patch targets required correction**
- **Found during:** Task 2 (test execution)
- **Issue:** Initial tests patched `heyvox.herald.orchestrator.set_system_volume_cached` and `get_system_volume_cached`, but these don't exist as module-level names — the functions are imported inline inside `_duck_audio` and `_restore_audio` function bodies
- **Fix:** Redirect patches to `heyvox.herald.coreaudio.get_system_volume` and `heyvox.herald.coreaudio.set_system_volume` which the cached wrappers ultimately call
- **Files modified:** tests/test_herald_orchestrator.py
- **Commit:** 5b2385b (same commit, fixed before final commit)

## Known Stubs

None — all tests are real behavioral assertions against production code.

## Self-Check: PASSED

- FOUND: tests/test_herald_orchestrator.py
- FOUND: 5b2385b (test commit)
- FOUND: d1bfb5e (orchestrator cherry-pick)
- All 65 tests pass: `python3 -m pytest tests/test_herald_orchestrator.py -v` → 65 passed in 3.03s
