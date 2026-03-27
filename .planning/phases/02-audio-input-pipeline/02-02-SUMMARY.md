---
phase: 02-audio-input-pipeline
plan: 02
subsystem: input
tags: [adapter-pattern, text-injection, target-mode, last-agent, NSWorkspace, config]

# Dependency graph
requires:
  - phase: 01-foundation
    provides: VoxConfig pydantic system, vox.adapters.base AgentAdapter protocol
  - phase: 02-01
    provides: main.py event loop with headset detection and echo suppression

provides:
  - target_mode and agents fields on VoxConfig with field_validator (INPT-03, INPT-05)
  - GenericAdapter accepting target_app and enter_count constructor params (INPT-03)
  - LastAgentAdapter with NSWorkspace polling daemon thread and inject_text (INPT-04, INPT-05)
  - _build_adapter() factory in vox/main.py selecting adapter per config.target_mode (INPT-03)
  - adapter.inject_text() dispatch in _send_local() replacing direct focus_app/press_enter calls

affects: [03-hud, 04-mcp-server, adapters-extensions]

# Tech tracking
tech-stack:
  added:
    - AppKit.NSWorkspace (lazy import in LastAgentAdapter daemon thread)
  patterns:
    - "Adapter factory pattern: _build_adapter(config) returns concrete adapter based on config.target_mode"
    - "Module-level _adapter state: initialized in main() once, passed to _send_local() thread via args"
    - "PTT bypass: PTT mode always pastes into focused app, skipping adapter refocus logic"
    - "Lazy AppKit import: deferred inside daemon thread to avoid load-time failures in non-macOS/test contexts"
    - "should_auto_send() + enter_count properties: adapter controls whether Enter is pressed after inject"

key-files:
  created:
    - vox/adapters/last_agent.py
  modified:
    - vox/config.py
    - vox/adapters/generic.py
    - vox/main.py

key-decisions:
  - "PTT always bypasses adapter — pastes into focused app regardless of target_mode to prevent unexpected refocus"
  - "Module-level _adapter state (like other flags) rather than closure — consistent with existing threading pattern"
  - "LastAgentAdapter uses lazy AppKit import inside daemon thread — avoids import at module load for portability"
  - "GenericAdapter.should_auto_send() returns True only when target_app is set — no auto-send in always-focused mode"
  - "_build_adapter falls through to GenericAdapter(no target_app) for always-focused AND pinned-app with no target_app set"

# Metrics
duration: ~2min
completed: 2026-03-27
---

# Phase 2 Plan 2: Adapter Dispatch and Last-Agent Tracking Summary

**Adapter factory (_build_adapter) wires three target modes into main.py: always-focused (GenericAdapter, no refocus), pinned-app (GenericAdapter with app focus), and last-agent (LastAgentAdapter polling NSWorkspace every 1s)**

## Performance

- **Duration:** ~2 min
- **Started:** 2026-03-27T09:10:36Z
- **Completed:** 2026-03-27T09:12:52Z
- **Tasks:** 2
- **Files modified:** 4

## Accomplishments

- `VoxConfig` gains `target_mode` (validated enum: always-focused|pinned-app|last-agent) and `agents` fields; `generate_default_config()` YAML updated (INPT-03, INPT-05)
- `GenericAdapter` updated with `target_app` and `enter_count` constructor params; `inject_text()` focuses app when `target_app` is set; `should_auto_send()` returns True only for pinned-app mode (INPT-03)
- `LastAgentAdapter` created — polls `NSWorkspace.frontmostApplication()` every second in a daemon thread to track the last focused AI agent; `inject_text()` focuses that agent before pasting (INPT-04, INPT-05)
- `_build_adapter(config)` factory added to `main.py` — returns the correct adapter based on `config.target_mode`; `_adapter` module-level state initialized before audio stream opens
- `_send_local()` updated — wake-word path routes through `adapter.inject_text()`; PTT path calls `type_text()` directly (bypasses adapter); Enter key controlled by `adapter.should_auto_send()` and `adapter.enter_count`
- Direct `focus_app` and `press_enter` imports removed from `main.py` top-level (moved into adapters and local imports)

## Task Commits

Each task was committed atomically:

1. **Task 1: Add config fields, update GenericAdapter, create LastAgentAdapter** - `e8b7d32` (feat)
2. **Task 2: Wire adapter dispatch into main.py _send_local()** - `1c597f1` (feat)

**Plan metadata:** (docs commit — this summary)

## Files Created/Modified

- `vox/adapters/last_agent.py` — New: LastAgentAdapter with NSWorkspace polling daemon thread
- `vox/config.py` — Added target_mode (validated) and agents fields; updated YAML template
- `vox/adapters/generic.py` — Updated with target_app/enter_count params, conditional focus, updated should_auto_send()
- `vox/main.py` — Added _build_adapter() factory, _adapter module-level state, adapter dispatch in _send_local(), cleaned up imports

## Decisions Made

- PTT always bypasses the adapter and pastes into the focused app — prevents LastAgentAdapter from refocusing when user intentionally has a different app open
- Module-level `_adapter = None` state consistent with existing `busy`, `is_recording` pattern — initialized in main(), passed as arg to thread
- AppKit imported lazily inside daemon thread — avoids load-time failure on non-macOS and in test environments
- `GenericAdapter.should_auto_send()` returns `True` only when `target_app` is set (pinned-app = AI agent = wants auto-send)
- `_build_adapter` falls through to `GenericAdapter()` with no target_app for both `always-focused` and `pinned-app` without a configured app

## Deviations from Plan

None — plan executed exactly as written.

## Issues Encountered

None.

## User Setup Required

None — no external service configuration required.

## Next Phase Readiness

- INPT-03, INPT-04, INPT-05 complete
- All three target modes (always-focused, pinned-app, last-agent) implemented and selectable via config
- PTT bypass correctly isolates push-to-talk from adapter refocus logic
- Ready for Phase 3 (HUD) or Phase 4 (MCP server) — adapter protocol is complete and extensible

---
*Phase: 02-audio-input-pipeline*
*Completed: 2026-03-27*

## Self-Check: PASSED

- vox/adapters/last_agent.py — FOUND
- vox/config.py — FOUND
- vox/adapters/generic.py — FOUND
- vox/main.py — FOUND
- 02-02-SUMMARY.md — FOUND
- Commit e8b7d32 — FOUND
- Commit 1c597f1 — FOUND
