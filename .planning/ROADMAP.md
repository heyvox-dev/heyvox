# Roadmap: Vox

## Overview

Vox is a decoupling project: extract a working ~2000-line voice layer from Conductor, restructure it into a standalone package, and add missing capabilities (MCP server, guided setup, HUD improvements). The roadmap follows the natural dependency chain -- config and structure first (everything reads config), then the core audio pipeline, then the control surfaces (CLI, TTS, MCP), and finally the visual layer (HUD). Five phases, compressed for quick depth.

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [x] **Phase 1: Foundation** - Project structure, config system, and decoupling from Conductor ✓ 2026-03-27
- [x] **Phase 2: Audio + Input Pipeline** - Core voice-in path from mic to text injection ✓ 2026-03-27
- [x] **Phase 3: CLI + TTS Output** - User control layer and voice output ✓ 2026-03-27
- [ ] **Phase 4: MCP Server** - Agent integration via MCP protocol
- [ ] **Phase 5: HUD Overlay** - Visual feedback as separate AppKit process

## Phase Details

### Phase 1: Foundation
**Goal**: A standalone, installable Python package with clean config that runs without Conductor
**Depends on**: Nothing (first phase)
**Requirements**: PROJ-01, PROJ-02, PROJ-03, PROJ-04, PROJ-05, DECP-01, DECP-02, DECP-03, DECP-04, DECP-05, DECP-06, CONF-01, CONF-02, CONF-03, CONF-04
**Success Criteria** (what must be TRUE):
  1. `pip install -e .` succeeds and `vox` CLI entry point is registered
  2. Config loads from `~/.config/vox/config.yaml` with sensible defaults; invalid config produces actionable error messages
  3. No Conductor references, personal paths, or hardcoded `tts-ctl.sh` remain in the codebase
  4. Package follows modular structure (`vox/audio/`, `vox/input/`, `vox/hud/`, `vox/mcp/`, `vox/adapters/`)
  5. TTS and voice commands gracefully degrade when optional paths are not configured
**Plans**: 2 plans

Plans:
- [x] 01-01-PLAN.md — Project scaffolding, pyproject.toml, and monolith extraction into modular structure
- [x] 01-02-PLAN.md — Pydantic config system and Conductor decoupling

### Phase 2: Audio + Input Pipeline
**Goal**: User can speak via wake word or push-to-talk and have transcribed text appear in the focused app
**Depends on**: Phase 1
**Requirements**: AUDIO-01, AUDIO-02, AUDIO-03, AUDIO-04, AUDIO-05, AUDIO-06, AUDIO-07, AUDIO-08, AUDIO-09, AUDIO-10, INPT-01, INPT-02, INPT-03, INPT-04, INPT-05, INPT-06
**Success Criteria** (what must be TRUE):
  1. Saying the wake word activates recording; pressing the configured modifier key (fn) activates push-to-talk
  2. Speech is transcribed locally via MLX Whisper and the resulting text appears in the focused application
  3. Silence timeout auto-stops recording; audio cues play on start/stop/cancel
  4. User's clipboard content is preserved after text injection (save/restore around paste)
  5. Mic device priority is respected; USB audio dongles work; Bluetooth A2DP dead-mic auto-falls back to built-in; echo suppression engages when no headset is detected
**Plans**: 2 plans

Plans:
- [x] 02-01-PLAN.md — Headset detection, echo suppression, and silent-mic health check loop
- [x] 02-02-PLAN.md — Adapter protocol wiring and last-agent target tracking

### Phase 3: CLI + TTS Output
**Goal**: User can control Vox via CLI commands and AI agents can produce spoken output
**Depends on**: Phase 2
**Requirements**: CLI-01, CLI-02, CLI-03, CLI-04, CLI-05, CLI-06, TTS-01, TTS-02, TTS-03, TTS-04, TTS-05
**Success Criteria** (what must be TRUE):
  1. `vox start/stop/restart/status/logs` manages the launchd service
  2. `vox setup` walks through permissions, model downloads, and mic testing with progress indication
  3. `vox speak "hello"` produces TTS audio; `vox skip/mute/quiet` controls playback from the terminal
  4. TTS verbosity (full/summary/short/skip) is configurable and can be overridden per-message
  5. TTS pauses immediately when user starts speaking (wake word or PTT detected)
**Plans**: 2 plans

Plans:
- [x] 03-01-PLAN.md — Kokoro TTS engine with queue, verbosity, volume control, and CLI speak/skip/mute/quiet commands
- [x] 03-02-PLAN.md — launchd service management, setup wizard, and TTS interrupt wiring into main loop

### Phase 4: MCP Server
**Goal**: AI agents can discover and use voice capabilities via MCP tools
**Depends on**: Phase 3
**Requirements**: MCP-01, MCP-02, MCP-03, MCP-04, MCP-05, MCP-06, MCP-07
**Success Criteria** (what must be TRUE):
  1. Claude Code (or any MCP client) sees `voice_speak`, `voice_status`, `voice_queue`, `voice_config` tools after connecting
  2. Agent calling `voice_speak` produces audible TTS with the specified verbosity level
  3. All logging goes to stderr; stdout is clean for MCP stdio transport
  4. `vox setup` offers to write MCP server config to Claude Code's allowlist for auto-approval
  5. Server stays lean (4-5 tools) and tool schemas are concise to minimize context window consumption
**Plans**: 2 plans

Plans:
- [ ] 04-01: MCP server implementation and auto-approve setup

### Phase 5: HUD Overlay
**Goal**: User sees a beautiful, always-visible overlay showing voice state, transcription, and TTS progress
**Depends on**: Phase 2 (state events), Phase 3 (TTS events)
**Requirements**: HUD-01, HUD-02, HUD-03, HUD-04, HUD-05, HUD-06, HUD-07, HUD-08
**Success Criteria** (what must be TRUE):
  1. Top-center frosted-glass pill appears across all Spaces and fullscreen apps
  2. Pill expands during active states with correct colors (idle=gray, listening=red, processing=amber, speaking=green)
  3. Recording indicator modulates with input volume; partial transcription appears word-by-word
  4. TTS playback shows progress with pause/skip/stop controls
  5. HUD receives state updates via Unix socket IPC at `/tmp/vox-hud.sock` from the main process
**Plans**: 2 plans

Plans:
- [ ] 05-01: HUD process with AppKit overlay and IPC

## Progress

**Execution Order:**
Phases execute in numeric order: 1 -> 2 -> 3 -> 4 -> 5

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Foundation | 2/2 | ✓ Complete | 2026-03-27 |
| 2. Audio + Input Pipeline | 2/2 | ✓ Complete | 2026-03-27 |
| 3. CLI + TTS Output | 2/2 | ✓ Complete | 2026-03-27 |
| 4. MCP Server | 0/1 | Not started | - |
| 5. HUD Overlay | 0/1 | Not started | - |
