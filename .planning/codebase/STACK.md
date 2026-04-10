# Technology Stack

**Analysis Date:** 2026-04-10

## Languages

**Primary:**
- Python 3.12+ — All core logic, CLI, MCP server, HUD overlay, daemons
- Bash — Herald TTS pipeline scripts (`heyvox/herald/bin/herald`, `heyvox/herald/lib/*.sh`, `heyvox/herald/hooks/*.sh`, `heyvox/herald/modes/*.sh`)

**Secondary:**
- JavaScript — Hush Chrome extension (`heyvox/hush/extension/background.js`, `heyvox/hush/extension/content.js`, `heyvox/hush/extension/popup.js`)
- AppleScript — Text injection, media control, app focus (`heyvox/input/injection.py`, `heyvox/audio/media.py`)

## Runtime

**Environment:**
- Python 3.12 or 3.13 (classifiers list both)
- macOS only (Apple Silicon required for MLX Whisper; Intel falls back to sherpa-onnx)

**Package Manager:**
- pip with setuptools
- No lockfile present (uses version ranges in `pyproject.toml`)

## Frameworks

**Core:**
- PyObjC (Cocoa + Quartz) >= 10.0 — AppKit HUD overlay, Quartz event tap (PTT), NSWorkspace polling, AXUIElement accessibility API
- MCP SDK >= 1.0 — Model Context Protocol server for agent-initiated voice control
- Pydantic >= 2.0 — Configuration validation and schema (`heyvox/config.py`)

**Audio/ML:**
- openwakeword >= 0.6.0 — Wake word detection (ONNX models, e.g. `hey_jarvis_v0.1`)
- mlx-whisper >= 0.1.0 (optional: `apple-silicon` extra) — STT on Apple Silicon via Metal GPU
- sherpa-onnx >= 1.0 — STT fallback (CPU, int8 quantized Whisper)
- Kokoro >= 0.3.0 (optional: `tts` extra) — TTS voice synthesis
- mlx-community/Kokoro-82M-bf16 — MLX-native TTS model loaded by Kokoro daemon (`heyvox/herald/daemon/kokoro-daemon.py`)
- PyAudio >= 0.2.14 — Microphone stream management (`heyvox/audio/mic.py`)
- sounddevice >= 0.4.0 (optional: `tts` extra) — Audio playback for TTS output
- numpy >= 1.24.0 — Audio buffer manipulation throughout

**Testing:**
- pytest — Test runner
- pytest-asyncio — Async test support
- ruff — Linter

**Build/Dev:**
- setuptools >= 61 + wheel — Build backend (`pyproject.toml` `[build-system]`)

## Key Dependencies

**Critical (core functionality):**
- `openwakeword` >= 0.6.0 — Wake word detection engine, loads ONNX models. Config: `heyvox/config.py` `WakeWordConfig`
- `pyaudio` >= 0.2.14 — Microphone input streams. Uses CoreAudio underneath. `heyvox/audio/mic.py`
- `pyobjc-framework-Cocoa` >= 10.0 — AppKit for HUD overlay (`heyvox/hud/overlay.py`), NSWorkspace for app tracking (`heyvox/adapters/last_agent.py`)
- `pyobjc-framework-Quartz` >= 10.0 — CGEventTap for push-to-talk (`heyvox/input/ptt.py`), CGWindowListCopyWindowInfo for target detection (`heyvox/input/target.py`), media key simulation (`heyvox/audio/media.py`)
- `mcp` >= 1.0 — FastMCP server exposes 4 tools to LLM agents (`heyvox/mcp/server.py`)
- `pydantic` >= 2.0 — Config schema validation with field validators (`heyvox/config.py`)

**Optional extras:**
- `mlx-whisper` >= 0.1.0 (`[apple-silicon]`) — Metal GPU transcription. Lazy-loaded, auto-unloads after 2min idle to free ~855MB. `heyvox/audio/stt.py`
- `kokoro` >= 0.3.0 (`[tts]`) — TTS voice generation. Used by Kokoro daemon (`heyvox/herald/daemon/kokoro-daemon.py`)
- `sounddevice` >= 0.4.0 (`[tts]`) — WAV playback
- `huggingface_hub` >= 0.20.0 (`[tts]`) — Model downloads for Kokoro
- `livekit` >= 1.0.0 (`[aec]`) — WebRTC acoustic echo cancellation
- `websockets` >= 13.0 (`[chrome]`) — Chrome extension WebSocket bridge (`heyvox/chrome/bridge.py`)

