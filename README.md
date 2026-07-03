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
heyvox doctor             # Diagnose permissions, models, and dependencies
heyvox commands           # List the built-in voice commands
```

### Voice output (TTS)

```bash
heyvox speak "Hello"      # Speak text via Kokoro TTS
heyvox skip               # Skip current TTS playback
heyvox mute               # Toggle TTS mute
heyvox quiet              # Verbosity → short (first sentence only)
heyvox verbose full       # Get/set verbosity (full | short | skip)

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

### Diagnostics & maintenance

```bash
heyvox doctor              # Permissions, model presence, and dependency check
heyvox debug               # Recent STT recordings + why each was kept/discarded
heyvox log-health          # Daily digest of wake/STT/TTS log signals
heyvox calibrate           # Calibrate mic noise floor + silence threshold
heyvox bugreport           # Bundle logs + config into a GitHub-issue report
```

### MCP registration

```bash
heyvox register            # Register the MCP server with detected AI agents
heyvox register cursor     # ...or just one agent
```

`heyvox setup` also offers to do this. See [MCP Tools](#mcp-tools) below.

### Anonymous telemetry (opt-in, off by default)

```bash
heyvox telemetry           # Show status
heyvox telemetry preview   # Show exactly what would be sent
heyvox telemetry enable    # Opt in   (opt out with: heyvox telemetry disable)
```

Telemetry sends only a version string, a hashed hostname, and five numeric
counters — never transcripts, logs, or paths. See [Privacy](#privacy).

### Vocabulary learning (advanced, opt-in, off by default)

```bash
heyvox learn-vocab         # Improve STT recognition of your recurring terms
```

This sends recent dictation **transcripts** (text, not audio) to the Anthropic
API, so it is off by default and only runs when you invoke it. See [Privacy](#privacy).

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
wake_words:
  start: hey_jarvis_v0.1   # Wake word model (custom "hey_vox" coming soon)

threshold: 0.5
silence_timeout_secs: 5.0

stt:
  backend: local
  local:
    engine: mlx             # mlx (Apple Silicon GPU) or sherpa (CPU)
    mlx_model: mlx-community/whisper-small-mlx

tts:
  enabled: true
  voice: af_heart           # Kokoro voice (af_sarah, af_nova, af_sky, etc.)
  speed: 1.0
  verbosity: full           # full | short (first sentence only) | skip (mute)
  style: detailed           # detailed | concise | technical | casual
  pause_media: true         # Pause YouTube/Spotify during TTS

target_mode: last-agent     # last-agent | pinned | generic
agents:                     # Apps to track for last-agent mode
  - Claude
  - Conductor
  - Cursor
  - Terminal

push_to_talk:
  enabled: true
  key: fn                   # Push-to-talk key

mic_priority:               # Preferred mics (checked in order)
  - Jabra
  - G435
  - MacBook Pro Microphone
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

## Pairs Well With Visual MCPs

HeyVox is at its best when combined with **external MCP servers that emit visual artifacts**. Voice keeps the discussion flowing; visuals anchor it — together they make architecture conversations dramatically easier to follow.

**Example: external Mermaid MCP**

Pair HeyVox with a Mermaid MCP server (any MCP that renders Mermaid diagrams to images or SVG) and your agent gains a synchronized visual side-channel:

1. You discuss a design change out loud — wake word → STT → agent receives the question
2. Your agent narrates the answer via Herald TTS, so your eyes stay on the diagram
3. In parallel, the agent calls the Mermaid MCP to regenerate the diagram with the proposed change
4. You see the delta at a glance — boxes added/removed, edges rerouted — instead of holding every structural change in your head while listening

This pattern generalizes to any external MCP that returns images, SVGs, or diagrams (PlantUML, Excalidraw, mind-map tools, even slide generators). HeyVox owns the conversation channel; the visual MCP owns the artifact. They don't need to know about each other — your AI agent orchestrates both.

> **Why it feels good**: voice-only design reasoning forces you to remember every change as you hear it. Visual-only design reasoning makes you keep typing to drive updates. With both, you can talk *and* look — and a 10-minute design discussion finishes with an up-to-date diagram already in front of you.

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
- **Kokoro TTS** runs on Metal GPU via mlx-audio (~5-10x faster than CPU), auto-unloads after idle timeout
- **Wake word** model is tiny (~5 MB), always loaded
- **Memory watchdog** auto-restarts if RSS exceeds 1 GB

## Platform Support

- **macOS** (Apple Silicon) — fully supported, primary target
- **macOS** (Intel) — supported via sherpa-onnx STT (no MLX)
- **Windows / Linux** — coming soon (core engine is cross-platform Python; only UI and system integration need porting)

## Audio Devices

| Device Type | Mic Quality | Playback Quality | Notes |
|-------------|-------------|------------------|-------|
| 2.4 GHz wireless (USB dongle) | High | High | Best option — bypasses Bluetooth entirely |
| USB/3.5mm wired headset | High | High | Great option |
| Built-in Mac mic | Good | N/A | Works fine for voice input |
| Bluetooth headset (incl. AirPods) | Reduced | Reduced | Works — with automatic mitigations (see below) |

**Bluetooth and HFP mode**: Bluetooth can either stream high-quality audio (A2DP) or do bidirectional mic+speaker (HFP) — not both simultaneously. When the mic activates, audio quality drops to phone-call level (~16 kHz). This is a macOS/Bluetooth limitation, not a HeyVox bug.

**HeyVox mitigations**: Bluetooth headsets work in practice because HeyVox handles the rough edges automatically:
- **Dead device filtering** — CoreAudio queries skip paired-but-disconnected Bluetooth devices
- **Silent mic recovery** — detects when a Bluetooth mic goes silent and auto-switches to an alternative (with exponential backoff)
- **Echo suppression** — TTS echo buffer strips recently spoken text from STT output, preventing feedback loops in speaker mode

**For best results**: Headsets with a 2.4 GHz USB dongle (Logitech G435, SteelSeries Arctis, Jabra) bypass Bluetooth entirely and appear as standard USB audio — no codec switching, no quality drop.

## Privacy

HeyVox processes everything locally by default:
- Wake word detection: openwakeword (on-device)
- Speech-to-text: MLX Whisper or sherpa-onnx (on-device)
- Text-to-speech: Kokoro (on-device)
- **No audio is sent to any server, ever** — under any configuration.

Two optional features are **off by default** and only send non-audio data if you
explicitly enable them: anonymous telemetry (version + hashed hostname + numeric
counters) and `learn-vocab` (sends dictation *transcripts* to the Anthropic API).
Full details in the [privacy policy](docs/privacy.html).

## Development

```bash
# Install with dev extras
pip install -e ".[dev,apple-silicon,chrome]"

# Run tests
pytest tests/ -k "not e2e"

# Lint
ruff check heyvox/ tests/
```

## Troubleshooting

Run `heyvox doctor` first — it checks permissions, model presence, and
dependencies, and points at most of the issues below.

- **`heyvox start` exits immediately / wake word never fires.** Usually a missing
  model or a permission. Re-run `heyvox setup`, then `heyvox doctor`. Grant
  **Microphone**, **Accessibility**, and **Screen Recording** to your terminal in
  System Settings → Privacy & Security.
- **Dictation does nothing on an Intel Mac.** MLX Whisper needs Apple Silicon.
  Set `stt.local.engine: sherpa` in your config (HeyVox now defaults to this on
  Intel automatically).
- **No speech / TTS silent.** Install the TTS extra: `pip install 'heyvox[tts]'`.
  Check it isn't muted (`heyvox mute` toggles) and inspect `herald status`.
- **Wrong microphone or output device.** See [Audio Devices](#audio-devices);
  pick the device in the menu-bar HUD. A closed MacBook lid mutes the built-in
  mic.
- **Something's wrong and you want to report it.** `heyvox bugreport` bundles logs
  and config (redacted) for a GitHub issue; `heyvox logs` tails the live log.

## Uninstall

```bash
bash scripts/uninstall.sh                       # remove HeyVox, keep nothing extra
bash scripts/uninstall.sh --keep-config --yes   # keep ~/.config/heyvox, no prompts
```

This removes the HeyVox virtualenv, CLI symlinks, the launchd agent, the Herald
Claude-Code hooks, and the Hush native-messaging host. Shared system packages
(Homebrew, portaudio, Python) are left alone; config and model caches are removed
unless you pass `--keep-config`. Remove the Hush Chrome extension separately from
`chrome://extensions/`.

## License

MIT
