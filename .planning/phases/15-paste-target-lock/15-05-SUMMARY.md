---
phase: 15
plan: 05
title: Resolve ladder + fail-closed pipeline (resolve_lock + integration)
status: complete
completed: 2026-04-24
requirements: [R4, R5, R6, R8]
addendum_applied: [W10, Fact-4, Fact-5, Fact-6, W13]
---

## What Shipped

The heart of Phase 15. Three-tier paste resolver with unconditional
yank-back, fail-closed pipeline, and first caller for `app_fast_paste`.

- `~/.local/bin/conductor-switch-workspace` extended with `--id` and
  `--session` flags (B4). sqlite UPDATE for session is idempotent.
- `heyvox/input/target.py`: added `FailReason` enum, `PasteOutcome` frozen
  dataclass, `_walk_role_path`, `_find_window_by_number`,
  `_yank_back_app_and_workspace`, and `resolve_lock`. Deleted
  `restore_target`, `_walk_ax_tree`, `_find_window_text_fields`, and the
  `focus_app` osascript fallback in `_activate_app` (W10/Fact 4).
- `heyvox/recording.py::_send_local`: migrated to `resolve_lock` +
  `app_fast_paste` + fail-closed branch (clipboard + `audio_cue('error')`
  + `show_failure_toast`). History write at line 639-640 untouched (Fact 2).
- 12 new unit tests in `tests/test_resolve_lock.py` — all green.

## PasteOutcome Shape

```python
@dataclass(frozen=True)
class PasteOutcome:
    ok: bool
    element: Any = None        # AXUIElement on tier-1 Ok
    tier_used: int = 0          # 1, 2, or 0 (fail-closed)
    reason: Optional[FailReason] = None
    message: str = ""           # toast/log text
    elapsed_ms: int = 0
```

## FailReason Taxonomy + Toast Strings (post-W13)

```
no_text_field_at_start
  "HeyVox ({app_name}): transcript on clipboard — no text field was
   focused when you started speaking."

multi_field_no_shortcut
  "HeyVox ({app_name}): transcript on clipboard — this app has
   multiple inputs and no configured chat shortcut."

target_unreachable
  "HeyVox: transcript on clipboard — original {app_name} target
   is unreachable."
```

All three format uniformly via `.format(app_name=X)` — guarded by
`test_all_fail_reason_messages_format_with_app_name`.

## Log Line Formats ([PASTE] markers per D-26)

```
[PASTE] tier_used=1 reason=n/a elapsed_ms={ms}
[PASTE] tier_used=2 reason=n/a elapsed_ms={ms}
[PASTE] tier_used=fail_closed reason={no_text_field_at_start|multi_field_no_shortcut|target_unreachable} elapsed_ms={ms}
[PASTE] outcome ok={bool} tier_used={int} reason={name|n/a} elapsed_ms={ms}   # from recording.py
[PASTE] FAIL_CLOSED reason={name} message={str}                                # recording.py fail-closed
```

## conductor-switch-workspace Extensions (B4)

New script supports:
- `conductor-switch-workspace <search>` — positional (back-compat)
- `conductor-switch-workspace --id <uuid> [--force]` — direct workspace ID
- `conductor-switch-workspace --id <ws_uuid> --session <sess_uuid> [--force]`
  — session activation via idempotent sqlite UPDATE

Verified:
- No `RECORDING_FLAG` references (B3 — bypass not needed)
- Existing DEF-074 Hammerspoon liveness gate preserved
- Existing idle-time gate preserved (`--force` bypasses)

## History Write Evidence (Fact 2)

```
recording.py:639:                    from heyvox.history import save as _save_transcript
recording.py:640:                    _save_transcript(text, duration=duration, ptt=ptt)
```

Called unconditionally BEFORE the paste branch. SPEC R5 is satisfied with
zero structural changes. W5 test `test_heyvox_history_save_is_the_patch_target`
guards against future patches targeting a nonexistent `_add_history` method.

## focus_app NOT Imported (W10 / Fact 4)

