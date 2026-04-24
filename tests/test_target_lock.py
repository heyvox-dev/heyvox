"""Unit tests for heyvox.input.target.capture_lock + TargetLock (Plan 15-02).

Covers:
- Frozen dataclass invariant (SPEC R1)
- Role-path capture + MAX_ROLE_PATH_HOPS cap (D-03)
- leaf_role + leaf_axid + focused_was_text_field population
- Conductor adapter integration with branch filter (W-fix iteration-3)
- B2 per-call ThreadPoolExecutor no-leak
- B1 _ax_inject_text TargetLock compatibility (Phase 12 fast-path alive)
"""

import threading
import time
from dataclasses import FrozenInstanceError, dataclass, field
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeAppProfile:
    name: str = "Conductor"
    focus_shortcut: str = "l"
    enter_count: int = 1
    is_electron: bool = True
    settle_delay: float = 0.3
    enter_delay: float = 0.15
    has_workspace_detection: bool = True
    workspace_db: str = "~/fake-conductor.db"
    workspace_list_query: str = ""
    workspace_switch_cmd: str = ""


@dataclass
class FakeConfig:
    _profile: FakeAppProfile = field(default_factory=FakeAppProfile)

    def get_app_profile(self, app_name):
        if app_name and self._profile.name.lower() in app_name.lower():
            return self._profile
        return None


def _build_appkit_mock(app_name="Conductor", pid=1234, bundle_id="com.conductor.app"):
    """Minimal AppKit mock for capture_lock."""
    mock_appkit = MagicMock()
    mock_front = MagicMock()
    mock_front.localizedName.return_value = app_name
    mock_front.processIdentifier.return_value = pid
    mock_front.bundleIdentifier.return_value = bundle_id
    mock_appkit.NSWorkspace.sharedWorkspace.return_value.frontmostApplication.return_value = mock_front
    mock_appkit.NSRunningApplication.runningApplicationWithProcessIdentifier_.return_value = mock_front
    return mock_appkit


def _build_ax_mock(focused_role="AXTextArea", window_number=42, children_depth=1):
    """Minimal ApplicationServices mock.

    - AXFocusedUIElement → a fake element with AXRole = focused_role + AXTitle etc.
    - AXFocusedWindow → a fake window with AXWindowNumber = window_number
    - AXChildren walk returns `children_depth` level of nested children
    """
    mock_ax = MagicMock()
    fake_focused = MagicMock(name="focused_element")
    fake_window = MagicMock(name="window_element")

    def _fake_copy(element, attr, _):
        # Application-level calls
        if attr == "AXFocusedUIElement":
            return (0, fake_focused)
        if attr == "AXFocusedWindow":
            return (0, fake_window)
        # Leaf element attributes
        if element is fake_focused:
            if attr == "AXRole":
                return (0, focused_role)
            if attr == "AXIdentifier":
                return (0, "msg-input")
            if attr == "AXTitle":
                return (0, "Message")
            if attr == "AXDescription":
                return (0, None)
        # Window attributes
        if element is fake_window:
            if attr == "AXWindowNumber":
                return (0, window_number)
            if attr == "AXChildren":
                # return chain of length = children_depth ending at fake_focused
                if children_depth <= 1:
                    return (0, [fake_focused])
                mids = [MagicMock(name=f"mid-{i}") for i in range(children_depth - 1)]
                return (0, [mids[0]] if mids else [fake_focused])
        # Intermediate children walk — continue chain until fake_focused
        if attr == "AXRole":
            return (0, "AXGroup")
        if attr == "AXChildren":
            return (0, [fake_focused])
        if attr == "AXValue":
            return (0, None)
        return (-25200, None)

    mock_ax.AXUIElementCreateApplication.return_value = MagicMock()
    mock_ax.AXUIElementCopyAttributeValue.side_effect = _fake_copy
    return mock_ax, fake_focused, fake_window


# ---------------------------------------------------------------------------
# Frozen invariant + basic construction
# ---------------------------------------------------------------------------


def test_target_lock_is_frozen():
    from heyvox.input.target import TargetLock

    lock = TargetLock(
        app_bundle_id="x", app_pid=1, window_number=0, ax_role_path=()
    )
    with pytest.raises(FrozenInstanceError):
        lock.app_pid = 2  # type: ignore[misc]


