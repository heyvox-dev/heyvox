# Codebase Structure

**Analysis Date:** 2026-04-10

## Directory Layout

```
heyvox/                          # Main Python package (monorepo)
├── __init__.py                  # Package init, __version__ = "1.0.0"
├── __main__.py                  # python -m heyvox entry point
├── main.py                      # Main event loop (~1450 lines): wake word, recording, STT, injection
├── cli.py                       # CLI entry point: heyvox [command] (argparse)
├── config.py                    # Pydantic v2 config: HeyvoxConfig + nested models, YAML loading
├── constants.py                 # Shared constants: flag paths, audio defaults, timeouts
├── history.py                   # Transcript history: JSONL persistence, rotation
├── doctor.py                    # System diagnostics (heyvox doctor / heyvox bugreport)
├── audio/                       # Audio processing modules
│   ├── __init__.py
│   ├── mic.py                   # Microphone discovery, priority selection, CoreAudio ctypes
│   ├── wakeword.py              # openwakeword model loading (custom .onnx + built-in)
│   ├── stt.py                   # STT engines: MLX Whisper (Metal GPU) + sherpa-onnx (CPU)
│   ├── tts.py                   # TTS delegation to Herald CLI + voice command dispatch
│   ├── cues.py                  # Audio feedback cues (afplay .aiff files)
│   ├── echo.py                  # Echo suppression: TTS text buffer + optional WebRTC AEC
│   ├── media.py                 # Media pause/resume: Hush, MediaRemote, Chrome JS, media key
│   └── output.py                # CoreAudio output device management (list, get/set default)
├── input/                       # Text input/injection modules
│   ├── __init__.py
│   ├── ptt.py                   # Push-to-talk: Quartz CGEventTap on fn/modifier keys
│   ├── injection.py             # Text injection: osascript clipboard paste, Enter key press
│   └── target.py                # Target snapshot/restore: AXUIElement focus management
├── hud/                         # HUD overlay (separate AppKit process)
│   ├── __init__.py
│   ├── overlay.py               # AppKit window: frosted-glass pill, waveform, menu bar icon
│   └── ipc.py                   # Unix socket IPC: HUDServer + HUDClient
├── mcp/                         # MCP voice server
│   ├── __init__.py
│   └── server.py                # FastMCP server: voice_speak, voice_status, voice_queue, voice_config
├── adapters/                    # Text injection adapters (auto-send behavior)
│   ├── __init__.py
│   ├── base.py                  # AgentAdapter Protocol definition
│   ├── generic.py               # GenericAdapter: paste into focused/pinned app
│   └── last_agent.py            # LastAgentAdapter: tracks last active AI agent app
├── chrome/                      # Chrome companion bridge
│   ├── __init__.py
│   └── bridge.py                # WebSocket server for Chrome extension (port 9285)
├── setup/                       # Installation and setup
│   ├── __init__.py
│   ├── wizard.py                # Interactive setup: permissions, model download, hooks, MCP
│   ├── launchd.py               # launchd plist generation, bootstrap/bootout/status
│   ├── permissions.py           # macOS permission checks (Accessibility, Microphone, etc.)
│   └── hooks.py                 # Herald hooks installer for ~/.claude/settings.json
├── herald/                      # TTS orchestration service (merged from separate repo)
│   ├── __init__.py              # Python API: HERALD_HOME, run_herald()
│   ├── cli.py                   # Python CLI wrapper for Herald bash commands
│   ├── bin/
│   │   └── herald               # Main bash CLI: speak/pause/resume/skip/mute/status/queue
│   ├── lib/
│   │   ├── config.sh            # Shared bash config: paths, dirs, helper functions
│   │   ├── speak.sh             # Extract <tts> from Claude response, launch worker
│   │   ├── worker.sh            # Generate WAV via Kokoro daemon, enqueue for orchestrator
│   │   ├── orchestrator.sh      # Playback daemon: ducking, workspace switch, media control
│   │   └── media.sh             # Media pause/resume helpers for bash scripts
│   ├── daemon/
│   │   ├── kokoro-daemon.py     # Persistent Kokoro TTS process: Unix socket, Metal GPU, streaming
│   │   └── watcher.py           # Transcript file watcher: polls JSONL for <tts> blocks
│   ├── hooks/
│   │   ├── on-response.sh       # Claude Code hook: fires on message end → speak.sh
│   │   ├── on-notify.sh         # Claude Code hook: notification TTS
│   │   ├── on-session-start.sh  # Claude Code hook: greeting TTS
│   │   ├── on-session-end.sh    # Claude Code hook: session end cleanup
│   │   └── on-ambient.sh        # Claude Code hook: ambient mode TTS
│   └── modes/
│       ├── ambient.sh           # Ambient background TTS mode
│       ├── greeting.sh          # Session greeting mode
│       ├── notify.sh            # Notification mode
│       ├── recap.sh             # Recap/summary mode
│       └── cleanup.sh           # Session cleanup mode
├── hush/                        # Browser media control (merged from separate repo)
│   ├── __init__.py              # Python API: HUSH_HOME, HUSH_EXTENSION
│   ├── extension/               # Chrome Manifest V3 extension
│   │   ├── manifest.json
│   │   ├── background.js        # Service worker: native messaging, tab state tracking
│   │   ├── content.js           # Content script: media element detection
│   │   ├── popup.html           # Extension popup UI
│   │   └── popup.js             # Popup logic
│   ├── host/
│   │   ├── hush_host.py         # Native messaging host: stdin/stdout JSON protocol
│   │   └── com.hush.bridge.json # Native messaging manifest
│   ├── scripts/
│   │   ├── install.sh           # Install native messaging host
│   │   ├── uninstall.sh         # Uninstall native messaging host
│   │   └── hush-cli.sh          # CLI for manual pause/resume
│   └── integration/
│       ├── vox-media.py         # Reference: HeyVox media integration
│       ├── herald-media.sh      # Reference: Herald media integration
│       ├── herald-media.patch   # Patch file for Herald integration
│       ├── vox-media.patch      # Patch file for Vox integration
│       └── apply.sh             # Apply integration patches
└── cues/                        # Audio feedback sound files
    ├── listening.aiff           # Wake word detected / recording started
    ├── ok.aiff                  # Recording stopped (wake word mode)
    ├── sending.aiff             # Text sent to AI agent
    └── paused.aiff              # Recording cancelled / too short

training/                        # Wake word model training (not part of pip package)
├── README.md
├── record_samples.py            # Record positive wake word samples
├── record_negatives.py          # Record negative samples (speech, silence)
├── train_model.py               # Train openwakeword model locally
├── test_model.py                # Test trained model
├── colab_hey_vox.py             # Google Colab training script
├── hey_vox_colab.ipynb          # Colab notebook
├── recordings/                  # Positive samples (hey_vox_NNNN_*.wav)
├── negatives/                   # Negative samples (speech_*.wav)
└── personal_recordings.zip      # Archived recordings

pyproject.toml                   # Package metadata, dependencies, console scripts
LICENSE                          # MIT license
CLAUDE.md                        # AI assistant instructions (project context)
.mcp-managed                     # MCP registration marker
```

