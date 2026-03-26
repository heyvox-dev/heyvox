# Research Summary: Vox — macOS Voice Layer

## Stack

**Validated:** The existing stack (Python 3.12+, PyObjC, openwakeword, MLX Whisper, sherpa-onnx, pyaudio, Kokoro TTS) is solid. No major changes needed.

**Key additions:**
- `mcp` Python SDK for MCP server
- `uv` over `pipx` for distribution (faster, better dependency resolution)
- `ruff` for linting
- All missing deps must be declared in pyproject.toml (mlx-whisper, sherpa-onnx, pyobjc-framework-Cocoa, pyobjc-framework-Quartz)

**Avoid:** sounddevice (adds numpy dep), whisper.cpp (MLX faster on Apple Silicon), pynput (Quartz more reliable), Tkinter/Qt (can't do frosted glass HUD).

## Table Stakes (v1 must-haves)

All already built in existing codebase:
- Push-to-talk, local STT, text injection, silence detection, recording indicator, audio cues, CLI, YAML config, launchd service, mic selection

Need building:
- Guided setup (`vox setup`) with permission checks
- MCP server (voice_listen, voice_speak, voice_status)
- Adapter protocol (generic, Conductor, Cursor)
- Homebrew-friendly install path
- Full dependency declaration

## Differentiators (competitive edge)

1. **Wake word** — no other dev voice tool does this (already built)
2. **MCP server** — bidirectional voice for any MCP-compatible agent (new)
3. **100% local** — zero cloud, works offline
4. **Multi-agent targeting** — one voice layer for all AI tools
5. **HUD overlay** — demo video star, unique visual

## Architecture

- **Hybrid model:** Voice IN = OS-level (wake word → STT → osascript), Voice OUT = MCP (voice_speak)
- **Separate HUD process** (AppKit needs own run loop) + Unix socket IPC
- **MCP server in main process** (wraps existing functions)
- **Adapter protocol** isolates per-app quirks
- **Build order:** Config → Audio Pipeline → Input → Adapters → CLI → HUD → MCP

## Top Pitfalls to Watch

| # | Pitfall | Severity | Phase |
|---|---------|----------|-------|
| P1 | macOS permission hell (Mic + Accessibility + Screen Recording) | CRITICAL | Setup |
| P2 | Bluetooth A2DP → HFP audio quality degradation | HIGH | Audio |
| P3 | Hardcoded tts-ctl.sh paths crash non-Conductor users | HIGH | Decoupling |
| P7 | stdio MCP transport conflicts with stdout logging | MEDIUM | MCP |
| P5 | Model download on first run hangs with no feedback | MEDIUM | Setup |
| P8 | pyaudio/portaudio installation friction | MEDIUM | Distribution |

## v1 Scope Recommendation

Ship with: all existing capabilities (decoupled) + adapter protocol + MCP server basics + guided setup + clean install path.

Defer to v2: TTS engine bundling, native macOS app, multi-agent orchestration, voice command extensions, custom wake word training UI.
