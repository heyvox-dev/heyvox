# Phase 1: Foundation - Research

**Researched:** 2026-03-27
**Domain:** Python packaging, config management, codebase decoupling
**Confidence:** HIGH

## Summary

Phase 1 is a restructuring and decoupling phase, not a feature-building phase. The existing codebase at `/Users/work/conductor/workspaces/conductor-and-process/manama/wake-word/` is a working monolith (~940 lines in `wake_word_listener.py`) that needs to become a clean, installable Python package with modular structure and no Conductor-specific references.

The three pillars are: (1) proper Python packaging with hatchling and complete dependency declaration, (2) a config system that loads from `~/.config/vox/config.yaml` with validation and sensible defaults, and (3) systematic removal of all hardcoded Conductor paths, personal paths, and the `tts-ctl.sh` dependency.

**Primary recommendation:** Use hatchling for build backend, pydantic for config validation (it handles YAML config well and provides actionable error messages out of the box), and a methodical grep-and-replace approach for decoupling. Do NOT restructure the audio/STT/PTT logic in this phase -- only move code into the modular file structure.

## Standard Stack

### Core (Phase 1 specific)
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| hatchling | latest | Build backend for pyproject.toml | Modern PEP 517, fast, minimal config |
| pydantic | 2.x | Config validation with actionable errors | Built-in type coercion, clear error messages, YAML-friendly |
| PyYAML | 6.x | YAML config parsing | Already used in existing codebase |
| platformdirs | 4.x | XDG-compliant config/data paths | Cross-platform, handles `~/.config` on macOS properly |

### Full Dependency List (for pyproject.toml)
These are ALL runtime dependencies that must be declared. The existing pyproject.toml is missing most of them.

| Library | Version | Category | Notes |
|---------|---------|----------|-------|
| openwakeword | >=0.6.0 | audio | Already declared |
| pyaudio | >=0.2.14 | audio | Already declared, requires `brew install portaudio` |
| numpy | >=1.24.0 | audio | Already declared |
| PyYAML | >=6.0 | config | Already declared |
| pydantic | >=2.0 | config | NEW - for config validation |
| platformdirs | >=4.0 | config | NEW - for XDG config paths |
| mlx-whisper | >=0.1.0 | stt | MISSING - only works on Apple Silicon |
| sherpa-onnx | >=1.0 | stt | MISSING - CPU STT fallback |
| pyobjc-framework-Cocoa | >=10.0 | macos | MISSING - AppKit for HUD |
| pyobjc-framework-Quartz | >=10.0 | macos | MISSING - Event tap for PTT |

**Note on mlx-whisper:** This should be an optional dependency (`[project.optional-dependencies] apple-silicon = ["mlx-whisper"]`) since it only works on M1+. The base install should work on Intel with sherpa-onnx only.

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| pydantic | dataclasses + manual validation | More code, worse error messages, no type coercion |
| pydantic | attrs + cattrs | Less ecosystem support for config validation |
| platformdirs | hardcoded `~/.config` | Breaks if `XDG_CONFIG_HOME` is set differently |
| hatchling | setuptools | More boilerplate, slower to configure |

**Installation:**
```bash
pip install -e ".[apple-silicon]"  # Development with MLX Whisper
pip install -e .                    # Development without MLX (Intel)
```

## Architecture Patterns

### Recommended Project Structure
```
vox/
├── __init__.py          # Version string
├── __main__.py          # python -m vox support
├── main.py              # Entry point, main event loop (from wake_word_listener.py)
├── audio/
│   ├── __init__.py
│   ├── mic.py           # find_best_mic(), open_mic_stream()
│   ├── wakeword.py      # openwakeword Model wrapper
│   ├── stt.py           # init_local_stt(), transcribe_audio()
│   ├── tts.py           # TTS orchestration (configurable path)
│   └── cues.py          # audio_cue(), CUES_DIR resolution
├── input/
│   ├── __init__.py
│   ├── ptt.py           # start_ptt_listener(), Quartz event tap
│   └── injection.py     # type_text(), press_enter(), focus_app(), clipboard ops
├── hud/
│   ├── __init__.py
│   ├── overlay.py       # recording_indicator.py content (decoupled)
│   └── ipc.py           # Unix socket IPC (placeholder for now)
├── mcp/
│   ├── __init__.py
│   └── server.py        # MCP server (placeholder for now)
├── adapters/
│   ├── __init__.py
│   ├── base.py          # AgentAdapter protocol
│   └── generic.py       # Paste-into-focused-app adapter
├── config.py            # Config loading, validation, defaults
└── cli.py               # CLI entry point (vox start/stop/status)
```

