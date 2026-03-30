# HeyVox — Voice Coding, not Vibe Coding

Talk to your AI coding agent. Hands-free, fully local, zero cloud.

HeyVox adds a voice layer to any MCP-compatible AI coding agent (Claude Code, Cursor, Windsurf, Continue.dev). Wake word detection, speech-to-text, and text-to-speech — all running locally on your Mac.

## How It Works

**Voice IN** — Speak to your agent:
1. Say the wake word (or hold your push-to-talk key)
2. HeyVox records and transcribes your speech locally (MLX Whisper / sherpa-onnx)
3. Your words are pasted into the agent's input field and sent

**Voice OUT** — Your agent speaks back:
1. The agent calls `voice_speak("Done! Tests passing.")` via MCP
2. HeyVox generates speech locally (Kokoro TTS) and plays it
3. System media (YouTube, Spotify) auto-pauses during playback

**HUD** — See what's happening:
- Frosted-glass pill overlay shows recording state, live waveform, and TTS status
- Visible on all Spaces and fullscreen apps, click-through when not interactive

## Requirements

- macOS 14+ (Apple Silicon recommended for MLX Whisper)
- Python 3.12+
- Microphone access, Accessibility, and Screen Recording permissions

## Install

```bash
# Prerequisite: PortAudio (required by pyaudio for mic access)
brew install portaudio

# Install from PyPI
pip install heyvox

# Or install with Apple Silicon acceleration
pip install 'heyvox[apple-silicon]'

# Or install with TTS support (Kokoro)
pip install 'heyvox[tts]'

# Run the setup wizard
heyvox setup
```

The setup wizard will:
1. Check macOS permissions (Accessibility, Microphone, Screen Recording)
2. Download the Kokoro TTS model (~300 MB)
3. Test your microphone
4. Create a config file at `~/.config/heyvox/config.yaml`
5. Install a launchd service (auto-start at login)
6. Register the MCP server with your AI agent (Claude Code, Cursor, etc.)

## Usage

```bash
# Start (foreground)
heyvox start

# Start as background service
heyvox start --daemon

# Check status
heyvox status

# View logs
heyvox logs

# TTS controls
heyvox speak "Hello world"
heyvox skip          # Skip current TTS
heyvox mute          # Toggle mute

# Re-run setup
heyvox setup
```

## MCP Tools

Once registered, your AI agent gets these voice tools:

| Tool | Description |
|------|-------------|
| `voice_speak(text, verbosity)` | Speak text aloud (full/summary/short/skip) |
| `voice_status()` | Get current state (idle/recording/speaking) |
| `voice_queue(action)` | Manage TTS queue (list/skip/stop/mute/unmute) |
| `voice_config(action, key, value)` | Get or set voice settings at runtime |

## Configuration

Edit `~/.config/heyvox/config.yaml`:

```yaml
wake_word:
  model: hey_jarvis       # or custom model path
  threshold: 0.5

stt:
  backend: local           # local (MLX Whisper) or sherpa
  language: en

tts:
  voice: af_sarah
  speed: 1.2
  pause_media: true        # Pause YouTube/Spotify during TTS

input:
  mode: last_agent         # last_agent | pinned | generic
  agents:                  # Apps to track for last_agent mode
    - Claude
    - Cursor
    - Windsurf

ptt:
  enabled: true
  key: right_option        # Push-to-talk key
```

## Supported Agents

HeyVox works with any app that supports MCP (Model Context Protocol):

| Agent | Voice IN | Voice OUT (MCP) | Auto-register |
|-------|----------|----------------|---------------|
| Claude Code | Yes | Yes | Yes |
| Cursor | Yes | Yes | Yes |
| Windsurf | Yes | Yes | Yes |
| Continue.dev | Yes | Yes | Yes |
| Any focused app | Yes | — | — |

**Voice IN** works with any app — HeyVox pastes transcribed text into whatever's focused.
**Voice OUT** requires MCP support — the agent calls `voice_speak()` to talk back.

## Architecture

```
┌──────────────────────────────────────────────┐
│  Your AI Agent (Claude Code, Cursor, etc.)   │
│  ┌────────────┐                              │
│  │ MCP Client │◄── voice_speak(), status()   │
│  └─────┬──────┘                              │
└────────┼─────────────────────────────────────┘
         │ stdio / JSON-RPC
┌────────┼─────────────────────────────────────┐
│  HeyVox   ▼                                     │
│  ┌───────────┐  ┌──────────┐  ┌───────────┐ │
│  │ MCP Server│  │ Wake Word│  │ HUD       │ │
│  │ (TTS out) │  │ (mic in) │  │ (overlay) │ │
│  └───────────┘  └────┬─────┘  └───────────┘ │
│                      │                       │
│              ┌───────▼───────┐               │
│              │  Local STT    │               │
│              │ (MLX Whisper) │               │
│              └───────┬───────┘               │
│                      │                       │
│              ┌───────▼───────┐               │
│              │  Text Inject  │               │
│              │  (osascript)  │               │
│              └───────────────┘               │
└──────────────────────────────────────────────┘
         All processing runs locally.
         No audio leaves your machine.
```

## System Requirements

| Resource | Idle | Active (recording + TTS) |
|----------|------|--------------------------|
| RAM | ~150 MB | ~900 MB peak |
| GPU (Metal) | — | ~500 MB (Whisper STT) |
| Disk | ~800 MB (models) | — |
| CPU | <1% | ~15% during STT |

- **Whisper STT** runs on Apple Silicon GPU via MLX (not system RAM)
- **Kokoro TTS** model loads on-demand, auto-unloads after 5 min idle
- **Wake word** model is tiny (~5 MB), always loaded

## Platform Support

- **macOS** (Apple Silicon) — fully supported, primary target
- **macOS** (Intel) — supported via sherpa-onnx STT (no MLX)
- **Windows / Linux** — coming soon (core engine is cross-platform Python; only UI and system integration need porting)

## Audio Devices

HeyVox works best with a dedicated microphone. Bluetooth headsets have a fundamental limitation that affects voice coding:

| Device Type | Mic Quality | Playback Quality | Recommended |
|-------------|-------------|------------------|-------------|
| 2.4 GHz wireless (USB dongle) | High | High | Best option |
| USB/3.5mm wired headset | High | High | Great option |
| Built-in Mac mic | Good | N/A | Works fine |
| Bluetooth headset (incl. AirPods) | Low | Low | Not recommended |

**Why Bluetooth is problematic**: Bluetooth can either stream high-quality audio to your ears (A2DP profile) or do bidirectional audio with a microphone (HFP profile) — but not both simultaneously. When the mic activates, audio quality drops to phone-call level in both directions. This affects all Bluetooth headsets on macOS, including AirPods — Apple's H2 chip improves HFP quality slightly but cannot bypass the Bluetooth protocol limitation.

**Workaround**: Headsets with a proprietary 2.4 GHz USB dongle (Logitech G435, SteelSeries Arctis, etc.) bypass Bluetooth entirely. The dongle appears as a standard USB audio device with full-quality bidirectional audio. This is currently the most reliable option for wireless voice coding.

**Future**: Bluetooth LE Audio (LC3 codec) is designed to support high-quality bidirectional audio. Apple has partial support in iOS; full macOS support is expected in a future release.

## Privacy

HeyVox processes everything locally:
- Wake word detection: openwakeword (on-device)
- Speech-to-text: MLX Whisper or sherpa-onnx (on-device)
- Text-to-speech: Kokoro (on-device)
- No audio is sent to any server, ever

## License

MIT
