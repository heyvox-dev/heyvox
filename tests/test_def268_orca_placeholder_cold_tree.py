"""Guard tests for DEF-268 (Orca placeholder sent as text after an Orca restart).

Orca had just restarted, so its Chromium a11y tree was still off when the record-start
lock was captured: nothing focused, no AXDescription on the lock. The paste recovered
at stop time (2-3 s of polling a tree that only then started to build) and the AX
injector, which recognised the empty composer's placeholder only via the lock's
AXDescription, appended the transcript to "Ask anything, @ to mention files, …".
"""
import types
from unittest.mock import MagicMock, patch

from heyvox.input.injection import _ax_inject_text, _ax_set_focused_value

PLACEHOLDER = "Ask anything, @ to mention files, / for commands"


def _ax_mock(store, focused_after=0):
    """AX double: Orca composer whose empty AXValue is its placeholder.

    `focused_after`: number of AXFocusedUIElement reads that return "no value"
    before the element shows up (cold Chromium tree).
    """
    focused = object()
    reads = {"focused": 0}

    def _copy(el, attr, _):
        if attr == "AXFocusedUIElement":
            reads["focused"] += 1
            if reads["focused"] <= focused_after:
                return (-25212, None)
            return (0, focused)
        if attr == "AXValue":
            return (0, store.get("value") or PLACEHOLDER + "\n")
        if attr == "AXDescription":
            return (0, PLACEHOLDER)
        if attr == "AXRole":
            return (0, "AXTextArea")
        return (-25205, None)

    def _set(el, attr, val):
        store.setdefault("sets", []).append(attr)
        if attr == "AXValue":
            store["value"] = val
        return 0

    mock = MagicMock()
    mock.AXUIElementCreateApplication.return_value = MagicMock()
    mock.AXUIElementCopyAttributeValue = MagicMock(side_effect=_copy)
    mock.AXUIElementSetAttributeValue = MagicMock(side_effect=_set)
    return mock


def _snap_without_description():
    snap = MagicMock()
    snap.leaf_role = "AXTextArea"
    snap.app_pid = 1234
    snap.app_name = "Orca"
    snap.workspace_id = "wt-1::/repo"
    snap.leaf_description = None
    return snap


def test_ax_inject_drops_placeholder_known_only_from_live_element():
    store = {}
    with patch.dict("sys.modules", {"ApplicationServices": _ax_mock(store)}):
        assert _ax_inject_text(
            _snap_without_description(), "hallo", allow_workspace_managed=True,
        ) is True
    assert store["value"] == "hallo"


def test_ax_set_focused_value_drops_placeholder():
    store = {}
    with patch.dict("sys.modules", {"ApplicationServices": _ax_mock(store)}):
        assert _ax_set_focused_value(1234, "hallo") is True
    assert store["value"] == "hallo"


def test_real_draft_is_kept():
    store = {"value": "Entwurf "}
    with patch.dict("sys.modules", {"ApplicationServices": _ax_mock(store)}):
        assert _ax_inject_text(
            _snap_without_description(), "hallo", allow_workspace_managed=True,
        ) is True
    assert store["value"] == "Entwurf hallo"


def test_recapture_gives_lock_the_live_description(monkeypatch):
    from heyvox.input import target

    lock = types.SimpleNamespace(
        app_pid=1234, app_name="Orca", focused_was_text_field=False,
        leaf_role="", leaf_description=None,
    )
    appkit = MagicMock()
    monkeypatch.setattr(target, "_poll_live_focus", lambda pid: (object(), "AXTextArea"))
    monkeypatch.setattr(
        target, "_capture_leaf_tiebreakers", lambda el: (None, None, PLACEHOLDER),
    )
    with patch.dict("sys.modules", {"AppKit": appkit}):
        assert target._try_activate_and_recapture(lock) is True
    assert lock.leaf_description == PLACEHOLDER
    assert lock.leaf_role == "AXTextArea"


def test_capture_lock_switches_on_cold_chromium_tree(monkeypatch):
    from heyvox.input import frontmost, target

    front = MagicMock()
    front.localizedName.return_value = "Orca"
    front.processIdentifier.return_value = 1234
    front.bundleIdentifier.return_value = "com.stablyai.orca"
    appkit = MagicMock()
    appkit.NSRunningApplication.runningApplicationWithProcessIdentifier_.return_value = front
    monkeypatch.setattr(target, "_app_under_mouse", lambda: None)
    monkeypatch.setattr(frontmost, "frontmost_app", lambda: front)
    monkeypatch.setattr(target._time, "sleep", lambda s: None)
    store = {}
    ax = _ax_mock(store, focused_after=3)

    with patch.dict("sys.modules", {"AppKit": appkit, "ApplicationServices": ax}):
        lock = target.capture_lock(config=None)

    assert "AXManualAccessibility" in store["sets"]
    assert lock.focused_was_text_field is True
    assert lock.leaf_description == PLACEHOLDER
