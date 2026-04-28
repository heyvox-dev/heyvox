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

### CRITICAL: No app-specific hardcoding

HeyVox is a **generic voice layer** that works with ANY app. Conductor is just one of many possible frontends (others: Cursor, VS Code, Terminal, iTerm2, Claude Desktop, Warp, etc.).

**Rules:**
- **NEVER hardcode app names** like `"conductor"`, `"cursor"`, etc. in logic branches. All app-specific behavior MUST come from config (e.g., `config.yaml` app_profiles or app_delays).
- **NEVER use app-specific keyboard shortcuts** (e.g., Cmd+L for Conductor) inline. Shortcuts must be defined in a configurable app profile: `{ name: "Conductor", focus_shortcut: "l", enter_count: 1 }`.
- **NEVER string-match app names** to decide code paths (`if "conductor" in name.lower()`). Use the app profile system instead.
- **App profiles** define per-app behavior: focus shortcut, enter count, is_electron flag, settle delays. The config ships with sensible defaults for common apps, but any app can be added by the user.
- **The fast injection path** (combined focus + paste + enter in one osascript) must work for ANY app that has a profile, not just Conductor.
- **IPC paths** should be user-scoped (`$TMPDIR` or `~/Library/Caches/heyvox/`) not bare `/tmp/` — avoids multi-user clashes and sandboxing issues.

**Existing violations to fix:** There are ~20 places in the codebase that hardcode `"conductor"` checks. These must be migrated to the app profile system before public release. Search for: `"conductor" in`, `conductor_workspace`, `is_conductor`.

## Pending
- [ ] Pause/resume recording (Escape pauses, second press resumes) — cancel works, pause/resume state machine not built
- [ ] Generic app switching in Herald (not just Conductor) — app profile framework exists, ~15+ hardcoded "conductor" refs remain in injection.py etc.
- [ ] Evaluate Cohere Transcribe as alternative STT (v2)
- [ ] TTS server on Mac Mini (v2) — no remote TTS code yet

## Done (previously pending)
- [x] Volume control — CoreAudio ducking + restore in herald/coreaudio.py + orchestrator.py
- [x] Menu bar state text — NSStatusItem title updates in hud/overlay.py (_STATUS_LABELS)
- [x] Train "Hey Vox" custom wake word — MLP model deployed, conv-attention pipeline + auto-collection in place
- [x] Landing page on heyvox.dev — docs/index.html + CNAME, served via GitHub Pages
- [x] GitHub repo under heyvox org — heyvox-dev/heyvox.git remote configured
- [x] Hold queue cap enforcement — max_queued=10, _enforce_queue_cap() in orchestrator.py

## Defect Log Protocol

Every bug fix, regression, or process gap MUST be logged in `.planning/DEFECT-LOG.md` before committing the fix. This is non-optional — it feeds periodic reviews that improve testing and CI.

### When to log
- Any bug you fix (even trivial ones — patterns emerge from volume)
- Any regression (a bug that was fixed before)
- Any "should have been caught earlier" moment
- Any error handling gap discovered in production

### What to capture
Each entry needs: date, category, severity (S1/S2/S3), symptom, root cause, fix, how it was found, and **what would have caught it earlier** (the most important field — this drives process improvement).

### Categories
`race` | `regression` | `error-handling` | `dead-code` | `platform` | `state-pollution` | `config` | `string-handling` | `timing` | `integration` | `ux`

### Patterns section
When you see 2+ defects with the same root cause pattern, add it to the "Patterns & Process Gaps" section with a concrete action item.
