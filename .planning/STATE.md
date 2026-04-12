---
gsd_state_version: 1.0
milestone: v1.2
milestone_name: Polish & Reliability
status: executing
stopped_at: Completed 10-test-stability 10-01-PLAN
last_updated: "2026-04-12T07:03:57.223Z"
last_activity: 2026-04-12
progress:
  total_phases: 10
  completed_phases: 8
  total_plans: 24
  completed_plans: 25
---

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-04-12)

**Core value:** One voice layer that works across ALL your AI coding agents -- wake word, local STT, local TTS, beautiful HUD -- without sending audio to the cloud.
**Current focus:** Phase 10 — test-stability

## Current Position

Phase: 10 (test-stability) — EXECUTING
Plan: 2 of 2
Status: Ready to execute
Last activity: 2026-04-12

```
[Phase 10] [Phase 11] [Phase 12] [Phase 13]
[        ] [        ] [        ] [        ]
  0% complete
```

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

Key decisions for v1.2:

- Do NOT restore clipboard after paste — Electron reads clipboard async, a restore causes double-paste. test_no_clipboard_restore must not be changed.
- tts_playing dual-write: complete Python side only in v1.2, bash scripts keep reading old flag file. Full bash cutover deferred to v1.3.
- Shim removal: migrate test_flag_coordination.py to AppContext API BEFORE deleting shims (never same commit).
- PyPI name: verify heyvox ownership before any distribution work. If squatted, rename cascades everywhere.
- Paste timing: 50-100ms Electron settle delay is from competitor patterns, not measured. Validate empirically with Claude Code and Cursor in Phase 12 before adding per-app config overhead.
- [Phase 10-test-stability]: Use pytestmark + addopts to exclude integration tests from default run — composable and explicit

### Pending Todos

- Verify heyvox PyPI name ownership (first task of Phase 13) — check pypi.org + pip install heyvox --dry-run
- Validate paste timing empirically on Claude Code + Cursor in Phase 12
- Evaluate homebrew-pypi-poet output before committing to Homebrew formula (formula size may be impractical for ML deps)

### Blockers/Concerns

- PyPI name "heyvox" shows placeholder registration — ownership unconfirmed. Blocking decision for Phase 13.
- Homebrew formula: MLX Whisper + openwakeword + sherpa-onnx collectively hundreds of MB — may exceed practical formula limits. Fallback: pipx-only for v1.2, Homebrew deferred to v1.3.

## Session Continuity

Last session: 2026-04-12T07:03:57.220Z
Stopped at: Completed 10-test-stability 10-01-PLAN
Resume with: `/gsd:plan-phase 10`
