---
phase: 01-foundation
plan: 01
subsystem: infra
tags: [python, pyaudio, openwakeword, mlx-whisper, sherpa-onnx, pyobjc, setuptools, argparse]

requires: []

provides:
  - pip-installable vox-voice package with vox CLI entry point
  - modular package structure: vox/audio/, vox/input/, vox/hud/, vox/mcp/, vox/adapters/
  - all source code from wake_word_listener.py extracted into correct modules
  - recording indicator extracted from recording_indicator.py into vox/hud/overlay.py
  - audio cue files copied to cues/
  - launchd plist template updated with vox branding

affects:
  - 01-02 (config system builds on vox.constants and module signatures)
  - 01-03 (audio pipeline uses vox.audio.mic, vox.audio.stt)
  - 02-01 (HUD uses vox.hud.overlay, vox.hud.ipc)
  - 03-01 (MCP server uses vox.mcp.server)

tech-stack:
  added:
    - setuptools>=61 (build backend)
    - vox-voice 0.1.0 (package name, since "vox" taken on PyPI)
  patterns:
    - lazy imports for heavy deps (mlx_whisper, sherpa_onnx, Quartz) inside functions
    - parametric functions: config values as function parameters, not module globals
    - callbacks dict pattern for PTT listener (decouples event source from recording state)
    - placeholder modules with docstrings for future phases (hud/ipc.py, mcp/server.py)

key-files:
  created:
    - pyproject.toml
    - vox/__init__.py
    - vox/__main__.py
    - vox/cli.py
    - vox/constants.py
    - vox/main.py
    - vox/audio/mic.py
    - vox/audio/wakeword.py
    - vox/audio/stt.py
    - vox/audio/tts.py
    - vox/audio/cues.py
    - vox/input/ptt.py
    - vox/input/injection.py
    - vox/hud/overlay.py
    - vox/hud/ipc.py
    - vox/mcp/server.py
    - vox/adapters/base.py
    - vox/adapters/generic.py
    - cues/listening.aiff
    - cues/ok.aiff
    - cues/paused.aiff
    - cues/sending.aiff
    - com.vox.listener.plist
  modified: []

key-decisions:
  - "Package name vox-voice (vox taken on PyPI) — using vox as CLI command name regardless"
  - "setuptools build backend (hatchling not installed in dev environment)"
  - "Parametric functions pattern: monolith globals become function parameters with same defaults"
  - "PTT callbacks dict: decouples Quartz event tap from recording state management"
  - "Lazy imports for mlx_whisper, sherpa_onnx, Quartz — keeps module importable without all deps"

patterns-established:
  - "Lazy import pattern: heavy audio/graphics deps imported inside functions, not at module top"
  - "Parametric config: functions accept sample_rate, chunk_size, mic_priority as params"
  - "Callbacks dict for PTT: {on_start, on_stop, on_cancel_recording, on_cancel_transcription, is_busy, is_recording}"
  - "Module-level _log helper in vox.audio.mic to avoid circular import with vox.main"

duration: 7min
completed: 2026-03-27
---

# Phase 1 Plan 1: Package Skeleton and Monolith Extraction Summary

