# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-26)

**Core value:** One voice layer that works across ALL your AI coding agents -- wake word, local STT, local TTS, beautiful HUD -- without sending audio to the cloud.
**Current focus:** Phase 1: Foundation

## Current Position

Phase: 1 of 5 (Foundation)
Plan: 2 of 2 in current phase (COMPLETE)
Status: Phase 1 complete
Last activity: 2026-03-27 -- Plan 02 complete (pydantic config system + full decoupling)

Progress: [##........] 20%

## Performance Metrics

**Velocity:**
- Total plans completed: 2
- Average duration: 5.5 min
- Total execution time: 0.18 hours

**By Phase:**

| Phase | Plans | Total | Avg/Plan |
|-------|-------|-------|----------|
| 01-foundation | 2 | 11 min | 5.5 min |

**Recent Trend:**
- Last 5 plans: 7 min, 4 min
- Trend: accelerating

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
- [01-02] Config flows as typed VoxConfig object (not dict) through all functions
- [01-02] TTSConfig validates script_path existence at config load time (fail fast)
- [01-02] overlay.py always uses NSScreen.mainScreen() -- Conductor-specific detection removed entirely
- [01-02] ptt.py required no changes -- callbacks dict already decoupled RECORDING_FLAG usage

### Pending Todos

None yet.

### Blockers/Concerns

- Package name not finalized -- "vox" taken on PyPI and Homebrew. Candidates: heyvox, voxcode, hotmic, murmur, hark.

## Session Continuity

Last session: 2026-03-27
Stopped at: Completed 01-02-PLAN.md (pydantic config system + full decoupling)
Resume file: None
