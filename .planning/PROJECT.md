# Vox — Voice Layer for AI Coding Agents

## What This Is

Vox is a macOS voice layer that turns your voice into a first-class input device for AI coding agents. It combines wake word detection, push-to-talk, local STT (MLX Whisper), TTS orchestration (Kokoro), and a HUD overlay — all fully local, zero cloud dependency. It works with any AI coding agent (Claude Code, Cursor, Claude Desktop) via OS-level text injection for voice input and MCP for voice output.

Being decoupled from an existing Conductor-embedded implementation (~90% agent-agnostic already) into a standalone open-source product with a Pro tier.

## Core Value

**One voice layer that works across ALL your AI coding agents — wake word, local STT, local TTS, beautiful HUD — without sending audio to the cloud.**

## Requirements

### Validated

(None yet — ship to validate)

### Active

- [ ] Wake word detection (openwakeword, custom trainable)
- [ ] Push-to-talk (configurable modifier key, fn default)
- [ ] Local STT (MLX Whisper primary, sherpa-onnx fallback)
- [ ] Generic text injection into any focused app (osascript)
- [ ] Recording indicator / HUD overlay (AppKit)
- [ ] Audio cue feedback system
- [ ] Silence timeout + cancellation
- [ ] CLI (`vox start|stop|restart|status|logs|setup`)
- [ ] YAML configuration
- [ ] launchd service management
- [ ] Adapter protocol (Conductor, Cursor, Generic Terminal)
- [ ] MCP voice server (voice_listen, voice_speak, voice_status, voice_queue, voice_config)
- [ ] Homebrew-friendly installation (`pipx install` + `vox setup`)
- [ ] All dependencies declared in pyproject.toml
- [ ] No personal paths, no hardcoded Conductor references

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

**Existing codebase:** `/Users/work/Personal/Source/conductor-and-process/manama/wake-word/` — working voice layer tightly coupled to Conductor. ~2000 lines, ~90% already agent-agnostic. Key coupling: hardcoded `tts-ctl.sh` paths (HIGH — crashes if missing), Conductor bundle ID in recording indicator, `/tmp/claude-ww-recording` IPC flag.

**Competitive landscape (March 2026):**
- Spokenly: free, MCP-based, closest competitor — but no wake word, no TTS, no orchestration
- Cursor 2.0: built-in voice — but Cursor-only
- Agent Voice (VS Code): full-duplex with Copilot — but Azure-dependent, VS Code-only
- Wispr Flow: $30M funded, Cursor integration — but cloud-only
- SuperWhisper: local STT, $849 lifetime — but no agent integration

**Unique position:** Only tool combining wake word + local STT + local TTS + multi-agent orchestration. The "full loop" remains unique.

**Architecture:** Hybrid voice model — voice IN via OS-level (wake word → STT → osascript paste), voice OUT via MCP (`voice_speak`), voice HUD via independent AppKit process + Unix socket IPC.

**Target audience:** Power users with multiple AI agents, developers with RSI, hands-free workflow enthusiasts.

**Business model:** Lifestyle business. OSS core (MIT) + Pro tier ($12/mo or $99/yr). Conservative target: $72K ARR at month 24.

## Constraints

- **Platform**: macOS only (Apple Silicon required for MLX Whisper) — where paying dev audience is
- **Runtime**: Python 3.12+ — existing codebase, PyObjC needs it
- **Privacy**: All audio processing must stay local (zero cloud dependency) — core differentiator
- **Permissions**: Requires Accessibility, Microphone, Screen Recording — macOS permission UX is a critical barrier
- **Dependencies**: portaudio (via brew) required for PyAudio — installation friction point
- **Name**: "Vox" taken on Homebrew/PyPI — need alternative package name (heyvox, voxcode, hotmic, murmur, hark)
- **Solo maintainer**: Keep scope small, charge early, don't overbuild
- **Timeline**: Target v1.0 OSS launch within 8-12 weeks

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Hybrid voice model (OS-level IN, MCP OUT) | MCP has no "inject user message" primitive; voice input requires OS-level text injection. Community validates: CLI for input, MCP for output. | Confirmed |
| MCP server lean (4-5 tools) + CLI commands | MCP tool approval friction is #1 pain point (GH #10801, 28 upvotes). CLI commands (`vox speak`, `vox skip`) for hook integration, MCP for agent-initiated speech. | Confirmed |
| HUD as separate AppKit process | AppKit needs its own run loop; Unix socket IPC more reliable than file flags | Confirmed |
| Echo suppression for speaker mode | Without headset, mic picks up TTS — must mute mic during playback or use echo cancellation | New — v1 |
| USB dongle headset support | macOS Bluetooth has bugs; USB audio dongles are common workaround. Must handle non-default audio devices properly. | New — v1 |
| TTS verbosity configurable | User wants: full/summary/short/skip. Per-message override via MCP param. | New — v1 |
| Volume-modulated recording indicator | Current indicator is static; should pulse/modulate with actual input volume for better feedback | New — v1 |
| Smart target detection | Text injection needs to find the right input field. Config: always-focused / pinned-app / last-agent | New — v1 |
| MIT license for OSS core | Maximum adoption (same as Starship, Zoxide, Atuin) | Confirmed |
| pipx install as v1 distribution | Avoids Homebrew formula complexity; Homebrew tap at 100+ stars | Confirmed |
| macOS-first, no cross-platform | Paying dev audience on Mac; Linux in v2 | Confirmed |
| Package name TBD | "Vox" taken; need to check heyvox/voxcode/hotmic/murmur/hark availability | Pending |

---
*Last updated: 2026-03-26 after initialization*
