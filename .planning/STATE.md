# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-26)

**Core value:** One voice layer that works across ALL your AI coding agents -- wake word, local STT, local TTS, beautiful HUD -- without sending audio to the cloud.
**Current focus:** Phase 3 complete, ready for Phase 4: MCP Server

## Current Position

Phase: 3 of 5 (CLI + TTS Output)
Plan: 2 of 2 in current phase (COMPLETE)
Status: Phase 3 complete — launchd service management, setup wizard, TTS interrupt wired
Last activity: 2026-03-27 -- Phase 3 Plan 2: launchd service lifecycle + setup wizard + TTS main loop integration

Progress: [######....] 60%

## Performance Metrics

**Velocity:**
- Total plans completed: 4
- Average duration: 4.5 min
- Total execution time: 0.30 hours

**By Phase:**

| Phase | Plans | Total | Avg/Plan |
|-------|-------|-------|----------|
| 01-foundation | 2 | 11 min | 5.5 min |
| 02-audio-input-pipeline | 2 | 4 min | 2 min |
| 03-cli-tts-output | 2 | 8 min | 4 min |

**Recent Trend:**
- Last 5 plans: 7 min, 4 min, 2 min, 2 min, 5 min
- Trend: stable fast

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
- [02-02] PTT always bypasses adapter — pastes into focused app regardless of target_mode (no unexpected refocus)
- [02-02] Module-level _adapter state + thread arg passing — consistent with existing is_recording/busy pattern
- [02-02] LastAgentAdapter lazy imports AppKit in daemon thread — avoids load-time failure in non-macOS/test environments
- [02-02] GenericAdapter.should_auto_send() True only when target_app set (pinned-app = AI agent = wants Enter)
- [03-01] sd.play()+sd.wait() (not blocking=True) enables sd.stop() interrupt from another thread
- [03-01] Command file IPC (/tmp/vox-tts-cmd) for cross-process CLI control — consistent with flag-file pattern, no sockets
- [03-01] Single KPipeline singleton (module-level, lazy init) — never create per-call
- [03-01] TTSConfig.enabled=True by default in Phase 3 (was False in Phase 1/2 external-script era)
- [03-02] bootout() guards missing plist — returns "Not running" instead of confusing launchctl error (exit code 5)
- [03-02] sys.executable in plist ProgramArguments — always points to current venv Python, no activation needed
- [03-02] TTS interrupt uses try/except ImportError in main.py — allows running without sounddevice
- [03-02] MCP auto-approve writes to ~/.claude/settings.json mcpServers key with sys.executable for portability
- [03-02] Voice command skip/stop/mute dispatch to native TTS engine directly; tts-next/tts-replay fall through to execute_voice_command

### Pending Todos

None yet.

### Blockers/Concerns

- Package name not finalized -- "vox" taken on PyPI and Homebrew. Candidates: heyvox, voxcode, hotmic, murmur, hark.

## Session Continuity

Last session: 2026-03-27
Stopped at: Completed 03-cli-tts-output 03-02-PLAN.md — Phase 3 complete, ready for Phase 4: MCP Server
Resume file: None
