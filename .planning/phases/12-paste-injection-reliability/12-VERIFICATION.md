---
phase: 12-paste-injection-reliability
verified: 2026-04-13T00:00:00Z
status: human_needed
score: 11/12 must-haves verified
human_verification:
  - test: "Start HeyVox and dictate 5 phrases in Claude Code (Terminal/Conductor). Verify each pastes correctly on first attempt."
    expected: "Text appears in the agent input. No error cue. Logs show correct settle delay for Conductor (0.3s)."
    why_human: "Live clipboard + osascript paste cannot be verified without a running macOS session and a real target app."
  - test: "Open Cursor, dictate 5 phrases, verify paste works reliably."
    expected: "Text pastes on first attempt. Settle delay should be 0.15s (visible in logs)."
    why_human: "Electron app focus timing can only be validated empirically on the real hardware."
  - test: "Trigger clipboard theft: start a dictation, then immediately Cmd-C something else mid-transcription."
    expected: "error.aiff plays. Wrong content is NOT pasted. HUD shows 'Paste failed'."
    why_human: "Race condition requires real-time interaction between two concurrent clipboard writers."
  - test: "Confirm that PASTE-06 settle delay defaults are empirically correct (no adjustment needed based on Plan 03 checkpoint)."
    expected: "User typed 'approved' at the Plan 03 checkpoint. Document any timing adjustments made."
    why_human: "Plan 03 Task 2 is a blocking human checkpoint — empirical timing validation cannot be automated."
---

# Phase 12: Paste Injection Reliability — Verification Report

**Phase Goal:** Replace slow/unreliable pbcopy subprocess with NSPasteboard API, add per-app settle delays, clipboard race detection, focus verification, AX fast-path for native text fields, error cue on failure, and integrate paste success/failure signaling into recording.py.
**Verified:** 2026-04-13
**Status:** human_needed
**Re-verification:** No — initial verification

---

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|---------|
| 1 | `_set_clipboard` uses `AppKit.NSPasteboard.generalPasteboard()`, not subprocess pbcopy | VERIFIED | `injection.py:107` — `pb = AppKit.NSPasteboard.generalPasteboard()` inside `_set_clipboard`. No pbcopy grep match. |
| 2 | `get_clipboard_text` uses `AppKit.NSPasteboard`, not osascript subprocess | VERIFIED | `injection.py:511-515` — `pb.stringForType_(AppKit.NSPasteboardTypeString)` |
| 3 | Per-app settle delay resolved from config dict by case-insensitive substring match | VERIFIED | `_settle_delay_for` at `injection.py:202-216`. `InjectionConfig.app_delays` in `config.py:190-197`. |
| 4 | `_osascript_type_text` uses Python `time.sleep` for settle delay, no AppleScript `delay 0.3` | VERIFIED | `injection.py:297` — `time.sleep(settle_secs)`. The `already_frontmost` branch has no `delay` AppleScript; the non-frontmost branch has `delay 0.2` (focus settle only when switching). |
| 5 | `error.aiff` exists in `heyvox/cues/` for paste failure notification | VERIFIED | `ls heyvox/cues/` confirms `error.aiff` present. |
| 6 | Clipboard `changeCount` captured after write, verified before Cmd-V | VERIFIED | `injection.py:279` — `ok, expected_count = _set_clipboard(text)`. `injection.py:300` — `_clipboard_still_ours(expected_count)`. |
| 7 | Paste aborts and `error.aiff` plays when changeCount changes between write and paste | VERIFIED | `injection.py:300-308` — abort path with `audio_cue("error")` on clipboard theft. Tests in `TestClipboardRace.test_paste_aborts_on_stolen_clipboard` pass. |
| 8 | AX fast-path injects text directly into `AXTextField`/`AXTextArea` without clipboard | VERIFIED | `_ax_inject_text` at `injection.py:167-199`. Skips clipboard, uses `AXUIElementSetAttributeValue`. |
| 9 | AX fast-path is NOT attempted on `AXWebArea` (Electron apps) | VERIFIED | `injection.py:185` — `snap.element_role not in _AX_NATIVE_ROLES` returns `False`. `_AX_NATIVE_ROLES = frozenset({"AXTextField", "AXTextArea"})`. |
| 10 | `type_text` returns a bool indicating success/failure | VERIFIED | `injection.py:416-459` — signature `-> bool`, all paths return `True` or `False`. |
| 11 | `recording.py` plays error cue on paste failure instead of ok cue | VERIFIED | `recording.py:685-698` — `if not paste_ok: ... _log("Paste FAILED — error cue played by injection")` — error cue delegated to `type_text`; ok/sending cue only plays on `paste_ok=True`. |
| 12 | Paste works reliably in non-Conductor apps (Cursor, Windsurf, VS Code, iTerm, etc.) | HUMAN NEEDED | Integration tests pass (24/24 in `TestMultiAppInjection`). Empirical human validation (Plan 03 Task 2 checkpoint) was the gate — requires user to confirm. |

