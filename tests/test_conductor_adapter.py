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
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from heyvox.adapters.base import WorkspaceIdentity
from heyvox.adapters.conductor import (
    ConductorIdentity,
    ConductorWorkspaceProvider,
    SidebarRow,
    _MAX_ROW_SEARCH_ATTEMPTS,
    _dispatch_activation,
    _enumerate_sidebar_rows,
    _find_matching_row,
    _labels_for_workspace_id,
    _match_row,
    _normalize_text,
    _read_pos_size,
    _set_active_session,
    _synthesize_click,
    _try_ax_press,
    activate_workspace,
    get_active_workspace_and_session,
)


def _build_fixture_db(path: str, rows: list[tuple]) -> None:
    """rows = [(id, directory_name, branch, active_session_id, state[, workspace_name]), ...]

    workspace_name is optional (defaults to NULL) so existing 5-tuple callers
    are unaffected — only DEF-242's workspace_name-slug tests need to pass it.
    """
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE workspaces (
            id TEXT PRIMARY KEY,
            directory_name TEXT,
            branch TEXT,
            active_session_id TEXT,
            state TEXT,
            workspace_name TEXT
        )
        """
    )
    normalized = [row if len(row) == 6 else (*row, None) for row in rows]
    conn.executemany(
        "INSERT INTO workspaces "
        "(id, directory_name, branch, active_session_id, state, workspace_name) "
        "VALUES (?,?,?,?,?,?)",
        normalized,
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


def test_lookup_by_workspace_name_slug_returns_identity(tmp_path):
    """DEF-242: Conductor's exported workspace-name env var has been observed
    to carry the display name (space-to-hyphen, lowercased) rather than the
    internal directory_name/codename — the lookup must accept either."""
    db_path = str(tmp_path / "c.db")
    _build_fixture_db(
        db_path,
        [
            (
                "ws-dakar", "dakar", "geminicap/rfi110-replay-refresh",
                "sess-d", "ready", "Invoice Match-mcp",
            ),
        ],
    )

    result = get_active_workspace_and_session(
        directory_name="invoice-match-mcp", db_path=db_path
    )
    assert result is not None
    assert result.workspace_id == "ws-dakar"

    # Exact directory_name match still takes precedence / still works.
    result_by_codename = get_active_workspace_and_session(
        directory_name="dakar", db_path=db_path
    )
    assert result_by_codename is not None
    assert result_by_codename.workspace_id == "ws-dakar"


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


# ---------------------------------------------------------------------------
# activate() internals — Hammerspoon-free workspace switching
# ---------------------------------------------------------------------------
#
# Everything below covers heyvox/adapters/conductor.py's activate() feature:
# AX tree row enumeration, 4-tier label matching, AXPress/click dispatch, the
# sqlite session write, and activate_workspace()'s end-to-end orchestration.
# All AX/Quartz calls are mocked via sys.modules (PyObjC isn't available/
# meaningful in a test environment) — mirrors the pattern already used in
# tests/test_target_restore.py and tests/test_target_lock.py.


class _AXPoint:
    def __init__(self, x, y):
        self.x = x
        self.y = y


class _AXSize:
    def __init__(self, w, h):
        self.width = w
        self.height = h


class _AXNode:
    """Minimal stand-in for an opaque AXUIElementRef.

    Only carries what _enumerate_sidebar_rows/_read_pos_size actually read:
    role, text value, children, position/size, and a back-pointer to its
    parent (set automatically from the `children` list, matching how AXParent
    would resolve in the real tree).
    """

    def __init__(self, role, value=None, children=None, pos=None, size=None):
        self.role = role
        self.value = value
        self.children = children or []
        self.pos = pos
        self.size = size
        self.parent = None
        for c in self.children:
            c.parent = self


def _make_ax_module():
    """Fake ApplicationServices module wired to _AXNode's attributes.

    AXUIElementCopyAttributeValue/AXValueGetValue return (err, value) pairs
    exactly like the real PyObjC bridge; a nonzero err signals "no value",
    matching how the real functions behave when an attribute isn't present.
    """
    mock_ax = MagicMock()
    mock_ax.kAXValueCGPointType = "point"
    mock_ax.kAXValueCGSizeType = "size"

    def _copy_attr(elem, attr, _none):
        if attr == "AXRole":
            return (0, elem.role)
        if attr == "AXValue":
            return (0, elem.value) if elem.value else (1, None)
        if attr == "AXChildren":
            return (0, elem.children) if elem.children else (1, None)
        if attr == "AXParent":
            return (0, elem.parent) if elem.parent is not None else (1, None)
        if attr == "AXPosition":
            return (0, ("pos", elem.pos)) if elem.pos is not None else (1, None)
        if attr == "AXSize":
            return (0, ("size", elem.size)) if elem.size is not None else (1, None)
        return (1, None)

    def _value_get(value_ref, value_type, _none):
        kind, payload = value_ref
        if value_type == "point" and kind == "pos":
            return (True, _AXPoint(*payload))
        if value_type == "size" and kind == "size":
            return (True, _AXSize(*payload))
        return (False, None)

    mock_ax.AXUIElementCopyAttributeValue.side_effect = _copy_attr
    mock_ax.AXValueGetValue.side_effect = _value_get
    return mock_ax


# ---------------------------------------------------------------------------
# _read_pos_size
# ---------------------------------------------------------------------------


def test_read_pos_size_returns_point_and_size():
    node = _AXNode("AXStaticText", pos=(12.0, 34.0), size=(56.0, 78.0))
    with patch.dict("sys.modules", {"ApplicationServices": _make_ax_module()}):
        pos, size = _read_pos_size(node)
    assert pos == (12.0, 34.0)
    assert size == (56.0, 78.0)


def test_read_pos_size_missing_attrs_returns_none_none():
    node = _AXNode("AXStaticText")  # no pos/size set
    with patch.dict("sys.modules", {"ApplicationServices": _make_ax_module()}):
        pos, size = _read_pos_size(node)
    assert (pos, size) == (None, None)


# ---------------------------------------------------------------------------
# _enumerate_sidebar_rows
# ---------------------------------------------------------------------------


def test_enumerate_sidebar_rows_finds_pinned_and_grouped_rows():
    pinned_text = _AXNode("AXStaticText", value="HeyVox", pos=(60, 100), size=(180, 20))
    pinned_link = _AXNode("AXLink", children=[pinned_text])
    grouped_text = _AXNode("AXStaticText", value="Invoice Match-mcp", pos=(74, 140), size=(180, 20))
    grouped_link = _AXNode("AXLink", children=[grouped_text])
    other_text = _AXNode("AXStaticText", value="Settings", pos=(60, 400), size=(100, 20))
    other_group = _AXNode("AXGroup", children=[other_text])  # not AXLink-parented
    win = _AXNode("AXWindow", children=[pinned_link, grouped_link, other_group])

    with patch.dict("sys.modules", {"ApplicationServices": _make_ax_module()}):
        rows = _enumerate_sidebar_rows(win, win_pos=(0.0, 0.0))

    labels = {r.label for r in rows}
    assert labels == {"HeyVox", "Invoice Match-mcp"}


def test_enumerate_sidebar_rows_multimonitor_nonzero_window_origin():
    """Regression: the ported Lua script filtered on absolute screen-x and
    silently found zero rows once the window wasn't near x=0. Filtering
    relative to the window's own AXPosition fixes that."""
    win_x = 1920.0  # second monitor
    text = _AXNode("AXStaticText", value="HeyVox", pos=(win_x + 60, 100), size=(180, 20))
    link = _AXNode("AXLink", children=[text])
    win = _AXNode("AXWindow", children=[link])

    with patch.dict("sys.modules", {"ApplicationServices": _make_ax_module()}):
        rows = _enumerate_sidebar_rows(win, win_pos=(win_x, 0.0))

    assert len(rows) == 1
    assert rows[0].label == "HeyVox"