### Pattern 1: Config as Pydantic Model
**What:** Define config as nested pydantic models with defaults, load from YAML, validate on startup.
**When to use:** Config loading at startup.
**Example:**
```python
from pathlib import Path
from pydantic import BaseModel, Field, field_validator
from platformdirs import user_config_dir

CONFIG_DIR = Path(user_config_dir("vox"))
CONFIG_FILE = CONFIG_DIR / "config.yaml"

class STTConfig(BaseModel):
    backend: str = "local"
    engine: str = "mlx"  # "mlx" or "sherpa"
    mlx_model: str = "mlx-community/whisper-small-mlx"
    language: str = ""  # empty = auto-detect

class TTSConfig(BaseModel):
    enabled: bool = False
    script_path: str | None = None  # Was hardcoded tts-ctl.sh

    @field_validator("script_path")
    @classmethod
    def validate_script_path(cls, v):
        if v and not Path(v).exists():
            raise ValueError(f"TTS script not found: {v}. Set tts.script_path in config or disable TTS.")
        return v

class VoxConfig(BaseModel):
    wake_words: dict = {"start": "hey_jarvis_v0.1", "stop": "hey_jarvis_v0.1"}
    threshold: float = 0.5
    target_app: str = ""  # Empty = paste into focused app (was "Conductor")
    tts: TTSConfig = TTSConfig()
    stt: STTConfig = STTConfig()
    # ... etc

def load_config() -> VoxConfig:
    if CONFIG_FILE.exists():
        import yaml
        raw = yaml.safe_load(CONFIG_FILE.read_text())
        return VoxConfig(**(raw or {}))
    return VoxConfig()  # All defaults
```

### Pattern 2: Graceful Degradation for Optional Features
**What:** Features that depend on unconfigured paths silently disable with a log message.
**When to use:** TTS commands, optional adapters, MCP server.
**Example:**
```python
def execute_voice_command(action_key, feedback, config: VoxConfig):
    if not config.tts.script_path:
        log(f"Voice command '{action_key}' ignored: TTS not configured (set tts.script_path)")
        return
    # ... execute
```

### Pattern 3: Module Extraction (Decoupling)
**What:** Move functions from monolith into modules without changing logic.
**When to use:** Breaking `wake_word_listener.py` into `vox/audio/`, `vox/input/`, etc.
**Key rule:** In Phase 1, do NOT refactor logic. Only move code into the right module and update imports. Config globals become function parameters.

### Anti-Patterns to Avoid
- **Refactoring while restructuring:** Do NOT improve the audio loop, STT logic, or PTT handling in this phase. Only move code and remove hardcoded references.
- **Config in module globals:** The existing code uses `CFG = load_config()` at module level. Move to explicit parameter passing: `main(config: VoxConfig)`.
- **Incomplete dependency extraction:** Every `import` in the monolith must be traced to a `pyproject.toml` dependency.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Config validation | Manual if/else checks | pydantic BaseModel | Handles type coercion, nested models, actionable errors |
| Config file path | Hardcoded `~/.config/vox` | platformdirs `user_config_dir("vox")` | Respects XDG_CONFIG_HOME |
| CLI argument parsing | Raw sys.argv | argparse (stdlib) | Sufficient for start/stop/status; click is overkill for this |
| Default config generation | Manual string template | pydantic `model.model_dump()` + yaml.dump | Always matches actual schema |

**Key insight:** The config system is the foundation everything else reads from. Getting it right with pydantic means every subsequent phase gets validated config for free.

## Common Pitfalls

### Pitfall 1: Incomplete Dependency Declaration
**What goes wrong:** `pip install -e .` succeeds but `vox start` crashes on `import mlx_whisper`.
**Why it happens:** The existing pyproject.toml only declares 4 of ~10 runtime dependencies.
**How to avoid:** Trace every `import` in wake_word_listener.py and recording_indicator.py. Add all to pyproject.toml. Use optional-dependencies for Apple Silicon-only packages.
**Warning signs:** Any `import` in source code that doesn't map to a pyproject.toml dependency.

### Pitfall 2: Hardcoded Paths Survive Decoupling
**What goes wrong:** Non-Conductor user gets FileNotFoundError or wrong behavior.
**Why it happens:** Grep misses paths in string interpolation, comments that become code, or default values.
**How to avoid:** Systematic inventory (see Decoupling Inventory below). Verify with `grep -r` after decoupling.
**Warning signs:** Any reference to "conductor", "claude-ww", "tts-ctl", "/Users/work", "com.wakeword".

