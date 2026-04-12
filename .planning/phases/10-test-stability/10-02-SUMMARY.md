---
phase: 10-test-stability
plan: "02"
subsystem: tests, ci
tags: [testing, ci, test-stability, github-actions]
dependency_graph:
  requires:
    - phase: 10-test-stability
      plan: "01"
      provides: "TEST-STABILITY-01 — repaired stale test failures, integration marker"
  provides: [TEST-STABILITY-02]
  affects:
    - .github/workflows/ci.yml
tech_stack:
  added: []
  patterns:
    - CI uses --ignore path flags instead of -k keyword filter for precise test exclusion
key_files:
  created: []
  modified:
    - .github/workflows/ci.yml
key_decisions:
  - "Use --ignore=tests/test_e2e.py --ignore=tests/test_stress.py instead of -k 'not e2e' — path-based ignore is explicit, not dependent on test name strings, and matches what pyproject.toml addopts uses locally"
requirements-completed: []
duration: "3 min"
completed: "2026-04-12"
---

# Phase 10 Plan 02: CI Workflow Update Summary

**Updated CI workflow to use explicit --ignore path flags for e2e/stress test exclusion; all 383 tests pass green.**

## Performance

- Duration: ~3 min
- Tasks completed: 2/2
- Commits: 2 (cherry-pick of 10-01 fixes + CI workflow update)
- Files modified: 1 (.github/workflows/ci.yml)
- Test suite: 383 passed, 2 skipped

## What Was Built

### Task 1-2: Test Fixes (Pre-completed by Wave 1 / 10-01)

The test fixes described in this plan (test_media.py, test_injection.py, test_injection_enter.py, pyproject.toml integration markers) were already completed by Plan 10-01 on `start-heyvox-v1`. Those changes were cherry-picked into this worktree as a prerequisite.

Cherry-picked commit `26f52e4` (originally `c46ab24` from 10-01):
- `tests/test_media.py`: patched `_browser_has_media_tab` (renamed from `_browser_has_video_tab`)
- `tests/test_injection.py`: patched `_get_frontmost_app` to isolate subprocess call count
- `tests/test_injection_enter.py`: set `_last_agent_name` alongside `_last_injected_via_conductor`
- `tests/test_e2e.py` + `tests/test_stress.py`: added `pytestmark = pytest.mark.integration`
- `pyproject.toml`: added `[tool.pytest.ini_options]` with integration marker + `addopts` exclusion

### Task 3: CI Workflow Change (Commit ebbdc87)

Updated `.github/workflows/ci.yml`:
- **Before**: `pytest tests/ -k "not e2e" -v --tb=short`
- **After**: `pytest tests/ --ignore=tests/test_e2e.py --ignore=tests/test_stress.py -v --tb=short`
- Updated step name from "Run tests (excluding e2e)" to "Run tests (excluding e2e and stress)"

The `--ignore` flag approach:
1. Matches the `addopts` configuration in `pyproject.toml` (uses same flags locally)
2. Is explicit by file path rather than keyword matching on test names
3. Excludes `test_stress.py` which was previously missed by `-k "not e2e"`

## Verification

Full test suite run with new CI flags:
```
383 passed, 2 skipped, 4 warnings in 6.03s
```

## Deviations from Plan

### Pre-completion by Wave 1 Agent

**Tasks 1 and 2 in this plan were already completed by Plan 10-01** (executing in parallel on `start-heyvox-v1` branch). The fixes were cherry-picked into this worktree rather than re-implemented.

**cherry-pick commit**: `26f52e4` (original: `c46ab24`)
**Tracked as**: Rule 3 (blocking prerequisite — tests would fail without fixes)

### No 10-02-PLAN.md Found

The plan file was not present in this worktree (only in the `start-heyvox-v1` branch context). Executed based on objective context provided in the prompt.

## Self-Check: PASSED

- [x] CI workflow file updated: `.github/workflows/ci.yml`
- [x] Tests pass: 383 passed, 2 skipped
- [x] Commits exist: 26f52e4 (cherry-pick), ebbdc87 (CI fix)
- [x] SUMMARY.md created at `.planning/phases/10-test-stability/10-02-SUMMARY.md`
