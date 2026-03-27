---
phase: 03-cli-tts-output
plan: 01
subsystem: tts
tags: [kokoro, sounddevice, tts, queue, verbosity, volume-ducking, ipc, cli]

# Dependency graph
requires:
  - phase: 02-audio-input-pipeline
    provides: echo suppression flag pattern (TTS_PLAYING_FLAG)
  - phase: 01-foundation
    provides: VoxConfig/TTSConfig pydantic config system, constants.py, cli.py structure

provides:
  - Kokoro TTS engine with interruptible worker queue (vox/audio/tts.py)
  - Verbosity filtering: full/summary/short/skip modes
  - Volume boost and audio ducking (TTS-04) via osascript
  - Echo suppression flag IPC (/tmp/vox-tts-playing written during playback)
  - Cross-process command file IPC (TTS_CMD_FILE = /tmp/vox-tts-cmd)
  - CLI subcommands: vox speak, vox skip, vox mute, vox quiet
  - Updated TTSConfig with voice/speed/verbosity/volume_boost/ducking_percent

affects:
  - 03-02 (MCP server will call tts.speak/interrupt from voice_speak tool)
  - 04-mcp-server (uses start_worker/speak/interrupt from this engine)

# Tech tracking
tech-stack:
  added: [kokoro (KPipeline lazy import), sounddevice (sd.play/sd.wait/sd.stop lazy import)]
  patterns:
    - Lazy import pattern for heavy audio libs (kokoro, sounddevice) to avoid load-time cost
    - Command file IPC for cross-process control (write file + worker reads/deletes it)
    - Flag file IPC for echo suppression coordination (TTS_PLAYING_FLAG)
    - Queue-based worker thread with sentinel shutdown (None item)
    - try/finally around playback for guaranteed flag cleanup and volume restore

key-files:
  created: []
  modified:
    - vox/audio/tts.py
    - vox/config.py
    - vox/constants.py
    - vox/cli.py

key-decisions:
  - "sounddevice sd.play()+sd.wait() (non-blocking+wait) enables sd.stop() from another thread for true interrupt"
  - "Command file IPC (/tmp/vox-tts-cmd) for cross-process CLI control — consistent with existing flag-file pattern, no sockets needed"
  - "Single KPipeline instance (module-level singleton) per worker thread — do not create per-call"
  - "Volume ducking applied before boost: duck first, then add boost on top, restore original in finally"
  - "check_voice_command/execute_voice_command preserved unchanged for main.py backward compatibility"
  - "TTSConfig enabled=True by default in Phase 3 (was False in Phase 1/2 external-script era)"

patterns-established:
  - "TTS engine: lazy import kokoro/sounddevice inside worker function, not at module top"
  - "Worker shutdown: None sentinel on queue, join() for clean exit"
  - "Per-item volume restore: read original_volume at start of each item, restore in finally block"

# Metrics
duration: 3min
completed: 2026-03-27
---

# Phase 3 Plan 01: CLI + TTS Output Summary

**Kokoro TTS engine with interruptible queue, verbosity filtering, audio ducking, and four CLI subcommands (speak/skip/mute/quiet) using command file IPC**

## Performance

- **Duration:** 3 min
- **Started:** 2026-03-27T10:10:15Z
- **Completed:** 2026-03-27T10:13:39Z
- **Tasks:** 2
- **Files modified:** 4

## Accomplishments
- Full Kokoro TTS engine in vox/audio/tts.py: worker queue, interrupt, verbosity filtering, volume boost, audio ducking (TTS-04), echo suppression flag IPC, cross-process command file IPC
- TTSConfig expanded with voice/speed/verbosity/volume_boost/ducking_percent fields and pydantic validators
- TTS constants added to constants.py (TTS_MAX_HELD=5, TTS_SAMPLE_RATE=24000, TTS_CMD_FILE, etc.)
- Four CLI subcommands: `vox speak` (fires TTS, waits for completion), `vox skip`/`mute`/`quiet` (command file IPC to running daemon)

## Task Commits

1. **Task 1: Kokoro TTS engine with interruptible queue, volume control, and audio ducking** - `dc7f8a1` (feat)
2. **Task 2: CLI speak/skip/mute/quiet subcommands with command file IPC** - `453ec9c` (feat)

**Plan metadata:** (this commit)

## Files Created/Modified
- `vox/audio/tts.py` - Complete rewrite: Kokoro TTS engine with Verbosity enum, apply_verbosity(), _get_pipeline() lazy init, _tts_worker(), speak/interrupt/skip_current/stop_all/clear_queue/set_muted/is_muted/set_verbosity/get_verbosity/start_worker/shutdown; backward-compat check_voice_command/execute_voice_command preserved
- `vox/config.py` - TTSConfig rewritten: enabled=True, voice, speed, verbosity (validator), volume_boost, ducking_percent (clamped validator), script_path deprecated; generate_default_config() updated
- `vox/constants.py` - Added TTS_MAX_HELD, TTS_SAMPLE_RATE, TTS_DEFAULT_VOICE, TTS_DEFAULT_SPEED, TTS_DEFAULT_VOLUME_BOOST, TTS_DEFAULT_DUCKING_PERCENT, TTS_CMD_FILE
- `vox/cli.py` - Added _cmd_speak, _cmd_skip, _cmd_mute, _cmd_quiet handler functions; registered speak/skip/mute/quiet subparsers with proper help and argument definitions

## Decisions Made
- `sd.play() + sd.wait()` pattern instead of `sd.play(blocking=True)` — enables `sd.stop()` interrupt from another thread (plan specified this, confirmed correct approach)
- Command file IPC for cross-process CLI control: write file atomically, worker reads+deletes between chunks — consistent with existing flag-file pattern, no sockets/signals needed
- Single KPipeline instance as module-level singleton — created lazily on first call, never recreated per-call
- Audio ducking: duck first (reduce to ducking_percent of original), then add boost; restore original_volume in finally block per-item
- TTSConfig.enabled defaults to True in Phase 3 (previously False, as external script was optional)

## Deviations from Plan

None — plan executed exactly as written.

## Issues Encountered
None.

## User Setup Required
None — no external service configuration required. Kokoro and sounddevice must be installed (handled by package dependencies).

## Next Phase Readiness
- TTS engine fully functional: `vox speak "text"` produces audible Kokoro output
- `tts.interrupt()` ready for wiring into main.py `start_recording()` (Plan 02)
- `tts.speak()` ready for wiring into MCP `voice_speak` tool (Phase 4)
- Cross-process CLI control (skip/mute/quiet) verified via command file IPC

---
*Phase: 03-cli-tts-output*
*Completed: 2026-03-27*
