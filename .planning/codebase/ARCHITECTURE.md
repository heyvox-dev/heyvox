# Architecture

**Analysis Date:** 2026-04-10

## Pattern Overview

**Overall:** Event-driven pipeline with subprocess isolation

**Key Characteristics:**
- Hybrid voice model: Voice IN is OS-level (wake word + STT + osascript paste), Voice OUT is hook-driven (Herald TTS orchestration)
- Multi-process: main Python process, HUD overlay subprocess (AppKit), Kokoro daemon (Metal GPU TTS), Herald orchestrator (bash), Herald watcher (Python)
- IPC via Unix domain sockets, flag files, PID files, and filesystem queues
- macOS-native: CoreAudio via ctypes, Quartz event taps, AppKit via PyObjC, launchd service management
- Zero cloud dependency: all audio processing stays local

## Component Overview

**Voice IN Pipeline:**
- Wake word detection (openwakeword) OR push-to-talk (Quartz event tap)
- Audio recording with pre-roll buffer
- Local STT (MLX Whisper on Metal GPU, or sherpa-onnx CPU fallback)
- Text injection via osascript clipboard paste into any focused app

**Voice OUT Pipeline (Herald):**
- Claude Code hooks extract `<tts>` blocks from LLM responses
- Herald watcher races the hook by monitoring transcript JSONL files
- Kokoro daemon generates WAV via mlx-audio (Metal GPU)
- Orchestrator plays WAV queue with audio ducking, workspace switching, media pause

**HUD Overlay:**
- Independent AppKit process with frosted-glass pill window + NSStatusItem menu bar icon
- Receives state updates from main process via Unix socket IPC
- Visual states: idle (gray), listening (red with waveform), processing (amber), speaking (green)

**Media Control (Hush):**
- 3-tier fallback: Chrome extension via Unix socket, MediaRemote framework, media key simulation
- Pauses browser/native media during TTS playback and voice recording
- Tracks pause origin to only resume what HeyVox paused

## Data Flow: Voice IN

```
1. Audio stream (PyAudio, 16kHz, 1280-sample chunks)
   └── main loop reads chunks in while loop
       ├── openwakeword.predict() on each chunk (wake word detection)
       │   └── Score > threshold → start_recording()
       └── PTT event tap (Quartz CGEventTap on background thread)
           └── fn key press → start_recording(ptt=True)

2. start_recording()
   ├── Snapshot target app/text field (AXUIElement via Accessibility API)
   ├── Write /tmp/heyvox-recording flag (signals Herald to pause TTS)
   ├── Preload STT model in background thread
   ├── Pause media (Hush socket → Chrome extension, MediaRemote → native apps)
   ├── Play "listening" audio cue (afplay)
   └── Send HUD state: listening

3. Recording loop (chunks appended to _audio_buffer)
   ├── Silence detection → auto-stop after silence_timeout_secs
   ├── Stop wake word detection → stop_recording()
   └── PTT key release → stop_recording()

4. stop_recording()
   ├── Audio trim: remove wake word from start (~1.5s) and end (~0.5s)
   ├── Energy gate: skip STT if audio < -60 dBFS
   ├── Spawn _send_local() on daemon thread
   └── Send HUD state: processing

5. _send_local() (daemon thread)
   ├── transcribe_audio() → MLX Whisper or sherpa-onnx
   ├── Echo filter: strip recently spoken TTS text from transcription
   ├── Quality filter: discard garbled/hallucinated output
   ├── Wake word stripping: remove "hey jarvis"/"hey vox" variants
   ├── Voice command check: intercept "skip"/"mute"/"quiet" commands
   ├── Save to transcript history (JSONL)
   ├── restore_target() → refocus original app/text field
   ├── type_text() → osascript clipboard paste
   ├── Auto-send Enter (wake word mode only, configurable count)
   └── Resume media, clear recording flag, reset busy state
```

## Data Flow: Voice OUT (Herald)

