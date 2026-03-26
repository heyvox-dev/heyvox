# Architecture Research: Vox — macOS Voice Layer

## Component Overview

```
┌─────────────────────────────────────────────────────────┐
│                     CLI (vox)                            │
│  start | stop | restart | status | logs | setup          │
└──────────────────────┬──────────────────────────────────┘
                       │ launchctl
┌──────────────────────▼──────────────────────────────────┐
│                  Main Process                            │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐              │
│  │  Audio    │  │  Input   │  │  MCP     │              │
│  │  Pipeline │  │  Layer   │  │  Server  │              │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘              │
│       │              │              │                     │
│  ┌────▼─────────────▼──────────────▼─────┐              │
│  │           Event Loop / Dispatcher      │              │
│  └────────────────┬──────────────────────┘              │
│                   │ Unix socket                          │
└───────────────────┼──────────────────────────────────────┘
                    │
┌───────────────────▼──────────────────────────────────────┐
│              HUD Process (separate)                       │
│  AppKit run loop + NSVisualEffectView                    │
└──────────────────────────────────────────────────────────┘
```

## Component Boundaries

### 1. Audio Pipeline (`vox/audio/`)
Responsibility: All audio I/O — mic capture, wake word, STT, TTS, cues.
Boundary: Receives raw audio frames, emits text transcriptions and state events.

| Module | Input | Output | External Deps |
|--------|-------|--------|---------------|
| `mic.py` | Config (device priority) | Raw PCM frames | pyaudio, portaudio |
| `wakeword.py` | Audio frames | Detection events | openwakeword |
| `stt.py` | Audio frames | Transcribed text | mlx-whisper, sherpa-onnx |
| `tts.py` | Text to speak | Audio playback | Kokoro/sherpa-onnx, afplay |
| `cues.py` | Cue name | Audio playback | afplay, .wav files |

### 2. Input Layer (`vox/input/`)
Responsibility: User input methods and text delivery.

| Module | Input | Output | External Deps |
|--------|-------|--------|---------------|
| `ptt.py` | Quartz events (fn key) | Start/stop signals | PyObjC Quartz |
| `injection.py` | Text + target app | Text pasted into app | osascript, clipboard |

### 3. HUD (`vox/hud/`) — Separate Process
Responsibility: Visual feedback overlay.

| Module | Input | Output | External Deps |
|--------|-------|--------|---------------|
| `overlay.py` | IPC messages | Visual display | PyObjC AppKit |
| `ipc.py` | JSON over Unix socket | Parsed state updates | socket stdlib |

### 4. MCP Server (`vox/mcp/`)
Responsibility: Expose voice capabilities to AI agents via MCP protocol.

| Module | Input | Output | External Deps |
|--------|-------|--------|---------------|
| `server.py` | MCP tool calls (stdio) | Tool results | mcp SDK |

### 5. Adapters (`vox/adapters/`)
Responsibility: Agent-specific text injection.

| Module | Input | Output | External Deps |
|--------|-------|--------|---------------|
| `base.py` | Protocol definition | — | — |
| `generic.py` | Text | Paste into focused app | osascript |
| `conductor.py` | Text + workspace | Paste into Conductor | osascript |
| `cursor.py` | Text | Paste into Cursor | osascript |

### 6. Config (`vox/config.py`) + CLI (`vox/cli.py`)

## Data Flow

### Voice Input (wake word → injection)
```
Mic → Wake Word → STT → Adapter → Target App
         │          │                  │
    HUD: listening  HUD: transcript   HUD: processing
    Cue: start                        Cue: stop
```

### Voice Output (MCP TTS)
```
AI Agent → MCP: voice_speak → TTS Engine → Speaker
                                  │
                             HUD: speaking
                             Check /tmp/vox-recording (pause if user recording)
```

### IPC Messages
```
Main Process → (JSON over /tmp/vox-hud.sock) → HUD Process
  state, audio_level, transcript, tts_start, tts_progress, error
```

## Suggested Build Order

1. **Config + Project Structure** — everything reads config
2. **Audio Pipeline (mic + wake word + STT)** — core voice input
3. **Input Layer (PTT + injection)** — deliver text to apps
4. **Adapters (generic first)** — text reaches the right app
5. **CLI (start/stop/status/setup)** — user control
6. **HUD (overlay + IPC)** — visual feedback
7. **MCP Server** — agent integration

Parallelizable: HUD || adapters, MCP || CLI, audio cues independent.

## Key Architecture Decisions

1. **Separate HUD process**: AppKit requires own NSApplication run loop
2. **Adapter protocol**: Isolates per-app focus/paste/submit quirks
3. **MCP server in main process**: Avoids IPC complexity between two Python processes
4. **Config-driven behavior**: All hardcoded references become config entries
5. **Graceful degradation**: Missing TTS → voice commands become no-ops; no MCP → OS-level injection still works
