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
