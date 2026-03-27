---
phase: 01-foundation
plan: 02
subsystem: config
tags: [python, pydantic, platformdirs, yaml, config, decoupling]

requires:
  - 01-01 (package skeleton with modular files)

provides:
  - VoxConfig pydantic model with validation and sensible defaults
  - load_config() reading from ~/.config/vox/config.yaml via platformdirs
  - generate_default_config() for fully-commented YAML generation
  - ensure_config_dir() for XDG config directory initialization
  - Zero Conductor/personal references across entire vox/ package

affects:
  - 01-03 (audio pipeline reads config.audio.sample_rate, config.mic_priority)
  - 02-01 (HUD module fully importable without Conductor)
  - 03-01 (MCP server reads config via load_config)
  - all future phases (config system is the foundation)

tech-stack:
  added:
    - pydantic v2 (BaseModel, field_validator, model_validator, ValidationError)
    - platformdirs (user_config_dir for XDG-compliant path)
  patterns:
    - Nested pydantic models: WakeWordConfig, STTLocalConfig, STTConfig, TTSConfig, PushToTalkConfig, AudioConfig, VoxConfig
    - Graceful degradation: TTSConfig.script_path=None logs warning, no crash
    - Config flows as typed VoxConfig object through all functions (replaces cfg dict)
    - LOG_FILE path set module-globally at startup via _init_log()

key-files:
  created:
    - vox/config.py
  modified:
    - vox/main.py
    - vox/audio/tts.py
    - vox/audio/cues.py
    - vox/hud/overlay.py
    - vox/input/ptt.py
    - vox/constants.py

key-decisions:
  - "Config flows as typed VoxConfig object (not dict) through all functions — eliminates cfg.get() patterns"
  - "TTSConfig validates script_path existence at load time — fail fast on misconfiguration"
  - "overlay.py always uses NSScreen.mainScreen() — Conductor-specific window detection removed entirely"
  - "get_cues_dir() accepts config_cues_dir param — config-driven with fallback to package location"

duration: ~4 min
completed: 2026-03-27
---

# Phase 1 Plan 2: Config System and Decoupling Summary

**Pydantic v2 config system at vox/config.py with XDG-compliant path via platformdirs, plus systematic removal of all Conductor/personal references from the entire vox/ package**

## Performance

- **Duration:** ~4 min
- **Started:** 2026-03-27T03:48:24Z
- **Completed:** 2026-03-27T03:52:37Z
- **Tasks:** 2
- **Files created:** 1 (vox/config.py)
- **Files modified:** 6

## Accomplishments

- VoxConfig pydantic model with 7 nested sub-models, all fields defaulted
- load_config() reads from `~/.config/vox/config.yaml` or returns defaults — zero config required
- Invalid config produces actionable field-level pydantic errors with field path + input value shown
- generate_default_config() returns 70-line commented YAML covering all options
- ensure_config_dir() creates XDG dir and writes default config on first run
- Zero grep matches for: conductor, claude-ww, tts-ctl, /Users/work, com.wakeword, wake-word-listener, wake_word_listener
- TTS voice commands gracefully degrade (warning logged, no crash) when tts.script_path is None
- Recording indicator uses NSScreen.mainScreen() — no app-specific bundle ID lookup
- main.py fully wired to VoxConfig typed object instead of yaml dict + cfg.get() patterns

## Task Commits

1. **Task 1: Build pydantic config system** — `7e13bc3` (feat)
2. **Task 2: Decouple all modules and wire config** — `1226733` (feat)

## Files Created

- `vox/config.py` — VoxConfig, WakeWordConfig, STTConfig, STTLocalConfig, TTSConfig, PushToTalkConfig, AudioConfig models; load_config(), generate_default_config(), ensure_config_dir()

## Files Modified

- `vox/main.py` — replaces yaml dict cfg with VoxConfig typed object; _init_log() for log path; config.target_app empty-string check before focus_app()
- `vox/audio/tts.py` — removes tts-ctl.sh docstring reference; execute_voice_command logs config path in warning
- `vox/audio/cues.py` — get_cues_dir(config_cues_dir) param; warns if cues dir missing
- `vox/hud/overlay.py` — removes com.conductor.app bundle ID lookup and CGWindowListCopyWindowInfo; always uses mainScreen()
- `vox/input/ptt.py` — already clean; no changes needed (RECORDING_FLAG used via callbacks not direct file reference)
- `vox/constants.py` — adds LOG_FILE_DEFAULT alias; all constants verified clean

## Decisions Made

- Config flows as typed VoxConfig object rather than raw dict — enables IDE completion and type safety for all downstream modules
- TTSConfig validates script_path existence at config load time (field_validator) rather than at execution time — provides immediate feedback on misconfiguration
- overlay.py Conductor-specific window detection removed entirely — mainScreen() is the correct generic approach; per-app screen detection belongs in Phase 5 HUD
- vox/input/ptt.py required no changes — PTT callbacks dict pattern from Plan 01 already decoupled RECORDING_FLAG usage from the module

## Deviations from Plan

None — plan executed exactly as written.

The plan listed ptt.py in the files to modify but noted it was for replacing `/tmp/claude-ww-recording` with RECORDING_FLAG import. Examination showed ptt.py never directly referenced the flag file — it only manages callbacks for recording state. No change was needed.

## Issues Encountered

None.

## User Setup Required

None — config system auto-creates `~/.config/vox/config.yaml` with sensible defaults on first `vox start`.

## Next Phase Readiness

- Config system is the foundation for all subsequent phases
- 01-03 (audio pipeline) can import `from vox.config import load_config` cleanly
- 02-01 (HUD) imports overlay.py without Conductor dependency
- 03-01 (MCP server) has a clean config loading path
- All requirements DECP-01 through DECP-06 and CONF-01 through CONF-04 satisfied

## Self-Check: PASSED

- `vox/config.py` found on disk
- Commits 7e13bc3 and 1226733 verified in git log
- Zero grep matches for all Conductor/personal reference patterns
- All functional verification commands pass