```
1. Claude response contains <tts>speech text</tts> block

2. Detection (race between two paths):
   a. hooks/on-response.sh (Claude Code hook, fires on message end)
      └── lib/speak.sh → extract <tts>, content-hash dedup via /tmp/herald-claim/
   b. herald/daemon/watcher.py (polls transcript JSONL files every 300ms)
      └── extract_tts() → claim file dedup → send directly to Kokoro

3. lib/worker.sh
   ├── Extract <tts> content + detect mood/language
   ├── Language detection: German/French/Italian/Chinese/Japanese → voice switch
   ├── Mood detection: alert/cheerful/thoughtful → voice personality switch
   ├── Send to Kokoro daemon via /tmp/kokoro-daemon.sock
   └── Write WAV + .workspace sidecar to /tmp/herald-queue/

4. kokoro-daemon.py (persistent process, Metal GPU)
   ├── Receives JSON request on Unix socket
   ├── Multi-part streaming: generates first sentence immediately
   ├── Writes sequential WAV parts (out.wav, out.part2.wav, out.part3.wav)
   └── Auto-exits after 300s idle (IDLE_TIMEOUT)

5. lib/orchestrator.sh (persistent daemon)
   ├── Monitors /tmp/herald-queue/ for new WAV files
   ├── Audio ducking: lowers system volume via osascript
   ├── Media pause: Hush socket / MediaRemote / media key
   ├── Workspace switching: switches Conductor tab if frontmost
   ├── Hold queue: messages from inactive workspaces held in /tmp/herald-hold/
   ├── Plays WAV via afplay
   ├── Grace periods between recordings, TTS, and media resume
   └── Restores volume and resumes media after playback
```

## IPC Topology

**Unix Domain Sockets:**
- `/tmp/heyvox-hud.sock` — main process (HUDClient) → HUD overlay (HUDServer). Newline-delimited JSON. Message types: state, audio_level, transcript, tts_start, tts_end, queue_update, error
- `/tmp/kokoro-daemon.sock` — Herald worker/watcher → Kokoro daemon. JSON request/response. Request: {text, voice, lang, speed, output}. Response: {ok, duration, parts}
- `/tmp/hush.sock` — media.py / orchestrator.sh → Hush native messaging host. Newline-delimited JSON for browser media pause/resume

**Flag Files (presence = state active):**
- `/tmp/heyvox-recording` — recording in progress. Written by main.py start_recording(), read by Herald to pause TTS, removed by _release_recording_guard()
- `/tmp/heyvox-tts-playing` — TTS playback active. Written by TTS process, read by echo suppression to mute mic in speaker mode. Max age: 60s (crash guard)
- `/tmp/herald-mute` — TTS muted globally. Written by CLI/MCP, read by Herald scripts
- `/tmp/herald-pause` — Herald playback paused. Written during recording, read by orchestrator
- `/tmp/herald-ambient` — ambient mode active
- `/tmp/herald-mode` — current Herald mode (narrate/ambient/greeting/etc.)
- `/tmp/heyvox-media-paused-rec` — media was paused by recording (contents: "hush"/"mr"/"chrome-js"/"media-key")
- `/tmp/heyvox-verbosity` — current verbosity level (full/summary/short/skip)
- `/tmp/heyvox-tts-style` — current TTS style (detailed/concise/technical/casual)
- `/tmp/heyvox-tts-cmd` — cross-process TTS command (skip/mute-toggle/quiet/stop)
- `/tmp/heyvox-active-mic` — name of currently selected microphone (read by HUD menu)
- `/tmp/heyvox-mic-switch` — mic switch request from HUD menu (read and deleted by main.py)
- `/tmp/heyvox-hud-position.json` — persisted user-dragged HUD pill position

**PID Files:**
- `/tmp/heyvox.pid` — main process PID (singleton enforcement via flock)
- `/tmp/kokoro-daemon.pid` — Kokoro TTS daemon PID
- `/tmp/herald-orchestrator.pid` — orchestrator daemon PID
- `/tmp/herald-playing.pid` — currently playing afplay PID
- `/tmp/herald-watcher.pid` — watcher daemon PID

