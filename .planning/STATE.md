---
gsd_state_version: 1.0
milestone: v1.2
milestone_name: Paste Injection Reliability
status: executing
stopped_at: Phase 14 context gathered
last_updated: "2026-06-11T18:23:37.000Z"
last_activity: 2026-07-02
progress:
  total_phases: 16
  completed_phases: 9
  total_plans: 29
  completed_plans: 50
  percent: 100
---

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-04-11)

**Core value:** One voice layer that works across ALL your AI coding agents -- wake word, local STT, local TTS, beautiful HUD -- without sending audio to the cloud.
**Current focus:** Post-launch v1.1.x stabilization (see .planning/DEFECT-LOG.md DEF-207 through DEF-222); v1.2 Phase 14 (Homebrew tap, wake-word default swap) remains open.

> ⚠️ **This file's structured fields (status/stopped_at/progress counters below) are stale — last mechanically updated 2026-06-11.** Phase 16 (STT Auto-Glossary) completed 2026-06-11 per ROADMAP.md; v1.3 (Robustness Sweep) and v1.4 (Phase 16) both shipped after this file stopped being updated. Since then, work has landed as DEF-numbered fixes tracked in DEFECT-LOG.md rather than through the phase-plan flow, shipping in point releases 1.1.1 (2026-07-13), 1.1.2 (2026-07-19), and 1.1.3 (2026-07-24, current — live on PyPI). See CHANGELOG.md for the user-facing summary of those. This note + the Current Position section below were refreshed 2026-07-24 (prose only, hand-edited); run `/gsd:health` for a full structural reconciliation of the counters in the frontmatter.

## Current Position

Phase: none currently executing — work since 2026-06-11 has landed as DEF-numbered fixes tracked directly in DEFECT-LOG.md rather than through the phase-plan flow. Phase 14 (Distribution & UX Polish) is the last incomplete phase-plan; see ROADMAP.md for its per-plan status (PyPI + HUD mic display done; Homebrew tap + wake-word default swap pending).
Plan: n/a
Status: Post-launch stabilization (v1.1.x point releases)
Last activity: 2026-07-24 - v1.1.3 tagged and published to PyPI (DEF-221 STT-timeout orphan gate, DEF-222 ruff CI pin); branch heyvox/release-1.1.1 merged to main via PR #23

Progress: v1.0 / v1.1 / v1.3 / v1.4 (Phase 16) shipped; v1.2 Phase 14 partial (see note above). The "100%" figure previously here was a stale GSD counter, not reconciled since 2026-06-11 — removed rather than left misleading.

## Performance Metrics

**Velocity (v1.0):**

- Total plans completed: 14
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

### Roadmap Evolution

- Phase 15 added: Paste Target Lock — record-start snapshot + resolve ladder + fail-closed policy (2026-04-22)
- Phase 15 SPEC.md written (8 requirements, ambiguity 0.12) — 2026-04-22
- Phase 15 CONTEXT.md written (27 decisions across 4 gray areas + Conductor adapter shape + profile schema + log tags) — 2026-04-22
- Phase 15 planning: 7 plans in 4 waves written (2026-04-22); plan-checker iterations 1+2 completed; iteration 3 rate-limited mid-flight. 4 BLOCKERs + minor WARNINGs captured in 15-REVISION-ADDENDUM.md for executor to apply inline.
- Phase 15 executed + verified: 2026-04-24. All 7 plans shipped; B1/B2/B3/B4/B5/B6/W3/W5-W13/Fact 1-6 corrections applied. 74 new tests, net-zero regressions vs pre-phase baseline. `conductor-switch-workspace` extended with `--id` + `--session`. TargetSnapshot/restore_target/_detect_app_workspace/_switch_app_workspace/_walk_ax_tree/_find_window_text_fields fully retired. app_fast_paste landed as generalized Phase 12 fast-path.

### Pending Todos

None — Phase 15 shipped.

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
| 260525-hsb | HUDSurface banner primitive — unified API for silent-state-change detectors (closes DEF-113, addresses patterns P-new + P-detector-without-action) | 2026-05-25 | 356aa3ec6 | [260525-hudsurface-banner](./quick/260525-hudsurface-banner/) |
| 260525-dvh | DeviceHandle primitive — hotplug-safe wrappers (CoreAudio pre-write validation in Herald + PortAudio drift diagnostic in DeviceManager.reinit); closes P-hotplug-cache for CoreAudio side | 2026-05-25 | 293f1a49b | [260525-devicehandle](./quick/260525-devicehandle/) |
| 260525-hdd | Herald producer-parity — shared tts_helpers module + WATCHER_FIRED forensic tag + drift-guard tests; mitigates P-producer-parity | 2026-05-25 | f513b27a5 | [260525-herald-dedup](./quick/260525-herald-dedup/) |
| 260525-svg | Stop-wake VAD silence gate — fast-path stop-wake requires recent silence (closes DEF-117); NEAR_MISS_FAST_BLOCKED forensic tag added | 2026-05-25 | c351f2c55 | [260525-stop-wake-vad-gate](./quick/260525-stop-wake-vad-gate/) |
| 260525-d80 | DEF-080 herald CLI pinned to `[sys.executable, -m, heyvox.herald.cli]` — closes 5-week-old WIP parked fix; defect-guard tests for the pin + dispatch argv | 2026-05-25 | 40e254046 | [260525-def080-herald-cmd](./quick/260525-def080-herald-cmd/) |
| 260628-mhf | Wake-word latency instrumentation — t0/t1/t2 perf_counter timestamps + [WW_LATENCY] log tags in main.py, cues.py, recording.py; baseline measurement pending | 2026-06-28 | 2ef86947c | [260628-mhf-wakeword-latency](./quick/260628-mhf-wakeword-latency/) |
| 260701-dis | cues.py: sounddevice + cache statt afplay für non-USB output — eliminiert p99-Spike (212ms→<50ms erwartet); afplay als Fallback erhalten | 2026-07-01 | 01cf8dd40 | [260701-dis-cues-sounddevice](./quick/260701-dis-cues-sounddevice/) |
| 260702-cdg | Wake-word training-data quality gate — removed retroactive relabelers (DEF-167) + mandatory resumable Whisper gate (quarantine-only, collision-safe) + append-only eval-history log | 2026-07-02 | 31b3a0ddb | [260702-cdg-wake-word-training-data-quality-gate](./quick/260702-cdg-wake-word-training-data-quality-gate/) |

## Session Continuity

Last session: 2026-05-11T06:14:46.839Z
Stopped at: Phase 14 context gathered
Resume file: .planning/phases/14-distribution-ux-polish/14-CONTEXT.md
