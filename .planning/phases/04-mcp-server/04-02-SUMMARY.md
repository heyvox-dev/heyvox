---
phase: 04-mcp-server
plan: "02"
subsystem: mcp
tags: [mcp, fastmcp, tts, stdio, python]

# Dependency graph
requires:
  - phase: 04-01
    provides: FastMCP server with 4 voice tools, TTS worker lifespan
provides:
  - Hardened MCP server with stdout safety net (_original_stdout pattern)
  - Graceful ImportError handling for missing kokoro/sounddevice
  - Verified TTS integration: lifespan -> worker -> speak -> queue -> shutdown
  - Confirmed tool schemas lean (all under 80 chars), exactly 4 tools
affects: [05-packaging, future-mcp-extensions]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Stdout safety net: _original_stdout = sys.stdout; sys.stdout = sys.stderr at module top, restore in __main__"
    - "Graceful ImportError in lifespan: server starts even without optional TTS deps"

key-files:
  created: []
  modified:
    - vox/mcp/server.py

key-decisions:
  - "Stdout redirect pattern: save _original_stdout before redirect, restore in __main__ guard — ensures both rogue-print protection and FastMCP stdio transport work correctly"
  - "ImportError guard wraps start_worker() only (not whole lifespan) — TTS is optional, server must start regardless"
  - "Lifespan error logs to sys.stderr explicitly (not print()) — consistent with overall stderr-only logging policy"

patterns-established:
  - "Stdout safety net: module-top redirect to stderr + __main__ restore is the canonical pattern for MCP server modules"

# Metrics
duration: 5min
completed: 2026-03-27
---

# Phase 4 Plan 2: MCP Server Hardening Summary

**Stdout safety net via _original_stdout pattern and graceful TTS ImportError handling added to the FastMCP server, with full TTS integration verified end-to-end.**

## Performance

- **Duration:** ~5 min
- **Started:** 2026-03-27T00:00:00Z
- **Completed:** 2026-03-27T00:05:00Z
- **Tasks:** 2
- **Files modified:** 1

## Accomplishments
- Added module-top stdout safety net: `_original_stdout = sys.stdout; sys.stdout = sys.stderr` prevents any third-party print() from corrupting MCP stdio JSON-RPC framing
- Restored stdout in `__main__` guard before `mcp.run()` so FastMCP's stdio transport works correctly
- Wrapped `start_worker()` in `try/except ImportError` so server starts cleanly without kokoro/sounddevice installed
- Verified all TTS integration paths: `voice_speak → speak()`, lifespan `start_worker(config)/tts_shutdown()`, `voice_status` reads constants, `voice_queue` accesses `_tts_queue` directly
- Confirmed exactly 4 tools registered, all docstrings under 80 chars
- Confirmed `vox.mcp.server` module path appears twice in wizard.py (both in mcpServers config and manual-instructions fallback)

## Task Commits

Each task was committed atomically:

1. **Tasks 1+2: Validate + harden MCP server** - `5002b40` (feat)

**Plan metadata:** (docs commit follows)

## Files Created/Modified
- `vox/mcp/server.py` - Stdout safety net, graceful ImportError in lifespan

## Decisions Made
- **Stdout safety net pattern:** Save `_original_stdout` before redirecting `sys.stdout` to `sys.stderr` at module top; restore in `__main__` guard before `mcp.run()`. This is the cleanest way to protect against rogue prints while keeping FastMCP's stdio transport intact.
- **ImportError scope:** Only `start_worker()` is wrapped, not the whole lifespan. If lifespan itself fails, that's a real error that should surface.
- **Error message uses `file=sys.stderr` explicitly:** After the stdout redirect, `print()` already goes to stderr — but using `file=sys.stderr` makes the intent unambiguous and survives any future refactor.

## Deviations from Plan

None — plan executed exactly as written. All validation checks passed before hardening; hardening additions were implemented as specified.

## Issues Encountered
- The smoke test `echo '{}' | timeout 2 python -m vox.mcp.server` produced a JSON-RPC error response on stdout — this is correct behavior (server responding to malformed input), not a bug. The timeout exit masked the exit code but no traceback appeared.

## User Setup Required
None — no external service configuration required.

## Next Phase Readiness
- Phase 4 (MCP server) is fully complete: 4 lean tools, clean stdio transport, graceful error handling, setup wizard integration verified
- Ready for Phase 5: Packaging — `vox_voice` PyPI package, install docs, and distribution

---
*Phase: 04-mcp-server*
*Completed: 2026-03-27*

## Self-Check: PASSED

- vox/mcp/server.py: FOUND
- 04-02-SUMMARY.md: FOUND
- Commit 5002b40: FOUND
