---
phase: 01-foundation
verified: 2026-03-27T03:56:00Z
status: passed
score: 5/5 must-haves verified
gaps: []
human_verification:
  - test: "Run `vox start` with a microphone attached and speak the wake word"
    expected: "Recording indicator appears, voice is transcribed and pasted into focused app"
    why_human: "Requires physical microphone, openwakeword model files, and end-to-end audio pipeline at runtime"
  - test: "Create ~/.config/vox/config.yaml with invalid YAML (e.g. threshold: 'bad'), then run `vox start`"
    expected: "Clear error message with field name and expected type printed to stderr, process exits 1"
    why_human: "Validation path through sys.exit(1) branch requires actual broken config file on disk"
---

# Phase 01: Foundation Verification Report

**Phase Goal:** A standalone, installable Python package with clean config that runs without Conductor
**Verified:** 2026-03-27T03:56:00Z
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | `pip install -e .` succeeds and `vox` CLI entry point is registered | VERIFIED | `pip show vox-voice` confirms editable install at /Users/work/conductor/workspaces/vox-v2/mogadishu; `vox --help` shows all 6 subcommands |
| 2 | Config loads from `~/.config/vox/config.yaml` with sensible defaults; invalid config produces actionable errors | VERIFIED | `load_config()` returns `target_app=''`, `tts.enabled=False`, `threshold=0.5` without config file; pydantic ValidationError raised on bad input with field name and input value shown |
| 3 | No Conductor references, personal paths, or hardcoded `tts-ctl.sh` remain | VERIFIED | `grep -rni "conductor\|claude-ww\|tts-ctl\|/Users/work\|com\.wakeword\|wake-word-listener\|wake_word_listener" vox/` returns zero results |
| 4 | Package follows modular structure (`vox/audio/`, `vox/input/`, `vox/hud/`, `vox/mcp/`, `vox/adapters/`) | VERIFIED | All 5 subpackages exist with expected files; all module imports succeed |
| 5 | TTS and voice commands gracefully degrade when optional paths are not configured | VERIFIED | `execute_voice_command('tts-skip', 'Skipping', tts_script_path=None)` logs warning message and returns without crashing |

**Score:** 5/5 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `pyproject.toml` | Package definition with `vox.cli:main` entry | VERIFIED | Contains `vox = "vox.cli:main"` under `[project.scripts]`; all dependencies listed; hatchling replaced with setuptools (functionally equivalent) |
| `vox/__init__.py` | Package root with `__version__` | VERIFIED | Single line: `__version__ = "0.1.0"` |
| `vox/cli.py` | CLI entry point with argparse subcommands | VERIFIED | argparse-based; 6 subcommands; lazy import of `vox.main` on `start` |
| `vox/main.py` | Main event loop (>50 lines) | VERIFIED | 529 lines; full wake word loop with config wiring |
| `vox/audio/mic.py` | Microphone management with `find_best_mic` | VERIFIED | `find_best_mic` and `open_mic_stream` present with correct signatures |
| `vox/audio/stt.py` | STT engine with `transcribe_audio` | VERIFIED | Both `init_local_stt` and `transcribe_audio` present; lazy imports for mlx_whisper and sherpa_onnx |
| `vox/input/ptt.py` | Push-to-talk with `start_ptt_listener` | VERIFIED | `start_ptt_listener(ptt_key, callbacks, log_fn)` present; Quartz lazy import; callback dict pattern |
| `vox/input/injection.py` | Text injection with `type_text` | VERIFIED | `type_text`, `press_enter`, `focus_app`, `get_clipboard_text`, `clipboard_is_image` all present |
| `vox/hud/overlay.py` | Recording indicator (>20 lines) | VERIFIED | 81 lines; uses `NSScreen.mainScreen()` exclusively; no Conductor bundle ID lookup |
| `vox/config.py` | Pydantic config with `VoxConfig` and `load_config` | VERIFIED | Full pydantic v2 model hierarchy; `load_config`, `generate_default_config`, `ensure_config_dir` all present |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `pyproject.toml` | `vox/cli.py` | `project.scripts` entry point | WIRED | Pattern `vox = "vox.cli:main"` present at line 29 |
| `vox/cli.py` | `vox/main.py` | lazy import on `start` command | WIRED | `from vox.main import run` inside `_cmd_start` function |
| `vox/main.py` | `vox/config.py` | `load_config()` call at startup | WIRED | `from vox.config import load_config, VoxConfig` at top; `config = load_config()` as first call in `main()` |
| `vox/audio/tts.py` | `vox/config.py` | `TTSConfig` graceful degradation | WIRED | `tts_script_path` parameter; `if not tts_script_path:` guard with config path in warning message |
| `vox/config.py` | `platformdirs` | `user_config_dir` for XDG path | WIRED | `from platformdirs import user_config_dir`; `CONFIG_DIR = Path(user_config_dir("vox"))` |

