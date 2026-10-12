"""Guard tests for DEF-271 (Orca paste failed closed: focus on a chat AXGroup).

2026-10-12, 2 of 70 Orca pastes: at record start and at paste time Orca's focused
element was an AXGroup, so resolve_lock failed closed with no_text_field_at_start
although the window held exactly one composer. The paste now focuses the field a
paste last went into (per app) or the lock's own field when it is the only match
in the window, and logs what held the focus.
"""
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from heyvox.input import target

PID = 4242
BUNDLE = "com.example.agent"
COMPOSER = "Ask anything"


class El:
    def __init__(self, role, desc=None, children=(), classes=()):
        self.attrs = {"AXRole": role, "AXDescription": desc, "AXDOMClassList": list(classes)}
        self.children = list(children)
        self.parent = None
        for c in self.children:
            c.parent = self


def _ax(tree, focused):
    """AX double over an El tree. Setting AXFocused moves the app's focus."""
    state = {"focused": focused, "sets": []}

    def copy_attr(el, attr, _):
        if el == "ax-app":
            if attr == "AXFocusedUIElement":
                f = state["focused"]
                return (0, f) if f is not None else (-25212, None)
            if attr == "AXFocusedWindow":
                return 0, tree
            return -25205, None
        if attr == "AXChildren":
            return 0, el.children
        if attr == "AXParent":
            return (0, el.parent) if el.parent else (-25205, None)
        v = el.attrs.get(attr)
        return (0, v) if v is not None else (-25205, None)

    def set_attr(el, attr, val):
        state["sets"].append((el, attr, val))
        if attr == "AXFocused" and val:
            state["focused"] = el
        return 0

    ax = types.SimpleNamespace(
        AXUIElementCreateApplication=lambda pid: "ax-app",
        AXUIElementCopyAttributeValue=copy_attr,
        AXUIElementSetAttributeValue=set_attr,
    )
    return ax, state


def _window(*composers, group=None):
    group = group or El("AXGroup", classes=["relative", "flex"])
    fields = [El("AXTextArea", COMPOSER) for _ in composers]
    other = El("AXTextField", "Find files")
    win = El("AXWindow", children=[El("AXGroup", children=[group, *fields, other])])
    return win, group, fields, other


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    target._KNOWN_FIELDS.clear()
    monkeypatch.setattr(target._time, "sleep", lambda s: None)
    logs = []
    monkeypatch.setattr(target, "_log", logs.append)
    yield logs
    target._KNOWN_FIELDS.clear()


def _recapture_lock():
    return types.SimpleNamespace(
        app_pid=PID, app_name="Agent", app_bundle_id=BUNDLE, window_number=0,
        focused_was_text_field=False, leaf_role="AXGroup", leaf_description=None,
    )


def _run_recapture(ax, lock):
    with patch.dict(sys.modules, {"ApplicationServices": ax, "AppKit": MagicMock()}), \
            patch("heyvox.input.activation.activate_running_app"), \
            patch.object(target, "_poll_live_focus",
                         lambda pid: (ax.state["focused"], "AXGroup")):
        return target._try_activate_and_recapture(lock)


def test_group_focus_refocuses_the_remembered_field(_clean):
    win, group, (composer,), _ = _window(1)
    ax, state = _ax(win, group)
    ax.state = state
    target._KNOWN_FIELDS[BUNDLE] = ("AXTextArea", COMPOSER)
    lock = _recapture_lock()

    assert _run_recapture(ax, lock) is True
    assert state["focused"] is composer
    assert lock.focused_was_text_field is True
    assert lock.leaf_role == "AXTextArea"
    assert lock.leaf_description == COMPOSER
    assert any("AXGroup[relative.flex]" in m for m in _clean)
    assert any("(refocused)" in m for m in _clean)


def test_group_focus_without_remembered_field_fails_closed(_clean):
    win, group, _, _ = _window(1)
    ax, state = _ax(win, group)
    ax.state = state

    assert _run_recapture(ax, _recapture_lock()) is False
    assert state["focused"] is group
    assert any("no proven field remembered" in m for m in _clean)


def test_two_matching_fields_fail_closed():
    win, group, _, _ = _window(1, 2)
    ax, state = _ax(win, group)
    ax.state = state
    target._KNOWN_FIELDS[BUNDLE] = ("AXTextArea", COMPOSER)

    assert _run_recapture(ax, _recapture_lock()) is False
    assert not any(a == "AXFocused" for _, a, _ in state["sets"])


def test_focus_that_does_not_move_fails_closed():
    win, group, (composer,), _ = _window(1)
    ax, state = _ax(win, group)
    ax.state = state
    ax.AXUIElementSetAttributeValue = lambda el, attr, val: 0  # app ignores it
    target._KNOWN_FIELDS[BUNDLE] = ("AXTextArea", COMPOSER)

    assert _run_recapture(ax, _recapture_lock()) is False


# --- Tier 1b: the lock's own field, refocused by its identity --------------

def _tier1b_lock():
    return types.SimpleNamespace(
        app_pid=PID, app_name="Agent", app_bundle_id=BUNDLE, window_number=0,
        leaf_role="AXTextArea", leaf_axid=None, leaf_title=None,
        leaf_description=COMPOSER,
    )


def _run_tier1b(ax, live_role):
    with patch.dict(sys.modules, {"ApplicationServices": ax}), \
            patch.object(target, "_poll_live_focus",
                         lambda pid: (ax.state["focused"], live_role)):
        return target._live_focus_matches_lock(_tier1b_lock())


def test_tier1b_group_focus_refocuses_lock_field_and_remembers_it():
    win, group, (composer,), _ = _window(1)
    ax, state = _ax(win, group)
    ax.state = state

    assert _run_tier1b(ax, "AXGroup") is True
    assert state["focused"] is composer
    assert target._KNOWN_FIELDS[BUNDLE] == ("AXTextArea", COMPOSER)


def test_tier1b_other_text_field_keeps_focus():
    win, _, _, other = _window(1)
    ax, state = _ax(win, other)
    ax.state = state

    assert _run_tier1b(ax, "AXTextField") is False
    assert state["focused"] is other
    assert not state["sets"]


def test_remember_field_only_keeps_text_fields_with_a_description():
    target._remember_field(types.SimpleNamespace(
        app_bundle_id=BUNDLE, leaf_role="AXGroup", leaf_description="x"))
    target._remember_field(types.SimpleNamespace(
        app_bundle_id=BUNDLE, leaf_role="AXTextArea", leaf_description=None))
    assert BUNDLE not in target._KNOWN_FIELDS
    target._remember_field(types.SimpleNamespace(
        app_bundle_id=BUNDLE, leaf_role="AXTextArea", leaf_description=COMPOSER))
    assert target._KNOWN_FIELDS[BUNDLE] == ("AXTextArea", COMPOSER)
