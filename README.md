# HeyVox — Voice Coding, not Vibe Coding

> **Beta** — HeyVox is under active development. It works, but expect rough edges. If something breaks, please [open an issue](https://github.com/heyvox-dev/heyvox/issues) — your feedback shapes what gets fixed next. See [heyvox.dev](https://heyvox.dev) for the full picture.

Your AI coding agent doesn't just listen — it talks back. HeyVox adds a voice layer to any MCP-compatible AI coding agent (Claude Code, Cursor, Windsurf, Continue.dev). Wake word detection, speech-to-text, text-to-speech with configurable verbosity, and browser media control — all running locally on your Mac.

## How It Works

**Voice IN** — Speak to your agent:
1. Say the wake word (or hold your push-to-talk key)
2. HeyVox records and transcribes your speech locally (MLX Whisper / sherpa-onnx)
3. Your words are pasted into the agent's input field and sent

**Voice OUT** — Your agent speaks back:
1. Claude Code responses with `<tts>` blocks trigger Herald (TTS orchestration)
2. Herald generates speech locally via Kokoro TTS and plays it
3. System media (YouTube, Spotify) auto-pauses during playback via Hush
4. Alternatively, agents can call `voice_speak()` via MCP

**HUD** — See what's happening:
- Menu bar icon shows current state (idle/recording/transcribing/speaking)
- Frosted-glass pill overlay shows live waveform during recording
- Recent transcript history in dropdown menu

## What's Included

HeyVox is a monorepo with three integrated components:

| Component | What it does |
|-----------|-------------|
| **HeyVox Core** | Wake word detection, push-to-talk, local STT, text injection, HUD overlay, MCP server |
| **Herald** | TTS orchestration — Kokoro speech generation, playback queue, audio ducking, workspace-aware delivery |
| **Hush** | Browser media control — Chrome extension that pauses/resumes YouTube, Spotify Web, etc. |

One install gets everything. One setup command wires it all up.

## Requirements

- macOS 14+ (Apple Silicon recommended for MLX Whisper)
- Python 3.12+
- Microphone access, Accessibility, and Screen Recording permissions
- PortAudio (`brew install portaudio`)

## Install

### From source (recommended for now)

```bash
# Prerequisites
brew install portaudio

# Clone and install
git clone https://github.com/heyvox-dev/heyvox.git
cd heyvox
pip install -e ".[apple-silicon,chrome]"

# Run the setup wizard
heyvox setup
```

### From PyPI (coming soon)

```bash
pip install heyvox
pip install 'heyvox[apple-silicon]'   # Apple Silicon acceleration (MLX Whisper)
pip install 'heyvox[tts]'             # Kokoro TTS support
pip install 'heyvox[full]'            # Everything
```

### What `heyvox setup` does

1. Checks macOS permissions (Accessibility, Microphone, Screen Recording)
2. Installs PortAudio if missing
3. Downloads the Kokoro TTS model (~300 MB)
4. Tests your microphone
5. Creates config at `~/.config/heyvox/config.yaml`
6. Installs launchd service (auto-start at login)
7. Installs Herald TTS hooks for Claude Code (`~/.claude/settings.json`)
8. Registers the MCP voice server with your AI agent
9. Shows setup summary

## Usage

### Core commands

```bash
heyvox start              # Start in foreground
heyvox start --daemon     # Start as background service (launchd)
heyvox stop               # Stop the service
heyvox restart            # Restart the service
heyvox status             # Show status
heyvox logs               # Tail service logs
```

### Voice output (TTS)

```bash
heyvox speak "Hello"      # Speak text via Kokoro TTS
heyvox skip               # Skip current TTS playback
heyvox mute               # Toggle TTS mute

# Herald CLI (advanced TTS control)
herald status              # Queue, hold, playing, muted, paused
herald pause               # Pause TTS playback
herald resume              # Resume TTS playback
herald queue               # Show queued messages
herald skip                # Skip current message
```

### Transcription history

```bash
heyvox history             # Show recent transcriptions
heyvox history -c          # Copy last transcript to clipboard
```

### Browser media control (Hush)

Hush is a Chrome extension that pauses browser media during recording and TTS.

1. Open Chrome → `chrome://extensions/`
2. Enable Developer mode
3. Click "Load unpacked" → select the `heyvox/hush/extension/` folder
4. Run the install script to set up native messaging:
   ```bash
   bash heyvox/hush/scripts/install.sh
   ```

Hush is optional — without it, HeyVox falls back to native MediaRemote for Spotify/Apple Music, or media key simulation.

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

| Agent | Voice IN | Voice OUT (MCP) | Voice OUT (Herald hooks) | Auto-register |
|-------|----------|----------------|-------------------------|---------------|
| Claude Code | Yes | Yes | Yes | Yes |
| Cursor | Yes | Yes | — | Yes |
| Windsurf | Yes | Yes | — | Yes |
| Continue.dev | Yes | Yes | — | Yes |
| Any focused app | Yes | — | — | — |

**Voice IN** works with any app — HeyVox pastes transcribed text into whatever's focused.
**Voice OUT via MCP** — the agent calls `voice_speak()` to talk back.
**Voice OUT via Herald** — Claude Code hooks automatically speak `<tts>` blocks in responses.

## Architecture

```
┌──────────────────────────────────────────────────┐
│  Your AI Agent (Claude Code, Cursor, etc.)       │
│  ┌────────────┐  ┌─────────────────────────┐     │
│  │ MCP Client │  │ Claude hooks (on-response)│    │
│  └─────┬──────┘  └──────────┬──────────────┘     │
└────────┼────────────────────┼────────────────────┘
         │ stdio              │ <tts> blocks
┌────────┼────────────────────┼────────────────────┐
│  HeyVox                    │                     │
│  ┌───────────┐  ┌──────────▼──────────┐          │
│  │ MCP Server│  │ Herald              │          │
│  │ (TTS out) │  │ (Kokoro → queue →   │          │
│  └───────────┘  │  play → duck audio) │          │
│                 └──────────┬──────────┘          │
│  ┌───────────┐             │ pause/resume        │
│  │ Wake Word │  ┌──────────▼──────────┐          │
│  │ (mic in)  │  │ Hush (browser media)│          │
│  └─────┬─────┘  └────────────────────┘          │
│        │                                         │
│  ┌─────▼───────┐  ┌───────────┐                 │
│  │  Local STT  │  │ HUD       │                 │
│  │ (MLX Whisper│  │ (overlay +│                 │
│  │  sherpa-onnx│  │  menu bar)│                 │
│  └─────┬───────┘  └───────────┘                 │
│        │                                         │
│  ┌─────▼───────┐                                │
│  │ Text Inject │                                │
│  │ (osascript) │                                │
│  └─────────────┘                                │
└──────────────────────────────────────────────────┘
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
- **Kokoro TTS** model loads on-demand, auto-unloads after idle timeout
- **Wake word** model is tiny (~5 MB), always loaded
- **Memory watchdog** auto-restarts if RSS exceeds 1 GB

## Platform Support

- **macOS** (Apple Silicon) — fully supported, primary target
- **macOS** (Intel) — supported via sherpa-onnx STT (no MLX)
- **Windows / Linux** — coming soon (core engine is cross-platform Python; only UI and system integration need porting)

## Audio Devices

HeyVox works best with a dedicated microphone. Bluetooth headsets have a fundamental limitation:

| Device Type | Mic Quality | Playback Quality | Recommended |
|-------------|-------------|------------------|-------------|
| 2.4 GHz wireless (USB dongle) | High | High | Best option |
| USB/3.5mm wired headset | High | High | Great option |
| Built-in Mac mic | Good | N/A | Works fine |
| Bluetooth headset (incl. AirPods) | Low | Low | Not recommended |

**Why Bluetooth is problematic**: Bluetooth can either stream high-quality audio (A2DP) or do bidirectional mic+speaker (HFP) — not both. When the mic activates, quality drops to phone-call level. This affects all Bluetooth headsets on macOS, including AirPods.

**Workaround**: Headsets with a 2.4 GHz USB dongle (Logitech G435, SteelSeries Arctis, etc.) bypass Bluetooth entirely and appear as standard USB audio.

## Privacy

HeyVox processes everything locally:
- Wake word detection: openwakeword (on-device)
- Speech-to-text: MLX Whisper or sherpa-onnx (on-device)
- Text-to-speech: Kokoro (on-device)
- No audio is sent to any server, ever

## Development

```bash
# Install with dev extras
pip install -e ".[dev,apple-silicon,chrome]"

# Run tests
pytest tests/ -k "not e2e"

# Lint
ruff check heyvox/ tests/
```

## License

MIT