**Queue Directories:**
- `/tmp/herald-queue/` — WAV files waiting for playback. Each WAV has a `.workspace` sidecar file identifying the source workspace
- `/tmp/herald-hold/` — WAV files held because they came from an inactive workspace
- `/tmp/herald-history/` — played WAV files (moved after playback)
- `/tmp/herald-claim/` — content-hash dedup claims (prevents hook + watcher from processing the same TTS block twice)
- `/tmp/herald-watcher-handled/` — processed transcript entries (dedup)

**Other Runtime Files:**
- `/tmp/heyvox-heartbeat` — mtime updated every 10s as proof of life (detects SIGKILL deaths)
- `/tmp/herald-debug.log` — Herald pipeline debug log (rotates at 2MB)
- `/tmp/herald-violations.log` — logged when TTS plays during recording (should never happen)
- `/tmp/herald-original-vol` — system volume before audio ducking (for restore)
- `/tmp/herald-last-play` — timestamp of last playback
- `/tmp/herald-workspace` — workspace of last played message
- `/tmp/heyvox-hud-stderr.log` — HUD overlay subprocess stderr

## Process Model

**Main Process (`heyvox.main`):**
- Entry: `heyvox start` (foreground) or `heyvox start --daemon` (launchd)
- Singleton enforcement via PID file + flock
- Runs the main audio loop (wake word detection + recording)
- Spawns all subprocesses and daemon threads
- Managed by launchd as `com.heyvox.listener` (KeepAlive, RunAtLoad)

**HUD Overlay Subprocess (`heyvox.hud.overlay`):**
- Launched by main process via `subprocess.Popen`
- Independent AppKit process (NSApplication run loop)
- Receives state via HUDServer on `/tmp/heyvox-hud.sock`
- Survives main process restarts (killed explicitly on shutdown)
- Has its own stderr log at `/tmp/heyvox-hud-stderr.log`

**Kokoro TTS Daemon (`herald/daemon/kokoro-daemon.py`):**
- Launched by Herald worker.sh on first TTS request
- Persistent process: keeps MLX model warm in GPU memory
- Auto-exits after 300s idle (configurable via KOKORO_IDLE_TIMEOUT)
- Listens on `/tmp/kokoro-daemon.sock` for JSON TTS requests
- Writes multi-part WAV files for streaming playback

**Herald Orchestrator (`herald/lib/orchestrator.sh`):**
- Launched by Herald worker.sh (auto-starts if not running)
- Singleton via PID file + pgrep check
- Monitors `/tmp/herald-queue/` for new WAV files
- Handles audio ducking, workspace switching, media control, hold queue

**Herald Watcher (`herald/daemon/watcher.py`):**
- Optional daemon that polls Claude Code transcript files
- Races the on-response hook for faster TTS delivery
- Content-hash dedup via `/tmp/herald-claim/` directory

## Threading Model

**Main Thread:**
- Audio read loop (blocking PyAudio stream.read)
- Wake word detection (openwakeword.predict)
- Silence detection and recording state machine
- Device hotplug scanning (every 3s)
- Health checks: dead mic detection, memory watchdog, heartbeat
- HUD reconnection attempts

**PTT Event Tap Thread (daemon):**
- Started by `heyvox.input.ptt.start_ptt_listener()`
- Runs Quartz CFRunLoop to capture fn/modifier key events
- Calls start_recording()/stop_recording() callbacks
- Also handles Escape key for cancel actions

**STT Transcription Thread (daemon, per-recording):**
- Spawned by stop_recording() as `threading.Thread(target=_send_local)`
- Runs MLX Whisper transcription (may take 1-30s)
- Handles echo filtering, wake word stripping, text injection
- Sets/clears busy flag under _state_lock

**TTS Worker Thread (daemon):**
- Started by `heyvox.audio.tts.start_worker()`
- Delegates to Herald CLI for TTS generation
- Manages verbosity, mute state, voice commands