def test_target_lock_defaults_constructable():
    from heyvox.input.target import TargetLock

    lock = TargetLock(
        app_bundle_id="", app_pid=0, window_number=0, ax_role_path=()
    )
    assert lock.leaf_role == ""
    assert lock.leaf_axid is None
    assert lock.conductor_workspace_id is None
    assert lock.focused_was_text_field is False
    assert lock.captured_at == 0.0


def test_max_role_path_hops_is_12():
    from heyvox.input.target import MAX_ROLE_PATH_HOPS

    assert MAX_ROLE_PATH_HOPS == 12


# ---------------------------------------------------------------------------
# capture_lock — AppKit unavailable
# ---------------------------------------------------------------------------


def test_capture_lock_returns_none_when_appkit_unavailable():
    import sys as _sys

    from heyvox.input.target import capture_lock

    saved_appkit = _sys.modules.get("AppKit")
    saved_as = _sys.modules.get("ApplicationServices")

    # Make AppKit import fail
    class BadMod:
        def __getattr__(self, name):
            raise ImportError("boom")

    with patch.dict(_sys.modules, {"AppKit": None, "ApplicationServices": None}):
        result = capture_lock(config=None)
    assert result is None


# ---------------------------------------------------------------------------
# capture_lock — happy path + field population
# ---------------------------------------------------------------------------


def test_capture_lock_populates_stable_fields(monkeypatch):
    from heyvox.input.target import capture_lock

    mock_appkit = _build_appkit_mock(app_name="Conductor", pid=1234,
                                     bundle_id="com.conductor.app")
    mock_ax, _focused, _window = _build_ax_mock(
        focused_role="AXTextArea", window_number=42
    )

    # _app_under_mouse should return None so we fall through to frontmost
    monkeypatch.setattr("heyvox.input.target._app_under_mouse", lambda: None)
    # _detect_conductor_branch returns empty so adapter is NOT called
    monkeypatch.setattr(
        "heyvox.input.target._detect_conductor_branch", lambda pid: ""
    )

    with patch.dict("sys.modules", {
        "AppKit": mock_appkit, "ApplicationServices": mock_ax,
    }):
        lock = capture_lock(config=FakeConfig())

    assert lock is not None
    assert lock.app_pid == 1234
    assert lock.app_bundle_id == "com.conductor.app"
    assert lock.window_number == 42
    assert lock.leaf_role == "AXTextArea"
    assert lock.focused_was_text_field is True
    assert lock.leaf_axid == "msg-input"
    assert lock.captured_at > 0


def test_focused_was_text_field_false_for_button(monkeypatch):
    from heyvox.input.target import capture_lock

    mock_appkit = _build_appkit_mock()
    mock_ax, _, _ = _build_ax_mock(focused_role="AXButton")

    monkeypatch.setattr("heyvox.input.target._app_under_mouse", lambda: None)
    monkeypatch.setattr(
        "heyvox.input.target._detect_conductor_branch", lambda pid: ""
    )

    with patch.dict("sys.modules", {
        "AppKit": mock_appkit, "ApplicationServices": mock_ax,
    }):
        lock = capture_lock(config=FakeConfig())

    assert lock is not None
    assert lock.leaf_role == "AXButton"
    assert lock.focused_was_text_field is False


def test_leaf_role_captured_even_for_non_text_field(monkeypatch):
    from heyvox.input.target import capture_lock

    mock_appkit = _build_appkit_mock()
    mock_ax, _, _ = _build_ax_mock(focused_role="AXCheckBox")

    monkeypatch.setattr("heyvox.input.target._app_under_mouse", lambda: None)
    monkeypatch.setattr(
        "heyvox.input.target._detect_conductor_branch", lambda pid: ""
    )

    with patch.dict("sys.modules", {
        "AppKit": mock_appkit, "ApplicationServices": mock_ax,
    }):
        lock = capture_lock(config=FakeConfig())

    assert lock.leaf_role == "AXCheckBox"


# ---------------------------------------------------------------------------
# Branch-filter adapter integration (W-fix iteration-3)
# ---------------------------------------------------------------------------


def test_capture_lock_passes_detected_branch_to_adapter(monkeypatch):
    """W-fix: capture_lock MUST pass branch=detected_branch to the adapter."""
    from heyvox.input.target import capture_lock

    captured_kwargs = {}

    def _fake_adapter(**kwargs):
        captured_kwargs.update(kwargs)
        return None

    mock_appkit = _build_appkit_mock()
    mock_ax, _, _ = _build_ax_mock()

    monkeypatch.setattr("heyvox.input.target._app_under_mouse", lambda: None)
    monkeypatch.setattr(
        "heyvox.input.target._detect_conductor_branch", lambda pid: "feature-xyz"
    )
    monkeypatch.setattr(
        "heyvox.input.target.get_active_workspace_and_session", _fake_adapter
    )

    with patch.dict("sys.modules", {
        "AppKit": mock_appkit, "ApplicationServices": mock_ax,
    }):
        capture_lock(config=FakeConfig())

    assert captured_kwargs.get("branch") == "feature-xyz", (
        f"adapter must be called with branch='feature-xyz', got {captured_kwargs!r}"
    )