### Requirements Coverage

| Requirement | Status | Notes |
|-------------|--------|-------|
| DECP-01: No hardcoded target app | SATISFIED | `target_app: str = ""` default; focus_app() only called when non-empty |
| DECP-04: Recording flag is `/tmp/vox-recording` | SATISFIED | `RECORDING_FLAG = "/tmp/vox-recording"` in constants.py; used in main.py and start/stop_recording |
| DECP-05: TTS graceful degradation | SATISFIED | execute_voice_command logs warning and returns when tts_script_path is None |
| DECP-06: Indicator uses mainScreen() | SATISFIED | overlay.py: `screen = NSScreen.mainScreen().frame()` with comment citing requirement |
| CONF-01: All defaults, no config required | SATISFIED | `VoxConfig()` with all defaults; load_config returns defaults when file missing |
| CONF-02: XDG config path | SATISFIED | `Path(user_config_dir("vox"))` resolves to `~/.config/vox/` |
| CONF-03: Actionable validation errors | SATISFIED | ValidationError caught; field path and input value printed to stderr; sys.exit(1) |
| CONF-04: Default config generation | SATISFIED | `generate_default_config()` returns 70-line commented YAML |

### Anti-Patterns Found

| File | Pattern | Severity | Impact |
|------|---------|----------|--------|
| `vox/mcp/server.py` | Module is a docstring placeholder | Info | Intentional — MCP implementation deferred to Phase 4 per plan |
| `vox/hud/ipc.py` | Module is a docstring placeholder | Info | Intentional — Unix socket IPC deferred to Phase 5 per plan |

No blockers. Both placeholders are deliberately scoped out of Phase 1 per the plan specification.

### Human Verification Required

#### 1. End-to-end wake word → transcription → paste flow

**Test:** Run `vox start` with a microphone attached, openwakeword model downloaded, and speak the configured wake word ("hey_jarvis_v0.1")
**Expected:** Recording indicator (red dot) appears at top-center of screen, speech is transcribed via MLX Whisper, text is pasted into the focused application
**Why human:** Requires physical microphone, downloaded model weights (~150MB), Metal GPU availability, and Accessibility permission — cannot verify programmatically

#### 2. Invalid config produces actionable error

**Test:** Write `threshold: bad_value` to `~/.config/vox/config.yaml`, then run `vox start`
**Expected:** Error printed to stderr: `Field 'threshold': Input should be a valid number` with the bad value shown; process exits with code 1
**Why human:** The error path requires an actual malformed file on disk going through the `load_config` → ValidationError → sys.exit(1) branch at runtime startup

## Gaps Summary

No gaps. All 5 observable truths verified against the actual codebase. The package:

- Installs cleanly via pip (`vox-voice 0.1.0`, editable install confirmed)
- Registers the `vox` CLI entry point with all 6 subcommands
- Loads config from `~/.config/vox/config.yaml` with full pydantic v2 validation
- Contains zero Conductor references, personal paths, or hardcoded tool paths
- Has the complete 5-subpackage modular structure with substantive implementations
- Gracefully degrades TTS when script_path is not configured

---

_Verified: 2026-03-27T03:56:00Z_
_Verifier: Claude (gsd-verifier)_