```
grep -nc "from heyvox.input.injection import focus_app" heyvox/input/target.py  → 0
grep -nE "focus_app\(" heyvox/input/target.py                                    → 0
```

Saved ~50ms per paste (no redundant `tell application ... activate`
osascript fork on top of NSRunningApplication bundle-ID activation).

## Module-Level subprocess (Fact 5)

```
grep -nE "^import subprocess" heyvox/input/target.py  → 1
```

Enables test patches via `monkeypatch.setattr("heyvox.input.target.subprocess.run", ...)`
to intercept. Runtime argv assertions for `--id`/`--session` depend on this.

## Deletions (B5 cross-referenced)

Deleted by this plan:
- `restore_target` (replaced by `resolve_lock`)
- `_walk_ax_tree` (promiscuous fallback rejected per SPEC R4)
- `_find_window_text_fields` (same)
- `focus_app` osascript fallback in `_activate_app` (W10)

Already deleted by Plan 15-02 (B5 cross-reference, not re-deleted):
- `_detect_app_workspace`
- `_switch_app_workspace`

## Symbols Left in target.py for Plan 15-06

- `TargetLock` (frozen dataclass)
- `RoleHop`, `MAX_ROLE_PATH_HOPS`
- `PasteOutcome`, `FailReason`, `_REASON_MESSAGES`
- `capture_lock`, `resolve_lock`
- `_walk_role_path` (reusable if 15-06 needs role-path walk)
- Internal helpers: `_capture_role_path`, `_capture_leaf_tiebreakers`,
  `_find_window_by_number`, `_yank_back_app_and_workspace`,
  `_activate_app`, `_app_under_mouse`, `_detect_conductor_branch`,
  `_log`, `_TEXT_ROLES`

Plan 15-06 will ADD: `verify_paste`, `VerifyResult`, `_normalize_text`,
`_read_ax_value`, `_focus_unchanged`.

## Test Results

- **tests/test_resolve_lock.py (new):** 12 tests pass in 2.34s
- **Regression audit:** post-15-05 failures = 23 (same as post-15-02 baseline).
  Zero new regressions introduced.

## Acceptance Criteria — all met

- [x] `resolve_lock` + three-tier ladder per SPEC R4
- [x] `FailReason` taxonomy + W13 format consistency
- [x] `PasteOutcome` frozen dataclass
- [x] `restore_target` / `_walk_ax_tree` / `_find_window_text_fields` DELETED
- [x] `_detect_app_workspace` / `_switch_app_workspace` still absent (B5)
- [x] Yank-back uses `--id` (not positional)
- [x] Session flag appended unconditionally when set (B4)
- [x] `focus_app` not imported / not called (W10)
- [x] Module-level `import subprocess` (Fact 5)
- [x] No duplicate `audio_cue`/`get_cues_dir` imports (Fact 6)
- [x] `_send_local` migrated; `restore_target` reference gone
- [x] `app_fast_paste` finally has a caller (R8)
- [x] `outcome = None` explicit init (W6)
- [x] DEF-070 preservation comment in recording.py
- [x] Fail-closed: clipboard, NO Cmd+V, error cue, toast, history unchanged
- [x] HUD message distinguishes fail-closed ("clipboard saved")

## Edge Cases Observed

- `_walk_role_path` early-out: if we land on a text role (AXTextField/
  AXTextArea/AXWebArea/AXComboBox) before the path ends, accept it. Real
  Conductor sessions often shrink tree depth by 1-2 hops between capture
  and paste because the assistant response panel re-mounts. The early-out
  keeps tier 1 live across those shrinkages.
- `window_number=0` fallback to AXFocusedWindow — happens when capture's
  AXWindowNumber read errors (common on multi-monitor transitions).
- Tier-2 osascript uses `_get_frontmost_app()` (lowercase-preserving,
  DEF-027) not `lock.app_name` (display name).

## Self-Check: PASSED

All B1/B2/B3/B4/B5/W10/W13 + Fact-4/5/6 corrections from
REVISION-ADDENDUM applied. Module imports cleanly, 12 new tests green,
zero regressions, `conductor-switch-workspace` script extended and verified.
