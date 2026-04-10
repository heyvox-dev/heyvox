# Coding Conventions

**Analysis Date:** 2026-04-10

## Naming Patterns

**Files:**
- Python modules: `snake_case.py` (e.g., `heyvox/audio/mic.py`, `heyvox/input/injection.py`)
- Shell scripts: `kebab-case.sh` or `snake_case.sh` in `heyvox/herald/lib/` (e.g., `config.sh`, `worker.sh`, `speak.sh`)
- Shell entry points: plain name without extension (e.g., `heyvox/herald/bin/herald`)
- Test files: `test_<module>.py` in `tests/` directory (e.g., `tests/test_config.py`)

**Functions:**
- Public: `snake_case` (e.g., `find_best_mic()`, `load_config()`, `transcribe_audio()`)
- Private: `_snake_case` with leading underscore (e.g., `_log()`, `_hud_send()`, `_release_recording_guard()`)
- CLI commands: `_cmd_<verb>` pattern in `heyvox/cli.py` (e.g., `_cmd_start()`, `_cmd_stop()`, `_cmd_status()`)

**Variables:**
- Module-level state: `_snake_case` with leading underscore (e.g., `_state_lock`, `_audio_buffer`, `_hud_client`)
- Constants: `UPPER_SNAKE_CASE` (e.g., `RECORDING_FLAG`, `TTS_PLAYING_MAX_AGE_SECS`, `DEFAULT_SAMPLE_RATE`)
- Private constants: `_UPPER_SNAKE_CASE` (e.g., `_ZOMBIE_FAIL_THRESHOLD`, `_BUSY_TIMEOUT`, `_DEVICE_COOLDOWN_SECS`)

**Types/Classes:**
- PascalCase for Pydantic models and classes (e.g., `HeyvoxConfig`, `WakeWordConfig`, `HUDServer`, `AgentAdapter`)
- Enum values: `UPPER_SNAKE_CASE` (e.g., `Verbosity.FULL`, `Verbosity.SKIP`)

**Shell variables:**
- Constants: `UPPER_SNAKE_CASE` (e.g., `HERALD_QUEUE_DIR`, `KOKORO_DAEMON_SOCK`)
- Local variables: `snake_case` with `local` keyword (e.g., `local size`, `local flag_age`)

## Code Style

**Formatting:**
- Ruff is the sole linter/formatter (configured as dev dependency in `pyproject.toml`)
- No `.ruff.toml` or ruff section in `pyproject.toml` detected — uses ruff defaults
- CI runs `ruff check heyvox/ tests/`

**Linting:**
- Ruff only. No pylint, flake8, black, or isort configured.
- `# noqa: E402` annotations used for imports after sys.stdout redirection in `heyvox/mcp/server.py`

**Type Hints:**
- Use modern Python 3.12+ type syntax: `str | None` (not `Optional[str]`), `list[str]` (not `List[str]`)
- Exception: some older code still uses `from typing import Optional` (e.g., `heyvox/audio/tts.py`)
- Pydantic models use full type annotations on all fields
- Function signatures include type hints on parameters and return values in most modules
- `typing.Protocol` used for adapter interfaces (`heyvox/adapters/base.py`)

## Import Organization

**Order:**
1. Standard library (`os`, `sys`, `time`, `threading`, `subprocess`, `json`, `signal`)
2. Third-party (`numpy`, `pyaudio`, `yaml`, `pydantic`, `mcp`)
3. Local package (`from heyvox.config import ...`, `from heyvox.constants import ...`)

**Path Aliases:**
- None. All imports use absolute paths from the `heyvox` package root.

**Pattern:**
- Constants are imported explicitly: `from heyvox.constants import RECORDING_FLAG, TTS_PLAYING_FLAG`
- Never `from heyvox.constants import *`
- Lazy imports inside functions for heavy dependencies (MLX Whisper, Kokoro TTS) to avoid startup cost

## Module Documentation

**Every module starts with a docstring** containing:
1. Brief description of purpose
2. Key responsibilities or requirements
3. Requirement IDs where applicable (e.g., `Requirement: CONF-01, CONF-02`)

Example pattern from `heyvox/audio/echo.py`:
```python
"""
Echo suppression and acoustic echo cancellation for heyvox.

Provides three layers of echo protection for speaker mode...

Requirements: ECHO-03, ECHO-05, ECHO-06
"""
```

