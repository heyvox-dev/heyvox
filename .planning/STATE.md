# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-26)

**Core value:** One voice layer that works across ALL your AI coding agents -- wake word, local STT, local TTS, beautiful HUD -- without sending audio to the cloud.
**Current focus:** Phase 1: Foundation

## Current Position

Phase: 1 of 5 (Foundation)
Plan: 1 of 2 in current phase
Status: In progress
Last activity: 2026-03-27 -- Plan 01 complete (package skeleton + monolith extraction)

Progress: [#.........] 10%

## Performance Metrics

**Velocity:**
- Total plans completed: 1
- Average duration: 7 min
- Total execution time: 0.12 hours

**By Phase:**

| Phase | Plans | Total | Avg/Plan |
|-------|-------|-------|----------|
| 01-foundation | 1 | 7 min | 7 min |

**Recent Trend:**
- Last 5 plans: 7 min
- Trend: baseline established

*Updated after each plan completion*

## Accumulated Context

### Decisions

Decisions are logged in PROJECT.md Key Decisions table.
Recent decisions affecting current work:

- Hybrid voice model confirmed (OS-level IN, MCP OUT)
- MCP server lean (4-5 tools) + CLI commands for hook integration
- HUD as separate AppKit process with Unix socket IPC
- pipx/uv install as v1 distribution (not Homebrew formula)
- Package name TBD ("vox" taken on PyPI/Homebrew)
- [01-01] Package published as vox-voice (placeholder), CLI command stays vox
- [01-01] setuptools build backend (hatchling not in dev environment)
- [01-01] Parametric functions: config values as parameters, not globals (enables Plan 02 config system)
- [01-01] PTT callbacks dict pattern for decoupled event handling
- [01-01] Lazy imports for mlx_whisper, sherpa_onnx, Quartz

### Pending Todos

None yet.

### Blockers/Concerns

- Package name not finalized -- "vox" taken on PyPI and Homebrew. Candidates: heyvox, voxcode, hotmic, murmur, hark.

## Session Continuity

Last session: 2026-03-27
Stopped at: Completed 01-01-PLAN.md (package skeleton + monolith extraction)
Resume file: None