**Score:** 11/12 truths verified (12th requires human confirmation of Plan 03 checkpoint)

---

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `heyvox/input/injection.py` | NSPasteboard clipboard write/read, per-app settle delay, race detection, AX fast-path, focus verify, error cue, bool return | VERIFIED | All 7 functions present and substantive: `_set_clipboard`, `get_clipboard_text`, `_settle_delay_for`, `_clipboard_still_ours`, `_verify_target_focused`, `_ax_inject_text`, `_osascript_type_text`, `type_text` |
| `heyvox/config.py` | `InjectionConfig` nested model with `app_delays` dict | VERIFIED | `config.py:183-197` — `class InjectionConfig(BaseModel)` with `focus_settle_secs`, `max_retries`, `app_delays`. Field wired into `HeyvoxConfig` at line 265. |
| `heyvox/cues/error.aiff` | Error audio cue file | VERIFIED | File present in `heyvox/cues/`. |
| `heyvox/recording.py` | `paste_ok = type_text(...)` integration, HUD/cue/Enter gated on result | VERIFIED | `recording.py:644-698` — `paste_ok` captures result, gates auto-Enter at line 660, gates cue/HUD at lines 685-698. |
| `tests/test_injection.py` | NSPasteboard mocks, `TestSettleDelay`, `TestClipboardRace`, `TestFocusVerification`, `TestAXFastPath`, `TestErrorCue` | VERIFIED | All 6 test classes present. 41 tests collected and passing. |
| `tests/test_multiapp_injection.py` | `TestMultiAppInjection` covering all app types | VERIFIED | File created with 24 tests. All pass. |

---

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `injection.py` | `AppKit.NSPasteboard` | `import AppKit` inside function body | VERIFIED | `injection.py:106`, `512` — `import AppKit` inside `_set_clipboard` and `get_clipboard_text` |
| `injection.py` | `AppKit.NSWorkspace` | `_verify_target_focused` | VERIFIED | `injection.py:151-152` — `AppKit.NSWorkspace.sharedWorkspace()` |
| `injection.py` | `heyvox/config.py` | `InjectionConfig` consumed by `_settle_delay_for` | VERIFIED | `recording.py:635` — `injection_cfg = getattr(self.config, "injection", None)`, then passed to `_settle_delay_for` |
| `injection.py` | `heyvox/audio/cues.py` | `audio_cue("error")` on paste failure | VERIFIED | `injection.py:16` — `from heyvox.audio.cues import audio_cue`. Called at `injection.py:275, 283, 288, 307, 351` |
| `recording.py` | `injection.py` | `type_text` return value determines cue | VERIFIED | `recording.py:418` imports `type_text, save_frontmost_pid, restore_frontmost, _settle_delay_for`. `recording.py:644` — `paste_ok = type_text(...)` |
| `recording.py` | `InjectionConfig` | `self.config.injection.app_delays` passed to `_settle_delay_for` | VERIFIED | `recording.py:635-643` — `getattr(self.config, "injection", None)` then reads `injection_cfg.app_delays` and `injection_cfg.focus_settle_secs` |

