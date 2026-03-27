---
phase: 04-mcp-server
plan: 01
subsystem: mcp
tags: [mcp, fastmcp, tts, voice, stdio, python]

# Dependency graph
requires:
  - phase: 03-cli-tts-output
    provides: Kokoro TTS engine (vox/audio/tts.py) with speak, skip_current, stop_all, set_muted, set_verbosity, start_worker, shutdown
  - phase: 01-foundation
    provides: vox/config.py (load_config, VoxConfig) and vox/constants.py (flag file paths)
provides:
  - FastMCP server at vox/mcp/server.py with 4 voice tools
  - voice_speak: enqueues text for TTS playback (fire-and-forget)
  - voice_status: reads flag files for recording/speaking/idle state
  - voice_queue: list/skip/stop/clear/mute/unmute TTS queue
  - voice_config: get/set verbosity and muted state
  - mcp>=1.0 declared in pyproject.toml
affects: [05-hud-overlay, future-phases, users-claude-settings-json]

# Tech tracking
tech-stack:
  added: [mcp>=1.0 (FastMCP)]
  patterns:
    - FastMCP lifespan context manager for TTS worker lifecycle
    - Lazy imports inside tool functions (keep module load fast)
    - loguru stdout suppression before any third-party TTS imports
    - Sync tool functions for fire-and-forget enqueue operations

key-files:
  created: []
  modified:
    - vox/mcp/server.py
    - pyproject.toml

key-decisions:
  - "FastMCP lifespan starts TTS worker before first tool call — avoids silent no-audio bug"
  - "loguru stdout patched at module top before any imports — prevents MCP stdio corruption"
  - "Sync tool functions (not async) — all TTS ops are non-blocking enqueue"
  - "voice_status reads flag files not module state — works cross-process with launchd daemon"
  - "voice_config SET does not persist to YAML — live session state only (v1 scope)"

patterns-established:
  - "MCP-05: logging.basicConfig(stream=sys.stderr) + loguru.remove() at module top guards stdout"
  - "Lazy imports in tool functions: keeps import time fast, avoids load-time failures"

# Metrics
duration: 1min
completed: 2026-03-27
---

# Phase 4 Plan 01: MCP Server Summary

**FastMCP server with 4 voice tools (voice_speak/status/queue/config), lifespan-managed TTS worker, and full stdout pollution suppression for clean MCP stdio transport**

## Performance

- **Duration:** ~1 min
- **Started:** 2026-03-27T10:41:51Z
- **Completed:** 2026-03-27T10:43:04Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments

- Implemented complete FastMCP server replacing the docstring-only stub in vox/mcp/server.py
- Four voice tools registered: voice_speak (TTS enqueue), voice_status (flag file state), voice_queue (skip/stop/clear/mute), voice_config (get/set verbosity+muted)
- TTS worker lifecycle managed via asynccontextmanager lifespan (start on server startup, shutdown on exit)
- Loguru stdout pollution suppressed at module top before any kokoro imports — prevents MCP stdio corruption
- Added mcp>=1.0 to pyproject.toml dependencies (satisfies PROJ-02)

## Task Commits

Each task was committed atomically:

1. **Task 1: Implement FastMCP server with 4 voice tools** - `a7bb077` (feat)
2. **Task 2: Add mcp dependency to pyproject.toml** - `14af3f0` (chore)

## Files Created/Modified

- `vox/mcp/server.py` - Complete FastMCP server: loguru suppression, lifespan TTS management, 4 tools, __main__ guard (~120 lines)
- `pyproject.toml` - Added mcp>=1.0 to [project.dependencies]

## Decisions Made

- **loguru suppression at module top**: The try/except ImportError block runs before lifespan, before any vox imports. This guarantees kokoro's loguru calls never reach stdout regardless of import order.
- **Sync tool functions**: All TTS operations (speak, skip_current, stop_all, etc.) are non-blocking queue operations — no need for async.
- **voice_status reads flag files**: Cross-process truth. Module-level `_muted` is process-local; TTS_PLAYING_FLAG and RECORDING_FLAG are shared with the launchd daemon.
- **voice_config SET is session-only**: Setting verbosity/muted takes effect immediately via TTS module state. YAML persistence deferred to v1.1 (unnecessary complexity for v1).
- **Lazy imports in all tool functions**: Module loads in <10ms; kokoro KPipeline is only initialized when TTS worker starts (in lifespan), not at import.

## Deviations from Plan

None - plan executed exactly as written. All patterns from 04-RESEARCH.md followed precisely.

## Issues Encountered

None.

## User Setup Required

None - no external service configuration required. The setup wizard (vox/setup/wizard.py, Phase 3) already writes the MCP server entry to ~/.claude/settings.json.

## Next Phase Readiness

- MCP server is complete and importable without errors
- All 5 verification checks pass
- `python -m vox.mcp.server` will start the stdio MCP server
- Ready for Phase 5 (HUD overlay) or integration testing

## Self-Check: PASSED

All files found. All commits verified.

---
*Phase: 04-mcp-server*
*Completed: 2026-03-27*
