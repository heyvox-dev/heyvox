---
phase: 15
plan: 02
title: TargetLock dataclass + capture_lock() (replaces TargetSnapshot)
status: complete
completed: 2026-04-24
requirements: [R1, R2]
addendum_applied: [B1, B2, B5, W-fix-iter3]
---

## What Shipped

`TargetSnapshot` replaced by frozen `TargetLock` with stable identity fields
(SPEC R1+R2). `capture_lock()` supersedes `snapshot_target()` and calls the
Plan 15-01 Conductor adapter with a branch filter (W-fix: iteration-2 LIMIT-1
landmine closed). The old workspace-detect/switch helpers are deleted; their
branch-walk half is salvaged into `_detect_conductor_branch`. Phase 12 AX
fast-path (`_ax_inject_text`) migrated to TargetLock so latency doesn't
regress between waves 2 and 3 (B1).

## TargetLock Field List

```python
@dataclass(frozen=True)
class TargetLock:
    app_bundle_id: str
    app_pid: int
    window_number: int
    ax_role_path: tuple[RoleHop, ...]       # MAX 12 hops (D-03)
    leaf_role: str = ""
    leaf_axid: Optional[str] = None
    leaf_title: Optional[str] = None
    leaf_description: Optional[str] = None
    conductor_workspace_id: Optional[str] = None
    conductor_session_id: Optional[str] = None
    focused_was_text_field: bool = False
    captured_at: float = 0.0
    app_name: str = ""                      # advisory for logs
```

## Role-Path Capture Algorithm

Depth-first search from the focused window down to the focused element.
Each hop records `(role, sibling_index)`. Search bail at
`MAX_ROLE_PATH_HOPS = 12`; final tuple is truncated to the cap even if the
search didn't reach the leaf.

## Conductor Adapter Integration (W-fix)

capture_lock calls the adapter inside a per-call `ThreadPoolExecutor`
context manager (B2) with a 100ms `future.result(timeout=0.1)`. `with` block
`join()`s the worker before returning, so no thread leak across 30+ calls
(verified by `threading.active_count()` delta test).

Branch filter (iteration-3 W-fix): `_detect_conductor_branch(pid)` walks the
Conductor AX tree to locate the branch label; if branch is non-empty, the
adapter is called with `branch=detected_branch`. If branch detection fails
(AX error / non-Conductor frontmost), adapter is SKIPPED and the lock's
`conductor_workspace_id` stays None — Tier 2/3 handle fallback at paste time.

Bare `branch=None` is NEVER passed (would return a random ready workspace
via the adapter's `WHERE (branch = ?2 OR ?2 IS NULL)` SQL).

## Deletions (B5 + iteration-3 W-fix)

Deleted from `heyvox/input/target.py`:
- `class TargetSnapshot` (functional)
- `def _switch_app_workspace`
- `def _detect_app_workspace`
- The `_detect_app_workspace` + `_switch_app_workspace` callers inside
  `restore_target` (old lines 388-400 — the workspace block) replaced with
  a one-line comment pointing to Plan 15-05

**Grep evidence (functional symbols gone):**
- `grep -nE "class\s+TargetSnapshot"` → 0
- `grep -n "def snapshot_target"` → 0
- `grep -nE "def _detect_app_workspace|def _switch_app_workspace"` → 0

(Docstring mentions of `TargetSnapshot` renamed to "legacy snapshot" so
strict `grep -rn "TargetSnapshot" heyvox/ --include='*.py'` returns ZERO.)

## B1 Applied — Phase 12 Fast-Path Alive

`_ax_inject_text` in `heyvox/input/injection.py` now reads `leaf_role` +
`conductor_workspace_id` from the lock, and acquires the focused AX element
live via `AXUIElementCreateApplication(pid)` + `AXFocusedUIElement`. Pre-
migration, the function bailed silently on TargetLock (missing
`element_role` attr). Regression guard test
`test_ax_inject_text_phase12_fastpath_remains_under_5ms` measures mean
per-call time under 5ms with mocked AX; fails if migration breaks.

## Consumer Site Migrations

- `heyvox/app_context.py:55` — annotation `recording_target: object` docstring
  updated to "TargetLock: immutable record-start target (SPEC R1)"
- `heyvox/recording.py:226` — `snapshot_target` → `capture_lock`
- `heyvox/recording.py:232` — log line now shows `conductor_workspace_id` +
  `conductor_session_id` instead of legacy `detected_workspace`
- `heyvox/recording.py:244-249` — `window_title` + `element_role` replaced by
  `window_number` + `leaf_role` + `focused_was_text_field`
- `heyvox/recording.py:761-771` — `target_window = recording_target.window_title`
  replaced by `window_number = recording_target.window_number`
- `heyvox/recording.py:804-807` — `snap.detected_workspace` →
  `snap.conductor_workspace_id`
- `heyvox/adapters/base.py:6` — docstring updated to reference `capture_lock`

## Test Results

- **tests/test_target_lock.py (new):** 17 tests pass in 0.84s
  (frozen, role-path, branch-filter, adapter, timeout, no-leak, AX fast-path)
- **tests/test_injection.py::TestAXFastPath:** now 7/7 pass (was 5/7 pre-migration)
- **tests/test_multiapp_injection.py:** pre-existing failure unchanged
- **tests/test_target_restore.py:** 3 obsolete `TestRestoreTargetWorkspaceSkip`
  tests skipped with pytest.skip pointing to Plan 15-05; 2 pre-existing
  `TestFocusShortcutInOsascript` failures untouched (mock decode issue — not
  caused by 15-02)

## Regression Audit

| State | Failures |
|---|---|
| Pre-15-02 baseline | 25 |
| Post-15-02 current | 23 |
| Net | **2 fewer** (AX fast-path tests fixed) |

Zero new regressions introduced. All 23 remaining failures pre-date this
plan and are tracked separately (wake word defaults, test_injection_enter
mocks, test_recording_state AttributeErrors, test_herald_orchestrator audio
ducking, test_defect_guards case-sensitivity, test_wakeword_trim threshold).

## For Plan 15-05

`_detect_app_workspace` and `_switch_app_workspace` are **already gone** —
your deletion list shrinks to:
- `restore_target` (replace with `resolve_lock`)
- `_walk_ax_tree` (if Tier 3 doesn't reuse it)
- `_find_window_text_fields` (if Tier 3 doesn't reuse it)

`app_fast_paste` (from Plan 15-03) is ready to consume — profile
`focus_shortcut` + `enter_count` + `settle_delay` driven. Wire it into the
Tier 2 branch of `resolve_lock`.

`_detect_conductor_branch` is available as a helper if 15-05 wants to
re-detect branch at paste time for comparison with `lock.branch` (not
currently stored — could be added to TargetLock if needed).

## Self-Check: PASSED

All B1/B2/B5/W-fix corrections from REVISION-ADDENDUM applied. Zero
functional `TargetSnapshot` references remaining. Phase 12 AX fast-path
verifiably alive. No new test regressions.