**Infrastructure:**
- `psutil` >= 5.9 — Process management, memory watchdog (auto-restart at 1GB RSS)
- `PyYAML` >= 6.0 — Config file parsing (`heyvox/config.py`)
- `platformdirs` >= 4.0 — Cross-platform config directory resolution (with XDG override)
- `rich` >= 13.0 — CLI output formatting

## ML/AI Models

**Wake Word:**
- Default: `hey_jarvis_v0.1` (bundled openwakeword model)
- Custom: `.onnx` files in `~/.config/heyvox/models/` or `heyvox/models/`
- Config: `heyvox/config.py` `WakeWordConfig.start`, `WakeWordConfig.stop`

**Speech-to-Text:**
- MLX Whisper: `mlx-community/whisper-small-mlx` (default, Apple Silicon Metal GPU)
- sherpa-onnx Whisper: `models/sherpa-onnx-whisper-small` (CPU fallback)
- Lazy load/unload: loaded on first use, unloaded after 120s idle to free GPU memory
- Config: `heyvox/config.py` `STTLocalConfig`

**Text-to-Speech:**
- Kokoro MLX: `mlx-community/Kokoro-82M-bf16` (primary, Metal GPU, ~5-10x faster)
- Kokoro ONNX: `~/.kokoro-tts/kokoro-v1.0.onnx` (CPU fallback)
- Piper TTS: lightweight alternative (~80MB vs ~400MB), CPU only
- Config: `heyvox/config.py` `TTSConfig`
- Voices: `af_heart` (default), `af_sarah`, `af_nova`, `af_sky`, etc.
- Languages: English, German, French, Italian, Chinese, Japanese (auto-detected)

## Configuration

**Environment:**
- Config file: `~/.config/heyvox/config.yaml` (XDG preferred) or `~/Library/Application Support/heyvox/config.yaml` (platformdirs fallback)
- Config loading: `heyvox/config.py` `load_config()` — Pydantic validation, YAML parsing, sensible defaults for all fields
- Thread-safe config updates: `heyvox/config.py` `update_config()` — atomic file writes via temp + rename
- `.env` files: Not used. All config via YAML. No cloud API keys needed.

**Key environment variables:**
- `HERALD_HOME` — Path to Herald package root (auto-detected from Python package)
- `KOKORO_IDLE_TIMEOUT` — Kokoro daemon idle timeout in seconds (default: 300)
- `HEYVOX_LOG_FILE` — Override log file path (default: `/tmp/heyvox.log`)
- `CONDUCTOR_WORKSPACE_PATH` / `CONDUCTOR_WORKSPACE_NAME` — Set by Conductor for workspace detection

**Build:**
- `pyproject.toml` — Single config file for project metadata, dependencies, extras, entry points, build system
- Entry points: `heyvox` (CLI), `herald` (TTS CLI), `heyvox-chrome-bridge` (WebSocket bridge)

## Platform Requirements

**Development:**
- macOS (Apple Silicon recommended for full MLX Whisper + Kokoro Metal performance)
- Intel Mac: works with sherpa-onnx STT (CPU) and Piper TTS (CPU)
- Python 3.12+ (uses `X | Y` union syntax, modern typing)
- macOS permissions required: Accessibility (PTT event tap), Microphone, Screen Recording (AX tree reading)

**Production:**
- launchd user agent: `com.heyvox.listener` (`heyvox/setup/launchd.py`)
- Plist location: `~/Library/LaunchAgents/com.heyvox.listener.plist`
- RunAtLoad + KeepAlive (restart on crash, throttle 5s)
- Logs: `/tmp/heyvox.log` (1MB rotation)
- Zero cloud dependency — all audio processing is local

**macOS Version Notes:**
- macOS 26+ renamed CoreAudio property `dout` to `dOut` — runtime detection in `heyvox/audio/output.py`
- Minimum Chrome version for Hush extension: 116 (Manifest V3)

---

*Stack analysis: 2026-04-10*