def test_enumerate_sidebar_rows_excludes_far_right_content():
    far_text = _AXNode("AXStaticText", value="Some chat content", pos=(500, 300), size=(400, 20))
    far_link = _AXNode("AXLink", children=[far_text])
    win = _AXNode("AXWindow", children=[far_link])

    with patch.dict("sys.modules", {"ApplicationServices": _make_ax_module()}):
        rows = _enumerate_sidebar_rows(win, win_pos=(0.0, 0.0))

    assert rows == []


def test_enumerate_sidebar_rows_ignores_whitespace_only_text():
    empty_text = _AXNode("AXStaticText", value="   ", pos=(60, 100), size=(180, 20))
    link = _AXNode("AXLink", children=[empty_text])
    win = _AXNode("AXWindow", children=[link])

    with patch.dict("sys.modules", {"ApplicationServices": _make_ax_module()}):
        rows = _enumerate_sidebar_rows(win, win_pos=(0.0, 0.0))

    assert rows == []


# ---------------------------------------------------------------------------
# _normalize_text / _match_row — pure 4-tier label matching
# ---------------------------------------------------------------------------


def test_normalize_text_collapses_middle_dot_and_whitespace():
    assert _normalize_text("HeyVox  ·  main") == "heyvox main"
    assert _normalize_text("  Invoice   Match-mcp ") == "invoice match-mcp"