## Section Separators

Use comment blocks with dashes to delineate code sections:
```python
# ---------------------------------------------------------------------------
# Section Name
# ---------------------------------------------------------------------------
```

This pattern is used consistently across `heyvox/main.py`, `heyvox/config.py`, `heyvox/constants.py`, `heyvox/audio/tts.py`, and others. Use it for any section that groups related state, functions, or constants.

## Logging Patterns

**Three distinct logging approaches coexist:**

1. **Custom `log()` function** in `heyvox/main.py` — writes timestamped lines directly to `/tmp/heyvox.log` with file rotation:
   ```python
   def log(msg: str) -> None:
       ts = time.strftime("%H:%M:%S")
       line = f"[{ts}] {msg}"
       # ... rotation + file write
   ```

2. **Custom `_log()` function** in leaf modules — prints to stderr with a prefix tag:
   ```python
   def _log(msg: str) -> None:
       print(f"[injection] {msg}", file=sys.stderr, flush=True)
   ```
   Used in: `heyvox/input/injection.py`, `heyvox/audio/mic.py`, `heyvox/audio/stt.py`, `heyvox/audio/media.py`, `heyvox/input/ptt.py`, `heyvox/input/target.py`

3. **Standard `logging` module** in newer modules:
   ```python
   log = logging.getLogger(__name__)
   ```
   Used in: `heyvox/audio/tts.py`, `heyvox/audio/echo.py`, `heyvox/audio/output.py`

**For new code:** Use `log = logging.getLogger(__name__)` (pattern 3). The stderr `_log()` pattern is legacy.

**Shell logging** via `herald_log()` in `heyvox/herald/lib/config.sh`:
```bash
herald_log() {
  echo "[$(date)] $1" >> "$HERALD_DEBUG_LOG"
  # Rotate at ~2MB
}
```

**MCP server rule:** All logging MUST go to stderr. stdout is reserved for MCP stdio JSON-RPC transport. See the stdout protection block at the top of `heyvox/mcp/server.py`.

## Error Handling Patterns

**Pattern 1: Silent degradation with try/except**
Used throughout for optional features (HUD, media control, Hush socket). The system must never crash due to an optional subsystem failure:
```python
try:
    _hud_client.send(msg)
except Exception as e:
    log(f"[HUD-DBG] Send failed: {e}")
```

**Pattern 2: Fallback chains**
Multi-tier fallback for features with multiple implementations. Example from `heyvox/audio/media.py`:
1. Try Hush socket (Chrome extension)
2. Try MediaRemote (native apps)
3. Try media key simulation (blind toggle)

**Pattern 3: Pydantic validation with `sys.exit(1)`**
Config validation errors are displayed with field paths and expected types, then exit:
```python
except ValidationError as e:
    for err in e.errors():
        loc = " -> ".join(str(p) for p in err["loc"])
        print(f"  Field '{loc}': {err['msg']}", file=sys.stderr)
    sys.exit(1)
```

**Pattern 4: FileNotFoundError guards on flag files**
Always use try/except around flag file operations since they may be cleaned up by other processes:
```python
try:
    os.remove(flag)
except FileNotFoundError:
    pass
```

## Configuration Patterns

**YAML config via Pydantic v2:**
- Config file: `~/.config/heyvox/config.yaml` (preferred) or `~/Library/Application Support/heyvox/config.yaml`
- All fields have defaults — config file is optional
- Nested Pydantic models: `HeyvoxConfig` > `WakeWordConfig`, `STTConfig`, `TTSConfig`, etc.
- `model_config = ConfigDict(extra="ignore")` — unknown fields silently ignored
- Validators use `@field_validator` and `@model_validator` decorators
- Thread-safe updates via `update_config()` with atomic file writes (tempfile + `os.replace`)

**Environment variables:**
- Used in Herald shell scripts with defaults: `HERALD_DUCK_ENABLED="${AUDIO_DUCK_ENABLED:-true}"`
- Python code does NOT read environment variables for config — everything goes through YAML
- Exception: `CONDUCTOR_WORKSPACE_NAME` read by Herald for workspace-aware playback

## IPC Patterns

