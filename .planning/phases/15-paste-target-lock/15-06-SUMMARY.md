---
phase: 15
plan: 06
title: Post-paste verification (verify_paste + drift detection + retry)
status: complete
completed: 2026-04-24
requirements: [R7]
addendum_applied: [W3, W7, W11, W12]
---

## What Shipped

AX-value post-paste verification with single retry + drift toast — SPEC R7
fully implemented. Tier-2 (element=None) gets strong AX verification via
live `AXFocusedUIElement` re-acquire (W3 graceful degradation to
focus-unchanged if acquire fails).

## Key Additions to heyvox/input/target.py

```python
_WS_RUN = re.compile(r"\s+")
def _normalize_text(s: str) -> str             # strip + collapse WS, case preserved (D-05)
def _read_ax_value(element) -> Optional[str]   # element AXValue read, None on error
def _focus_unchanged(lock) -> bool              # bundle-id frontmost check (D-07)
def _acquire_focused_element(lock)              # W3: Tier-2 live element acquire

@dataclass(frozen=True)
class VerifyResult:
    verified: bool
    retried: bool
    drift: bool
    detail: str = ""

def verify_paste(lock, element, transcript, profile) -> VerifyResult
```

## [PASTE] Log Line Variants (4 outcomes)

```
[PASTE] verified=true retried=false drift=false (ax_value_len=N)
[PASTE] verified=true retried=true  drift=false (retry-ax_value_len=N)
[PASTE] verified=false retried=false drift=true (non-AX focus moved ... )
[PASTE] verified=false retried=true  drift=true (drift first_len=X second_len=Y)
```

Plus:
```
[PASTE] verify: profile=None (treating as AX-capable, app='...')
[PASTE] verify: re-acquired focused element for Tier-2 AX verify (app='...')
[PASTE] first-verify miss (ax_value_len=N), re-setting clipboard + retrying paste
```

## Addendum Corrections Applied

**W3 — Tier-2 element acquisition:** When resolve_lock returned tier 2
(element=None), earlier iterations would fall back to focus-unchanged only,
losing strong content verification. Fix: `_acquire_focused_element(lock)`
calls `AXFocusedUIElement` on the locked app's live PID and runs FULL AX
content readback. Graceful degradation: if the acquire fails (focus moved
to a different app), falls back to focus-unchanged with `detail="tier2-acquire-fail-focus-unchanged"`.

**W7 — profile=None log:** When profile is None, verify_paste takes the
AX-capable path. A new explicit log line `[PASTE] verify: profile=None
(treating as AX-capable, app='...')` makes debugging unambiguous.

**W11 — clipboard re-set on retry:** Before the retry Cmd+V, `_set_clipboard(transcript)`
is called explicitly so the retry pastes the transcript (not whatever the
target app may have mutated the pasteboard to between attempts).

**W12 — defensive outcome gate in recording.py:**
`if paste_ok and recording_target is not None and outcome is not None and outcome.ok:`
Prevents AttributeError on `outcome.element` when `recording_target` was None
upstream (15-05 explicitly initializes `outcome = None` per W6).

## Tests (16 new, all pass in 0.14s)

Located in `tests/test_verify_paste.py`:
1. `test_normalize_collapses_whitespace` — strip + collapse WS
2. `test_normalize_preserves_case` — D-05 case preservation
3. `test_normalize_handles_none` — None input returns ""
4. `test_verify_result_is_frozen` — FrozenInstanceError invariant
5. `test_verify_succeeds_first_try_when_ax_value_contains_transcript`
6. `test_verify_drift_after_retry_resets_clipboard` — W11
7. `test_verify_succeeds_on_retry` — first-miss-then-match
8. `test_non_ax_profile_focus_unchanged_verifies`
9. `test_non_ax_profile_focus_moved_drift`
10. `test_tier2_acquires_focused_element_when_element_none` — W3
11. `test_tier2_acquire_fail_falls_back_to_focus_unchanged` — W3 degradation
12. `test_tier2_acquire_fail_focus_moved_drift`
13. `test_verify_with_profile_none_emits_explicit_log` — W7
14. `test_verified_true_first_try_log` — log format check
15. `test_drift_log_format` — drift log format check
16. `test_recording_send_local_has_defensive_outcome_guard` — W12 sentinel

## Integration in recording.py::_send_local

```python
if paste_ok and recording_target is not None and outcome is not None and outcome.ok:
    from heyvox.input.target import verify_paste
    verify = verify_paste(recording_target, outcome.element, paste_text, profile)
    if verify.drift:
        # Content WAS sent (just possibly mis-placed). Don't downgrade paste_ok.
        audio_cue("error", cues_dir)
        show_failure_toast(drift_message, title="HeyVox paste drift")
```

## Acceptance Criteria — all met

- [x] `def verify_paste` + helpers present
- [x] `class VerifyResult` frozen (grep: frozen=True matches at least 3)
- [x] 4 log line variants emitted
- [x] `AXFocusedUIElement` referenced (W3)
- [x] `_set_clipboard(transcript)` called on retry (W11)
- [x] `profile=None` explicit log line (W7)
- [x] `outcome is not None and outcome.ok` defensive gate (W12)
- [x] All 16 tests pass

## Regression Audit

Post-15-06 full suite run: same 23 failures as pre-15-02 baseline.
Zero new regressions from Plan 15-06.

## Self-Check: PASSED

SPEC R7 implemented. All W3/W7/W11/W12 corrections applied and verified
by tests. Drift outcome surfaces to user via error cue + toast without
invalidating the paste.