def _row(label):
    return SimpleNamespace(label=label)


def test_match_row_exact_tier_is_tried_first():
    rows = [_row("Invoice Match-mcp"), _row("HeyVox")]
    assert _match_row(rows, ["heyvox"]) is rows[1]


def test_match_row_contains_tier():
    rows = [_row("Invoice Match-mcp"), _row("Other")]
    assert _match_row(rows, ["Match"]) is rows[0]


def test_match_row_normalized_tier_handles_middle_dot_drift():
    rows = [_row("Hey  Vox")]  # double space, no middle dot
    assert _match_row(rows, ["Hey · Vox"]) is rows[0]


def test_match_row_word_tier_handles_glued_text():
    """"invoice match" (space) isn't a substring of "invoice-match" (hyphen),
    so exact/contains/normalized tiers all fail; word-tier finds both words
    independently."""
    rows = [_row("The invoice-match Project")]
    assert _match_row(rows, ["Invoice Match"]) is rows[0]


def test_match_row_tries_candidate_labels_in_priority_order():
    rows = [_row("Old PR Title"), _row("New Workspace Name")]
    assert _match_row(rows, ["No Such Label", "New Workspace Name"]) is rows[1]


def test_match_row_returns_none_when_nothing_matches():
    rows = [_row("Something Else")]
    assert _match_row(rows, ["Totally Different"]) is None


def test_match_row_skips_empty_candidate_labels():
    rows = [_row("HeyVox")]
    assert _match_row(rows, ["", "HeyVox"]) is rows[0]


# ---------------------------------------------------------------------------
# _find_matching_row — bounded retry when the AX tree isn't populated yet
# ---------------------------------------------------------------------------


def test_find_matching_row_succeeds_first_try_no_retry():
    row = _row("HeyVox")
    with patch("heyvox.adapters.conductor._enumerate_sidebar_rows", return_value=[row]), \
         patch("heyvox.adapters.conductor._match_row", return_value=row), \
         patch("heyvox.adapters.conductor.time.sleep") as mock_sleep:
        result = _find_matching_row(win=object(), win_pos=(0, 0), labels=["HeyVox"])

    assert result is row
    mock_sleep.assert_not_called()