**Flag files (presence-only sentinels):**
- Empty files created with `open(flag, "w").close()`
- Checked with `os.path.exists(flag)`
- Stale flag detection via `os.path.getmtime()` — flags older than threshold are cleaned up
- Defined in `heyvox/constants.py`, used across processes
- Key flags: `RECORDING_FLAG`, `TTS_PLAYING_FLAG`, `TTS_CMD_FILE`, `VERBOSITY_FILE`

**Unix domain sockets (JSON over newline-delimited protocol):**
- HUD: `/tmp/heyvox-hud.sock` — `heyvox/hud/ipc.py` (`HUDServer` + `HUDClient`)
- Kokoro daemon: `/tmp/kokoro-daemon.sock` — `heyvox/herald/daemon/kokoro-daemon.py`
- Hush: `/tmp/hush.sock` — `heyvox/hush/host/hush_host.py`
- Protocol: JSON object per line, terminated by `\n`

**PID files:**
- Written by daemon processes to `/tmp/` (e.g., `HERALD_ORCH_PID`, `KOKORO_DAEMON_PID`)
- Checked for stale PIDs on startup

**Command files (write-once, read-delete):**
- `TTS_CMD_FILE` (`/tmp/heyvox-tts-cmd`): CLI writes a command, TTS worker reads and deletes

## Common Patterns

**Lazy loading for heavy dependencies:**
```python
# In heyvox/audio/stt.py — MLX Whisper loaded on first use, not at import time
def init_local_stt(config):
    # Only imports mlx_whisper here
```

**Fallback chains (3-tier):**
Used in media control, mic selection, and TTS engine resolution. Always try the best option first, fall back gracefully.

**Health checks with timeout-based recovery:**
- Dead mic detection: reinit after 30s of silence (`_DEAD_MIC_TIMEOUT` in `heyvox/main.py`)
- Zombie stream detection: force reinit after N consecutive failed recordings (`_ZOMBIE_FAIL_THRESHOLD`)
- Memory watchdog: auto-restart at 1GB RSS
- Busy timeout: force-reset after 60s (`_BUSY_TIMEOUT`)

**Thread safety:**
- `threading.Lock()` for shared state (`_state_lock`, `_tts_lock`, `_config_lock`, `_inject_lock`)
- `threading.Event()` for coordination (`_shutdown`, `_cancel_transcription`, `_cancel_requested`)
- Daemon threads for background listeners (HUD server, PTT event tap, last-agent tracker)

**Atomic file writes:**
```python
fd, tmp_path = tempfile.mkstemp(dir=CONFIG_FILE.parent)
os.write(fd, content.encode("utf-8"))
os.close(fd)
os.replace(tmp_path, CONFIG_FILE)
```

## Shell Script Conventions

**Strict mode:** All Herald scripts start with `set -euo pipefail`

**Config sourcing:** Every script sources `config.sh` for shared constants:
```bash
source "${SCRIPT_DIR}/../lib/config.sh"
```

**Variable defaults:** Use `${VAR:-default}` pattern:
```bash
HERALD_DUCK_ENABLED="${AUDIO_DUCK_ENABLED:-true}"
```

**Process management:**
- `nohup ... </dev/null >/dev/null 2>&1 &` for background workers
- PID files for tracking daemon processes
- `pkill -f` patterns for cleanup

**State checks as functions:** Boolean checks as shell functions returning 0/1:
```bash
herald_is_muted() { [ -f "$HERALD_MUTE_FLAG" ] && return 0; return 1; }
herald_is_paused() { ... }
```

## Anti-Patterns Observed

**Inconsistent logging:** Three different logging mechanisms coexist (`log()`, `_log()`, `logging.getLogger()`). New code should use `logging.getLogger(__name__)`.

**Module-level state mutation:** `heyvox/main.py` uses ~20 module-level global variables modified via `global` keyword. This makes testing harder (requires `monkeypatch` on every test). Consider grouping into a state dataclass.

**Hardcoded `/tmp/` paths:** Flag files and sockets use hardcoded `/tmp/` paths in `heyvox/constants.py` and `heyvox/herald/lib/config.sh`. Not configurable without editing constants. The `isolate_flags` test fixture works around this by monkeypatching.

**Mixed `str | None` and `Optional`:** Some modules use modern `str | None` syntax, others use `from typing import Optional`. Prefer `str | None` for consistency.

---

*Convention analysis: 2026-04-10*
