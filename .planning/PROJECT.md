# HeyVox — Voice Layer for AI Coding Agents

## What This Is

HeyVox is a macOS voice layer that turns your voice into a first-class input device for AI coding agents. It's a monorepo combining three components: **HeyVox Core** (wake word, STT, text injection, HUD), **Herald** (TTS orchestration via Kokoro), and **Hush** (browser media control via Chrome extension). All fully local, zero cloud dependency. Works with any AI coding agent (Claude Code, Cursor, Windsurf) via OS-level text injection for voice input and MCP + Claude hooks for voice output.

## Core Value

**One voice layer that works across ALL your AI coding agents — wake word, local STT, local TTS, beautiful HUD — without sending audio to the cloud.**

## Requirements

### Validated

- ✓ Wake word detection (openwakeword, custom trainable) — v1.0
- ✓ Push-to-talk (configurable modifier key, fn default) — v1.0
- ✓ Local STT (MLX Whisper primary, sherpa-onnx fallback) — v1.0
- ✓ Generic text injection into any focused app (osascript) — v1.0
- ✓ Recording indicator / HUD overlay (AppKit frosted-glass pill) — v1.0
- ✓ Audio cue feedback system — v1.0
- ✓ Silence timeout + cancellation — v1.0
- ✓ CLI (`vox start|stop|restart|status|logs|setup`) — v1.0
- ✓ YAML configuration with Pydantic validation — v1.0
- ✓ launchd service management — v1.0
- ✓ Adapter protocol (Generic, LastAgent, Conductor) — v1.0
- ✓ MCP voice server (voice_speak, voice_status, voice_queue, voice_config) — v1.0
- ✓ pipx-installable (`pipx install` + `vox setup`) — v1.0
- ✓ All dependencies declared in pyproject.toml — v1.0
- ✓ No personal paths, no hardcoded Conductor references — v1.0
- ✓ Echo suppression for speaker mode — v1.0
- ✓ USB dongle headset support with fallback — v1.0
- ✓ TTS verbosity configurable (full/summary/short/skip) — v1.0
- ✓ Volume-modulated waveform in HUD — v1.0
- ✓ Smart target detection (always-focused / pinned-app / last-agent) — v1.0
- ✓ HUD state machine (idle/listening/processing/speaking) with colors — v1.0
- ✓ Unix socket IPC between main process and HUD — v1.0
- ✓ All IPC paths consolidated in heyvox/constants.py — v1.1 Phase 8
- ✓ Atomic state file (/tmp/heyvox-state.json) for cross-process coordination — v1.1 Phase 8
- ✓ Queue garbage collection for orphaned Herald files — v1.1 Phase 8
- ✓ main.py decomposed into RecordingStateMachine, DeviceManager, WakeWordProcessor, AppContext — v1.1 Phase 6
- ✓ Herald TTS orchestrator ported from bash to pure Python — v1.1 Phase 7
- ✓ CoreAudio ctypes bindings for volume (no osascript) — v1.1 Phase 7
- ✓ Volume/mute detection cached at 5s TTL — v1.1 Phase 7
- ✓ 114-test pytest suite (pure functions, state machines, IPC, device selection) — v1.1 Phase 9

### Active

## Current Milestone: v1.2 Polish & Reliability

**Goal:** Clean up accumulated rough edges and make HeyVox reliable enough for public release.