def test_find_matching_row_retries_while_rows_empty_then_finds():
    row = _row("HeyVox")
    with patch(
        "heyvox.adapters.conductor._enumerate_sidebar_rows",
        side_effect=[[], [], [row]],
    ), patch(
        "heyvox.adapters.conductor._match_row",
        side_effect=[None, None, row],
    ), patch("heyvox.adapters.conductor.time.sleep") as mock_sleep:
        result = _find_matching_row(win=object(), win_pos=(0, 0), labels=["HeyVox"])

    assert result is row
    assert mock_sleep.call_count == 2


def test_find_matching_row_gives_up_after_max_attempts():
    with patch("heyvox.adapters.conductor._enumerate_sidebar_rows", return_value=[]), \
         patch("heyvox.adapters.conductor._match_row", return_value=None), \
         patch("heyvox.adapters.conductor.time.sleep") as mock_sleep:
        result = _find_matching_row(win=object(), win_pos=(0, 0), labels=["HeyVox"])

    assert result is None
    assert mock_sleep.call_count == _MAX_ROW_SEARCH_ATTEMPTS - 1


def test_find_matching_row_does_not_retry_when_rows_exist_but_no_match():
    """Retry is gated on EMPTY rows, not on 'no match' — if the tree is
    populated but genuinely has no matching row, retrying won't help."""
    row = _row("Something Else")
    with patch("heyvox.adapters.conductor._enumerate_sidebar_rows", return_value=[row]), \
         patch("heyvox.adapters.conductor._match_row", return_value=None), \
         patch("heyvox.adapters.conductor.time.sleep") as mock_sleep:
        result = _find_matching_row(win=object(), win_pos=(0, 0), labels=["HeyVox"])

    assert result is None
    mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# _try_ax_press / _synthesize_click / _dispatch_activation
# ---------------------------------------------------------------------------


def test_try_ax_press_supported_and_succeeds():
    mock_ax = MagicMock()
    mock_ax.AXUIElementCopyActionNames.return_value = (0, ["AXPress", "AXShowMenu"])
    mock_ax.AXUIElementPerformAction.return_value = 0
    with patch.dict("sys.modules", {"ApplicationServices": mock_ax}):
        assert _try_ax_press(object()) is True


def test_try_ax_press_not_in_action_list():
    mock_ax = MagicMock()
    mock_ax.AXUIElementCopyActionNames.return_value = (0, ["AXShowMenu"])
    with patch.dict("sys.modules", {"ApplicationServices": mock_ax}):
        assert _try_ax_press(object()) is False
    mock_ax.AXUIElementPerformAction.assert_not_called()


def test_try_ax_press_supported_but_perform_fails():
    mock_ax = MagicMock()
    mock_ax.AXUIElementCopyActionNames.return_value = (0, ["AXPress"])
    mock_ax.AXUIElementPerformAction.return_value = -1
    with patch.dict("sys.modules", {"ApplicationServices": mock_ax}):
        assert _try_ax_press(object()) is False


def test_try_ax_press_copy_action_names_errors():
    mock_ax = MagicMock()
    mock_ax.AXUIElementCopyActionNames.return_value = (1, None)
    with patch.dict("sys.modules", {"ApplicationServices": mock_ax}):
        assert _try_ax_press(object()) is False


def test_synthesize_click_posts_down_and_up_at_given_coords():
    mock_quartz = MagicMock()
    with patch.dict("sys.modules", {"Quartz": mock_quartz}):
        assert _synthesize_click(123.0, 456.0) is True

    down_call, up_call = mock_quartz.CGEventCreateMouseEvent.call_args_list
    assert down_call[0][2] == (123.0, 456.0)
    assert up_call[0][2] == (123.0, 456.0)
    assert mock_quartz.CGEventPost.call_count == 2


def test_dispatch_activation_ax_press_tries_link_parent_then_elem():
    row = SidebarRow(label="HeyVox", elem="elem", link_parent="link", pos=(0, 0), size=(10, 10))
    with patch("heyvox.adapters.conductor._try_ax_press", side_effect=[False, True]) as mock_press:
        assert _dispatch_activation(row, "ax_press") is True
    assert mock_press.call_args_list == [call("link"), call("elem")]


