# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-26)

**Core value:** One voice layer that works across ALL your AI coding agents -- wake word, local STT, local TTS, beautiful HUD -- without sending audio to the cloud.
**Current focus:** Phase 2: Audio + Input Pipeline — Plan 01 complete (echo suppression + health check)

## Current Position

Phase: 2 of 5 (Audio + Input Pipeline)
Plan: 1 of N in current phase (COMPLETE)
Status: Phase 2 in progress
Last activity: 2026-03-27 -- Plan 02-01 complete (headset detection, echo suppression, silent-mic health check)

Progress: [###.......] 30%

## Performance Metrics

**Velocity:**
- Total plans completed: 2
- Average duration: 5.5 min
- Total execution time: 0.18 hours

**By Phase:**

| Phase | Plans | Total | Avg/Plan |
|-------|-------|-------|----------|
| 01-foundation | 2 | 11 min | 5.5 min |
| 02-audio-input-pipeline | 1 | 2 min | 2 min |

**Recent Trend:**
- Last 5 plans: 7 min, 4 min, 2 min
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
- [02-01] Echo suppression uses file flag IPC (/tmp/vox-tts-playing) — TTS runs out-of-process, flag is the natural IPC boundary
- [02-01] Stale flag guard 60s — generous for long TTS responses, fast enough to recover from TTS crash
- [02-01] Health check: 30s interval, 3 consecutive strikes required (90s minimum) to avoid false positives
- [02-01] detect_headset() bidirectional substring matching handles asymmetric macOS BT/USB device naming

### Pending Todos

None yet.

### Blockers/Concerns

- Package name not finalized -- "vox" taken on PyPI and Homebrew. Candidates: heyvox, voxcode, hotmic, murmur, hark.

## Session Continuity

Last session: 2026-03-27
Stopped at: Completed 02-01-PLAN.md (echo suppression + silent-mic health check)
Resume file: None
