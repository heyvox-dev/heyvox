---
phase: 09-test-suite
plan: "01"
subsystem: tests
tags: [testing, is_garbled, echo-suppression, wake-word, recording-state-machine, pure-functions, behavioral-tests]
dependency_graph:
  requires:
    - phase: 06-decomposition
      provides: "text_processing.py (is_garbled, strip_wake_words), recording.py (RecordingStateMachine), app_context.py"
  provides: [TEST-01, TEST-02]
  affects: [tests/test_echo_suppression.py, tests/test_wake_word_strip.py, tests/test_adapters.py, tests/test_recording_state.py]
tech_stack:
  added: []
  patterns: [autouse fixture for module-level state isolation, contextlib.ExitStack-style patch composition, behavioral state machine testing without hardware]
key_files:
  created:
    - tests/test_recording_state.py
  modified:
    - tests/test_echo_suppression.py
    - tests/test_wake_word_strip.py
    - tests/test_adapters.py
key_decisions:
  - "Import from heyvox.text_processing (not heyvox.main) — Phase 9 cleanup of backward-compat re-export"
  - "autouse fixture for _echo_buffer clears module-level deque before+after each test to prevent cross-test leakage"
  - "Broken inject_text tests removed entirely (GenericAdapter has no inject_text method post-Phase 6)"
  - "LastAgentAdapter tests patched at heyvox.input.injection level (where the function lives, not where it's imported)"
requirements-completed: [TEST-01, TEST-02]
duration: "5 min"
completed: "2026-04-11"
---

# Phase 09 Plan 01: Behavioral Tests for Pure Functions and RecordingStateMachine Summary

**Behavioral tests for is_garbled/strip_wake_words pure functions plus 7 RecordingStateMachine transition tests — all 60 pass without hardware or audio devices.**

## Performance

- **Duration:** ~5 min
- **Completed:** 2026-04-11
- **Tasks:** 2
- **Files modified:** 4

## Accomplishments

- Added `TestIsGarbled` class with 12 behavioral tests covering repeated words, bigram repetition, YouTube artifacts, hallucination patterns, and normal text passthrough
- Fixed `TestEchoTextBuffer` buffer isolation with autouse fixture clearing module-level `_echo_buffer` deque before and after each test
- Completed Phase 9 cleanup: `test_wake_word_strip.py` now imports from `heyvox.text_processing` instead of backward-compat `heyvox.main`
- Removed 3 broken `inject_text` tests in `TestGenericAdapterAlwaysFocused` and `TestGenericAdapterPinnedApp` (GenericAdapter has no `inject_text` method post-Phase 6)
- Fixed `LastAgentAdapter` tests to patch at `heyvox.input.injection` module level (where functions live)
- Created `tests/test_recording_state.py` with 7 behavioral transition tests for `RecordingStateMachine` covering start/stop/cancel/zombie/shutdown flows

## Task Commits

Each task was committed atomically:

1. **Task 1: Add is_garbled tests + fix echo buffer isolation + fix wake word import** - `8f667f6` (test)
2. **Task 2: Add RecordingStateMachine behavioral transition tests** - `386f9be` (test)

## Files Created/Modified

- `tests/test_echo_suppression.py` — Added `TestIsGarbled` class (12 tests) + `clear_echo_buffer` autouse fixture on `TestEchoTextBuffer`
- `tests/test_wake_word_strip.py` — Changed import from `heyvox.main` to `heyvox.text_processing` (Phase 9 cleanup)
- `tests/test_adapters.py` — Removed 3 broken `inject_text` tests; fixed `LastAgentAdapter` patch targets
- `tests/test_recording_state.py` — Created: 5 structural tests (kept) + 7 behavioral transition tests

## Decisions Made

- **Import fix (Phase 9 cleanup):** `_strip_wake_words` in test file now imports directly from `heyvox.text_processing`. The backward-compat re-export in `main.py` was intentionally preserved through Phase 8 and removed in this Phase 9 cleanup task.
- **Broken test removal:** The 3 `inject_text` tests in adapter tests were testing an API that no longer exists post-Phase 6 decomposition. Deleted rather than rewritten — the simplified `GenericAdapter` no longer has this method.
- **Patch target for LastAgentAdapter:** Tests patch at `heyvox.input.injection.type_text` and `heyvox.input.injection.focus_app` (where the functions are defined), not at `heyvox.adapters.last_agent.type_text` (where they're imported). This matches Python mock patching best practices.
- **RecordingStateMachine tests without hardware:** All `start()` patches include `heyvox.audio.media.pause_media`, `heyvox.audio.tts.set_recording`, and `heyvox.ipc.update_state` to prevent filesystem/network/audio side effects.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None. All 60 tests passed on first run (2 skipped: hook file documentation tests that require `~/.claude/hooks/` to exist).

## Next Phase Readiness

- TEST-01 and TEST-02 complete: pure function tests and state machine tests fully green
- TEST-03 and TEST-04 (HUD IPC and device selection) handled by Plan 09-02
- Full test suite ready for CI validation

---
*Phase: 09-test-suite*
*Completed: 2026-04-11*
