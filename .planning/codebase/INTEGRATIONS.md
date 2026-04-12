# External Integrations

**Analysis Date:** 2026-04-10

## MCP Server (Model Context Protocol)

**Purpose:** Expose voice control tools to LLM agents (Claude Code, Cursor, etc.) via stdio JSON-RPC transport.

**Implementation:** `heyvox/mcp/server.py`
- Framework: FastMCP from `mcp.server.fastmcp`
- Transport: stdio (stdout reserved for protocol framing, all logging to stderr)
- Lifespan: starts TTS worker on server startup, shuts down cleanly on exit

**Tools (4 total):**
- `voice_speak(text, verbosity)` — Speak text via Kokoro TTS. Verbosity: full|summary|short|skip
- `voice_status()` — Returns current state (idle|recording|speaking), muted flag, verbosity, style, queue/hold counts, and `style_instruction` for Claude to follow
- `voice_queue(action)` — Manage TTS queue: list|skip|stop|clear|mute|unmute
- `voice_config(action, key, value)` — Get/set voice config: verbosity|muted|style

**Stdout protection pattern:**
```python
# Save original stdout, redirect to stderr during imports
_original_stdout = sys.stdout
sys.stdout = sys.stderr
# ... imports and tool definitions ...
# Restore for FastMCP's stdio transport
sys.stdout = _original_stdout
mcp.run(transport="stdio")
```

**Registration:** `heyvox setup` writes MCP server config to `~/.claude/settings.json` (or equivalent per-agent config). Entry point: `python -m heyvox.mcp.server`.

## Claude Code Hooks (Herald)

**Purpose:** Trigger TTS speech on Claude Code lifecycle events. Herald hooks are shell scripts registered in `~/.claude/settings.json`.

**Implementation:** `heyvox/setup/hooks.py` installs hooks; scripts live in `heyvox/herald/hooks/`

**Hook events:**
| Event | Script | Purpose |
|-------|--------|---------|
| `Stop` | `heyvox/herald/hooks/on-response.sh` | Extract `<tts>` blocks from Claude response, send to Kokoro TTS |
| `Notification` | `heyvox/herald/hooks/on-notify.sh` | Voice warnings for dangerous operations |
| `Stop_session` | `heyvox/herald/hooks/on-session-end.sh` | Cleanup TTS queue on session end |

**Additional hooks (not auto-installed):**
- `heyvox/herald/hooks/on-ambient.sh` — Background ambient sounds
- `heyvox/herald/hooks/on-session-start.sh` — Session startup greeting

**Hook flow (on-response):**
```
Claude response with <tts> block
  → hooks/on-response.sh
    → resolves HERALD_HOME from heyvox Python package
    → exec bash "${HERALD_HOME}/lib/speak.sh"
      → extracts <tts> text, deduplicates
      → lib/worker.sh (mood detection, language detection, Kokoro generation)
        → /tmp/herald-queue/ (WAV + .workspace sidecar)
          → lib/orchestrator.sh (playback, workspace switching, hold queue)
```

**Installation/removal:**
- `heyvox/setup/hooks.py` `install_herald_hooks()` — Adds/updates hooks in `~/.claude/settings.json`
- `heyvox/setup/hooks.py` `uninstall_herald_hooks()` — Removes Herald hooks from settings

## Herald TTS Pipeline (Bash + Python)

**Purpose:** Full TTS orchestration — generation, queuing, playback, workspace awareness.

**Components:**
- `heyvox/herald/bin/herald` — Bash CLI entry point (speak/pause/resume/skip/mute/status/queue)
- `heyvox/herald/lib/speak.sh` — Extract `<tts>` blocks, deduplicate, dispatch to worker
- `heyvox/herald/lib/worker.sh` — Mood/language detection, Kokoro daemon RPC, WAV generation
- `heyvox/herald/lib/orchestrator.sh` — Playback daemon, workspace switching, hold queue management
- `heyvox/herald/lib/config.sh` — Shared bash config (paths, workspace detection via Conductor DB)
- `heyvox/herald/lib/media.sh` — Media pause/resume for bash-layer orchestrator
- `heyvox/herald/modes/*.sh` — Specialized TTS modes (ambient, greeting, notify, recap, cleanup)

