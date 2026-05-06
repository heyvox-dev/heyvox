---
phase: 15
plan: 04
title: Toast helper (heyvox/input/toast.py)
status: complete
completed: 2026-04-24
requirements: [R5]
---

## What Shipped

New module `heyvox/input/toast.py` delivering user-facing paste-failure
toasts. Two-tier: Hammerspoon rich alert → osascript native notification
fallback. Standalone — no callers yet (Plans 15-05 + 15-06 consume).

## Key Files Created

- `heyvox/input/toast.py` (98 LOC) — toast helper
- `tests/test_toast.py` (211 LOC) — 12 unit tests

## Exact Shape

```python
def show_failure_toast(reason_message: str, title: str = "HeyVox paste") -> None
def _hammerspoon_running() -> bool  # DEF-074 liveness gate
def _log(msg: str) -> None          # private helper
```

- Tier 1 (HS up, hs binary present): `subprocess.Popen([hs, "-c", "hs.alert.show(<json>, 2.5)"])`
- Tier 2 (HS down OR hs missing OR tier-1 OSError): `subprocess.Popen(["osascript", "-e", "display notification ..."])`
- `json.dumps()` protects message quoting in the HS script.
- Backslash/quote escaping protects the osascript fallback.
- All subprocess errors swallowed silently after stderr log.

## Acceptance Criteria — all met

- [x] `wc -l heyvox/input/toast.py` = 98 (between 40 and 130)
- [x] `grep -E '^def ' heyvox/input/toast.py | wc -l` = 3 (2-3 allowed: show_failure_toast, _hammerspoon_running, _log)
- [x] `grep -n "def show_failure_toast"` returns 1 match
- [x] `grep -n "def _hammerspoon_running"` returns 1 match
- [x] `grep -n "json.dumps"` returns 1 match (hs script quoting)
- [x] `grep -n "display notification"` returns 1 match (osascript fallback)
- [x] `grep -nE "pgrep.*Hammerspoon"` returns 1 match (DEF-074 liveness gate)
- [x] `grep -n "from heyvox.herald"` returns 0 matches (W4 — Herald independence)
- [x] Module imports cleanly
- [x] 12 unit tests pass (0.18s wall time)

## Test Coverage (12 tests)

1. `test_hs_up_uses_hammerspoon` — HS path + hs binary → Popen
2. `test_hs_message_is_json_quoted` — quote/newline safety
3. `test_hs_up_but_binary_missing_falls_through` — hs missing → osascript
4. `test_hs_down_falls_back_to_osascript` — pgrep=1 → osascript
5. `test_osascript_default_title_present` — default title plumbed
6. `test_osascript_custom_title_plumbed` — custom title plumbed
7. `test_osascript_quotes_escaped` — backslash/quote escaping
8. `test_hs_popen_oserror_falls_through_to_osascript` — HS OSError → tier 2
9. `test_both_paths_oserror_silent` — both fail → silent
10. `test_pgrep_oserror_treated_as_hs_not_running` — pgrep error safe
11. `test_hammerspoon_running_uses_pgrep_q_hammerspoon` — exact argv
12. `test_hammerspoon_running_returns_false_on_exit_1` — exit code check

## For Plans 15-05 + 15-06

```python
from heyvox.input.toast import show_failure_toast

# Plan 15-05 fail-closed (SPEC R5):
show_failure_toast(
    f"Paste failed: {lock.app_name} → clipboard + history instead"
)

# Plan 15-06 verify drift:
show_failure_toast(
    f"Paste landed in wrong field in {lock.app_name} — check before continuing"
)
```

## Self-Check: PASSED

Standalone, Herald-independent, all criteria met. Ready for wave 3 (Plan 15-05)
and wave 4 (Plan 15-06) to consume.