### Pitfall 3: Config Migration Breaks Existing Users
**What goes wrong:** Existing user's `config.yaml` (next to script) stops being found after move to `~/.config/vox/`.
**Why it happens:** Config location changed without migration path.
**How to avoid:** Check old location, copy to new if not present, warn user. Or: accept config path as CLI argument.
**Warning signs:** User says "my config stopped working after update."

### Pitfall 4: pyaudio Requires System Library
**What goes wrong:** `pip install` fails with "portaudio.h not found".
**Why it happens:** pyaudio needs `brew install portaudio` first.
**How to avoid:** Document in README. Check in `vox setup`. Consider adding a build-time check.
**Warning signs:** Install instructions don't mention portaudio.

### Pitfall 5: Module-Level Imports of Heavy Libraries
**What goes wrong:** `vox status` takes 5 seconds because importing `vox` loads mlx_whisper.
**Why it happens:** Top-level imports of ML libraries in modules that get imported by CLI.
**How to avoid:** Lazy imports for heavy libraries (mlx_whisper, sherpa_onnx, openwakeword). Only import when actually starting the listener.
**Warning signs:** CLI commands that don't need audio taking seconds to respond.

### Pitfall 6: Recording Flag Path Not Updated
**What goes wrong:** TTS coordination breaks silently.
**Why it happens:** `/tmp/claude-ww-recording` needs to become `/tmp/vox-recording` in multiple places.
**How to avoid:** Make the flag path a constant derived from config, not scattered literals.
**Warning signs:** `grep -r "claude-ww"` returns results after decoupling.

## Decoupling Inventory

Exact list of Conductor/personal references that must be changed:

### In wake_word_listener.py
| Line | Current | Target | Req |
|------|---------|--------|-----|
| 60 | `target_app: "Conductor"` default | `target_app: ""` (focused app) | DECP-05 |
| 284-288 | 5x hardcoded `/Users/work/.claude/hooks/tts-ctl.sh` | `config.tts.script_path` (configurable) | DECP-01 |
| 408, 431, 681, 888 | `/tmp/claude-ww-recording` | `/tmp/vox-recording` | DECP-03 |
| 274-289 | Voice commands crash if tts path missing | Graceful no-op when unconfigured | DECP-02 |

### In recording_indicator.py
| Line | Current | Target | Req |
|------|---------|--------|-----|
| 22 | `"com.conductor.app"` bundle ID | Configurable or removed | DECP-04 |
| 30 | `"conductor"` window name | Configurable or use main screen | DECP-04 |
| 18 | Comment "Conductor app" | Update comment | DECP-06 |

### In ww (CLI script)
| Line | Current | Target | Req |
|------|---------|--------|-----|
| 5 | `LABEL="com.wakeword.listener"` | `LABEL="com.vox.listener"` | PROJ-05 |
| 10 | `TTS_CTL="$HOME/.claude/hooks/tts-ctl.sh"` | Read from config | DECP-01 |
| 8-9 | `wake-word-listener` log names | `vox` log names | PROJ-04 |
| 128-141 | TTS commands call tts-ctl.sh directly | Delegate to Python | DECP-01 |

### In config.yaml
| Line | Current | Target | Req |
|------|---------|--------|-----|
| 25 | `target_app: "Conductor"` | `target_app: ""` | DECP-05 |
| 79-80 | Personal mic names ("Jabra", "G435") | Generic defaults | DECP-06 |
| 86 | `log_file: "/tmp/wake-word-listener.log"` | `/tmp/vox.log` | PROJ-04 |

## Code Examples

### pyproject.toml Structure
```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "vox-voice"  # "vox" is taken on PyPI
version = "0.1.0"
description = "Voice layer for AI coding agents — wake word, STT, TTS, HUD"
readme = "README.md"
license = {text = "MIT"}
requires-python = ">=3.12"
dependencies = [
    "openwakeword>=0.6.0",
    "pyaudio>=0.2.14",
    "numpy>=1.24.0",
    "PyYAML>=6.0",
    "pydantic>=2.0",
    "platformdirs>=4.0",
    "sherpa-onnx>=1.0",
    "pyobjc-framework-Cocoa>=10.0",
    "pyobjc-framework-Quartz>=10.0",
]

[project.optional-dependencies]
apple-silicon = ["mlx-whisper>=0.1.0"]
dev = ["pytest", "ruff"]

[project.scripts]
vox = "vox.cli:main"
```

