# Testing Patterns

**Analysis Date:** 2026-04-10

## Test Framework

**Runner:**
- pytest (version unspecified, from `[dev]` extras)
- pytest-asyncio (for async Chrome bridge tests)
- Config: No `pytest.ini` or `pyproject.toml` `[tool.pytest]` section detected — uses pytest defaults

**Assertion Library:**
- pytest built-in assertions
- `pytest.approx()` for floating-point comparisons
- `pytest.raises()` for exception testing
- `pytest.mark.skipif` for conditional test skipping

**Run Commands:**
```bash
pytest tests/ -v                    # Run all tests
pytest tests/ -k "not e2e" -v      # Run without e2e (CI default)
pytest tests/test_e2e.py -v        # E2E tests (requires BlackHole + running HeyVox)
pytest tests/test_stress.py -v -s  # Stress tests (requires BlackHole + running HeyVox)
ruff check heyvox/ tests/          # Lint only (CI step)
```

## Test File Organization

**Location:**
- All tests in top-level `tests/` directory (separate from source, not co-located)
- Shared fixtures in `tests/conftest.py`
- No test subdirectories — flat structure

**Naming:**
- `tests/test_<feature>.py` — maps to features, not 1:1 to source modules
- Example: `tests/test_flag_coordination.py` tests cross-module flag behavior
- Example: `tests/test_injection_enter.py`, `tests/test_injection_sanitize.py` split injection concerns

**Test count:** 17 test files, ~3,275 total lines

**Structure:**
```
tests/
├── __init__.py               # Empty
├── conftest.py               # Shared fixtures (isolate_flags, mock_config)
├── test_adapters.py          # GenericAdapter, LastAgentAdapter
├── test_chrome_bridge.py     # ChromeBridge WebSocket state
├── test_config.py            # Pydantic config loading/validation
├── test_cues.py              # Audio cue playback
├── test_echo_suppression.py  # Echo suppression flags/buffer
├── test_flag_coordination.py # Recording flag lifecycle
├── test_hud_ipc.py           # HUD Unix socket client/server
├── test_injection.py         # Text injection via clipboard
├── test_injection_enter.py   # Enter key pressing after injection
├── test_injection_sanitize.py # Input sanitization
├── test_media.py             # Media pause/resume control
├── test_stale_flags.py       # Stale flag cleanup
├── test_stress.py            # Memory/performance stress tests
├── test_tts_state.py         # TTS state machine
├── test_wake_word_strip.py   # Wake word stripping from transcripts
├── test_wakeword_trim.py     # Wake word audio trimming
└── test_e2e.py               # Full pipeline end-to-end tests
```

## Test Structure

**Suite Organization:**
```python
class TestFeatureArea:
    """Docstring describing what this group tests."""

    def test_specific_behavior(self):
        """Docstring explaining what is being verified."""
        # Arrange
        cfg = HeyvoxConfig()
        # Act
        result = cfg.threshold
        # Assert
        assert result == 0.5
```

**Patterns:**
- Group related tests in classes (e.g., `TestHeyvoxConfigDefaults`, `TestTTSFlagSuppression`)
- Each test method has a docstring explaining the assertion
- No `setUp`/`tearDown` — use pytest fixtures instead
- Bug-driven tests include bug reference in module docstring (e.g., "Covers Bug #4: External TTS...")

## Key Fixtures

**`isolate_flags` (autouse, in `tests/conftest.py`):**
Redirects all `/tmp/` flag files to `tmp_path` so tests never interfere with a running HeyVox instance. Patches both `heyvox.constants` and consumer modules that import constants at module level:
```python
@pytest.fixture(autouse=True)
def isolate_flags(tmp_path, monkeypatch):
    monkeypatch.setattr("heyvox.constants.RECORDING_FLAG", str(tmp_path / "heyvox-recording"))
    # Also patches heyvox.audio.tts, heyvox.main, etc.
```

**`mock_config` (in `tests/conftest.py`):**
Returns a `HeyvoxConfig` with test-friendly defaults (TTS disabled, no PTT, BlackHole mic):
```python
@pytest.fixture
def mock_config():
    return HeyvoxConfig(
        target_mode="always-focused",
        enter_count=0,
        push_to_talk={"enabled": False, "key": "fn"},
        tts={"enabled": False},
        mic_priority=["BlackHole 2ch", "MacBook Pro Microphone"],
        log_file="/dev/null",
    )
```

**Conditional skip markers (in `tests/conftest.py`):**
```python
blackhole_installed = pytest.mark.skipif(
    not _blackhole_available(),
    reason="BlackHole virtual audio driver not installed",
)
vox_running = pytest.mark.skipif(
    not _vox_running(),
    reason="Vox is not running",
)
```

