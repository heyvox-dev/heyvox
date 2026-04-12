---
gsd_state_version: 1.0
milestone: v1.2
milestone_name: Polish & Reliability
status: verifying
stopped_at: Completed 11-02-PLAN.md
last_updated: "2026-04-12T10:19:33.393Z"
last_activity: 2026-04-12
progress:
  total_phases: 11
  completed_phases: 10
  total_plans: 27
  completed_plans: 29
  percent: 25
---

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-04-12)

**Core value:** One voice layer that works across ALL your AI coding agents -- wake word, local STT, local TTS, beautiful HUD -- without sending audio to the cloud.
**Current focus:** Phase 11 — tech-debt-cleanup

## Current Position

Phase: 11
Plan: Not started
Status: Phase complete — ready for verification
Last activity: 2026-04-12

Progress: [██░░░░░░░░░░░░░░░░░░] 25% (1/4 phases complete)

## Performance Metrics

**Velocity (v1.0):**

- Total plans completed: 10
- Average duration: 3.5 min
- Total execution time: ~0.6 hours

**Velocity (v1.1):**

- Total plans completed: 14
- Commits: 83
- Timeline: 2 days (2026-04-10 → 2026-04-11)

**Velocity (v1.2 — in progress):**

- Total plans completed: 2
- Average duration: ~3 min
- Phase 10 complete (2026-04-12)

## Accumulated Context

### Decisions

- Use `--ignore` path flags instead of `-k` keyword filter in CI pytest command — path-based is explicit and matches pyproject.toml addopts
- Use pytestmark + addopts rather than conftest skipif to exclude integration tests — marks are composable and explicit
- Patch `_get_frontmost_app` in injection tests to isolate call counts — added after tests were written, patching is cleaner than updating expected counts

Key decisions for v1.2:

- Do NOT restore clipboard after paste — Electron reads clipboard async, a restore causes double-paste. test_no_clipboard_restore must not be changed.
- tts_playing dual-write: complete Python side only in v1.2, bash scripts keep reading old flag file. Full bash cutover deferred to v1.3.
- Shim removal: migrate test_flag_coordination.py to AppContext API BEFORE deleting shims (never same commit).
- PyPI name: verify heyvox ownership before any distribution work. If squatted, rename cascades everywhere.
- Paste timing: 50-100ms Electron settle delay is from competitor patterns, not measured. Validate empirically with Claude Code and Cursor in Phase 12 before adding per-app config overhead.
- [Phase 10-test-stability]: Use pytestmark + addopts to exclude integration tests from default run — composable and explicit
- [Phase 10-test-stability]: Intent-based subprocess assertions (filter by command name) instead of call_count for injection tests — decoupled from implementation details
- [Phase 11-tech-debt-cleanup]: Migrate tests before deleting shims (separate commits) ensures tests never reference deleted code
- [Phase 11-tech-debt-cleanup]: Use websockets.asyncio.server.serve via __aenter__() to eliminate DeprecationWarning without restructuring ChromeBridge API
- [Phase 11-tech-debt-cleanup]: Dual-write: atomic state file is primary for tts_playing, legacy flag file is parallel write for safe gradual cutover

### Pending Todos

- Verify heyvox PyPI name ownership (first task of Phase 13) — check pypi.org + pip install heyvox --dry-run
- Validate paste timing empirically on Claude Code + Cursor in Phase 12
- Evaluate homebrew-pypi-poet output before committing to Homebrew formula (formula size may be impractical for ML deps)

### Blockers/Concerns

- PyPI name "heyvox" shows placeholder registration — ownership unconfirmed. Blocking decision for Phase 13.
- Homebrew formula: MLX Whisper + openwakeword + sherpa-onnx collectively hundreds of MB — may exceed practical formula limits. Fallback: pipx-only for v1.2, Homebrew deferred to v1.3.

## Session Continuity

Last session: 2026-04-12T10:08:58.972Z
Stopped at: Completed 11-02-PLAN.md
Resume with: `/gsd:plan-phase 11`
