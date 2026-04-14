---
gsd_state_version: 1.0
milestone: v1.2
milestone_name: Paste Injection Reliability
status: executing
stopped_at: Completed 13-audio-reliability-04-PLAN.md
last_updated: "2026-04-13T10:54:36.563Z"
last_activity: 2026-04-13
progress:
  total_phases: 13
  completed_phases: 12
  total_plans: 34
  completed_plans: 36
  percent: 100
---

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-04-11)

**Core value:** One voice layer that works across ALL your AI coding agents -- wake word, local STT, local TTS, beautiful HUD -- without sending audio to the cloud.
**Current focus:** Phase 13 — audio-reliability

## Current Position

Phase: 13
Plan: Not started
Status: Ready to execute
Last activity: 2026-04-14 - Completed quick task 260414-cki: Phase 4 dual-write IPC fix

Progress: [████████████████████] 100% (v1.1 shipped; v1.2 in progress)

## Performance Metrics

**Velocity (v1.0):**

- Total plans completed: 10
- Average duration: 3.5 min
- Total execution time: ~0.6 hours

**Velocity (v1.1):**

- Total plans completed: 14
- Commits: 83
- Timeline: 2 days (2026-04-10 → 2026-04-11)

## Accumulated Context

### Decisions

Full decision log in PROJECT.md Key Decisions table.

- [Phase 12-paste-injection-reliability]: NSPasteboard replaces pbcopy subprocess: zero fork overhead, atomic, in-process API
- [Phase 12-paste-injection-reliability]: InjectionConfig per-app delays: Conductor 0.3s, Cursor/Windsurf/VSCode 0.15s, iTerm2/Terminal 0.03s
- [Phase 12]: _verify_target_focused fails-open on exception: don't block paste if NSWorkspace check throws
- [Phase 12]: audio_cue imported at module level in injection.py: enables test mocking via patch()
- [Phase 12-03]: _run_type_text helper patches _verify_target_focused=True: isolates clipboard/injection path in integration tests
- [Phase 13-audio-reliability]: herald interrupt kills afplay but preserves queue for orchestrator selective purge (D-06)
- [Phase 13-audio-reliability]: herald stop kills afplay + clears queue; TTS state flag cleared synchronously for echo suppression (D-07/AUDIO-03)
- [Phase 13-audio-reliability]: MicProfileManager: config overrides always win over calibration cache (D-03)
- [Phase 13-audio-reliability]: MicProfileEntry calibration uses median of chunk peaks * 3.5 (capped 500) for Bluetooth noise resilience (D-04, D-12)
- [Phase 13-audio-reliability]: Echo suppression gate now checks headset_mode first, then profile.echo_safe override, then force_disabled — last wins, all can override
- [Phase 13-audio-reliability]: Grace period constants are device-aware (0.5s headset / 2.0s speaker) per D-10, no longer configurable
- [Phase 13-audio-reliability]: RECORDING_FLAG written before tts.interrupt() so orchestrator sees recording before purging (Pitfall 3)
- [Phase 13-audio-reliability]: _calibrate_open_pa() and _calibrate_get_cache_dir() injectable helpers enable testing heyvox calibrate without real hardware

### Pending Todos

None.

### Blockers/Concerns

None — milestone complete.

### Quick Tasks Completed

| # | Description | Date | Commit | Directory |
|---|-------------|------|--------|-----------|
| 260413-j7m | Add BlackHole-based integration tests for Phase 13 audio reliability features | 2026-04-13 | 791ff19 | [260413-j7m-add-blackhole-based-integration-tests-fo](./quick/260413-j7m-add-blackhole-based-integration-tests-fo/) |
| 260413-o6p | Phase 1: Dead code deletion (~490 lines removed) | 2026-04-13 | 116c9c5 | [260413-o6p-phase-1-dead-code-deletion-remove-500-li](./quick/260413-o6p-phase-1-dead-code-deletion-remove-500-li/) |
| 260413-os9 | Phase 2: App Profile System — replace hardcoded conductor checks | 2026-04-13 | 81393fa | [260413-os9-phase-2-app-profile-system-replace-all-h](./quick/260413-os9-phase-2-app-profile-system-replace-all-h/) |
| 260413-rc2 | Phase 5: Simplify abstractions — deduplicate WAV normalization, remove unused echo config | 2026-04-13 | 2792e39 | [260413-rc2-phase-5-simplify-abstractions-adapter-to](./quick/260413-rc2-phase-5-simplify-abstractions-adapter-to/) |
| 260414-b68 | Phase 3: IPC consolidation — move /tmp paths to user-scoped paths, cleanup function | 2026-04-14 | 314abe2 | [260414-b68-phase-3-ipc-consolidation-move-tmp-paths](./quick/260414-b68-phase-3-ipc-consolidation-move-tmp-paths/) |
| 260414-cki | Phase 4: Fix dual-write IPC bug — tests import constants, legacy flag refs fixed | 2026-04-14 | 7fdc82f | [260414-cki-phase-4-fix-dual-write-ipc-bug-standalon](./quick/260414-cki-phase-4-fix-dual-write-ipc-bug-standalon/) |

## Session Continuity

Last session: 2026-04-13T15:50:42.672Z
Stopped at: Completed quick tasks 260413-o6p (Phase 1) and 260413-os9 (Phase 2)
Resume file: None