def test_capture_lock_skips_adapter_when_branch_unknown(monkeypatch):
    """W-fix: when _detect_conductor_branch returns '', adapter is NOT called."""
    from heyvox.input.target import capture_lock

    adapter_calls = []

    def _fake_adapter(**kwargs):
        adapter_calls.append(kwargs)
        return None

    mock_appkit = _build_appkit_mock()
    mock_ax, _, _ = _build_ax_mock()

    monkeypatch.setattr("heyvox.input.target._app_under_mouse", lambda: None)
    monkeypatch.setattr(
        "heyvox.input.target._detect_conductor_branch", lambda pid: ""
    )
    monkeypatch.setattr(
        "heyvox.input.target.get_active_workspace_and_session", _fake_adapter
    )

    with patch.dict("sys.modules", {
        "AppKit": mock_appkit, "ApplicationServices": mock_ax,
    }):
        lock = capture_lock(config=FakeConfig())

    assert adapter_calls == [], "adapter should not be called when branch unknown"
    assert lock.conductor_workspace_id is None
    assert lock.conductor_session_id is None


def test_capture_lock_populates_conductor_ids_from_adapter(monkeypatch):
    from heyvox.adapters.conductor import ConductorIdentity
    from heyvox.input.target import capture_lock

    fake_identity = ConductorIdentity(
        workspace_id="ws-abc",
        session_id="sess-42",
        branch="feature-x",
        directory_name="seattle",
    )

    mock_appkit = _build_appkit_mock()
    mock_ax, _, _ = _build_ax_mock()

    monkeypatch.setattr("heyvox.input.target._app_under_mouse", lambda: None)
    monkeypatch.setattr(
        "heyvox.input.target._detect_conductor_branch", lambda pid: "feature-x"
    )
    monkeypatch.setattr(
        "heyvox.input.target.get_active_workspace_and_session",
        lambda **kw: fake_identity,
    )

    with patch.dict("sys.modules", {
        "AppKit": mock_appkit, "ApplicationServices": mock_ax,
    }):
        lock = capture_lock(config=FakeConfig())

    assert lock.conductor_workspace_id == "ws-abc"
    assert lock.conductor_session_id == "sess-42"


def test_capture_lock_survives_slow_adapter_with_timeout(monkeypatch):
    """Adapter that blocks >100ms is cancelled; lock returned without IDs."""
    from heyvox.input.target import capture_lock

    def _slow_adapter(**kwargs):
        threading.Event().wait(0.5)
        return None

    mock_appkit = _build_appkit_mock()
    mock_ax, _, _ = _build_ax_mock()

    monkeypatch.setattr("heyvox.input.target._app_under_mouse", lambda: None)
    monkeypatch.setattr(
        "heyvox.input.target._detect_conductor_branch", lambda pid: "x"
    )
    monkeypatch.setattr(
        "heyvox.input.target.get_active_workspace_and_session", _slow_adapter
    )

    with patch.dict("sys.modules", {
        "AppKit": mock_appkit, "ApplicationServices": mock_ax,
    }):
        t0 = time.perf_counter()
        lock = capture_lock(config=FakeConfig())
        # executor context-manager waits for the worker on __exit__, so the
        # wall time includes the full 0.5s even though the future returns
        # early via TimeoutError. What matters is (a) the lock returns, and
        # (b) the future's cancel() was best-effort called.
        elapsed = time.perf_counter() - t0

    assert lock is not None
    assert lock.conductor_workspace_id is None
    assert lock.conductor_session_id is None
    # Sanity: regardless of executor join behaviour, we should never be
    # blocked for >1s (the slow adapter returns after 0.5s max).
    assert elapsed < 1.0, f"capture_lock wall time {elapsed:.2f}s too high"


# ---------------------------------------------------------------------------
# Executor no-leak (B2) — threads joined on exit
# ---------------------------------------------------------------------------


