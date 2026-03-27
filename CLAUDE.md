# Vox — Voice Layer for AI Coding Agents

## Project Profile
- **purpose**: macOS voice layer (wake word + local STT + local TTS + HUD) for any AI coding agent via MCP
- **owner**: Franz Felberer
- **context**: Personal product (lifestyle business, OSS core + Pro tier)
- **tech**: Python, PyObjC (AppKit/Quartz), openwakeword, MLX Whisper, sherpa-onnx, Kokoro TTS, MCP SDK, pyaudio, launchd
- **current_focus**: Decoupling from Conductor, initial project structure, v1 MVP
- **keywords**: vox, voice, wake word, STT, TTS, speech, microphone, MCP, HUD, recording indicator, push-to-talk

## Existing Codebase

The source to decouple from lives at:
`/Users/work/Personal/Source/conductor-and-process/manama/wake-word/`

Key files:
- `wake_word_listener.py` — main loop (wake word, PTT, STT, text injection)
- `recording_indicator.py` — AppKit overlay showing recording state
- `ww` — CLI script (start/stop/restart/status/logs via launchctl)
- `config.yaml` — all user configuration
- `install.sh` — setup script
- `pyproject.toml` — package definition (missing several deps)
- `cues/` — audio feedback files
- `training/` — custom wake word training pipeline
- `models/` — wake word models directory
- `test_diagnostics.py` — diagnostic tests

## Architecture

### Hybrid Voice Model
- **Voice IN**: OS-level (wake word → STT → osascript paste). Works with ANY app.
- **Voice OUT**: MCP tool (`voice_speak`). LLM decides when to speak.
- **Voice HUD**: Independent AppKit process, receives state via Unix socket.

### Target Module Structure
```
vox/
├── __init__.py
├── main.py              # Entry point, main event loop
├── audio/
│   ├── mic.py           # Microphone management, device priority
│   ├── wakeword.py      # openwakeword integration
│   ├── stt.py           # STT engines (MLX Whisper, sherpa-onnx)
│   ├── tts.py           # TTS orchestration (Kokoro)
│   └── cues.py          # Audio feedback (afplay)
├── input/
│   ├── ptt.py           # Push-to-talk (Quartz event tap)
│   └── injection.py     # Text injection (osascript, clipboard)
├── hud/
│   ├── overlay.py       # HUD window (NSVisualEffectView)
│   └── ipc.py           # Unix socket IPC protocol
├── mcp/
│   └── server.py        # MCP voice server (voice_listen, voice_speak, etc.)
├── adapters/
│   ├── base.py          # AgentAdapter protocol
│   ├── generic.py       # Paste-into-focused-app adapter
│   ├── conductor.py     # Conductor-specific adapter
│   └── cursor.py        # Cursor-specific adapter
├── config.py            # YAML config loading
└── cli.py               # CLI entry point (vox start/stop/status/setup)
```

### IPC
- Unix domain socket: `/tmp/vox-hud.sock`
- JSON messages (state, audio_level, transcript, tts_*, queue_update, error)
- File flag: `/tmp/vox-recording` (for TTS coordination)

## Key Architecture Decisions

- **Hybrid voice model**: Voice IN = OS-level (wake word → STT → osascript), Voice OUT = MCP (`voice_speak`). Community-validated.
- **MCP lean (4-5 tools) + CLI commands**: MCP for agent-initiated speech, CLI (`vox speak/skip/mute`) for hooks. Avoids MCP approval friction.
- **Echo suppression**: Mute mic during TTS when no headset detected (speaker mode picks up TTS output).
- **USB dongle support**: Must handle non-default audio devices properly (common Bluetooth bug workaround).
- **TTS verbosity**: Configurable full/summary/short/skip, per-message override via MCP param.
- **Volume-modulated recording indicator**: Live waveform bars, not static red dot.
- **Smart target detection**: Configurable always-focused / pinned-app / last-agent.

## Development Guidelines

- macOS-first (Apple Silicon required for MLX Whisper)
- Python 3.12+
- Keep the existing `config.yaml` format as much as possible
- MIT license for OSS core
- All audio processing must stay local (zero cloud dependency)
- Test on macOS permission edge cases (Accessibility, Microphone, Screen Recording)
- All MCP logging to stderr (stdout reserved for stdio transport)
- Reference requirement IDs (DECP-01, AUDIO-01, etc.) in code comments and commits