## Directory Purposes

**`heyvox/audio/`:**
- Purpose: all audio I/O and processing
- Contains: microphone management, wake word detection, STT engines, TTS delegation, audio cues, echo suppression, media control, output device management
- Key files: `mic.py` (device selection), `stt.py` (transcription), `tts.py` (TTS delegation to Herald)

**`heyvox/input/`:**
- Purpose: user input capture and text injection
- Contains: push-to-talk event tap, clipboard-based text injection, target app snapshot/restore
- Key files: `ptt.py` (Quartz event tap), `injection.py` (osascript paste), `target.py` (AXUIElement focus)

**`heyvox/hud/`:**
- Purpose: visual feedback via floating overlay and menu bar icon
- Contains: AppKit overlay process, Unix socket IPC
- Key files: `overlay.py` (full AppKit UI), `ipc.py` (HUDClient/HUDServer)

**`heyvox/mcp/`:**
- Purpose: MCP server for AI agent integration
- Contains: FastMCP server with 4 voice tools
- Key files: `server.py` (complete MCP server implementation)

**`heyvox/adapters/`:**
- Purpose: agent-specific text injection behavior (auto-send vs paste-only)
- Contains: protocol definition and implementations
- Key files: `base.py` (Protocol), `generic.py` (default), `last_agent.py` (multi-agent tracking)