**Target features:**
- Tech debt cleanup (shim vars, tts_playing dual-write, stale code)
- Paste/injection reliability & speed (#1 UX pain point)
- UI/UX polish
- Distribution prep (package name resolution, Homebrew formula)
- Test stability (fix 6 stale test failures)

## Current State

**Shipped v1.1** on 2026-04-11 (Architecture Hardening). All 15 requirements satisfied.
**v1.2** in progress (Polish & Reliability). Phase 10 (test-stability) complete — CI test suite stable with intent-based assertions, proper dev deps, and audio marker for CI skipping.

### Out of Scope

- Native macOS .app (SwiftUI) — v2 Pro feature, not v1
- Cross-platform (Linux/Windows) — macOS-first, Linux in v2+
- Multi-agent workspace orchestration — v2 Pro feature
- Built-in TTS engine management — v2 Pro feature
- Voice command extensions — v2 Pro feature
- Smart transcription (context-aware, project vocabulary) — v2 Pro feature
- Meeting transcription — different product category
- Mac App Store distribution — sandboxing blocks Accessibility API
- Cloud STT/TTS — zero cloud is a core differentiator

## Context

**Shipped v1.1** with 15,105 LOC Python across 9 phases (v1.0 + v1.1) over 2 milestones.
Tech stack: Python 3.12+, PyObjC (AppKit/Quartz), openwakeword, MLX Whisper, sherpa-onnx, Kokoro TTS, FastMCP, pyaudio, sounddevice, Pydantic, launchd, CoreAudio ctypes.

**Architecture:** Hybrid voice model — voice IN via OS-level (wake word → STT → osascript paste), voice OUT via MCP (`voice_speak`), voice HUD via independent AppKit process + Unix socket IPC at `/tmp/vox-hud.sock`. Core decomposed into RecordingStateMachine, DeviceManager, WakeWordProcessor modules with shared AppContext. Herald TTS runs pure Python (no bash). IPC uses atomic state file.

**Competitive landscape (March 2026):**
- Spokenly: free, MCP-based — but no wake word, no TTS, no orchestration
- Cursor 2.0: built-in voice — Cursor-only
- Agent Voice (VS Code): full-duplex with Copilot — Azure-dependent, VS Code-only
- Wispr Flow: $30M funded — cloud-only
- SuperWhisper: local STT, $849 lifetime — no agent integration

**Unique position:** Only tool combining wake word + local STT + local TTS + multi-agent orchestration + HUD. The "full loop" remains unique.

**Target audience:** Power users with multiple AI agents, developers with RSI, hands-free workflow enthusiasts.

**Business model:** Lifestyle business. OSS core (MIT) + Pro tier ($12/mo or $99/yr). Conservative target: $72K ARR at month 24.

**Known issues / tech debt:**
- Package name not finalized — "vox" taken on PyPI/Homebrew. Candidates: heyvox, voxcode, hotmic, murmur, hark.
- HUD visual quality needs human testing on macOS display (frosted glass, animations, click-through)
- 7 backward-compat shim vars in main.py (test scaffolding only)
- tts_playing state field dual-write incomplete (old flag file still primary)
- 6 stale test failures in pre-v1.1 tests (injection, media, e2e)
- Homebrew formula not yet created (pipx install works)

## Constraints

- **Platform**: macOS only (Apple Silicon required for MLX Whisper)
- **Runtime**: Python 3.12+
- **Privacy**: All audio processing local (zero cloud)
- **Permissions**: Requires Accessibility, Microphone, Screen Recording
- **Dependencies**: portaudio (via brew) required for PyAudio
- **Name**: "Vox" taken on Homebrew/PyPI — need alternative package name
- **Solo maintainer**: Keep scope small, charge early, don't overbuild

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Hybrid voice model (OS-level IN, MCP OUT) | MCP has no "inject user message" primitive | ✓ Good — clean separation |
| MCP server lean (4-5 tools) + CLI commands | MCP tool approval friction (#10801) | ✓ Good — 4 tools, CLI for hooks |
| HUD as separate AppKit process | AppKit needs its own run loop | ✓ Good — Unix socket IPC works |
| Echo suppression via file flag IPC | TTS runs out-of-process, flag is natural IPC | ✓ Good — simple, reliable |
| Pydantic config with VoxConfig typed object | Type safety, validation, sensible defaults | ✓ Good — all modules use it |
| Parametric functions (config as parameter) | Enables testing, avoids globals | ✓ Good — clean DI pattern |
| setuptools build backend | hatchling not in dev environment | — Acceptable |
| sd.play()+sd.wait() for TTS | Enables sd.stop() interrupt from another thread | ✓ Good — instant interrupt |
| Command file IPC for TTS control | Cross-process CLI control, consistent with flag-file pattern | ✓ Good |
| FastMCP with loguru stdout patch | Prevents MCP stdio corruption | ✓ Good — clean transport |
| performSelectorOnMainThread for HUD dispatch | Lower overhead than NSTimer for high-frequency audio_level | ✓ Good |
| MIT license for OSS core | Maximum adoption | Confirmed |
| pipx install as v1 distribution | Avoids Homebrew formula complexity | Confirmed |
| Package name TBD | "vox" taken; need availability check | ⚠️ Revisit |
| Decompose main.py into modules | 2000-line monolith untestable | ✓ Good — 896 lines, 4 focused modules |
| Port Herald bash→Python | 4 shell boundary crossings per TTS | ✓ Good — pure Python, no subprocess |
| CoreAudio ctypes for volume | osascript spawns shell per call | ✓ Good — direct API, cached 5s |
| Atomic state file for IPC | 25+ flag files with race conditions | ✓ Good — single JSON, temp+rename |
| Dual-write migration for state | Safe rollback if state file has bugs | ✓ Good — old flags still work |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd:transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd:complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-04-12 after Phase 10 (test-stability) complete*
