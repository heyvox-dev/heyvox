---
phase: 12-paste-injection-reliability
plan: 03
subsystem: input
tags: [injection, multiapp, integration-tests, tdd, checkpoint]

# Dependency graph
requires:
  - phase: 12-01
    provides: NSPasteboard _set_clipboard, InjectionConfig, error.aiff
  - phase: 12-02
    provides: _clipboard_still_ours, _verify_target_focused, _ax_inject_text, type_text bool, recording.py paste_ok gating
provides:
  - TestMultiAppInjection: 24 integration tests covering all app targets and failure modes

affects: [tests/test_multiapp_injection.py]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Integration test helper _run_type_text: patches _verify_target_focused=True for path-focused tests"
    - "Per-app settle coverage: all 6 production app profiles verified via InjectionConfig defaults"

key-files:
  created:
    - tests/test_multiapp_injection.py
  modified: []

key-decisions:
  - "_run_type_text helper patches _verify_target_focused=True to isolate clipboard/injection path logic"
  - "Focus mismatch test uses real AppKit mock returning wrong bundle ID — not a patch — to test actual code path"

requirements-completed: []

# Metrics
duration: 5min
completed: 2026-04-12
---

# Phase 12 Plan 03: TestMultiAppInjection Integration Tests Summary

**24 integration tests covering the full injection pipeline for all app targets — Conductor, Cursor, Windsurf, VSCode, iTerm2, Terminal, Chrome, AX fast-path, and all failure modes**

## Status: CHECKPOINT PENDING — awaiting human validation (Task 2)

## Performance

- **Duration:** ~5 min
- **Completed:** 2026-04-12
- **Tasks:** 1 of 2 (Task 1 auto: DONE; Task 2 checkpoint: pending)
- **Files modified:** 1

## Accomplishments

- Created `tests/test_multiapp_injection.py` with `TestMultiAppInjection` class
- 24 tests passing, total suite: 432 passed (up from 408 in plan 12-02)
- Covers: Conductor (0.3s), Cursor (0.15s), Windsurf (0.15s), VSCode (0.15s), iTerm2 (0.03s), Terminal (0.03s), Chrome (Hush socket), unknown apps (default 0.1s)
- AX fast-path: AXTextField success, AXTextArea success, AXWebArea bypass, AX error fallback
- Failure signaling: osascript failure → False + error cue, clipboard stolen → False + error cue, focus mismatch → False + error cue
- InjectionConfig production defaults assertion (all 6 app profiles)

## Task Commits

1. **Task 1: TestMultiAppInjection integration tests** - `c3e4a4a` (test)

## Files Created/Modified

- `tests/test_multiapp_injection.py` — 24 integration tests for full per-app injection pipeline

## Decisions Made

- `_run_type_text` helper patches `_verify_target_focused=True` to isolate clipboard/injection path logic — focus verification is tested separately in `TestFocusVerification` (test_injection.py)
- Focus mismatch test (`test_returns_false_on_focus_mismatch`) uses real AppKit mock returning wrong bundle ID — exercises actual `_verify_target_focused` code path end-to-end

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Tests failed due to unpatched _verify_target_focused in _run_type_text helper**
- **Found during:** Task 1 test run (9 of 24 tests failing)
- **Issue:** `_run_type_text` helper didn't patch `_verify_target_focused`. When `snap.app_bundle_id` is set, the real function calls NSWorkspace which returns a MagicMock (not a string), causing focus verify to fail and tests to return False unexpectedly.
- **Fix:** Added `patch("heyvox.input.injection._verify_target_focused", return_value=True)` to `_run_type_text` helper, and individually patched the 4 tests that call `type_text` directly. The focus-mismatch test intentionally omits this patch.
- **Files modified:** tests/test_multiapp_injection.py
- **Verification:** 24/24 tests pass

## Known Stubs

None — tests exercise the real injection code; no placeholder assertions.

## Self-Check: PASSED

- tests/test_multiapp_injection.py: FOUND
- Commit c3e4a4a (test): FOUND
- 24 tests pass, 432 total suite pass

---
*Phase: 12-paste-injection-reliability*
*Completed (Task 1): 2026-04-12*
*Task 2: CHECKPOINT PENDING*