**Python CLI wrapper:** `heyvox/herald/cli.py` (entry point: `herald` command)
**Python API:** `heyvox/herald/__init__.py` — `get_herald_home()`, `run_herald()`

## Herald Watcher (Transcript Scanner)

**Purpose:** Races the Stop hook by monitoring Claude Code transcript JSONL files for `<tts>` blocks in near-real-time.

**Implementation:** `heyvox/herald/daemon/watcher.py`
- Watches: `~/.claude/projects/` for JSONL transcript files
- Polls every 0.3s, tracks file positions
- Sends TTS requests to Kokoro daemon via Unix socket
- Deduplication: 3-second window to avoid re-speaking the same text
- Claim directory: `/tmp/herald-claim/` prevents duplicate processing across hook + watcher
- Gets workspace labels from Conductor DB via sqlite3

## Kokoro TTS Daemon

**Purpose:** Persistent TTS process — keeps model warm in GPU memory, accepts requests via Unix socket.

**Implementation:** `heyvox/herald/daemon/kokoro-daemon.py`
- Socket: `/tmp/kokoro-daemon.sock` (Unix domain socket, JSON protocol)
- PID file: `/tmp/kokoro-daemon.pid`
- Engine: mlx-audio (Metal GPU) with kokoro-onnx (CPU) fallback
- Model: `mlx-community/Kokoro-82M-bf16`
- Auto-exits after idle timeout (default 300s, configurable via `KOKORO_IDLE_TIMEOUT` env var)

**Request format:**
```json
{"text": "...", "voice": "af_sarah", "lang": "en-us", "speed": 1.2, "output": "/tmp/out.wav"}
```

**Response format:**
```json
{"ok": true, "duration": 1.23, "parts": 3}
```

**Streaming:** Splits text into sentences, generates first sentence immediately (low latency), writes subsequent parts as `output.part2.wav`, `output.part3.wav`, etc.

**Language mapping:** Full codes (`en-us`, `ja`, `cmn`, `fr-fr`, `it`, `de`) mapped to mlx-audio single-letter codes (`a`, `j`, `z`, `f`, `i`). See `LANG_MAP` in `heyvox/herald/daemon/kokoro-daemon.py`.

## Hush — Chrome Extension + Native Messaging Host

**Purpose:** Reliable browser media pause/resume during TTS playback and recording. Also provides text injection into Chrome tabs.

### Chrome Extension (Manifest V3)

**Location:** `heyvox/hush/extension/`
- `manifest.json` — Manifest V3, permissions: tabs, nativeMessaging, scripting, `<all_urls>`
- `background.js` — Service worker, handles native messaging + tab media state tracking
- `content.js` — Injected into all frames, monitors `<video>` and `<audio>` elements
- `popup.html` / `popup.js` — Extension popup UI
- Minimum Chrome version: 116

### Native Messaging Host

**Location:** `heyvox/hush/host/`
- `hush_host.py` — Bridges Chrome's Native Messaging protocol (4-byte length-prefixed JSON on stdin/stdout) with a Unix domain socket server
- `com.hush.bridge.json` — Chrome native messaging host manifest
- Socket: `/tmp/hush.sock` (Unix domain socket, newline-delimited JSON)
- TCP fallback: `127.0.0.1:9847`
- Log: `/tmp/hush.log` (1MB rotation, 2 backups)

**Wire protocol:**
```
Socket client → host:   {"action": "pause"}\n
Host → Chrome:           {"id": "abc123", "action": "pause"}  (4-byte prefixed)
Chrome → host:           {"id": "abc123", "state": "paused", "tabs": [...]}
Host → client:           {"state": "paused", "tabs": [...]}\n
```

**Actions:** `pause`, `resume` (with `rewindSecs`, `fadeInMs`), `type-text`, `press-enter`, `query`

### Integration Points

Used by:
- `heyvox/audio/media.py` — Tier 1 media pause/resume (highest priority)
- `heyvox/input/injection.py` — Chrome text insertion (tried before osascript fallback)
- `heyvox/herald/lib/media.sh` — Bash-layer media control

