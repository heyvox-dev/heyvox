---
phase: 06-decomposition
plan: 01
subsystem: core
tags: [refactor, decomposition, app-context, text-processing, backward-compat]
dependency_graph:
  requires: []
  provides: [heyvox.app_context.AppContext, heyvox.text_processing.is_garbled, heyvox.text_processing.strip_wake_words]
  affects: [heyvox/main.py, tests/test_app_context.py]
tech_stack:
  added: [heyvox/app_context.py, heyvox/text_processing.py]
  patterns: [dataclass-dependency-injection, backward-compat-re-exports, type-checking-guard]
key_files:
  created:
    - heyvox/app_context.py
    - heyvox/text_processing.py
  modified:
    - heyvox/main.py
    - tests/test_app_context.py
decisions:
  - "Use dataclasses.field(default_factory=...) for all mutable defaults (Lock, Event, list) to prevent shared state between instances"
  - "Backward-compat re-export aliases in main.py preserve all existing test imports without modification"
  - "TYPE_CHECKING guard avoids circular imports at runtime (HUDClient, HeyvoxConfig referenced only as type hints)"
  - "Public API in text_processing.py uses unprefixed names (is_garbled, strip_wake_words); private prefix preserved in re-exports for test compat"
metrics:
  duration: 4min
  completed_date: "2026-04-11"
  tasks_completed: 2
  files_changed: 4
---

# Phase 06 Plan 01: AppContext Dataclass and Text Processing Extraction Summary

**One-liner:** Extracted 17+ module globals into AppContext dataclass and moved pure STT text processing functions to heyvox.text_processing with full backward-compat re-exports.

## What Was Built

### Task 1: AppContext dataclass + text_processing module
- **heyvox/app_context.py**: Typed dataclass with 20 fields covering all shared mutable state from main.py:
  - Recording state (lock, is_recording, busy, busy_since, audio_buffer, triggered_by_ptt, recording_target, cancel_transcription, shutdown, cancel_requested, adapter, last_inject_time, inject_lock)
  - Device state (consecutive_failed_recordings, zombie_mic_reinit, last_good_audio_time)
  - HUD state (hud_client, hud_last_reconnect, hud_last_level_send)
  - Process state (indicator_proc)
- **heyvox/text_processing.py**: Pure functions extracted from main.py:
  - `is_garbled(text)` — detects Whisper hallucinations and garbled output
  - `strip_wake_words(text, start_model, stop_model)` — removes wake word phrases from STT output
  - `_WAKE_WORD_PHRASES` — mapping of wake word model names to transcription variants
- **tests/test_app_context.py**: Removed `pytestmark = pytest.mark.skip` — all 3 tests now pass

### Task 2: Wire main.py re-exports and remove extracted code
- Added backward-compat re-export block in main.py so existing tests (`from heyvox.main import _strip_wake_words`) continue to work
- Removed ~181 lines from main.py: `_WAKE_WORD_PHRASES` dict, `_is_garbled()`, `_strip_wake_words()` function bodies
- Replaced extracted section with single comment: "Moved to heyvox/text_processing.py"
- All 28 tests pass (25 existing + 3 new app_context tests)

## Verification Results

1. `python -c "from heyvox.app_context import AppContext; ctx = AppContext(); print(type(ctx.lock), type(ctx.shutdown))"` — `<class '_thread.lock'> <class 'threading.Event'>` ✓
2. `python -c "from heyvox.text_processing import is_garbled, strip_wake_words; print('OK')"` — OK ✓
3. `python -c "from heyvox.main import _is_garbled, _strip_wake_words; print('re-exports OK')"` — re-exports OK ✓
4. `pytest tests/test_wake_word_strip.py tests/test_wakeword_trim.py tests/test_app_context.py` — 28 passed ✓
5. `grep -c "def _is_garbled" heyvox/main.py` — 0 ✓
6. `grep -c "def is_garbled" heyvox/text_processing.py` — 1 ✓

## Decisions Made

- **dataclasses.field(default_factory=...)**: All mutable defaults (Lock, Event, list) use factory functions to ensure each AppContext instance gets independent objects. This prevents shared-state bugs across tests.
- **Backward-compat re-exports**: Rather than updating all existing test imports (which would be noisy noise commits), re-export aliases `is_garbled as _is_garbled` and `strip_wake_words as _strip_wake_words` in main.py preserve the existing test API. These will be cleaned up in Phase 9.
- **TYPE_CHECKING guard**: HUDClient and HeyvoxConfig are referenced only as type hints in AppContext, using `if TYPE_CHECKING:` to avoid circular imports at runtime. The fields use `object` type at runtime.
- **Public API naming**: Functions in text_processing.py use unprefixed public names (`is_garbled`, `strip_wake_words`). The `_` prefix is preserved only in the re-export aliases for backward compat.

## Deviations from Plan

None — plan executed exactly as written.

## Self-Check

- [ ] heyvox/app_context.py exists — FOUND
- [ ] heyvox/text_processing.py exists — FOUND
- [ ] tests/test_app_context.py has no pytestmark skip — CONFIRMED
- [ ] commits 1292a5c and fbcc36e exist — CONFIRMED