**`heyvox/setup/`:**
- Purpose: first-run setup, launchd management, permission checks
- Contains: interactive wizard, plist generation, hooks installation
- Key files: `wizard.py` (setup flow), `launchd.py` (service management), `hooks.py` (Claude Code hooks)

**`heyvox/herald/`:**
- Purpose: TTS orchestration pipeline (voice output)
- Contains: bash CLI, TTS generation workers, playback orchestrator, Kokoro daemon, transcript watcher, Claude Code hook shims
- Key files: `lib/orchestrator.sh` (playback), `daemon/kokoro-daemon.py` (TTS engine), `lib/config.sh` (shared state)

**`heyvox/hush/`:**
- Purpose: browser media control during TTS/recording
- Contains: Chrome extension, native messaging host, install scripts
- Key files: `extension/background.js` (service worker), `host/hush_host.py` (native messaging)

**`heyvox/chrome/`:**
- Purpose: WebSocket bridge between HeyVox and Chrome extension
- Contains: asyncio WebSocket server for per-tab media state
- Key files: `bridge.py` (ChromeBridge server)

**`training/`:**
- Purpose: custom wake word model training tools
- Contains: sample recording scripts, training scripts, Colab notebook, audio samples
- Not included in pip package

## Key File Locations

**Entry Points:**
- `heyvox/cli.py`: CLI dispatcher (heyvox command)
- `heyvox/main.py`: Main event loop (run() function)
- `heyvox/__main__.py`: `python -m heyvox` entry point
- `heyvox/mcp/server.py`: MCP server (`python -m heyvox.mcp.server`)
- `heyvox/hud/overlay.py`: HUD overlay (`python -m heyvox.hud.overlay`)
- `heyvox/herald/bin/herald`: Herald bash CLI
- `heyvox/herald/hooks/on-response.sh`: Claude Code on-response hook

**Configuration:**
- `heyvox/config.py`: Pydantic config models + YAML loading
- `heyvox/constants.py`: Flag paths, audio defaults, timeouts
- `heyvox/herald/lib/config.sh`: Herald bash config (paths, dirs, helpers)
- `pyproject.toml`: Package metadata, dependencies, console scripts
- Runtime config: `~/.config/heyvox/config.yaml`
- launchd plist: `~/Library/LaunchAgents/com.heyvox.listener.plist`

**Core Logic:**
- `heyvox/main.py`: Recording state machine, STT pipeline, text injection
- `heyvox/audio/stt.py`: MLX Whisper + sherpa-onnx transcription
- `heyvox/audio/tts.py`: Herald TTS delegation, voice commands
- `heyvox/herald/daemon/kokoro-daemon.py`: Kokoro TTS generation (Metal GPU)
- `heyvox/herald/lib/orchestrator.sh`: Audio playback orchestration
- `heyvox/audio/media.py`: 4-tier media pause/resume
- `heyvox/input/target.py`: AXUIElement target snapshot/restore

**Testing:**
- No test directory currently present in the repo
- `training/test_model.py`: wake word model testing (not unit tests)

## Naming Conventions

**Files:**
- Python modules: `snake_case.py` (e.g., `last_agent.py`, `kokoro-daemon.py`)
- Bash scripts: `kebab-case.sh` (e.g., `on-response.sh`, `config.sh`)
- Audio cues: `descriptive.aiff` (e.g., `listening.aiff`, `sending.aiff`)