## Chrome WebSocket Bridge

**Purpose:** Alternative to native messaging for real-time per-tab media state tracking.

**Implementation:** `heyvox/chrome/bridge.py`
- WebSocket server on `127.0.0.1:9285`
- `ChromeBridge` dataclass — manages client connections and tab states
- Message types: `tab_state`, `tab_closed`, `tab_states` (bulk), `pause`, `play`, `query`
- Entry point: `heyvox-chrome-bridge` CLI command
- Requires: `websockets` >= 13.0 (`[chrome]` extra)

## Unix Socket IPC

**HUD overlay:**
- Socket: `/tmp/heyvox-hud.sock`
- Implementation: `heyvox/hud/ipc.py` — `HUDServer` (receiver in overlay process) + `HUDClient` (sender in main/MCP)
- Protocol: Newline-delimited JSON over Unix domain socket
- Message types:
  - `{"type": "state", "state": "idle|listening|processing|speaking"}`
  - `{"type": "audio_level", "level": 0.0-1.0}`
  - `{"type": "transcript", "text": "..."}`
  - `{"type": "tts_start", "text": "..."}`
  - `{"type": "tts_end"}`
  - `{"type": "queue_update", "count": N}`
  - `{"type": "error", "message": "..."}`
- Graceful degradation: both sides silently ignore when the other is not running

**Kokoro daemon:**
- Socket: `/tmp/kokoro-daemon.sock`
- Protocol: JSON request/response over Unix domain socket
- Used by: Herald worker.sh, Herald watcher.py

**Hush host:**
- Socket: `/tmp/hush.sock`
- Protocol: Newline-delimited JSON
- Used by: `heyvox/audio/media.py`, `heyvox/input/injection.py`, Herald bash scripts

## Flag File IPC

**Coordination via `/tmp/` flag files:**
| File | Writer | Reader | Purpose |
|------|--------|--------|---------|
| `/tmp/heyvox-recording` | `heyvox/main.py` | Herald orchestrator, TTS worker | Pause TTS during recording |
| `/tmp/heyvox-tts-playing` | TTS process | Echo suppression (`heyvox/audio/echo.py`) | Mute mic during TTS |
| `/tmp/heyvox-media-paused-rec` | `heyvox/audio/media.py` | `resume_media()` | Track who paused media |
| `/tmp/herald-media-paused-*` | Herald orchestrator | `resume_media()` | Herald's media pause tracking |
| `/tmp/heyvox-tts-cmd` | CLI (`heyvox skip/mute/quiet`) | TTS worker | Cross-process TTS control |
| `/tmp/heyvox-verbosity` | CLI / MCP | All TTS processes | Shared verbosity state |
| `/tmp/heyvox-tts-style` | MCP `voice_config` | `voice_status()` | TTS style setting |
| `/tmp/heyvox-active-mic` | `heyvox/main.py` | HUD overlay menu bar | Current mic name |
| `/tmp/heyvox-mic-switch` | HUD overlay menu | `heyvox/main.py` | Request mic switch |
| `/tmp/heyvox-hud-position.json` | HUD overlay (drag) | HUD overlay (startup) | Persist pill position |
| `/tmp/herald-queue/` | Herald worker | Herald orchestrator | WAV files + .workspace sidecars |
| `/tmp/herald-playing.pid` | Herald orchestrator | `voice_status()` | Track active playback |
| `/tmp/kokoro-daemon.pid` | Kokoro daemon | Herald scripts | Daemon lifecycle management |
| `/tmp/herald-watcher.pid` | Herald watcher | Herald scripts | Watcher lifecycle management |
| `/tmp/herald-claim/` | Herald hook/watcher | Both | Prevent duplicate TTS processing |

## CoreAudio ctypes Bindings

**Input device management:** `heyvox/audio/mic.py`
- Enumerates CoreAudio input devices via `AudioObjectGetPropertyDataSize` / `AudioObjectGetPropertyData`
- Checks `kAudioDevicePropertyDeviceIsAlive` to filter disconnected Bluetooth devices
- Checks `kAudioDevicePropertyStreams` to confirm input capability
- Uses `CFStringRef` to read device names
- Supports device cooldown (120s) for dead Bluetooth devices

