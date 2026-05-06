---
phase: 15
plan: 03
title: AppProfileConfig extension + app_fast_paste generalization
status: complete
completed: 2026-04-24
requirements: [R8]
addendum_applied: [B1]
---

## What Shipped

Phase 12 fast-path generalized into profile-driven `app_fast_paste(profile, text)`.
Old `conductor_paste_and_send` deleted outright (dead code — B1 addendum).
`AppProfileConfig` gained three new fields needed by Plan 15-05 verifier
(Wave 3) and Plan 15-06 post-paste AXValue check (Wave 4).

## AppProfileConfig New Fields

```python
# Defaults
supports_ax_verify: bool = True
has_session_detection: bool = False
ax_settle_before_verify: float = 0.1
```

## Profile Defaults Updated (D-22)

| Profile | supports_ax_verify | has_session_detection | ax_settle_before_verify |
|---|---|---|---|
| Conductor | True (default) | **True** | **0.15** |
| Cursor | True (default) | False (default) | 0.1 (default) |
| Claude | True (default) | False (default) | 0.1 (default) |
| Terminal | **False** | False (default) | 0.1 (default) |
| iTerm2 | **False** | False (default) | 0.1 (default) |

## app_fast_paste Signature

```python
def app_fast_paste(profile, text: str) -> bool:
    """One-shot paste using profile-driven shortcuts: focus-shortcut -> Cmd+V -> Enter*N."""
```

Reads from profile: `focus_shortcut`, `enter_count`, `settle_delay`, `name`.
Process name for `tell process` comes from live `_get_frontmost_app()`
(preserves DEF-027 lowercase fix). Fictional apps with their own
`focus_shortcut` get the same fast-path with zero code changes.

## B1 Addendum Applied

Plan 15-03 Task 2 originally said "find the existing `conductor_paste_and_send(...)`
call site in recording.py and replace with `app_fast_paste`". The addendum
(verified by grep) showed zero such call sites — `conductor_paste_and_send`
was dead code. Applied B1:

- Deleted `conductor_paste_and_send` outright from `heyvox/input/injection.py`.
- Did NOT touch `heyvox/recording.py` (removed from this plan's files_modified).
- Did NOT add the W2 regression-guard test (wrong wave — Plan 15-05 wires it in).
- `app_fast_paste` is now orphaned at end of Wave 1. Plan 15-05 picks it up
  as part of the resolver's Tier 2 branch.

No latency regression between Waves 1 and 3 because current `_send_local`
already uses `type_text(...)`, not `conductor_paste_and_send`.

## Grep Evidence

```
grep -n "def conductor_paste_and_send" heyvox/input/injection.py     → 0 matches (deleted)
grep -n "def app_fast_paste" heyvox/input/injection.py               → 1 match (new function)
grep -niE '"conductor"' heyvox/input/injection.py heyvox/input/target.py → 0 matches
grep -n "conductor_paste_and_send" heyvox/ -r                        → 0 matches (gone everywhere)
grep -n "_get_frontmost_app" heyvox/input/injection.py               → multiple (live frontmost used)
```

Three example-comment hits ("conductor" vs "Conductor") at lines 348, 433,
and in the app_fast_paste docstring were rephrased to generic
"lowercase vs TitleCase display name" wording — no logic change.

## Test Results

- `tests/test_config.py::TestAppProfileNewFields` — 9 tests pass
- `tests/test_app_fast_paste.py` — 13 tests pass (profile-driven, DEF-027, failures, generality)
- Pre-existing unrelated failures in `test_default_wake_words` and
  `test_partial_yaml_merges_with_defaults` (hey_jarvis vs hey_vox legacy)
  are untouched and not caused by this plan.

## Acceptance Criteria — all met

- [x] 3 new AppProfileConfig fields with correct defaults
- [x] Conductor/Terminal/iTerm2 profile overrides per D-22
- [x] `conductor_paste_and_send` deleted from injection.py (was callerless per B1)
- [x] `app_fast_paste(profile, text)` is the new public API
- [x] Zero hardcoded `"conductor"` string-matches in `heyvox/input/`
- [x] `app_fast_paste` calls `_get_frontmost_app()` for process name
- [x] Profile-driven: `profile.focus_shortcut`, `profile.enter_count`, `profile.settle_delay`
- [x] 22 new tests pass total (9 config + 13 fast-paste)
- [x] B1 corrections applied (recording.py untouched)

## For Plan 15-05

`app_fast_paste(profile, text)` is ready to wire into the resolver's Tier 2
branch. Suggested call pattern:

```python
# Tier 2: profile fast-path (SPEC R4 b.)
if profile.focus_shortcut:
    ok = app_fast_paste(profile, paste_text)
    if ok and profile.supports_ax_verify:
        ok = verify_paste(paste_text, profile)  # Plan 15-06
    return PasteOutcome(ok=ok, tier=2, ...)
```

## Self-Check: PASSED

All acceptance criteria met. `app_fast_paste` is intentionally orphaned per
B1 — that is the correct end state for Wave 1.