def test_dispatch_activation_click_uses_center_of_row():
    row = SidebarRow(
        label="HeyVox", elem="elem", link_parent="link",
        pos=(100.0, 200.0), size=(40.0, 20.0),
    )
    with patch("heyvox.adapters.conductor._synthesize_click", return_value=True) as mock_click:
        assert _dispatch_activation(row, "click") is True
    mock_click.assert_called_once_with(120.0, 210.0)


def test_dispatch_activation_click_without_pos_size_fails_closed():
    row = SidebarRow(label="HeyVox", elem="elem", link_parent="link", pos=None, size=None)
    with patch("heyvox.adapters.conductor._synthesize_click") as mock_click:
        assert _dispatch_activation(row, "click") is False
    mock_click.assert_not_called()


# ---------------------------------------------------------------------------
# _labels_for_workspace_id — display-name candidates, most-authoritative first
# ---------------------------------------------------------------------------


def _build_labels_fixture_db(path: str, rows: list[tuple]) -> None:
    """rows = [(id, workspace_name, user_set_workspace_name, pr_title, branch, state), ...]"""
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE workspaces (
            id TEXT PRIMARY KEY,
            workspace_name TEXT,
            user_set_workspace_name INTEGER,
            pr_title TEXT,
            branch TEXT,
            state TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO workspaces "
        "(id, workspace_name, user_set_workspace_name, pr_title, branch, state) "
        "VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_labels_for_workspace_id_user_set_name_takes_priority(tmp_path):
    db_path = str(tmp_path / "c.db")
    _build_labels_fixture_db(db_path, [
        ("ws-1", "HeyVox", 1, "feat: something", "main", "ready"),
    ])
    assert _labels_for_workspace_id("ws-1", db_path) == ["HeyVox", "feat: something", "main"]


def test_labels_for_workspace_id_skips_unset_workspace_name(tmp_path):
    db_path = str(tmp_path / "c.db")
    _build_labels_fixture_db(db_path, [
        ("ws-1", "some-directory-name", 0, "feat: something", "main", "ready"),
    ])
    assert _labels_for_workspace_id("ws-1", db_path) == ["feat: something", "main"]


def test_labels_for_workspace_id_omits_missing_fields(tmp_path):
    db_path = str(tmp_path / "c.db")
    _build_labels_fixture_db(db_path, [
        ("ws-1", None, 0, None, "main", "ready"),
    ])
    assert _labels_for_workspace_id("ws-1", db_path) == ["main"]


def test_labels_for_workspace_id_excludes_archived(tmp_path):
    db_path = str(tmp_path / "c.db")
    _build_labels_fixture_db(db_path, [
        ("ws-1", "HeyVox", 1, "feat: x", "main", "archived"),
    ])
    assert _labels_for_workspace_id("ws-1", db_path) == []


def test_labels_for_workspace_id_unknown_id_returns_empty(tmp_path):
    db_path = str(tmp_path / "c.db")
    _build_labels_fixture_db(db_path, [("ws-1", "HeyVox", 1, "t", "main", "ready")])
    assert _labels_for_workspace_id("does-not-exist", db_path) == []


def test_labels_for_workspace_id_missing_db_returns_empty(tmp_path):
    missing = str(tmp_path / "nope.db")
    assert _labels_for_workspace_id("ws-1", missing) == []


# ---------------------------------------------------------------------------
# _set_active_session — read-write UPDATE, error-swallowing
# ---------------------------------------------------------------------------


def test_set_active_session_updates_row(tmp_path):
    db_path = str(tmp_path / "c.db")
    _build_fixture_db(db_path, [("ws-1", "seattle", "main", "old-sess", "ready")])

    assert _set_active_session("ws-1", "new-sess", db_path) is True

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT active_session_id FROM workspaces WHERE id = ?", ("ws-1",)
    ).fetchone()
    conn.close()
    assert row[0] == "new-sess"