**Output device management:** `heyvox/audio/output.py`
- Lists system output devices, gets/sets default output device
- Runtime detection of macOS 26+ property rename (`dout` → `dOut`)
- Used by HUD overlay menu for speaker/headphone selection
- CoreAudio + CoreFoundation loaded via `ctypes.cdll.LoadLibrary`

## MediaRemote Framework (Private)

**Purpose:** Control native media apps (Spotify, Apple Music, Podcasts) via system-level commands.

**Implementation:** `heyvox/audio/media.py`
- Loads `/System/Library/PrivateFrameworks/MediaRemote.framework/MediaRemote` via ctypes
- Commands: `MRMediaRemoteSendCommand(0=play, 1=pause, None)`
- Detection: `nowplaying-cli get playbackRate` (external CLI tool)
- Thread-safe lazy loading with `_mr_lock`

## macOS Accessibility API (AXUIElement)

**Target snapshot:** `heyvox/input/target.py`
- `AXUIElementCreateApplication(pid)` — Create AX reference for app
- `AXUIElementCopyAttributeValue` — Read AXRole, AXValue, AXChildren, AXFocusedWindow
- Walk AX tree to find focused text field for precise text injection target
- Text input roles: `AXTextField`, `AXTextArea`, `AXWebArea`, `AXComboBox`

**Conductor workspace detection:** `heyvox/input/target.py` `_detect_conductor_workspace()`
- Walks Conductor's AX tree to find branch name (first `AXStaticText` after `AXSplitter`)
- Maps branch → workspace city name via SQLite query on Conductor DB

**Permission check:** `heyvox/setup/permissions.py`
- `ApplicationServices.AXIsProcessTrusted()` — Check Accessibility permission
- Deep-link URLs for macOS System Settings (Accessibility, Microphone, Screen Recording)

**App tracking:** `heyvox/adapters/last_agent.py`
- `NSWorkspace.sharedWorkspace().frontmostApplication()` — Poll frontmost app every second
- Match against configured agent list (Claude, Cursor, Terminal, iTerm2)

## Conductor Integration

**Workspace detection:** `heyvox/input/target.py` `_detect_conductor_workspace()`
1. Read branch name from Conductor's AX tree (requires Screen Recording permission)
2. Query `~/Library/Application Support/com.conductor.app/conductor.db`:
   ```sql
   SELECT directory_name, branch FROM workspaces WHERE state = 'ready'
   ```
3. Match AX branch name to DB branch → return city name (workspace directory_name)

**Workspace switching:** `heyvox/input/target.py` `_switch_conductor_workspace()`
- Calls `~/.local/bin/conductor-switch-workspace` CLI (Hammerspoon-based sidebar click)

**Herald workspace awareness:** `heyvox/herald/daemon/watcher.py`, `heyvox/herald/lib/config.sh`
- WAV files tagged with `.workspace` sidecar files
- Herald hold queue: messages from inactive workspaces held until user switches
- Workspace label lookup from Conductor DB

## osascript / System Events

**Text injection:** `heyvox/input/injection.py`
- Primary: `pbcopy` + `osascript` keystroke `"v" using command down` (clipboard paste)
- Process targeting: `tell process "AppName"` → `set frontmost to true` → keystroke
- Enter presses: `keystroke return` with delay 0.2s between
- App activation: `tell application "AppName" to activate`
- Clipboard verification: re-read after set to confirm

**Browser media control:** `heyvox/audio/media.py`
- Chrome JS execution: `execute front window's active tab javascript "..."`
- Chrome JS availability test: detect "Allow JavaScript from Apple Events" setting
- Browser video state detection: querySelectorAll `video, audio` elements
- Media key simulation via Quartz `NSEvent.otherEventWithType` + `CGEventPost`
- Left-arrow key for rewind: `key code 123` via System Events

**App detection:** `heyvox/input/injection.py` `_get_frontmost_app()`
- `tell application "System Events" to get name of first application process whose frontmost is true`

## launchd Service