## Mocking

**Framework:** `unittest.mock` (standard library `patch`, `MagicMock`)

**Patterns:**
```python
# Decorator-style patching
@patch("heyvox.adapters.generic.type_text")
def test_inject_text_pastes_directly(self, mock_type):
    adapter = GenericAdapter()
    adapter.inject_text("hello world")
    mock_type.assert_called_once_with("hello world")

# monkeypatch for module-level state
monkeypatch.setattr("heyvox.audio.tts.RECORDING_FLAG", rec_flag)

# Lambda mocks for subprocess delegation
monkeypatch.setattr(tts, "_herald", lambda cmd, *a, **kw: herald_calls.append(cmd) or
                    __import__("subprocess").CompletedProcess([], 0, "", ""))
```

**What to Mock:**
- `subprocess.run` for external commands (osascript, afplay, pgrep)
- Flag file paths (via `isolate_flags` fixture)
- Module-level functions that have side effects (`type_text`, `focus_app`)
- Heavy imports (MLX Whisper, Kokoro) — never loaded in unit tests

**What NOT to Mock:**
- Pydantic model construction and validation (test the real validators)
- IPC protocol parsing (test actual JSON over socket in `test_hud_ipc.py`)
- Config file loading (uses real YAML parsing via `tmp_path`)

## Fixtures and Factories

**Test Data:**
```python
# Config test data created inline with YAML strings
f = tmp_path / "config.yaml"
f.write_text("tts:\n  voice: bf_emma\n  speed: 1.5\n")
cfg = load_config(f)

# E2E audio fixtures directory
FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
WAKE_WORD_WAV = os.path.join(FIXTURES_DIR, "hey_jarvis.wav")
```

**Audio generation helpers** in `tests/test_e2e.py`:
```python
generate_silence(duration_secs, sample_rate=16000)  # numpy zeros
generate_tone(freq, duration_secs, amplitude=0.5)   # sine wave
write_wav(path, samples, sample_rate=16000)          # save to WAV
play_to_blackhole(wav_path)                          # route to virtual mic
```

**Location:**
- `tests/fixtures/` for audio WAV files (used by e2e and stress tests)
- No separate factory module — data created inline per test

## Coverage

**Requirements:** No coverage target enforced. No `pytest-cov` in dependencies.

**No coverage command configured.** To add:
```bash
pip install pytest-cov
pytest tests/ -k "not e2e" --cov=heyvox --cov-report=term-missing
```

## CI/CD Setup

**GitHub Actions workflows:**

1. **`.github/workflows/ci.yml`** — runs on every push and PR to main:
   - Runner: `macos-14` (Apple Silicon)
   - Python 3.12
   - Installs: `portaudio` (brew), `pip install -e ".[dev,chrome]"`
   - Steps: `ruff check` → `pytest -k "not e2e"` → verify CLI entry points → verify Python imports

2. **`.github/workflows/install-test.yml`** — runs on push to main only (slow):
   - Clean install test in fresh venv (non-editable)
   - Verifies `heyvox --help`, `heyvox setup --help`
   - Verifies key imports resolve (`herald`, `hush`, `config`)

**CI exclusions:** E2E and stress tests are excluded from CI (`-k "not e2e"`) because they require BlackHole audio driver and a running HeyVox instance.

## Test Types

**Unit Tests** (~80% of test suite):
- Config validation: `tests/test_config.py`
- Adapter behavior: `tests/test_adapters.py`
- Flag file lifecycle: `tests/test_flag_coordination.py`, `tests/test_stale_flags.py`
- Echo suppression: `tests/test_echo_suppression.py`
- Media control: `tests/test_media.py`
- Text injection: `tests/test_injection.py`, `tests/test_injection_enter.py`, `tests/test_injection_sanitize.py`
- Chrome bridge state: `tests/test_chrome_bridge.py`
- Wake word stripping: `tests/test_wake_word_strip.py`, `tests/test_wakeword_trim.py`
- TTS state machine: `tests/test_tts_state.py`
- Audio cues: `tests/test_cues.py`

**Integration Tests:**
- HUD IPC: `tests/test_hud_ipc.py` — real Unix socket server/client communication
- Chrome bridge async: `tests/test_chrome_bridge.py` — WebSocket protocol testing

**E2E Tests** (requires hardware setup):
- Full pipeline: `tests/test_e2e.py` — wake word → recording → STT → injection via BlackHole loopback
- Stress tests: `tests/test_stress.py` — memory stability, rapid-fire dictation, concurrent TTS+recording