def test_set_active_session_missing_table_returns_false(tmp_path):
    missing = str(tmp_path / "nope.db")
    assert _set_active_session("ws-1", "sess", missing) is False


def test_set_active_session_locked_db_returns_false(tmp_path):
    db_path = str(tmp_path / "locked.db")
    _build_fixture_db(db_path, [("ws-1", "seattle", "main", "old-sess", "ready")])

    blocker = sqlite3.connect(db_path)
    try:
        blocker.execute("PRAGMA journal_mode=DELETE")
        blocker.execute("BEGIN EXCLUSIVE")
        assert _set_active_session("ws-1", "new-sess", db_path) is False
    finally:
        blocker.rollback()
        blocker.close()


# ---------------------------------------------------------------------------
# activate_workspace() — end-to-end orchestration, internals mocked
# ---------------------------------------------------------------------------


def _identity(workspace_id="ws-1", session_id=None):
    return WorkspaceIdentity(workspace_id=workspace_id, session_id=session_id)


def _match_row_fixture():
    return SidebarRow(label="HeyVox", elem="elem", link_parent="link", pos=(0, 0), size=(10, 10))


def test_activate_workspace_conductor_not_running_returns_false():
    with patch("heyvox.adapters.conductor._find_conductor_pid", return_value=None), \
         patch("heyvox.adapters.conductor._set_active_session") as mock_set_session:
        result = activate_workspace(_identity(), profile=None)

    assert result is False
    mock_set_session.assert_not_called()


def test_activate_workspace_already_on_target_short_circuits():
    mock_provider = MagicMock()
    mock_provider.detect_context.return_value = "main"
    mock_provider.resolve.return_value = _identity("ws-1")

    with patch("heyvox.adapters.conductor.ConductorWorkspaceProvider", return_value=mock_provider), \
         patch("heyvox.adapters.conductor._set_active_session") as mock_set_session, \
         patch("heyvox.adapters.conductor._labels_for_workspace_id") as mock_labels, \
         patch("heyvox.adapters.conductor._get_app_and_window") as mock_get_win, \
         patch("heyvox.adapters.conductor._find_matching_row") as mock_find_row, \
         patch("heyvox.adapters.conductor._dispatch_activation") as mock_dispatch:
        result = activate_workspace(_identity("ws-1", "sess-1"), profile=None, pid=1234)

    assert result is True
    mock_set_session.assert_called_once_with("ws-1", "sess-1", None)
    mock_labels.assert_not_called()
    mock_get_win.assert_not_called()
    mock_find_row.assert_not_called()
    mock_dispatch.assert_not_called()


def test_activate_workspace_no_session_id_skips_session_write():
    mock_provider = MagicMock()
    mock_provider.detect_context.return_value = "main"
    mock_provider.resolve.return_value = _identity("ws-1")  # already on target

    with patch("heyvox.adapters.conductor.ConductorWorkspaceProvider", return_value=mock_provider), \
         patch("heyvox.adapters.conductor._set_active_session") as mock_set_session:
        result = activate_workspace(_identity("ws-1", session_id=None), profile=None, pid=1234)

    assert result is True
    mock_set_session.assert_not_called()


def test_activate_workspace_ax_press_succeeds_first_try():
    mock_provider = MagicMock()
    mock_provider.detect_context.return_value = "main"
    mock_provider.resolve.side_effect = [
        _identity("other-ws"),   # pre-check: not already on target
        _identity("ws-1"),       # post-ax_press verify: matches
    ]
    match_row = _match_row_fixture()

    with patch("heyvox.adapters.conductor.ConductorWorkspaceProvider", return_value=mock_provider), \
         patch("heyvox.adapters.conductor._set_active_session"), \
         patch("heyvox.adapters.conductor._labels_for_workspace_id", return_value=["HeyVox"]), \
         patch("heyvox.adapters.conductor._get_app_and_window", return_value=("app", "win")), \
         patch("heyvox.adapters.conductor._read_pos_size", return_value=((0, 0), (100, 100))), \
         patch("heyvox.adapters.conductor._find_matching_row", return_value=match_row), \
         patch("heyvox.adapters.conductor._dispatch_activation", return_value=True) as mock_dispatch, \
         patch("heyvox.adapters.conductor.time.sleep"):
        result = activate_workspace(_identity("ws-1"), profile=None, pid=1234)

    assert result is True
    mock_dispatch.assert_called_once_with(match_row, "ax_press")


