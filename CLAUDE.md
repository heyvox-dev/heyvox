# HeyVox — Voice Layer for AI Coding Agents

## Project Profile
- purpose: HeyVox is a macOS voice layer that adds wake-word activation, local speech-to-text, and local text-to-speech to any MCP-compatible AI coding agent (Claude Code, Cursor, Windsurf, Continue.dev). It ships as one PyPI package (`heyvox`) bundling three components: HeyVox Core (wake word, STT, text injection, HUD, MCP server), Herald (TTS orchestration), and Hush (Chrome extension for browser media ducking).
- owner: Franz Felberer (Personal)
- context: Personal product, not just internal tooling — MIT-licensed OSS core (`heyvox` on PyPI, `heyvox-dev` GitHub org, heyvox.dev site) plus a paid Pro tier per the maintainer's own framing; pricing/business docs are deliberately kept untracked (DEF-185) while CLAUDE.md and .planning/ stay public. Run as a full GSD project (.planning/ROADMAP.md, MILESTONES.md, STATE.md, DEFECT-LOG.md).
- tech: Python 3.12+, Bash; PyObjC (AppKit/Quartz) for HUD/menu bar; openwakeword (wake word), MLX Whisper + sherpa-onnx (local STT), Kokoro/mlx-audio (local TTS); MCP Python SDK; pyaudio/sounddevice (audio I/O); launchd (background services); Chrome Manifest V3 extension (Hush, vanilla JS); pytest, ruff; GitHub Actions CI; setuptools packaging published to PyPI.
- current_focus: Post-v1.1.0-launch stabilization (liveness watchdog DEF-213, audio-transport caching DEF-217, HUD overlay desync fix DEF-215; branch heyvox/release-1.1.1, version bumped to 1.1.2) alongside the v1.2 "Paste Injection Reliability" milestone, Phase 16 (STT auto-glossary, wake-word latency, audio cues/sounddevice).
- keywords: heyvox, vox, voice, wake word, hey vox, STT, TTS, speech-to-text, text-to-speech, MCP, HUD, herald, hush, push-to-talk, media control, openwakeword, MLX Whisper, sherpa-onnx, Kokoro, launchd, Chrome extension, microphone, recording indicator
- workflow_mode: gsd-quick
- model: sonnet

## Wrong Workspace Detection
On every message, silently check if the request matches this project's keywords. If it clearly belongs to a different project, say: "This sounds like it's for **[other project]**. Want me to answer here anyway?"

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
│   ├── __init__.py      # Python API: get_herald_home(), start_orchestrator()
│   ├── cli.py           # Python CLI: heyvox.herald.cli speak/pause/resume/...
│   ├── worker.py        # TTS extraction + WAV generation (mood/language/voice)
│   ├── orchestrator.py  # Playback daemon (workspace switching, hold queue)
│   ├── coreaudio.py     # CoreAudio ducking helper
│   ├── daemon/          # kokoro-daemon.py + qwen-daemon.py (persistent TTS)
│   └── hooks/           # Claude Code hook shims (on-response, on-notify, etc.)
└── hush/                # Media control (merged from hush repo)
    ├── __init__.py      # Python API: HUSH_HOME, HUSH_EXTENSION
    ├── extension/       # Chrome Manifest V3 extension
    ├── host/            # Native messaging host (hush_host.py)
    └── scripts/         # install.sh, uninstall.sh, hush-cli.sh
```

### Herald — TTS Pipeline
```
Claude response with <tts> block
  → hooks/on-response.sh (hook shim) → python3 -m heyvox.herald.worker
    → worker.py (extract, mood/language detection, dispatch to engine)
      → kokoro-daemon (Unix socket, Metal GPU) | qwen-daemon | Piper fallback
        → /tmp/herald-queue/ (WAV + .workspace sidecar)
          → orchestrator.py (playback daemon, workspace switching, hold queue)
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
- **Memory watchdog**: Auto-restart at 1GB RSS, MLX Whisper lazy load/unload after idle timeout (`stt.local.unload_secs`, default 300s).
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

**Status (2026-07-23): violations migrated.** Zero `"conductor"` logic branches remain outside the sanctioned places: `adapters/conductor.py` (the dedicated adapter — sole owner of Conductor coupling), the provider registry in `adapters/__init__.py` (one name→implementation entry per app), and the shipped Conductor defaults in `config.py` app_profiles. Workspace detection/resolution goes through the generic `WorkspaceProvider` protocol (`adapters/base.py`: `detect_context` + `resolve`, declared per profile via `workspace_provider`); the DEF-192 AX fast path is per-app via the `ax_value_paste` profile flag. Deprecated-but-working compat shims (documented as such in code): `injection.ax_conductor` global config, `HEYVOX_AX_CONDUCTOR` env, `CONDUCTOR_WORKSPACE_NAME`/`CONDUCTOR_AGENT` Herald env fallbacks. Remaining `conductor` mentions in shared code are explanatory comments only. Keep it that way — new app-specific behavior goes into profiles/providers, never branches.

## Pending
- [ ] Pause/resume recording (Escape pauses, second press resumes) — cancel works, pause/resume state machine not built
- [ ] Evaluate Cohere Transcribe as alternative STT (v2)
- [ ] TTS server on Mac Mini (v2) — no remote TTS code yet
- [ ] Evaluate ripping out the Bluetooth mic-selection machinery entirely (A2DP→HFP wait/trigger, BT gain-boost, BT cooldown tiers in device_manager.py/mic.py/bt.py) now that a single pinned USB/wired default + built-in fallback is the model (2026-07-16) — currently left in place (dormant, not on the selection path) rather than deleted; revisit once the simplified model has run for a while

## Done (previously pending)
- [x] Generic app switching / de-hardcoded Conductor (2026-07-23) — WorkspaceProvider protocol + registry, generic TargetLock workspace_id/session_id, per-app ax_value_paste flag; Herald was already profile-driven
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
