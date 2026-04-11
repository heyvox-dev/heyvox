---
gsd_state_version: 1.0
milestone: v1.1
milestone_name: Architecture Hardening
status: executing
stopped_at: Phase 7 context gathered
last_updated: "2026-04-11T14:52:58.056Z"
last_activity: 2026-04-11 -- Phase 07 execution started
progress:
  total_phases: 4
  completed_phases: 1
  total_plans: 8
  completed_plans: 4
  percent: 50
---

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-04-10)

**Core value:** One voice layer that works across ALL your AI coding agents -- wake word, local STT, local TTS, beautiful HUD -- without sending audio to the cloud.
**Current focus:** Phase 07 — herald-python-port

## Current Position

Phase: 07 (herald-python-port) — EXECUTING
Plan: 1 of 4
Status: Executing Phase 07
Last activity: 2026-04-11 -- Phase 07 execution started

Progress: [██████████          ] 50% (5/10 v1.0 phases complete; v1.1 starting)

## Performance Metrics

**Velocity (v1.0):**

- Total plans completed: 10
- Average duration: 3.5 min
- Total execution time: ~0.6 hours

**v1.1 plans:** Not yet started

## Accumulated Context

### Decisions

Full decision log in PROJECT.md Key Decisions table.

- [Phase 06-decomposition]: Module-level pytestmark skip allows Plans 01-03 to unskip tests with one line removal
- [Phase 06]: dataclasses.field(default_factory=...) for all mutable AppContext defaults to prevent shared state
- [Phase 06]: Backward-compat re-exports in main.py preserve test API until Phase 9 cleanup
- [Phase 06-decomposition]: Device-private state on DeviceManager (not AppContext): pa, stream, headset_mode, _mic_pinned, cv_history live on DeviceManager; AppContext holds cross-concern state only
- [Phase 06-decomposition]: Bridge pattern for recording globals in Plan 02: ctx.is_recording = is_recording at loop top; removed in Plan 03 when recording state moves to AppContext
- [Phase 06-decomposition]: HUD process lifecycle extracted to heyvox/hud/process.py as natural decomposition

### Pending Todos

None.

### Blockers/Concerns

- main.py is ~2000 lines with 17+ globals — Phase 6 (Decomposition) addresses this first
- Herald orchestrator.sh crosses shell/Python boundary 4x per TTS request — Phase 7 eliminates this
- 25+ flag files in /tmp/ with race conditions — Phase 8 consolidates to atomic state file
- Phase ordering is strict: Decomp → Herald Port → IPC → Tests (each depends on prior)

## Session Continuity

Last session: 2026-04-11T14:26:03.867Z
Stopped at: Phase 7 context gathered
Resume file: .planning/phases/07-herald-python-port/07-CONTEXT.md