**No E2E framework** (no Playwright, Selenium, etc.) — E2E tests use subprocess + virtual audio.

## Common Test Patterns

**Async Testing:**
```python
@pytest.mark.asyncio
async def test_websocket_handler(self):
    bridge = ChromeBridge()
    # ... async test body
```

**Error Testing:**
```python
def test_invalid_verbosity_raises(self):
    with pytest.raises(Exception):
        TTSConfig(verbosity="loud")

def test_invalid_yaml_exits(self, tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text("tts:\n  verbosity: screaming\n")
    with pytest.raises(SystemExit):
        load_config(f)
```

**Flag file testing pattern:**
```python
def test_fresh_tts_flag_suppresses(self, isolate_flags):
    flag = isolate_flags["tts_flag"]
    open(flag, "w").close()
    assert os.path.exists(flag)
    age = time.time() - os.path.getmtime(flag)
    assert age < TTS_PLAYING_MAX_AGE_SECS
```

**Thread safety testing:**
```python
def test_recording_coordination_is_thread_safe(self, monkeypatch):
    t = threading.Thread(target=set_from_thread)
    t.start()
    t.join()
    assert "pause" in herald_calls
```

## Test Coverage Gaps

**Modules with NO test coverage:**
- `heyvox/cli.py` — CLI argument parsing and command dispatch
- `heyvox/main.py` — main event loop (only `start_recording()` and `_release_recording_guard()` tested)
- `heyvox/audio/wakeword.py` — openwakeword integration
- `heyvox/audio/stt.py` — STT engine initialization and transcription
- `heyvox/audio/mic.py` — microphone discovery and stream management
- `heyvox/audio/output.py` — output device management
- `heyvox/hud/overlay.py` — AppKit HUD window (requires macOS GUI)
- `heyvox/input/ptt.py` — push-to-talk event tap (requires Accessibility permissions)
- `heyvox/input/target.py` — target app snapshot/restore
- `heyvox/mcp/server.py` — MCP tool handlers
- `heyvox/setup/wizard.py` — interactive setup wizard
- `heyvox/setup/launchd.py` — launchd service management
- `heyvox/setup/permissions.py` — macOS permission checks
- `heyvox/setup/hooks.py` — Herald hooks installer
- `heyvox/herald/cli.py` — Herald Python CLI wrapper
- `heyvox/herald/daemon/kokoro-daemon.py` — Kokoro TTS daemon
- `heyvox/herald/daemon/watcher.py` — file watcher daemon
- `heyvox/history.py` — transcript history (JSONL)
- `heyvox/hush/host/hush_host.py` — Hush native messaging host
- All Herald shell scripts (`heyvox/herald/lib/*.sh`, `heyvox/herald/bin/herald`)

**Modules with partial coverage:**
- `heyvox/config.py` — well tested (config loading, validation, defaults) but `update_config()` and `generate_default_config()` not tested
- `heyvox/audio/tts.py` — `set_recording()` tested, but `speak()`, `start_worker()`, `shutdown()` not tested
- `heyvox/audio/echo.py` — `register_tts_text()` and `filter_tts_echo()` tested, but AEC integration not tested
- `heyvox/chrome/bridge.py` — state management tested, but WebSocket server lifecycle not tested

**Key risks from missing coverage:**
- **Main event loop** (`heyvox/main.py`) — core pipeline untested; regressions only caught by manual E2E testing
- **MCP server** (`heyvox/mcp/server.py`) — agent-facing API untested; breaking changes undetected
- **STT engine** (`heyvox/audio/stt.py`) — transcription quality regressions undetected
- **Herald shell scripts** — TTS pipeline changes could break without automated verification
- **Setup/permissions** — macOS permission flow changes could break first-run experience

## Manual Testing Procedures

**E2E testing requires:**
1. Install BlackHole virtual audio: `brew install blackhole-2ch`
2. Set mic_priority to `["BlackHole 2ch"]` in config
3. Start HeyVox: `heyvox start`
4. Run: `pytest tests/test_e2e.py -v --timeout=60`

**Stress testing requires same setup plus:**
- Audio fixture files in `tests/fixtures/` (WAV files for wake word + test phrases)
- Run: `pytest tests/test_stress.py -v -s`

**Ad-hoc debugging:**
- Logs: `tail -f /tmp/heyvox.log`
- Herald logs: `tail -f /tmp/herald-debug.log`
- STT debug: recordings saved to `/tmp/heyvox-debug/`
- Herald timing: `grep TIMING /tmp/herald-debug.log`

---

*Testing analysis: 2026-04-10*
