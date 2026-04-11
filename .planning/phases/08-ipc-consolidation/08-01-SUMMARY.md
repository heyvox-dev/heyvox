---
phase: 08-ipc-consolidation
plan: 01
subsystem: ipc
tags: [constants, ipc, refactor, test-scaffold]
dependency_graph:
  requires: []
  provides: [IPC-01, wave-0-scaffolds]
  affects: [heyvox.main, heyvox.cli, heyvox.audio.tts, heyvox.audio.media, heyvox.mcp.server, heyvox.hud.overlay, heyvox.herald.orchestrator, heyvox.herald.worker]
tech_stack:
  added: []
  patterns: [single-source-of-truth constants, source-of-truth comments for standalone daemons, wave-0 test scaffolds with pytestmark skip]
key_files:
  created:
    - tests/test_ipc_state.py
    - tests/test_queue_gc.py
  modified:
    - heyvox/constants.py
    - heyvox/main.py
    - heyvox/cli.py
    - heyvox/audio/tts.py
    - heyvox/audio/media.py
    - heyvox/mcp/server.py
    - heyvox/input/injection.py
    - heyvox/hud/overlay.py
    - heyvox/hud/ipc.py
    - heyvox/hud/process.py
    - heyvox/herald/orchestrator.py
    - heyvox/herald/worker.py
    - heyvox/config.py
    - heyvox/herald/daemon/watcher.py
    - heyvox/herald/daemon/kokoro-daemon.py
    - heyvox/hush/host/hush_host.py
    - heyvox/hush/integration/vox-media.py
decisions:
  - "Standalone daemons (watcher.py, kokoro-daemon.py, hush_host.py, vox-media.py) cannot import heyvox.constants — annotated with 'Must match heyvox.constants.*' comments instead"
  - "overlay.py standalone fallback retains one raw /tmp string with source-of-truth comment (runs without full vox package in some configs)"
  - "Wave 0 scaffolds use module-level pytestmark skip — Plans 02 and 03 remove it to unskip by section"
metrics:
  duration: "~8 min"
  completed: "2026-04-11"
  tasks: 3
  files_modified: 17
  files_created: 2
---

# Phase 08 Plan 01: IPC Constants Consolidation Summary

Single source of truth for all 25+ IPC path constants in heyvox/constants.py, migrating 16 package-importable modules from hardcoded /tmp strings, with standalone daemons annotated with source-of-truth comments.

## What Was Built

**IPC-01 complete:** Every /tmp path in the Python codebase now traces to `heyvox/constants.py` as single source of truth. Eliminated 60+ hardcoded /tmp strings scattered across 14+ files.

### New Constants Added (heyvox/constants.py)

- **Core process files:** `HEYVOX_PID_FILE`, `HEYVOX_HEARTBEAT_FILE`, `HEYVOX_RESTART_LOG`
- **Legacy compat:** `CLAUDE_TTS_MUTE_FLAG`, `CLAUDE_TTS_PLAYING_PID`
- **Herald workspace/ambient:** `HERALD_AMBIENT_FLAG`, `HERALD_WORKSPACE_FILE`, `HERALD_ORIGINAL_VOL_FILE`, `HERALD_GENERATING_WAV_PREFIX`, `HERALD_WATCHER_PID`, `HERALD_WATCHER_HANDLED_DIR`, `HERALD_MEDIA_PAUSED_PREFIX`
- **HUD files:** `HUD_POSITION_FILE`, `HUD_STDERR_LOG`
- **TTS style:** `TTS_STYLE_FILE`
- **Media pause coordination:** `HEYVOX_MEDIA_PAUSED_REC`, `HEYVOX_MEDIA_PAUSED_PREFIX`
- **Hush:** `HUSH_SOCK`, `HUSH_LOG`
- **Atomic state file (IPC-02 placeholder):** `HEYVOX_STATE_FILE`

### Wave 0 Test Scaffolds

- `tests/test_ipc_state.py` — 6 skipped tests for Plan 08-02 (atomic state file)
- `tests/test_queue_gc.py` — 5 skipped tests for Plan 08-03 (Herald queue GC)
- 11 tests collected by pytest, all skipped pending implementation

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Missing] heyvox/hud/ipc.py and heyvox/hud/process.py were not in the original task list**
- **Found during:** Task 2 verification
- **Issue:** grep found hardcoded /tmp strings in hud/ipc.py (DEFAULT_SOCKET_PATH) and hud/process.py (HUD_STDERR_LOG)
- **Fix:** Migrated both files to use HUD_SOCKET_PATH and HUD_STDERR_LOG constants
- **Files modified:** heyvox/hud/ipc.py, heyvox/hud/process.py
- **Commit:** f526479

## Test Results

- 204 tests passed
- 11 skipped (our new Wave 0 scaffolds + pre-existing skips)
- 3 pre-existing failures unrelated to this plan (test_adapters: `_browser_has_video_tab` mock, test_media: same attribute mock — both pre-date this plan)

## Known Stubs

None — this plan is purely refactor (no new features, no data flows).

## Self-Check: PASSED

All files exist: constants.py, test_ipc_state.py, test_queue_gc.py
All commits exist: 4245990 (Task 1), f526479 (Task 2), 08327db (Task 3)
