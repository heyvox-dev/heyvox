---
phase: 15
plan: 01
title: Conductor adapter + DB schema coupling
status: complete
completed: 2026-04-24
requirements: [R3]
---

## What Shipped

New module `heyvox/adapters/conductor.py` as the SOLE owner of Conductor sqlite
coupling (SPEC R3). Exposes `get_active_workspace_and_session()` returning
stable IDs (`workspaces.id` + `workspaces.active_session_id`) that survive
display-name renames. Standalone — no callers yet (Plan 15-02 wires into
`capture_lock()`).

## Key Files Created

- `heyvox/adapters/conductor.py` (124 LOC) — adapter module
- `tests/test_conductor_adapter.py` (246 LOC) — 10 unit tests

## Exact Shape

### `ConductorIdentity` (frozen dataclass)

```python
@dataclass(frozen=True)
class ConductorIdentity:
    workspace_id: str
    session_id: Optional[str]
    branch: str
    directory_name: str
```

### `get_active_workspace_and_session()`

```python
def get_active_workspace_and_session(
    directory_name: Optional[str] = None,
    branch: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Optional[ConductorIdentity]
```

- Uses `sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)` (read-only URI per D-20).
- SQL exactly as D-19:
  ```sql
  SELECT id, active_session_id, branch, directory_name
  FROM workspaces
  WHERE (directory_name = ?1 OR ?1 IS NULL)
    AND (branch = ?2 OR ?2 IS NULL)
    AND state = 'ready'
  LIMIT 1
  ```
- Wraps in `try/except (sqlite3.Error, OSError)` — returns `None` + logs warn.
- Closes connection in `finally`.
- Module constant `DEFAULT_DB_PATH = os.path.expanduser("~/Library/Application Support/com.conductor.app/conductor.db")`.

## Test Functions (10 total)

1. `test_lookup_by_directory_name_returns_identity` — hit + miss, archived skipped
2. `test_lookup_with_no_filters_returns_first_ready_row` — LIMIT 1 fallback
3. `test_lookup_skips_non_ready_workspaces` — archived/deleted filtered
4. `test_lookup_by_branch_fallback` — branch-only filter
5. `test_null_session_id_preserved` — active_session_id=NULL handled
6. `test_missing_db_returns_none` — missing file swallowed
7. `test_no_workspaces_table_returns_none` — schema mismatch swallowed
8. `test_locked_db_returns_none_without_raising` — **W9 regression guard**
9. `test_identity_is_frozen` — FrozenInstanceError on assignment
10. `test_under_100ms_p95` — SPEC R3 timing budget

## W9 Locked-DB Test

Journal mode set to `DELETE` (rollback-journal) before `BEGIN EXCLUSIVE`
because macOS system sqlite builds with WAL enabled permit read-only readers
under an exclusive lock. With `DELETE`, `BEGIN EXCLUSIVE` blocks the RO URI
reader, which is what we need to prove `sqlite3.Error` catches
`OperationalError: database is locked`. Documented in the test file's module
docstring.

## Timing Measurement

```
p50=0.10ms p95=0.17ms max=0.20ms  (20 samples, 3 rows, SSD)
```

Well under SPEC R3's 100ms budget. Headroom sufficient for Conductor's live
DB which typically has 5-50 rows.

## Note on "AgentAdapter" grep

The plan's acceptance criterion `grep -n "AgentAdapter" heyvox/adapters/conductor.py`
returns ZERO matches" conflicts with CONTEXT D-17's mandate: "The module
docstring explicitly calls out the distinction" (between AgentAdapter and this
adapter). D-17 wins — the module docstring says "This is NOT an AgentAdapter".
The true intent (no import / no subclass) is enforced by:
- `grep -nE "^from heyvox\.adapters\.base" conductor.py` → zero matches ✓
- No `AgentAdapter` in any subclass/import position ✓

## For Plan 15-02

Call from `capture_lock()` like this:

```python
from heyvox.adapters.conductor import get_active_workspace_and_session

# detect current Conductor branch via AX (salvage from _detect_app_workspace)
current_branch = _detect_conductor_branch()  # may be None

identity = get_active_workspace_and_session(
    directory_name=None,
    branch=current_branch,
    db_path=os.path.expanduser(profile.workspace_db),  # D-23
)
# identity may be None — that's fine, Tier 2/3 resolves
lock.conductor_workspace_id = identity.workspace_id if identity else None
lock.conductor_session_id = identity.session_id if identity else None
```

The branch filter is critical per addendum's "capture-time workspace lookup filter"
warning — passing `directory_name=None, branch=None` returns a random ready
workspace. Branch detection keeps the lock tied to the workspace Conductor is
actually showing.

## Acceptance Criteria — all met

- [x] Module imports cleanly
- [x] `ConductorIdentity` frozen
- [x] SQL matches D-19 exactly
- [x] `uri=True` read-only form used
- [x] `state = 'ready'` filter present
- [x] `sqlite3.Error` catch present (W9 safety net)
- [x] No import from `heyvox.adapters.base`
- [x] No subclass of `AgentAdapter`
- [x] 10 tests pass in 2.29s wall clock
- [x] p95 = 0.17ms (well under 100ms)

## Self-Check: PASSED

Module + tests land as a standalone unit. Wave 2 (Plan 15-02) will be the
first caller when `capture_lock()` is implemented.