**Directories:**
- Python packages: `snake_case` (e.g., `audio/`, `input/`, `setup/`)
- Subsystems: `lowercase` (e.g., `herald/`, `hush/`, `chrome/`)

**Constants:**
- Module-level: `UPPER_SNAKE_CASE` (e.g., `RECORDING_FLAG`, `TTS_DEFAULT_VOICE`)
- Private: `_UPPER_SNAKE_CASE` (e.g., `_ZOMBIE_FAIL_THRESHOLD`, `_PID_FILE`)
- Flag file paths: `/tmp/heyvox-*` for HeyVox, `/tmp/herald-*` for Herald, `/tmp/kokoro-*` for Kokoro

## Where to Add New Code

**New Audio Feature (new STT engine, new audio processing):**
- Implementation: `heyvox/audio/` — add new module
- Wire into: `heyvox/main.py` (for recording pipeline) or `heyvox/audio/tts.py` (for output pipeline)
- Config model: add nested Pydantic model in `heyvox/config.py`
- Constants: add flag/path constants in `heyvox/constants.py`

**New MCP Tool:**
- Implementation: `heyvox/mcp/server.py` — add `@mcp.tool()` decorated function
- Keep imports inside the function body (lazy loading for fast MCP startup)

**New CLI Command:**
- Implementation: `heyvox/cli.py` — add `_cmd_newcommand(args)` function + argparse subparser
- Follow existing pattern: lazy imports inside the command function

**New Herald Mode:**
- Implementation: `heyvox/herald/modes/newmode.sh`
- Wire into: `heyvox/herald/lib/config.sh` (if new constants needed)
- Hook: add corresponding `heyvox/herald/hooks/on-newmode.sh` if triggered by Claude Code

**New Adapter (text injection target):**
- Implementation: `heyvox/adapters/newadapter.py` — implement `AgentAdapter` Protocol from `base.py`
- Wire into: `heyvox/main.py` `_build_adapter()` function
- Config: add new `target_mode` value in `HeyvoxConfig.validate_target_mode()`

**New HUD Feature:**
- UI changes: `heyvox/hud/overlay.py` (AppKit/PyObjC)
- New message type: add to `heyvox/hud/ipc.py` protocol docs, send from `heyvox/main.py` via `_hud_send()`

**New Setup Step:**
- Implementation: `heyvox/setup/wizard.py` — add step to `run_setup()` flow
- Permission check: `heyvox/setup/permissions.py`

## Special Directories

**`heyvox/cues/`:**
- Purpose: audio feedback sound files (bundled with pip package)
- Generated: No (hand-created .aiff files)
- Committed: Yes

**`heyvox/herald/`:**
- Purpose: TTS orchestration (merged from standalone herald repo)
- Contains: mix of bash scripts and Python (daemon, watcher)
- Generated: No
- Committed: Yes

**`heyvox/hush/`:**
- Purpose: browser media control (merged from standalone hush repo)
- Contains: Chrome extension + native messaging host
- Generated: No
- Committed: Yes

**`training/`:**
- Purpose: wake word model training data and scripts
- Contains: WAV recordings, training scripts, Colab notebook
- Generated: Partially (WAV files are recorded, models are trained)
- Committed: Yes (but not included in pip package)

**`/tmp/herald-queue/`, `/tmp/herald-hold/`, `/tmp/herald-history/`, `/tmp/herald-claim/`:**
- Purpose: Herald runtime queue directories
- Generated: Yes (created by `herald_ensure_dirs()` at runtime)
- Committed: No (runtime artifacts in /tmp)

**`~/.config/heyvox/`:**
- Purpose: user configuration
- Contains: `config.yaml`, `models/` (custom wake word .onnx files)
- Generated: Yes (by `heyvox setup`)
- Committed: No (user-local)

---

*Structure analysis: 2026-04-10*