**HUD IPC Server Thread (daemon, in overlay process):**
- Runs in the HUD overlay subprocess
- Accepts connections on Unix socket, parses JSON
- Dispatches to AppKit main thread via `performSelectorOnMainThread`

**Media Pause Thread (daemon, per-recording):**
- Spawned by start_recording() for non-blocking media pause
- Calls `heyvox.audio.media.pause_media()` which may block on osascript

## State Management

**Recording State (protected by `_state_lock`):**
- `is_recording: bool` — recording in progress
- `recording_start_time: float` — when recording started
- `busy: bool` — STT transcription in progress (prevents re-entry)
- `_audio_buffer: list` — recorded audio chunks
- `_triggered_by_ptt: bool` — True if PTT, False if wake word
- `_recording_target: TargetSnapshot` — app/text field at recording start
- `_cancel_transcription: threading.Event` — set by Escape during STT

**Device State (main loop):**
- `dev_index: int` — current PyAudio device index
- `headset_mode: bool` — True if headset detected (disables echo suppression)
- `_mic_pinned: bool` — True after manual mic switch (suppresses auto-switch)
- `_zombie_mic_reinit: bool` — True when dead mic detected, triggers reinit
- `_consecutive_failed_recordings: int` — tracks empty recordings for zombie detection
- `_last_good_audio_time: float` — last time real audio was seen (AUDIO-13)

**TTS/Media State (cross-process via flag files):**
- Verbosity: `/tmp/heyvox-verbosity` (full/summary/short/skip)
- Mute: `/tmp/herald-mute` (presence = muted)
- Style: `/tmp/heyvox-tts-style` (detailed/concise/technical/casual)
- Recording flag: `/tmp/heyvox-recording` (presence = recording active)
- TTS playing: `/tmp/heyvox-tts-playing` (presence = TTS output active)
- Media paused by: `/tmp/heyvox-media-paused-rec` (contents = method used)

**Echo Suppression State:**
- `_tts_last_seen: float` — last time TTS flag was active (for grace period)
- TTS echo buffer: deque of (timestamp, text) pairs in `heyvox.audio.echo`
- Grace period: 0.6s after TTS ends before re-enabling wake word
- Speaker mode threshold multiplier: 1.4x higher wake word threshold

## Key Abstractions

**AgentAdapter Protocol:**
- Purpose: controls auto-send behavior (whether Enter is pressed after paste)
- Defined in: `heyvox/adapters/base.py`
- Implementations: `GenericAdapter` (`heyvox/adapters/generic.py`), `LastAgentAdapter` (`heyvox/adapters/last_agent.py`)
- Selected by `config.target_mode`: "always-focused" / "pinned-app" / "last-agent"

**TargetSnapshot:**
- Purpose: captures focused app + text field at recording start for reliable injection
- Defined in: `heyvox/input/target.py`
- Contains: app_name, app_pid, AXUIElement reference, window_title, conductor_workspace
- Used by: `restore_target()` to refocus the correct app/field after transcription

**HeyvoxConfig (Pydantic model):**
- Purpose: typed configuration with validation and defaults
- Defined in: `heyvox/config.py`
- Loaded from: `~/.config/heyvox/config.yaml` (XDG) or `~/Library/Application Support/heyvox/config.yaml` (platformdirs)
- Nested models: WakeWordConfig, STTConfig, TTSConfig, PushToTalkConfig, AudioConfig, EchoSuppressionConfig

**HUDClient/HUDServer:**
- Purpose: Unix socket IPC between main process and HUD overlay
- Defined in: `heyvox/hud/ipc.py`
- Protocol: newline-delimited JSON
- Design: HUDClient silently degrades if HUD not running; auto-reconnects

## Entry Points

**CLI (`heyvox`):**
- Location: `heyvox/cli.py` → registered as `heyvox` console script in `pyproject.toml`
- Subcommands: start, stop, restart, status, setup, logs, speak, skip, mute, quiet, verbose, commands, history, chrome-bridge, debug, doctor, bugreport, register

