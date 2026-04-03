# HeyVox — Voice Layer for AI Coding Agents

## Project Profile
- **purpose**: macOS voice layer (wake word + local STT + local TTS + HUD + media control) for any AI coding agent via MCP
- **owner**: Franz Felberer
- **context**: Personal product (lifestyle business, OSS core + Pro tier)
- **tech**: Python, Bash, PyObjC (AppKit/Quartz), openwakeword, MLX Whisper, sherpa-onnx, Kokoro TTS, MCP SDK, pyaudio, launchd
- **current_focus**: Monorepo consolidation, public release prep, GitHub Actions CI
- **keywords**: heyvox, vox, voice, wake word, STT, TTS, speech, microphone, MCP, HUD, recording indicator, push-to-talk, herald, hush, media control
- **workflow_mode**: gsd-quick

## Architecture

### Hybrid Voice Model
- **Voice IN**: OS-level (wake word → STT → osascript paste). Works with ANY app.
- **Voice OUT**: Herald TTS orchestration via Claude Code hooks. LLM decides when to speak.
- **Voice HUD**: Independent AppKit process, receives state via Unix socket.
- **Media Control**: Hush Chrome extension pauses/resumes browser media during TTS/recording.

### Monorepo Structure
```
heyvox/
├── main.py              # Entry point, main event loop
├── cli.py               # CLI: heyvox start/stop/status/setup/speak/...
├── config.py            # YAML config loading
├── constants.py         # Shared constants
├── history.py           # Transcript history (JSONL)
├── audio/
│   ├── mic.py           # Microphone management, device priority
│   ├── wakeword.py      # openwakeword integration
│   ├── stt.py           # STT engines (MLX Whisper, sherpa-onnx)
│   ├── tts.py           # TTS worker (Kokoro)
│   ├── cues.py          # Audio feedback (afplay)
│   ├── echo.py          # Echo suppression
│   └── media.py         # Media pause/resume (Hush → MediaRemote → media key)
├── input/
│   ├── ptt.py           # Push-to-talk (Quartz event tap)
│   └── injection.py     # Text injection (osascript, clipboard)
├── hud/
│   ├── overlay.py       # HUD window + menu bar icon (NSStatusItem)
│   └── ipc.py           # Unix socket IPC protocol
├── mcp/
│   └── server.py        # MCP voice server (voice_speak, voice_status, etc.)
├── adapters/
│   ├── base.py          # AgentAdapter protocol
│   ├── generic.py       # Paste-into-focused-app adapter
│   └── last_agent.py    # Track last active agent
├── chrome/
│   └── bridge.py        # WebSocket bridge for Chrome extension
├── setup/
│   ├── wizard.py        # Interactive setup (permissions, model, hooks, MCP)
│   ├── launchd.py       # launchd service management
│   ├── permissions.py   # macOS permission checks
│   └── hooks.py         # Herald hooks installer for ~/.claude/settings.json
├── herald/              # Voice OUTPUT — TTS orchestration (merged from herald repo)
│   ├── __init__.py      # Python API: get_herald_home(), run_herald()
│   ├── cli.py           # Python CLI wrapper → bash
│   ├── bin/herald       # Bash CLI (speak/pause/resume/skip/mute/status/queue)
│   ├── lib/             # config.sh, speak.sh, worker.sh, orchestrator.sh, media.sh
│   ├── daemon/          # kokoro-daemon.py (persistent TTS), watcher.py
│   ├── hooks/           # Claude Code hook shims (on-response, on-notify, etc.)
│   └── modes/           # ambient, greeting, notify, recap, cleanup
└── hush/                # Media control (merged from hush repo)
    ├── __init__.py      # Python API: HUSH_HOME, HUSH_EXTENSION
    ├── extension/       # Chrome Manifest V3 extension
    ├── host/            # Native messaging host (hush_host.py)
    ├── scripts/         # install.sh, uninstall.sh, hush-cli.sh
    └── integration/     # Reference integration files
```

### Herald — TTS Pipeline
```
Claude response with <tts> block
  → hooks/on-response.sh → lib/speak.sh (extract, dedup)
    → lib/worker.sh (mood/language detection, Kokoro generation)
      → /tmp/herald-queue/ (WAV + .workspace sidecar)
        → lib/orchestrator.sh (playback daemon, workspace switching, hold queue)
```

Key features:
- **Kokoro daemon** — persistent TTS process, Unix socket, Metal GPU
- **Multi-part streaming** — first sentence plays while rest generates
- **Audio ducking** — lowers volume during speech, restores after
- **Emotional voice switching** — alert/cheerful/thoughtful → different voices
- **Language detection** — auto-switches German/French/Italian/Chinese/Japanese
- **Hold queue** — messages from inactive workspaces held until user idle
- **Media pause** — via Hush (browser) or MediaRemote (native apps)

### Hush — Browser Media Control
Chrome extension + native messaging host. 3-tier fallback:
1. **Hush socket** (`/tmp/hush.sock`) — browser tabs via Chrome extension
2. **MediaRemote** — native apps (Spotify, Apple Music, Podcasts)
3. **Media key** — keyboard event simulation (blind toggle)

### IPC
- HUD socket: `/tmp/heyvox-hud.sock` (JSON messages)
- Kokoro daemon: `/tmp/kokoro-daemon.sock` (JSON over Unix socket)
- Hush: `/tmp/hush.sock` (newline-delimited JSON)
- Recording flag: `/tmp/heyvox-recording` (coordination with Herald)
- Herald queue: `/tmp/herald-queue/` (WAV files + .workspace sidecars)

## Key Architecture Decisions

- **Hybrid voice model**: Voice IN = OS-level (wake word → STT → osascript), Voice OUT = Herald hooks + MCP.
- **Monorepo**: Herald + Hush merged into heyvox package. One `pip install`, one `heyvox setup`.
- **MCP lean (4-5 tools) + CLI commands**: MCP for agent-initiated speech, CLI for hooks.
- **Echo suppression**: Mute mic during TTS when no headset detected.
- **USB dongle support**: Handle non-default audio devices (common Bluetooth bug workaround).
- **TTS verbosity**: Configurable full/summary/short/skip, per-message override via MCP param.
- **Volume-modulated recording indicator**: Live waveform bars, not static red dot.
- **Smart target detection**: Configurable always-focused / pinned-app / last-agent.
- **Dead mic recovery**: Health check every 15s, auto-restart audio session after 30s of silence.
- **Memory watchdog**: Auto-restart at 1GB RSS, MLX Whisper lazy load/unload after 2min idle.
- **Transcription timeout**: 30s max to prevent STT hangs blocking the pipeline.

## Development Guidelines

- macOS-first (Apple Silicon required for MLX Whisper)
- Python 3.12+
- MIT license for OSS core
- All audio processing stays local (zero cloud dependency)
- Test on macOS permission edge cases (Accessibility, Microphone, Screen Recording)
- All MCP logging to stderr (stdout reserved for stdio transport)
- CI via GitHub Actions on macos-14 (Apple Silicon)

## Pending
- [ ] Volume control — respect system volume, stop auto-increasing
- [ ] Pause/resume recording (Escape pauses, second press resumes)
- [ ] Menu bar state text ("Recording...", "Transcribing...")
- [ ] Train "Hey Vox" custom wake word
- [ ] Evaluate Cohere Transcribe as alternative STT (v2)
- [ ] Landing page on heyvox.dev
- [ ] GitHub repo under heyvox org
- [ ] TTS server on Mac Mini (v2)
- [ ] Generic app switching in Herald (not just Conductor)
- [ ] Hold queue cap enforcement