### CLI Entry Point (vox/cli.py)
```python
#!/usr/bin/env python3
"""Vox CLI — voice layer for AI coding agents."""
import argparse
import sys

def main():
    parser = argparse.ArgumentParser(prog="vox", description="Voice layer for AI coding agents")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("start", help="Start the voice listener")
    sub.add_parser("stop", help="Stop the voice listener")
    sub.add_parser("restart", help="Restart the voice listener")
    sub.add_parser("status", help="Show listener status")
    sub.add_parser("setup", help="Interactive setup wizard")
    logs_p = sub.add_parser("logs", help="Show recent logs")
    logs_p.add_argument("-f", "--follow", action="store_true")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    # Lazy imports to keep CLI fast
    if args.command == "start":
        from vox.service import start_service
        start_service()
    elif args.command == "stop":
        from vox.service import stop_service
        stop_service()
    # ... etc
```

### Config Default Generation
```python
def generate_default_config() -> str:
    """Generate a default config.yaml with comments."""
    return """\
# Vox — Voice Layer Configuration
# Location: ~/.config/vox/config.yaml

# Wake word models (openwakeword model names or paths to .onnx files)
wake_words:
  start: "hey_jarvis_v0.1"
  stop: "hey_jarvis_v0.1"

threshold: 0.5
cooldown_secs: 2.0

# Target app — empty string pastes into focused app
target_app: ""

# TTS control — set script_path to enable voice commands
tts:
  enabled: false
  script_path: null  # Path to TTS control script

# Speech-to-text
stt:
  backend: "local"
  engine: "mlx"  # "mlx" (Apple Silicon) or "sherpa" (CPU)
  mlx_model: "mlx-community/whisper-small-mlx"
  language: ""  # empty = auto-detect

# Push-to-talk
push_to_talk:
  enabled: true
  key: "fn"

# Microphone priority (substring match, case-insensitive)
mic_priority:
  - "MacBook Pro Microphone"
"""
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| setup.py + setuptools | pyproject.toml + hatchling | PEP 621 (2021), mainstream 2023+ | Single config file, faster builds |
| Manual config validation | pydantic v2 BaseModel | pydantic v2 (2023) | Type coercion, clear errors, nested models |
| pipx for tool install | uv tool install | uv 0.4+ (2024) | 10-100x faster installs |
| `launchctl load/unload` | `launchctl bootstrap/bootout` | macOS 10.10+ (preferred) | Modern API, better error reporting |

## Open Questions

1. **Package name on PyPI**
   - What we know: "vox" is taken on PyPI
   - What's unclear: Best alternative name
   - Recommendation: Use `vox-voice` in pyproject.toml, keep `vox` as the CLI command. Users install with `uv tool install vox-voice` but run `vox start`.

2. **Config migration from old location**
   - What we know: Existing users have config.yaml next to the script
   - What's unclear: How many users exist, whether migration matters for v0.1
   - Recommendation: On first run, if `~/.config/vox/config.yaml` doesn't exist, check old location and offer to copy. Low priority for v0.1.

3. **pydantic as dependency weight**
   - What we know: pydantic adds ~5MB to install, has rust extensions
   - What's unclear: Whether this bothers users installing via uv/pipx
   - Recommendation: Worth it for config validation quality. The alternative (manual validation) means worse error messages and more code.

## Sources

### Primary (HIGH confidence)
- Existing source code at `/Users/work/conductor/workspaces/conductor-and-process/manama/wake-word/` -- direct inspection of all files
- Project research at `.planning/research/` -- STACK.md, ARCHITECTURE.md, PITFALLS.md, FEATURES.md, SUMMARY.md

### Secondary (MEDIUM confidence)
- [Python Packaging User Guide - pyproject.toml](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/) -- hatchling patterns
- [Pydantic v2 docs](https://docs.pydantic.dev/latest/) -- config validation patterns
- [platformdirs docs](https://platformdirs.readthedocs.io/en/latest/) -- XDG config paths

### Tertiary (LOW confidence)
- Package name availability ("vox" on PyPI) -- needs verification at publish time

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - direct inspection of existing code + verified library docs
- Architecture: HIGH - modular structure defined in CLAUDE.md, just needs implementation
- Pitfalls: HIGH - every pitfall identified from actual code inspection, not speculation
- Decoupling inventory: HIGH - every reference found via grep, line numbers verified

**Research date:** 2026-03-27
**Valid until:** 2026-04-27 (stable domain, no fast-moving dependencies)
