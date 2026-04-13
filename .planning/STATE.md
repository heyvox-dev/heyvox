---
gsd_state_version: 1.0
milestone: v1.2
milestone_name: Paste Injection Reliability
status: executing
stopped_at: Completed 13-audio-reliability-03-PLAN.md
last_updated: "2026-04-13T10:47:45.253Z"
last_activity: 2026-04-13
progress:
  total_phases: 13
  completed_phases: 11
  total_plans: 34
  completed_plans: 35
  percent: 100
---

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-04-11)

**Core value:** One voice layer that works across ALL your AI coding agents -- wake word, local STT, local TTS, beautiful HUD -- without sending audio to the cloud.
**Current focus:** Phase 13 — audio-reliability

## Current Position

Phase: 13 (audio-reliability) — EXECUTING
Plan: 4 of 4
Status: Ready to execute
Last activity: 2026-04-13

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

### Pending Todos

None.

### Blockers/Concerns

None — milestone complete.

## Session Continuity

Last session: 2026-04-13T10:47:45.250Z
Stopped at: Completed 13-audio-reliability-03-PLAN.md
Resume file: None
