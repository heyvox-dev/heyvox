---
phase: 13-audio-reliability
plan: "02"
subsystem: herald-cli-tts
tags: [tts, herald, stop, interrupt, escape, wake-word, audio-reliability]
dependency_graph:
  requires: []
  provides: [herald-stop-command, herald-interrupt-command, tts-stop-wiring]
  affects: [heyvox/audio/tts.py, heyvox/herald/cli.py]
tech_stack:
  added: []
  patterns: [sigterm-via-pid-file, tts-state-flag-cleanup]
key_files:
  created:
    - tests/test_herald_cli.py
  modified:
    - heyvox/herald/cli.py
    - heyvox/audio/tts.py
decisions:
  - "D-06: herald interrupt kills afplay but preserves unrelated queue messages for orchestrator purge"
  - "D-07: herald stop kills afplay and clears entire queue (Escape behavior)"
  - "_kill_afplay reads HERALD_PLAYING_PID with OSError/ValueError guard for robustness"
  - "_clear_tts_state removes TTS_PLAYING_FLAG synchronously so echo suppression restores immediately"
metrics:
  duration: "~7 minutes"
  completed: "2026-04-13"
  tasks_completed: 2
  files_created: 1
  files_modified: 2
---

# Phase 13 Plan 02: Herald Stop/Interrupt Commands Summary

**One-liner:** Fixed broken `herald stop` command (silent exit code 1) and added `herald interrupt` for wake-word-during-TTS selective purge, wiring both through `tts.py` with immediate TTS state cleanup.

## What Was Built

`heyvox/herald/cli.py` — Added four new functions and two new dispatch routes:

- `_cmd_stop()`: Kills afplay via SIGTERM, clears TTS state flag, then clears entire queue. Used by Escape key (D-07/AUDIO-03).
- `_cmd_interrupt()`: Kills afplay via SIGTERM, clears TTS state flag, but does NOT clear queue — the orchestrator's existing `_purge_message_parts()` handles selective removal of the interrupted message's parts only (D-06).
- `_kill_afplay()`: Reads `HERALD_PLAYING_PID`, sends `SIGTERM`. Guards against missing file, corrupt PID (ValueError), and dead process (OSError).
- `_clear_tts_state()`: Removes `TTS_PLAYING_FLAG` and updates IPC state via `update_state({tts_playing: False})` immediately — doesn't wait for orchestrator cleanup.
- `dispatch()` now routes `"stop"` and `"interrupt"` to these handlers.

`heyvox/audio/tts.py` — Fixed three callers:
- `interrupt()`: Was `_herald("skip")` — now `_herald("interrupt")` (selective purge per D-06)
- `clear_queue()`: Was `_herald("stop")` (broken) — now `_herald("skip")` (just clear files)
- `stop_all()`: Was `_herald("stop")` (broken) — now works because `stop` command exists

## Tests

`tests/test_herald_cli.py` — 15 unit tests:
- `TestCmdStopKillsAfplay` (3 tests): PID read + SIGTERM, no-PID-file guard, corrupt-PID guard
- `TestCmdStopClearsQueue` (2 tests): All queue files removed, empty queue handled
- `TestCmdStopClearsTtsState` (2 tests): TTS flag removed, no-flag case handled
- `TestCmdInterruptKillsAfplay` (2 tests): SIGTERM sent, queue NOT cleared
- `TestDispatchRouting` (3 tests): stop→0, interrupt→0, unknown→1
- `TestTtsWiring` (3 tests): stop_all→"stop", interrupt→"interrupt", clear_queue→"skip"

## Deviations from Plan

None — plan executed exactly as written.

## Known Stubs

None — all functions are fully implemented and wired.

## Self-Check: PASSED

Files exist:
- heyvox/herald/cli.py ✓
- heyvox/audio/tts.py ✓
- tests/test_herald_cli.py ✓

Commit 95d5b6c exists ✓

All 15 tests pass ✓

Acceptance criteria met:
- `heyvox/herald/cli.py` contains `def _cmd_stop() -> int:` ✓
- `heyvox/herald/cli.py` contains `def _cmd_interrupt() -> int:` ✓
- `heyvox/herald/cli.py` contains `def _kill_afplay() -> None:` ✓
- `heyvox/herald/cli.py` contains `def _clear_tts_state() -> None:` ✓
- dispatch contains `elif cmd == "stop":` ✓
- dispatch contains `elif cmd == "interrupt":` ✓
- `tts.py interrupt()` contains `_herald("interrupt")` ✓
- `tts.py clear_queue()` contains `_herald("skip")` ✓
- `dispatch(['stop'])` returns 0 ✓
