---
phase: 12-paste-injection-reliability
plan: 01
subsystem: input
tags: [nspasteboard, clipboard, injection, appkit, pyobjc, osascript, tdd]

# Dependency graph
requires:
  - phase: 08-ipc-consolidation
    provides: constants.py IPC paths (HUSH_SOCK used in injection.py)
  - phase: 06-decomposition
    provides: config.py nested model pattern (WakeWordConfig, EchoSuppressionConfig)
provides:
  - NSPasteboard direct clipboard write/read (no subprocess pbcopy)
  - InjectionConfig nested Pydantic model with per-app settle delay profiles
  - _settle_delay_for() helper for case-insensitive substring app delay resolution
  - error.aiff cue file for paste failure notification
  - _set_clipboard returning (bool, int) tuple with changeCount for race detection
affects: [13-paste-injection-reliability, recording.py, adapter usage of type_text]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "NSPasteboard direct API instead of subprocess pbcopy (zero fork overhead)"
    - "Per-app settle delay via case-insensitive substring match dict lookup"
    - "TDD red-green cycle: failing tests committed, then implementation makes them pass"

key-files:
  created:
    - heyvox/cues/error.aiff
  modified:
    - heyvox/input/injection.py
    - heyvox/config.py
    - tests/test_injection.py

key-decisions:
  - "NSPasteboard replaces pbcopy subprocess: zero fork overhead, atomic, in-process API"
  - "InjectionConfig defaults: Conductor 0.3s (Tauri), Cursor/Windsurf/VSCode 0.15s (Electron), iTerm2/Terminal 0.03s (native AppKit)"
  - "error.aiff source: /System/Library/Sounds/Sosumi.aiff — canonical macOS error sound, AIFF format, no deps"
  - "test_no_clipboard_restore assertion updated from count==2 to count==3+NSPasteboard assertion — same invariant, correct for NSPasteboard world"

patterns-established:
  - "Clipboard write: always pb.clearContents() before pb.setString_forType_() to prevent mixed-type leakage"
  - "Per-app delay lookup: _settle_delay_for(app_name, delays_dict, default) — case-insensitive substring"
  - "AppKit import inside function body: allows mocking via sys.modules patch in tests"

requirements-completed: [PASTE-01, PASTE-03]

# Metrics
duration: 8min
completed: 2026-04-12
---

# Phase 12 Plan 01: NSPasteboard Clipboard Migration Summary

**NSPasteboard direct API replaces pbcopy subprocess for clipboard write/read, with InjectionConfig per-app settle delays (Conductor 0.3s, Cursor 0.15s, iTerm2 0.03s) and error.aiff cue file**

## Performance

- **Duration:** 8 min
- **Started:** 2026-04-12T18:46:23Z
- **Completed:** 2026-04-12T18:54:00Z
- **Tasks:** 2 (TDD: RED + GREEN)
- **Files modified:** 4

## Accomplishments

- Replaced pbcopy subprocess with AppKit.NSPasteboard.generalPasteboard() for zero-fork clipboard writes
- Added InjectionConfig Pydantic model to HeyvoxConfig with configurable per-app focus settle delays
- Added _settle_delay_for() helper: case-insensitive substring match dict lookup for app delay profiles
- Added error.aiff (Sosumi) to heyvox/cues/ for paste failure notification
- Removed hardcoded AppleScript "delay 0.3" from _osascript_type_text; now controlled by settle_secs Python param
- All 21 injection tests pass (up from 13 pre-task-1)

## Task Commits

1. **Task 1: Write failing tests for NSPasteboard migration and per-app settle delays** - `b4d7be1` (test)
2. **Task 2: Implement NSPasteboard + InjectionConfig + error.aiff + _settle_delay_for** - `a4c0fa7` (feat)

## Files Created/Modified

- `heyvox/input/injection.py` - _set_clipboard uses NSPasteboard (returns tuple), get_clipboard_text uses NSPasteboard, added _settle_delay_for, _osascript_type_text takes settle_secs param
- `heyvox/config.py` - Added InjectionConfig nested model before EchoSuppressionConfig, added injection field to HeyvoxConfig
- `heyvox/cues/error.aiff` - Created from /System/Library/Sounds/Sosumi.aiff (canonical macOS error sound)
- `tests/test_injection.py` - Updated TestTypeText and TestGetClipboardText to mock AppKit.NSPasteboard; added TestSettleDelay class with 5 tests

## Decisions Made

- NSPasteboard import stays inside function body (`import AppKit` inside `_set_clipboard` and `get_clipboard_text`) to allow test mocking via `patch.dict("sys.modules", {"AppKit": mock_appkit})` — changing to module-level import would break the mock pattern
- test_no_clipboard_restore assertion updated from `mock_run.call_count == 2` (old: pbcopy + Cmd-V) to `mock_run.call_count == 3` (new: get_frontmost_before + Cmd-V + get_frontmost_after) plus `mock_pb.setString_forType_.assert_called_once()` — preserves the invariant (NSPasteboard never called twice), matches the new implementation
- InjectionConfig.injection field not yet wired into _osascript_type_text callers — settle_secs defaults to 0.1 but type_text doesn't pass config through yet; this is Wave 2 work (PASTE-02 changeCount race detection connects the wiring)

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Updated test_no_clipboard_restore assertion count**
- **Found during:** Task 2 (GREEN phase implementation)
- **Issue:** Test expected `mock_run.call_count == 1` (old world: pbcopy removed, only Cmd-V left). Actual count after NSPasteboard migration is 3: get_frontmost_before + Cmd-V + get_frontmost_after.
- **Fix:** Updated assertion to `count == 3` and added `mock_pb.setString_forType_.assert_called_once()` — the NSPasteboard "no restore" invariant is now asserted directly.
- **Files modified:** tests/test_injection.py
- **Verification:** All 21 tests pass
- **Committed in:** a4c0fa7 (Task 2 commit)

---

**Total deviations:** 1 auto-fixed (Rule 1 - bug in test assertion count)
**Impact on plan:** Required to make tests reflect reality. The invariant (no clipboard restore) is preserved and now tested more precisely.

## Issues Encountered

None - implementation matched the plan specifications exactly.

## Known Stubs

None - InjectionConfig is defined and defaults are set. The wiring of `config.injection.app_delays` into `_osascript_type_text` call sites is a Wave 2 concern (depends on how type_text receives config, PASTE-02/PASTE-05 scope).

## Next Phase Readiness

- NSPasteboard clipboard foundation complete — Wave 2 (PASTE-02 changeCount race detection, PASTE-04 AX fast-path, PASTE-05 error cue integration) can build on this
- InjectionConfig model ready; next plan should wire it into type_text call sites via config parameter
- error.aiff cue ready for use in recording.py paste failure detection

---
*Phase: 12-paste-injection-reliability*
*Completed: 2026-04-12*
