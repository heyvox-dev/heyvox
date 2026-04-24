"""Unit tests for heyvox/adapters/conductor.py.

Covers hit, miss, NULL session, missing DB, schema mismatch, locked DB (W9),
frozen dataclass invariant, and a 100ms p95 timing budget (SPEC R3).

Journal-mode note (W9): the locked-DB test uses PRAGMA journal_mode=DELETE
before BEGIN EXCLUSIVE because macOS system sqlite builds with WAL enabled
permit read-only readers under an exclusive lock. DELETE (rollback-journal)
ensures BEGIN EXCLUSIVE actually blocks the read-only URI reader, which is
what we need to prove the sqlite3.Error catch handles OperationalError.
"""

import dataclasses
import sqlite3
import time

import pytest

from heyvox.adapters.conductor import (
    ConductorIdentity,
    get_active_workspace_and_session,
)


def _build_fixture_db(path: str, rows: list[tuple]) -> None:
    """rows = [(id, directory_name, branch, active_session_id, state), ...]"""
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE workspaces (
            id TEXT PRIMARY KEY,
            directory_name TEXT,
            branch TEXT,
            active_session_id TEXT,
            state TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO workspaces (id, directory_name, branch, active_session_id, state) "
        "VALUES (?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Hits and misses
# ---------------------------------------------------------------------------


def test_lookup_by_directory_name_returns_identity(tmp_path):
    db_path = str(tmp_path / "c.db")
    _build_fixture_db(
        db_path,
        [
            ("ws-seattle", "seattle", "main", "sess-s", "ready"),
            ("ws-dakar", "dakar", "feature/x", "sess-d", "ready"),
            ("ws-deleted", "oldtown", "main", None, "archived"),
        ],
    )

    seattle = get_active_workspace_and_session(
        directory_name="seattle", db_path=db_path
    )
    assert seattle is not None
    assert seattle.workspace_id == "ws-seattle"
    assert seattle.session_id == "sess-s"
    assert seattle.branch == "main"
    assert seattle.directory_name == "seattle"

    dakar = get_active_workspace_and_session(
        directory_name="dakar", db_path=db_path
    )
    assert dakar is not None
    assert dakar.workspace_id == "ws-dakar"

    missing = get_active_workspace_and_session(
        directory_name="atlantis", db_path=db_path
    )
    assert missing is None


def test_lookup_with_no_filters_returns_first_ready_row(tmp_path):
    db_path = str(tmp_path / "c.db")
    _build_fixture_db(
        db_path,
        [
            ("ws-1", "seattle", "main", "sess-1", "ready"),
            ("ws-2", "dakar", "feature/x", "sess-2", "ready"),
        ],
    )
    result = get_active_workspace_and_session(db_path=db_path)
    assert result is not None
    assert result.workspace_id in ("ws-1", "ws-2")


def test_lookup_skips_non_ready_workspaces(tmp_path):
    db_path = str(tmp_path / "c.db")
    _build_fixture_db(
        db_path,
        [
            ("ws-archived", "seattle", "main", "sess-a", "archived"),
            ("ws-deleted", "seattle", "main", "sess-d", "deleted"),
        ],
    )
    result = get_active_workspace_and_session(
        directory_name="seattle", db_path=db_path
    )
    assert result is None, (
        "only state='ready' rows should be returned; got " + repr(result)
    )


def test_lookup_by_branch_fallback(tmp_path):
    db_path = str(tmp_path / "c.db")
    _build_fixture_db(
        db_path,
        [("ws-1", "seattle", "main", "sess-1", "ready")],
    )
    result = get_active_workspace_and_session(branch="main", db_path=db_path)
    assert result is not None
    assert result.workspace_id == "ws-1"


# ---------------------------------------------------------------------------
# NULL session handling
# ---------------------------------------------------------------------------


def test_null_session_id_preserved(tmp_path):
    db_path = str(tmp_path / "c.db")
    _build_fixture_db(
        db_path,
        [("ws-noses", "seattle", "main", None, "ready")],
    )
    result = get_active_workspace_and_session(
        directory_name="seattle", db_path=db_path
    )
    assert result is not None
    assert result.workspace_id == "ws-noses"
    assert result.session_id is None


# ---------------------------------------------------------------------------
# Failure modes — silent None
# ---------------------------------------------------------------------------


def test_missing_db_returns_none(tmp_path):
    missing_path = str(tmp_path / "does-not-exist.db")
    result = get_active_workspace_and_session(
        directory_name="seattle", db_path=missing_path
    )
    assert result is None


def test_no_workspaces_table_returns_none(tmp_path):
    db_path = str(tmp_path / "c.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE wrongtable (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

    result = get_active_workspace_and_session(
        directory_name="seattle", db_path=db_path
    )
    assert result is None


def test_locked_db_returns_none_without_raising(tmp_path):
    """W9: explicit regression guard for sqlite3.OperationalError 'database is locked'.

    The adapter's except clause catches (sqlite3.Error, OSError). If a future
    refactor tightens this to OSError-only, WAL contention against Conductor's
    live DB would raise OperationalError and crash capture_lock(). This test
    fails immediately if that regression lands.

    CONTEXT D-20 explicitly calls out WAL contention as an expected failure mode
    that MUST be swallowed silently.

    Journal-mode note: we set PRAGMA journal_mode=DELETE so BEGIN EXCLUSIVE
    actually blocks the read-only URI reader. WAL mode permits RO readers
    even under EXCLUSIVE on some macOS sqlite builds.
    """
    db_path = str(tmp_path / "locked.db")
    _build_fixture_db(db_path, [("ws-1", "seattle", "main", "sess-1", "ready")])

    blocker = sqlite3.connect(db_path)
    try:
        blocker.execute("PRAGMA journal_mode=DELETE")
        blocker.execute("BEGIN EXCLUSIVE")
        # MUST NOT raise; MUST return None
        result = get_active_workspace_and_session(
            directory_name="seattle", db_path=db_path
        )
        assert result is None, f"locked DB should return None, got {result!r}"
    finally:
        blocker.rollback()
        blocker.close()


# ---------------------------------------------------------------------------
# Frozen dataclass invariant
# ---------------------------------------------------------------------------


def test_identity_is_frozen():
    identity = ConductorIdentity(
        workspace_id="ws-1",
        session_id="sess-1",
        branch="main",
        directory_name="seattle",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        identity.workspace_id = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Timing budget (SPEC R3: <100ms per call)
# ---------------------------------------------------------------------------


def test_under_100ms_p95(tmp_path):
    db_path = str(tmp_path / "c.db")
    _build_fixture_db(
        db_path,
        [
            ("ws-1", "seattle", "main", "sess-1", "ready"),
            ("ws-2", "dakar", "feature/x", "sess-2", "ready"),
            ("ws-3", "tokyo", "main", None, "ready"),
        ],
    )

    timings = []
    for _ in range(20):
        t0 = time.perf_counter()
        get_active_workspace_and_session(
            directory_name="seattle", db_path=db_path
        )
        timings.append(time.perf_counter() - t0)

    timings.sort()
    p95 = timings[18]  # 95th percentile of 20 samples
    assert p95 < 0.1, f"p95 too slow: {p95*1000:.2f}ms (budget 100ms)"