---

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|--------------|--------|-------------------|--------|
| `recording.py` paste path | `paste_ok` | `type_text(...)` return value | Yes — bool from real osascript/AX/clipboard result | FLOWING |
| `recording.py` settle delay | `settle` | `_settle_delay_for(target_app, injection_cfg.app_delays, ...)` | Yes — reads `InjectionConfig.app_delays` dict from `HeyvoxConfig` | FLOWING |
| `injection.py _set_clipboard` | `(bool, int)` | `NSPasteboard.setString_forType_` + `changeCount()` | Yes — real NSPasteboard API calls | FLOWING |
| `injection.py _clipboard_still_ours` | `bool` | `pb.changeCount() == expected_count` | Yes — compares live changeCount to captured value | FLOWING |

---

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| All injection tests pass | `python -m pytest tests/test_injection.py tests/test_multiapp_injection.py -q --tb=no` | 65 passed | PASS |
| Full suite passes (432 tests) | `python -m pytest tests/ -q --tb=no` | 432 passed, 2 skipped, 20 deselected | PASS |
| No `pbcopy` in clipboard functions | `grep -c "pbcopy" heyvox/input/injection.py` | 0 | PASS |
| NSPasteboard present in injection.py | `grep "NSPasteboard" heyvox/input/injection.py` | Multiple matches at lines 99, 107, 109, 113, 120 | PASS |
| `_clipboard_still_ours` exists | Pattern match | Found at `injection.py:117` | PASS |
| `_verify_target_focused` exists | Pattern match | Found at `injection.py:134` | PASS |
| `_ax_inject_text` exists | Pattern match | Found at `injection.py:167` | PASS |
| `paste_ok` in `recording.py` | Pattern match | Found at `recording.py:622, 644, 660, 666, 685` | PASS |
| `audio_cue.*error` in `injection.py` | Pattern match | Found at lines 275, 283, 288, 307, 351 | PASS |
| `error.aiff` exists | `ls heyvox/cues/` | Listed | PASS |
| Real macOS paste in non-Conductor apps | Requires live macOS session | N/A | SKIP — human required |

---

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|---------|
| PASTE-01 | 12-01 | NSPasteboard direct replaces pbcopy subprocess for clipboard writes | SATISFIED | `_set_clipboard` at `injection.py:98-114` uses `NSPasteboard.generalPasteboard()`. `get_clipboard_text` at `injection.py:505-516` uses NSPasteboard. No pbcopy. |
| PASTE-02 | 12-02 | Configurable focus settle delay with retry on focus steal | SATISFIED | `_clipboard_still_ours` + retry loop in `_osascript_type_text` (lines 278-310). `max_retries` param threaded through `type_text`. |
| PASTE-03 | 12-01 | Per-app delay profiles (Conductor, Cursor, Windsurf, Terminal, generic) | SATISFIED | `InjectionConfig.app_delays` in `config.py:190-197` has all 6 profiles. `_settle_delay_for` resolves them case-insensitively. |
| PASTE-04 | 12-02 | AXUIElement fast-path for native AppKit apps | SATISFIED | `_ax_inject_text` at `injection.py:167-199`. Skips AXWebArea. `type_text` tries it before clipboard path. |
| PASTE-05 | 12-02 | Focus detection verifies correct app/field before paste, with fallback | SATISFIED | `_verify_target_focused` at `injection.py:134-160`. Checks `frontmostApplication().bundleIdentifier()`. Fails-open on exception. Abort + error cue on mismatch. |
| PASTE-06 | 12-03 | Paste works reliably in non-Conductor apps (Cursor, Windsurf, VS Code, iTerm, etc.) | PARTIAL — HUMAN NEEDED | 24 integration tests in `TestMultiAppInjection` pass, covering all app types and delay profiles. Plan 03 Task 2 (blocking human checkpoint) was the empirical gate — requires user to confirm paste works in at least 2 real apps with the correct settle delay. |