**Main Process:**
- Location: `heyvox/main.py` → `run()` → `main()`
- Also runnable as: `python -m heyvox.main`
- Invoked by: `heyvox start` (foreground) or launchd plist

**MCP Server:**
- Location: `heyvox/mcp/server.py`
- Run as: `python -m heyvox.mcp.server` (stdio transport)
- Registered with AI agents by `heyvox setup` or `heyvox register`
- Tools: voice_speak, voice_status, voice_queue, voice_config

**Herald CLI:**
- Location: `heyvox/herald/bin/herald` (bash script)
- Also: `heyvox/herald/cli.py` (Python wrapper, registered as `herald` console script)
- Subcommands: speak, pause, resume, skip, mute, status, queue

**Herald Hooks (Claude Code):**
- Location: `heyvox/herald/hooks/on-response.sh`, `on-notify.sh`, `on-session-start.sh`, `on-session-end.sh`, `on-ambient.sh`
- Installed to `~/.claude/hooks/` by `heyvox setup`
- Triggered by Claude Code lifecycle events

**HUD Overlay:**
- Location: `heyvox/hud/overlay.py`
- Run as: `python -m heyvox.hud.overlay [--menu-bar-only]`
- Launched by main.py as subprocess

**launchd Service:**
- Label: `com.heyvox.listener`
- Plist: `~/Library/LaunchAgents/com.heyvox.listener.plist`
- Generated by: `heyvox/setup/launchd.py`

## Error Handling

**Strategy:** Defensive degradation with logging. No component failure should crash the main process.

**Patterns:**
- HUD communication: all sends wrapped in `_hud_send()` which silently no-ops on failure. Auto-reconnects every 1s.
- Audio stream: consecutive IOError counter triggers mic reinit after 3 failures. Device cooldown prevents re-selecting dead Bluetooth devices.
- Zombie stream detection: tracks consecutive empty recordings; forces mic reinit after 2 failures. Also detects uniform-noise streams via coefficient of variation analysis.
- Dead mic timeout: forces reinit after 30s of no real audio (level < 10), even during PTT use.
- STT timeout: 30s max transcription time prevents MLX Whisper hangs from blocking the pipeline.
- MLX model management: lazy-load on first use, unload after 2min idle to free ~855MB GPU memory.
- Memory watchdog: logs warning at 1500MB RSS, checked every 60s.
- Stale flag cleanup: on startup, removes leftover flags from crashed predecessors. Age-based staleness for recording flags (300s max).
- Busy timeout: force-resets busy flag after 60s to recover from stuck transcription threads.
- Singleton enforcement: PID file with flock advisory lock eliminates race conditions between instances.
- TTS crash guard: TTS_PLAYING_FLAG older than 60s is treated as stale (prevents permanent mic mute).

## Cross-Cutting Concerns

**Logging:** Custom `log()` function in `heyvox/main.py` writes timestamped messages to `/tmp/heyvox.log` with 1MB rotation. Herald uses its own `herald_log()` to `/tmp/herald-debug.log` with 2MB rotation. MCP server redirects all stdout to stderr to protect stdio transport framing.

**Configuration:** Pydantic v2 models in `heyvox/config.py`. YAML config at `~/.config/heyvox/config.yaml`. All fields have defaults -- zero-config installation works. Thread-safe atomic updates via temp file + rename.

**Echo Suppression:** Three-layer defense in speaker mode (no headset): (1) mute wake word during TTS + grace period, (2) higher wake word threshold (1.4x multiplier), (3) STT echo text buffer strips recently spoken TTS from transcriptions. Optional WebRTC AEC via livekit.

**Media Control:** Coordinated via flag files. Recording pauses media; TTS pauses media. Only resumes what HeyVox paused (tracked by flag file contents). Grace periods between transitions prevent jarring audio jumps.

---

*Architecture analysis: 2026-04-10*
