# Milestones

## v1.0 MVP (Shipped: 2026-03-27)

**Phases completed:** 5 phases, 10 plans
**Lines of code:** 4,280 Python
**Timeline:** 2 days (2026-03-26 → 2026-03-27)

**Key accomplishments:**
1. Standalone Python package with modular structure and Pydantic config — zero Conductor dependency
2. Full audio pipeline: wake word detection, push-to-talk, MLX Whisper STT, echo suppression, headset detection, silent-mic health checks
3. Adapter protocol for multi-agent text injection (Generic, LastAgent, Conductor) with configurable target modes
4. Kokoro TTS engine with queue, verbosity levels (full/summary/short/skip), volume ducking, and CLI commands (speak/skip/mute/quiet)
5. FastMCP server with 4 voice tools (voice_speak, voice_status, voice_queue, voice_config) for AI agent integration
6. Frosted-glass pill HUD overlay with 4-state machine, live waveform, transcript display, TTS controls, and Unix socket IPC

**Delivered:** A complete macOS voice layer for AI coding agents — wake word to spoken response, fully local, works with any MCP-compatible agent.

---

## v2.0 Cross-Platform & Polish (Planned)

**Goal:** Make HeyVox work beyond a single Apple Silicon Mac — server-mode TTS, reliable media control, custom wake word, and cross-platform client support.

**Planned features:**

1. **TTS Server Mode** — Run Kokoro TTS on a powerful Mac (Mini/Studio) and stream audio to clients over WebSocket. Enables Windows/Linux/Intel Mac support without local ML models. Streaming chunks for <250ms first-audio latency on LAN.

2. **MediaRemote Integration** — Replace broken AppleScript+JS media control with `ungive/mediaremote-adapter` (Perl workaround for macOS 15.4+). Enables state-aware pause/resume for Chrome YouTube, Spotify, etc. without any user configuration.

3. **"Hey Vox" Custom Wake Word** — Train personal openwakeword model (75 recordings) and synthetic general model (Coqui TTS pipeline) to replace "hey_jarvis_v0.1".

4. **Companion Chrome Extension** (Vox Pro) — Minimal extension connecting via local WebSocket for high-fidelity per-tab media control. One-time install, no Chrome settings changes.

5. **HUD Improvements** — Show active microphone name in pill, mic mode indicator (standard/voice isolation).

6. **Cross-Platform Client** — Lightweight client (Python or Rust) for Windows/Linux that connects to TTS server, handles local STT via sherpa-onnx (no Apple Silicon required).

7. **Transcript History & Safe Paste** — Store all transcriptions in `~/.vox/transcript_history.json`. CLI `vox history` to review/copy. HUD pill click shows recent list. If no focused text field detected, fallback to clipboard + notification instead of losing text.

---