def test_activate_workspace_ax_press_verify_fails_falls_through_to_click():
    mock_provider = MagicMock()
    mock_provider.detect_context.return_value = "main"
    mock_provider.resolve.side_effect = [
        _identity("other-ws"),   # pre-check: not on target
        _identity("other-ws"),   # post-ax_press verify: still not on target
        _identity("ws-1"),       # post-click verify: matches
    ]
    match_row = _match_row_fixture()

    with patch("heyvox.adapters.conductor.ConductorWorkspaceProvider", return_value=mock_provider), \
         patch("heyvox.adapters.conductor._set_active_session"), \
         patch("heyvox.adapters.conductor._labels_for_workspace_id", return_value=["HeyVox"]), \
         patch("heyvox.adapters.conductor._get_app_and_window", return_value=("app", "win")), \
         patch("heyvox.adapters.conductor._read_pos_size", return_value=((0, 0), (100, 100))), \
         patch("heyvox.adapters.conductor._find_matching_row", return_value=match_row), \
         patch("heyvox.adapters.conductor._dispatch_activation", return_value=True) as mock_dispatch, \
         patch("heyvox.adapters.conductor.time.sleep"):
        result = activate_workspace(_identity("ws-1"), profile=None, pid=1234)

    assert result is True
    assert mock_dispatch.call_args_list == [
        call(match_row, "ax_press"),
        call(match_row, "click"),
    ]


def test_activate_workspace_both_mechanisms_fail_returns_false():
    mock_provider = MagicMock()
    mock_provider.detect_context.return_value = "main"
    mock_provider.resolve.return_value = _identity("other-ws")  # never matches
    match_row = _match_row_fixture()

    with patch("heyvox.adapters.conductor.ConductorWorkspaceProvider", return_value=mock_provider), \
         patch("heyvox.adapters.conductor._set_active_session"), \
         patch("heyvox.adapters.conductor._labels_for_workspace_id", return_value=["HeyVox"]), \
         patch("heyvox.adapters.conductor._get_app_and_window", return_value=("app", "win")), \
         patch("heyvox.adapters.conductor._read_pos_size", return_value=((0, 0), (100, 100))), \
         patch("heyvox.adapters.conductor._find_matching_row", return_value=match_row), \
         patch("heyvox.adapters.conductor._dispatch_activation", return_value=False) as mock_dispatch, \
         patch("heyvox.adapters.conductor.time.sleep") as mock_sleep:
        result = activate_workspace(_identity("ws-1"), profile=None, pid=1234)

    assert result is False
    assert mock_dispatch.call_count == 2
    mock_sleep.assert_not_called()  # dispatch itself failed both times — no settle wait


def test_activate_workspace_no_labels_skips_ax_search():
    mock_provider = MagicMock()
    mock_provider.detect_context.return_value = "main"
    mock_provider.resolve.return_value = _identity("other-ws")

    with patch("heyvox.adapters.conductor.ConductorWorkspaceProvider", return_value=mock_provider), \
         patch("heyvox.adapters.conductor._set_active_session"), \
         patch("heyvox.adapters.conductor._labels_for_workspace_id", return_value=[]), \
         patch("heyvox.adapters.conductor._get_app_and_window") as mock_get_win, \
         patch("heyvox.adapters.conductor._find_matching_row") as mock_find_row:
        result = activate_workspace(_identity("ws-1"), profile=None, pid=1234)

    assert result is False
    mock_get_win.assert_not_called()
    mock_find_row.assert_not_called()


