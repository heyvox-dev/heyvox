---
phase: "07-herald-python-port"
plan: "07-01"
subsystem: herald
tags: [tts, orchestrator, coreaudio, python-port, audio-ducking]
dependency_graph:
  requires: [heyvox.herald.__init__, heyvox.herald.lib.worker]
  provides: [heyvox.herald.orchestrator, heyvox.herald.coreaudio]
  affects: [heyvox.herald.lib.worker, heyvox.herald.__init__]
tech_stack:
  added: [ctypes CoreAudio, heyvox.herald.orchestrator, heyvox.herald.coreaudio]
  patterns: [dataclass config, threading watchdog, CoreAudio ctypes volume]
key_files:
  created:
    - heyvox/herald/coreaudio.py
    - heyvox/herald/orchestrator.py
  modified:
    - heyvox/herald/__init__.py
    - heyvox/herald/lib/worker.sh
decisions:
  - keep afplay for WAV playback (reliable, easy to kill, handles all WAV formats)
  - CoreAudio ctypes with osascript fallback (defensive: API changes should not break TTS)
  - OrchestratorConfig dataclass with all paths configurable (testability)
  - guard signal.signal with ValueError catch (supports threaded use in tests)
metrics:
  duration_minutes: 5
  tasks_completed: 4
  files_created: 2
  files_modified: 2
  completed_date: "2026-04-11"
requirements: [HERALD-01, HERALD-02, HERALD-03, HERALD-04]
---

# Phase 7 Plan 1: Herald Python Orchestrator Summary

## One-liner

Pure Python Herald orchestrator replacing orchestrator.sh: CoreAudio ctypes volume, threading watchdog, hold queue, workspace switching — same behavior, zero bash boundary crossings.

## What Was Built

### heyvox/herald/coreaudio.py (new)

CoreAudio volume control via ctypes — no osascript subprocesses:
- `get_system_volume()` / `set_system_volume()` via `AudioObjectGetPropertyData` / `AudioObjectSetPropertyData`
- `is_system_muted()` via `kAudioDevicePropertyMute`
- `get_system_volume_cached(ttl=5.0)` — cached reads, re-reads only after TTL expires (HERALD-04)
- `set_system_volume_cached()` — sets volume and updates cache
- Full osascript fallback on any CoreAudio error (defensive — macOS API changes won't break TTS)
- Reads current system volume: 0.33 (verified)

### heyvox/herald/orchestrator.py (new)

Pure Python port of `orchestrator.sh` — `HeraldOrchestrator` class:

```
HeraldOrchestrator
  .run()   # main loop (blocking)
  .stop()  # signal exit
```

Features (all ported from bash):
- **Queue polling**: sorted WAV files from `/tmp/herald-queue/`, 300ms interval
- **WAV normalization**: inline RMS-based (target_rms=3000, scale_cap=3x, peak softclip at 24000)
- **Audio ducking**: CoreAudio ctypes (cached), saves/restores volume via `/tmp/herald-original-vol`
- **Recording watchdog**: kills `afplay` subprocess if recording flag appears mid-playback
- **Workspace switching**: `conductor-switch-workspace` subprocess if Conductor is frontmost (AppKit + osascript fallback)
- **Hold queue**: move WAV + .workspace sidecar to hold dir, Hammerspoon notification, cap enforcement
- **Media pause/resume**: delegates to `media.sh` (unchanged)
- **Singleton enforcement**: PID file with liveness check
- **Signal handling**: SIGTERM/SIGINT/SIGHUP → clean exit (guarded for non-main-thread use)
- **CLI**: `python3 -m heyvox.herald.orchestrator [--no-duck] [--no-media-pause] [--log-level]`

### heyvox/herald/__init__.py (modified)

Exports `HeraldOrchestrator` and `OrchestratorConfig` from the herald package.

### heyvox/herald/lib/worker.sh (modified)

Replaced bash orchestrator launch:
```bash
# Before:
nohup bash "${HERALD_HOME}/lib/orchestrator.sh" ...
# After:
nohup "${KOKORO_DAEMON_PYTHON}" -m heyvox.herald.orchestrator ...
```

## Verification Results

All checks pass:
- `from heyvox.herald.orchestrator import HeraldOrchestrator` — OK
- `from heyvox.herald.coreaudio import get_system_volume_cached; v=...` — vol=0.33, in [0.0, 1.0]
- `python3 -m heyvox.herald.orchestrator --help` — exits cleanly
- `worker.sh` grep confirms Python orchestrator launch
- Orchestrator runs 0.5s empty queue, stops cleanly via `stop()` call

## Commits

| Task | Commit | Description |
|------|--------|-------------|
| 1 | e46a73f | feat(07-01): create CoreAudio ctypes volume helper for Herald |
| 2 | 48af1a1 | feat(07-01): create Python Herald orchestrator |
| 3 | 7ccbe97 | refactor(07-01): wire Python orchestrator into worker.sh |
| 4 (fix) | 518c648 | fix(07-01): guard signal.signal call for non-main-thread use |

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Signal registration fails in non-main threads**
- **Found during:** Task 4 verification
- **Issue:** `signal.signal()` raises `ValueError` when called from a non-main thread, crashing the orchestrator when used in test threads
- **Fix:** Catch `ValueError` alongside `OSError` in the signal registration guard
- **Files modified:** heyvox/herald/orchestrator.py
- **Commit:** 518c648

## Known Stubs

None — all functionality is fully implemented. The orchestrator ports 100% of the bash behavior.

## Self-Check

## Self-Check: PASSED

All created files confirmed on disk:
- FOUND: heyvox/herald/coreaudio.py
- FOUND: heyvox/herald/orchestrator.py
- FOUND: .planning/phases/07-herald-python-port/07-01-SUMMARY.md

All commits confirmed:
- FOUND: e46a73f (CoreAudio helper)
- FOUND: 48af1a1 (Python orchestrator)
- FOUND: 7ccbe97 (wire worker.sh)
- FOUND: 518c648 (signal fix)