**vox-voice Python package installable via pip, with argparse CLI and all wake_word_listener.py logic distributed into 13 modular files across vox/audio/, vox/input/, vox/hud/, vox/mcp/, and vox/adapters/**

## Performance

- **Duration:** ~7 min
- **Started:** 2026-03-27T03:39:22Z
- **Completed:** 2026-03-27T03:46:05Z
- **Tasks:** 2
- **Files created:** 23 (excluding __init__.py files)

## Accomplishments

- pip install -e . succeeds, vox CLI entry point registered and responds to --help
- All 6 subpackages created with correct modular structure matching CLAUDE.md target
- Every function from wake_word_listener.py has a home in the correct module
- Functions converted from global-reading to parametric (config values as parameters)
- PTT listener decoupled via callbacks dict instead of direct state mutation
- Lazy imports preserve importability without all heavy deps installed

## Task Commits

1. **Task 1: Create pyproject.toml and package skeleton** - `bc4acf8` (feat)
2. **Task 2: Extract monolith code into modular files** - `df6832c` (feat)

## Files Created

- `pyproject.toml` — package definition, dependencies, vox entry point, setuptools config
- `vox/__init__.py` — version 0.1.0
- `vox/__main__.py` — python -m vox support
- `vox/cli.py` — argparse CLI with start/stop/restart/status/setup/logs subcommands
- `vox/constants.py` — RECORDING_FLAG, LOG_FILE, LAUNCHD_LABEL, DEFAULT_SAMPLE_RATE, DEFAULT_CHUNK_SIZE
- `vox/main.py` — main event loop and run() entry point
- `vox/audio/mic.py` — find_best_mic, open_mic_stream
- `vox/audio/wakeword.py` — load_models wrapper for openwakeword
- `vox/audio/stt.py` — init_local_stt, transcribe_audio (mlx + sherpa)
- `vox/audio/tts.py` — VOICE_COMMANDS, check_voice_command, execute_voice_command
- `vox/audio/cues.py` — audio_cue, is_suppressed, get_cues_dir
- `vox/input/ptt.py` — start_ptt_listener with Quartz CGEventTap and callbacks dict
- `vox/input/injection.py` — type_text, press_enter, focus_app, clipboard helpers
- `vox/hud/overlay.py` — NSWindow recording indicator (from recording_indicator.py)
- `vox/hud/ipc.py` — placeholder with socket protocol documentation
- `vox/mcp/server.py` — placeholder with planned MCP tool list
- `vox/adapters/base.py` — AgentAdapter Protocol
- `vox/adapters/generic.py` — paste-into-focused-app adapter
- `cues/*.aiff` — 4 audio feedback files copied from source
- `com.vox.listener.plist` — launchd template updated with vox branding

## Decisions Made

- Package name is `vox-voice` on PyPI (vox is taken), CLI command stays `vox`
- Used setuptools as build backend (hatchling not present in environment)
- Functions are parametric: config values passed in rather than read from globals (makes Plan 02 config system integration clean)
- PTT uses callbacks dict rather than direct global mutation (testable, decoupled)
- Lazy imports for mlx_whisper, sherpa_onnx, Quartz (importable without all deps)

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Switched build backend from hatchling to setuptools**
- **Found during:** Task 1 verification (pip install -e .)
- **Issue:** hatchling not installed in the Python environment
- **Fix:** Changed build-backend to setuptools.build_meta in pyproject.toml
- **Files modified:** pyproject.toml
- **Verification:** pip install -e . succeeded
- **Committed in:** bc4acf8 (Task 1 commit)

**2. [Rule 1 - Bug] Fixed pyproject.toml package auto-discovery**
- **Found during:** Task 2 verification (second pip install -e .)
- **Issue:** setuptools auto-discovery found cues/ as a Python package alongside vox/, causing build failure. Also removed missing readme field that caused a warning.
- **Fix:** Added [tool.setuptools.packages.find] include=["vox*"] to restrict discovery to vox package only. Removed readme field and fixed license field to SPDX string.
- **Files modified:** pyproject.toml
- **Verification:** pip install -e . succeeded, all imports OK, vox --help works
- **Committed in:** a300e8c

---

**Total deviations:** 2 auto-fixed (1 blocking environment issue, 1 build config bug)
**Impact on plan:** Both fixes required for installability. No scope creep.

## Issues Encountered

None beyond the auto-fixed deviations above.

## User Setup Required

None — no external service configuration required.

## Next Phase Readiness

- Package skeleton is fully installable, importable, and CLI-functional
- All module signatures established — Plan 02 (config system) can wire up YAML loading cleanly
- Plan 02 can import from vox.audio.*, vox.input.*, vox.constants without changes
- Blocker remains: package name on PyPI not finalized (vox-voice is placeholder)

## Self-Check: PASSED

- All 13 key files found on disk
- Commits bc4acf8, df6832c, a300e8c verified in git log

---
*Phase: 01-foundation*
*Completed: 2026-03-27*