def test_activate_workspace_no_window_returns_false():
    mock_provider = MagicMock()
    mock_provider.detect_context.return_value = "main"
    mock_provider.resolve.return_value = _identity("other-ws")

    with patch("heyvox.adapters.conductor.ConductorWorkspaceProvider", return_value=mock_provider), \
         patch("heyvox.adapters.conductor._set_active_session"), \
         patch("heyvox.adapters.conductor._labels_for_workspace_id", return_value=["HeyVox"]), \
         patch("heyvox.adapters.conductor._get_app_and_window", return_value=("app", None)), \
         patch("heyvox.adapters.conductor._find_matching_row") as mock_find_row:
        result = activate_workspace(_identity("ws-1"), profile=None, pid=1234)

    assert result is False
    mock_find_row.assert_not_called()


def test_activate_workspace_no_matching_row_returns_false():
    mock_provider = MagicMock()
    mock_provider.detect_context.return_value = "main"
    mock_provider.resolve.return_value = _identity("other-ws")

    with patch("heyvox.adapters.conductor.ConductorWorkspaceProvider", return_value=mock_provider), \
         patch("heyvox.adapters.conductor._set_active_session"), \
         patch("heyvox.adapters.conductor._labels_for_workspace_id", return_value=["HeyVox"]), \
         patch("heyvox.adapters.conductor._get_app_and_window", return_value=("app", "win")), \
         patch("heyvox.adapters.conductor._read_pos_size", return_value=((0, 0), (100, 100))), \
         patch("heyvox.adapters.conductor._find_matching_row", return_value=None), \
         patch("heyvox.adapters.conductor._dispatch_activation") as mock_dispatch:
        result = activate_workspace(_identity("ws-1"), profile=None, pid=1234)

    assert result is False
    mock_dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# ConductorWorkspaceProvider — resolve_by_name / activate delegation
# ---------------------------------------------------------------------------


def test_provider_resolve_by_name_converts_identity(tmp_path):
    db_path = str(tmp_path / "c.db")
    _build_fixture_db(db_path, [("ws-1", "seattle", "main", "sess-1", "ready")])

    profile = SimpleNamespace(workspace_db=db_path)
    provider = ConductorWorkspaceProvider()
    identity = provider.resolve_by_name("seattle", profile)

    assert identity == WorkspaceIdentity(workspace_id="ws-1", session_id="sess-1")


def test_provider_resolve_by_name_no_match_returns_none(tmp_path):
    db_path = str(tmp_path / "c.db")
    _build_fixture_db(db_path, [("ws-1", "seattle", "main", "sess-1", "ready")])

    profile = SimpleNamespace(workspace_db=db_path)
    provider = ConductorWorkspaceProvider()
    assert provider.resolve_by_name("atlantis", profile) is None


def test_provider_resolve_by_name_matches_workspace_name_slug(tmp_path):
    """DEF-242 — same fix, exercised through the provider entry point that
    orchestrator.py._switch_workspace actually calls."""
    db_path = str(tmp_path / "c.db")
    _build_fixture_db(
        db_path,
        [(
            "ws-vienna", "vienna", "personal/todo-tracker",
            "sess-v", "ready", "Personal Admin",
        )],
    )

    profile = SimpleNamespace(workspace_db=db_path)
    provider = ConductorWorkspaceProvider()
    identity = provider.resolve_by_name("personal-admin", profile)

    assert identity == WorkspaceIdentity(workspace_id="ws-vienna", session_id="sess-v")


def test_provider_activate_delegates_to_activate_workspace():
    identity = _identity("ws-1")
    profile = object()
    with patch("heyvox.adapters.conductor.activate_workspace", return_value=True) as mock_activate:
        provider = ConductorWorkspaceProvider()
        result = provider.activate(identity, profile, pid=999)

    assert result is True
    mock_activate.assert_called_once_with(identity, profile, pid=999)