def test_executor_no_thread_leak_across_30_capture_lock_calls(monkeypatch):
    from heyvox.input.target import capture_lock

    mock_appkit = _build_appkit_mock()
    mock_ax, _, _ = _build_ax_mock()

    monkeypatch.setattr("heyvox.input.target._app_under_mouse", lambda: None)
    monkeypatch.setattr(
        "heyvox.input.target._detect_conductor_branch", lambda pid: "x"
    )
    monkeypatch.setattr(
        "heyvox.input.target.get_active_workspace_and_session", lambda **kw: None
    )

    with patch.dict("sys.modules", {
        "AppKit": mock_appkit, "ApplicationServices": mock_ax,
    }):
        baseline = threading.active_count()
        for _ in range(30):
            capture_lock(config=FakeConfig())
        time.sleep(0.1)
        after = threading.active_count()

    assert after - baseline <= 1, (
        f"thread count grew from {baseline} to {after} — "
        f"executor worker not joined"
    )


# ---------------------------------------------------------------------------
# _ax_inject_text TargetLock compatibility (B1)
# ---------------------------------------------------------------------------


def test_ax_inject_text_accepts_target_lock_with_leaf_role(monkeypatch):
    from heyvox.input.injection import _ax_inject_text
    from heyvox.input.target import TargetLock

    lock = TargetLock(
        app_bundle_id="com.test.app",
        app_pid=1234,
        window_number=42,
        ax_role_path=(),
        leaf_role="AXTextArea",
        conductor_workspace_id=None,
    )

    fake_focused = object()
    mock_ax = MagicMock()
    mock_ax.AXUIElementCreateApplication.return_value = MagicMock()
    mock_ax.AXUIElementCopyAttributeValue.return_value = (0, fake_focused)
    mock_ax.AXUIElementSetAttributeValue.return_value = 0

    with patch.dict("sys.modules", {"ApplicationServices": mock_ax}):
        result = _ax_inject_text(lock, "hello")

    assert result is True


def test_ax_inject_text_skips_workspace_managed_app():
    from heyvox.input.injection import _ax_inject_text
    from heyvox.input.target import TargetLock

    lock = TargetLock(
        app_bundle_id="com.conductor",
        app_pid=1234,
        window_number=42,
        ax_role_path=(),
        leaf_role="AXTextArea",
        conductor_workspace_id="ws-1",
    )
    assert _ax_inject_text(lock, "hello") is False


def test_ax_inject_text_rejects_non_text_field():
    from heyvox.input.injection import _ax_inject_text
    from heyvox.input.target import TargetLock

    lock = TargetLock(
        app_bundle_id="x",
        app_pid=1,
        window_number=0,
        ax_role_path=(),
        leaf_role="AXButton",
    )
    assert _ax_inject_text(lock, "hello") is False


def test_ax_inject_text_rejects_lock_with_no_pid():
    from heyvox.input.injection import _ax_inject_text
    from heyvox.input.target import TargetLock

    lock = TargetLock(
        app_bundle_id="x",
        app_pid=0,
        window_number=0,
        ax_role_path=(),
        leaf_role="AXTextArea",
    )
    assert _ax_inject_text(lock, "hello") is False


def test_ax_inject_text_phase12_fastpath_remains_under_5ms():
    """B1 regression guard: the fast-path must complete quickly under mocked
    AX setters. Pre-migration, the function bailed silently on TargetLock
    (missing element_role attribute) — that bail WAS fast but meant the
    fast-path was DEAD. Post-migration, it does real work and still stays fast.
    """
    from heyvox.input.injection import _ax_inject_text
    from heyvox.input.target import TargetLock

    lock = TargetLock(
        app_bundle_id="x",
        app_pid=1,
        window_number=0,
        ax_role_path=(),
        leaf_role="AXTextArea",
        conductor_workspace_id=None,
    )

    fake_focused = object()
    mock_ax = MagicMock()
    mock_ax.AXUIElementCreateApplication.return_value = MagicMock()
    mock_ax.AXUIElementCopyAttributeValue.return_value = (0, fake_focused)
    mock_ax.AXUIElementSetAttributeValue.return_value = 0

    with patch.dict("sys.modules", {"ApplicationServices": mock_ax}):
        durations = []
        for _ in range(50):
            t0 = time.perf_counter()
            ok = _ax_inject_text(lock, "hello world")
            durations.append(time.perf_counter() - t0)
            assert ok is True, "fast-path returned False — migration broken"

    mean = sum(durations) / len(durations)
    assert mean < 0.005, f"_ax_inject_text mean {mean*1000:.2f}ms — fast-path regressed"
