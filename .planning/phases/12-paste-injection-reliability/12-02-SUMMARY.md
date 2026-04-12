---
phase: 12-paste-injection-reliability
plan: 02
subsystem: input
tags: [injection, clipboard, changecount, ax-fastpath, focus-verification, error-cue, tdd, recording]

# Dependency graph
requires:
  - phase: 12-01
    provides: NSPasteboard _set_clipboard returns (bool, int) tuple, InjectionConfig, error.aiff
  - phase: 06-decomposition
    provides: config.py nested model pattern (InjectionConfig, HeyvoxConfig)
provides:
  - _clipboard_still_ours(expected_count): changeCount race detection before Cmd-V
  - _verify_target_focused(bundle_id): proactive focus verification via NSWorkspace (PASTE-05)
  - _ax_inject_text(snap, text): AX fast-path for AXTextField/AXTextArea native text fields (PASTE-04)
  - type_text returns bool (success/failure), accepts snap + settle_secs + max_retries
  - recording.py integrates paste_ok for HUD message, cue selection, auto-Enter gating
affects: [recording.py, tests/test_injection.py, tests/test_injection_enter.py]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "_clipboard_still_ours: post-write changeCount snapshot compared before Cmd-V (sub-50ms window)"
    - "_verify_target_focused: NSWorkspace.frontmostApplication().bundleIdentifier() before paste"
    - "_ax_inject_text: AXUIElementSetAttributeValue('AXValue') skips clipboard for native AppKit fields"
    - "audio_cue import at module level — mockable via patch('heyvox.input.injection.audio_cue')"
    - "type_text fallback chain: Chrome → AX fast-path → clipboard + Cmd-V"

key-files:
  created: []
  modified:
    - heyvox/input/injection.py
    - heyvox/recording.py
    - tests/test_injection.py
    - tests/test_injection_enter.py

key-decisions:
  - "_verify_target_focused fails-open on exception: don't block paste if NSWorkspace check throws"
  - "audio_cue imported at module level (not inline) to enable mock patching in tests"
  - "_ax_inject_text explicitly skips AXWebArea — Electron/WebKit AXValue writes have no effect"
  - "paste_ok initialized to True before the _injected_via_conductor branch (future-proofing)"
  - "test_injection_enter.py stale pbcopy tests replaced with NSPasteboard mocks (Rule 1 auto-fix)"

requirements-completed: [PASTE-02, PASTE-04, PASTE-05]

# Metrics
duration: 4min
completed: 2026-04-12
---

# Phase 12 Plan 02: Race Detection, Focus Verification, AX Fast-Path, Error Cue Integration Summary

**changeCount race detection + NSWorkspace focus verify + AX fast-path for native AppKit fields + error cue on all failure modes, with recording.py paste_ok gating HUD/cue/Enter**

## Performance

- **Duration:** 4 min
- **Started:** 2026-04-12T19:25:28Z
- **Completed:** 2026-04-12T19:29:51Z
- **Tasks:** 2 (TDD: RED+GREEN for Task 1; direct implementation for Task 2)
- **Files modified:** 4

## Accomplishments

- Added `_clipboard_still_ours(expected_count)`: compares NSPasteboard changeCount to detect clipboard theft during settle delay
- Added `_verify_target_focused(expected_bundle_id)`: checks NSWorkspace frontmost app bundle ID before paste (PASTE-05); fails-open on exception
- Added `_ax_inject_text(snap, text)`: injects text directly via AXUIElementSetAttributeValue('AXValue') for AXTextField/AXTextArea; explicitly skips AXWebArea; falls through to clipboard path on failure
- Updated `_osascript_type_text`: returns bool, adds focus verify pre-paste, changeCount check post-settle with retry loop, error cue on all failure modes (write fail, clipboard theft, focus mismatch, osascript fail)
- Updated `type_text`: returns bool, accepts snap + settle_secs + max_retries, tries AX fast-path before clipboard path
- Updated `recording.py`: captures `paste_ok = type_text(...)`, passes recording_target as snap, resolves per-app settle from InjectionConfig, gates auto-Enter on paste_ok, shows "Paste failed" HUD on failure
- Fixed stale `test_injection_enter.py` tests that expected pbcopy (replaced by NSPasteboard in Plan 01)
- Test count: 21 → 41 in test_injection.py; 7 → 9 in test_injection_enter.py; 408 total pass

## Task Commits

1. **Task 1 RED: Write failing tests** - `c2ed04f` (test)
2. **Task 1 GREEN: Implement injection.py functions** - `8b0f297` (feat)
3. **Task 2: Integrate recording.py + fix stale tests** - `c5b1ead` (feat)

## Files Created/Modified

- `heyvox/input/injection.py` — added _clipboard_still_ours, _verify_target_focused, _ax_inject_text, updated _osascript_type_text (bool return, focus verify, changeCount retry, error cue), updated type_text (bool return, snap param, AX fast-path)
- `heyvox/recording.py` — paste_ok captures type_text result, _settle_delay_for wired from InjectionConfig, snap passed to type_text, HUD/cue/Enter gated on paste_ok
- `tests/test_injection.py` — added TestClipboardRace, TestFocusVerification, TestAXFastPath, TestErrorCue (20 new tests)
- `tests/test_injection_enter.py` — replaced pbcopy-based TestTypeText with NSPasteboard mock tests (Rule 1 auto-fix)

## Decisions Made

- `_verify_target_focused` fails-open: if NSWorkspace check throws, returns True so paste proceeds rather than silently failing — paste to the right app is best-effort; aborting on check failure would be worse
- `audio_cue` imported at module level rather than inline — enables `patch("heyvox.input.injection.audio_cue")` in tests
- `_ax_inject_text` explicitly checks `snap.element_role not in _AX_NATIVE_ROLES` — AXWebArea silently accepts AXValue writes in some contexts but doesn't display them; safer to skip
- `paste_ok = True` initialized before `if not _injected_via_conductor:` block — when conductor path is active (currently always False), paste_ok defaults to True (no cue played)

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Stale pbcopy tests in test_injection_enter.py**
- **Found during:** Task 2 verification (full test suite run)
- **Issue:** `TestTypeText.test_type_text_sets_clipboard` and `test_type_text_escapes_quotes` still expected `pbcopy` subprocess calls. Plan 01 replaced pbcopy with NSPasteboard, making these assertions incorrect.
- **Fix:** Updated both test methods to use NSPasteboard mock pattern (same as test_injection.py) — mock AppKit.NSPasteboard.generalPasteboard(), verify setString_forType_ was called with the text.
- **Files modified:** tests/test_injection_enter.py
- **Verification:** 9 tests pass (up from 7 pre-fix, 1 failure)

---

**Total deviations:** 1 auto-fixed (Rule 1 — stale test assertions from prior plan's pbcopy→NSPasteboard migration)

## Known Stubs

None — all three new functions are fully implemented and tested. The AX fast-path fallback chain (Chrome → AX → clipboard) is complete.

## Self-Check: PASSED

- heyvox/input/injection.py: FOUND
- heyvox/recording.py: FOUND
- tests/test_injection.py: FOUND
- tests/test_injection_enter.py: FOUND
- Commit c2ed04f (test RED): FOUND
- Commit 8b0f297 (feat GREEN): FOUND
- Commit c5b1ead (feat Task 2): FOUND

---
*Phase: 12-paste-injection-reliability*
*Completed: 2026-04-12*