---

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `heyvox/recording.py` | 622 | `paste_ok = True` initialized before `if not _injected_via_conductor:` block | Info | When `_injected_via_conductor = True` (currently always False), paste_ok defaults True, so the ok cue plays. Comment documents this is intentional future-proofing. Not a bug. |
| `heyvox/input/injection.py` | 335-339 | `delay 0.2` remains in AppleScript for the non-frontmost activation path | Info | Not a regression — this is the activation delay when the app needs to be brought to front, separate from the configurable Python settle delay. Both are needed: AppleScript `delay 0.2` lets the window manager respond to `set frontmost to true`, Python `settle_secs` is the per-app input-focus wait. Plan correctly removed the old hardcoded `delay 0.3` from the paste path. |

No blocker or warning-level anti-patterns found.

---

### Human Verification Required

#### 1. Live paste in Conductor / Claude Code

**Test:** Start HeyVox (`python -m heyvox start`). Open Claude Code or Terminal running Claude. Dictate 5 phrases. Watch for successful paste into the agent input.
**Expected:** Text appears in the agent input each time. No error cue (`error.aiff`). Logs (`grep "settle" /tmp/heyvox.log`) show `settle=0.3` for Conductor target.
**Why human:** Live clipboard write + osascript Cmd-V to a real Electron/Tauri app cannot be simulated in a unit test. The focus timing (0.3s settle) is only validated on real hardware.

#### 2. Live paste in Cursor or Windsurf

**Test:** With HeyVox running, switch to Cursor. Dictate 5 phrases into the Cursor chat input.
**Expected:** Text pastes reliably on first attempt. Logs show `settle=0.15`. If any paste fails, increase `cursor` delay in `~/.config/heyvox/config.yaml` under `injection.app_delays`.
**Why human:** Electron app focus timing is hardware- and load-dependent. The 0.15s default is a best-practice starting point, not a guaranteed value for all machines.

#### 3. Clipboard theft detection

**Test:** Start a dictation (wake word or PTT). While transcription is happening, press Cmd-C to copy something. Observe what happens when the paste fires.
**Expected:** `error.aiff` plays. The wrong-content (what you Cmd-C'd) is NOT pasted into the app. HUD shows "Paste failed".
**Why human:** The race between the user's Cmd-C and the `_clipboard_still_ours` check happens in real time — timing cannot be reproduced deterministically in unit tests.

#### 4. PASTE-06 empirical timing confirmation (Plan 03 checkpoint gate)

**Test:** Confirm whether the Plan 03 Task 2 human checkpoint was formally approved. The SUMMARY.md shows `Status: CHECKPOINT PENDING — awaiting human validation (Task 2)`.
**Expected:** User typed "approved" (or equivalent) at the checkpoint. Settle delay defaults were confirmed correct or adjusted. If not yet done, this is a blocking gate for PASTE-06 completion.
**Why human:** Plan 03 Task 2 is explicitly marked `type: checkpoint:human-verify` with `gate: blocking`. The summary says checkpoint is pending.

---

### Gaps Summary

No implementation gaps found. All code artifacts exist, are substantive (not stubs), and are correctly wired together. The 432-test suite passes.

The single open item is **PASTE-06 human validation**: the Plan 03 Task 2 blocking checkpoint. The code implementing multi-app paste reliability is complete and tested, but the empirical human validation was the formal gate specified in the plan. The SUMMARY.md for Plan 03 explicitly documents "Status: CHECKPOINT PENDING."

If the user already approved the checkpoint verbally / in-session (context indicates "User approved the checkpoint after confirming paste works when cursor is in the textbox"), the remaining open question is whether the formal SUMMARY.md needs updating to reflect that approval and whether any timing values were adjusted.

---

_Verified: 2026-04-13_
_Verifier: Claude (gsd-verifier)_