**Implementation:** `heyvox/setup/launchd.py`
- Label: `com.heyvox.listener`
- Plist: `~/Library/LaunchAgents/com.heyvox.listener.plist`
- Program: `{sys.executable} -m heyvox.main`
- RunAtLoad: true (starts on login)
- KeepAlive: restart on non-zero exit (SuccessfulExit: false)
- ThrottleInterval: 5 seconds
- Logs: `/tmp/heyvox.log` (both stdout and stderr)
- Operations: `heyvox/setup/launchd.py` — `write_plist()`, `bootstrap()`, `bootout()`, `restart()`
- Uses `launchctl bootstrap gui/{uid}` / `launchctl bootout gui/{uid}` for service management

## Quartz Event Tap (Push-to-Talk)

**Implementation:** `heyvox/input/ptt.py`
- Creates a CGEventTap via `Quartz` (PyObjC) to intercept modifier key events
- Runs CFRunLoop in a background daemon thread
- Supported keys: fn (Globe), right_cmd, right_alt, right_ctrl, right_shift
- Escape key handling: cancel recording, cancel transcription, or skip TTS depending on state
- Requires Accessibility permission (AXIsProcessTrusted)

## Data Storage

**Databases:**
- Conductor SQLite DB (read-only): `~/Library/Application Support/com.conductor.app/conductor.db`
  - Queried for workspace branch → city name mapping
  - Used by: `heyvox/input/target.py`, `heyvox/herald/daemon/watcher.py`, `heyvox/herald/lib/config.sh`

**File Storage:**
- Config: `~/.config/heyvox/config.yaml` (YAML, Pydantic-validated)
- Custom wake word models: `~/.config/heyvox/models/*.onnx`
- TTS queue: `/tmp/herald-queue/` (WAV files, ephemeral)
- Hold queue: `/tmp/herald-hold/` (WAV files for inactive workspaces)
- Debug audio: `/tmp/heyvox-debug/` (raw recordings for STT analysis)
- Transcript history: JSONL format via `heyvox/history.py`
- Audio cues: `heyvox/cues/` (WAV/MP3/AIFF bundled with package)

**Caching:**
- MLX Whisper model: Hugging Face Hub cache (`~/.cache/huggingface/`)
- Kokoro MLX model: Hugging Face Hub cache
- No application-level cache layer

## Monitoring & Observability

**Error Tracking:**
- None (no external error tracking service)

**Logs:**
- Main log: `/tmp/heyvox.log` (1MB rotation via `log_max_bytes`)
- Herald debug: `/tmp/herald-debug.log`
- Hush host: `/tmp/hush.log` (1MB rotation, 2 backups)
- STT debug: `/tmp/heyvox-stt-debug.log`
- Kokoro daemon: stderr (captured by Herald)
- Herald TIMING lines: grep `TIMING` in `/tmp/herald-debug.log` for pipeline latency

**Health monitoring:**
- Dead mic recovery: health check every 15s, auto-restart audio session after 30s silence
- Memory watchdog: auto-restart at 1GB RSS (via psutil)
- MLX Whisper: lazy load/unload after 2min idle
- Kokoro daemon: auto-exit after 5min idle
- Transcription timeout: 30s max to prevent STT hangs

## CI/CD & Deployment

**Hosting:**
- Distribution: pip package (`pip install heyvox` or `pip install heyvox[apple-silicon,tts,chrome]`)
- No cloud hosting — runs entirely locally on macOS

**CI Pipeline:**
- GitHub Actions on `macos-14` (Apple Silicon runner)
- Repository: `github.com/heyvox-dev/heyvox` (planned)

## Environment Configuration

**Required env vars:**
- None — all configuration via `~/.config/heyvox/config.yaml` with sensible defaults

**Optional env vars:**
- `HERALD_HOME` — Override Herald package location
- `KOKORO_IDLE_TIMEOUT` — Kokoro daemon idle timeout (seconds)
- `HEYVOX_LOG_FILE` — Override main log file path
- `CONDUCTOR_WORKSPACE_PATH` / `CONDUCTOR_WORKSPACE_NAME` — Workspace context from Conductor

**Secrets:**
- None — zero cloud dependency, no API keys needed

---

*Integration audit: 2026-04-10*
