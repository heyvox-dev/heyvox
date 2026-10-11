"""Guard tests for DEF-270 (Orca paste failed closed: AXFocusedUIElement NoValue for 2.4 s).

The app was frontmost and the lock had been captured 7 s earlier, yet the live-focus
poll got -25212 on all 20 reads. The poll now cycles AXManualAccessibility once after
10 empty reads (only for the frontmost app) and logs one diagnostic line when it still
finds nothing.
"""
import sys
import types
from unittest.mock import patch

from heyvox.input import target

PID = 4242


def _ax(focus_after=None, sets=None):
    """AX double. `focus_after`: number of reads that return NoValue first
    (None = never focused). `sets` collects AXManualAccessibility writes."""
    reads = {"n": 0}
    sets = sets if sets is not None else []

    def copy_attr(el, attr, _):
        if attr == "AXFocusedUIElement":
            reads["n"] += 1
            if focus_after is not None and reads["n"] > focus_after:
                return 0, "focused-elem"
            return -25212, None
        if attr == "AXRole":
            return 0, "AXTextArea"
        if attr in ("AXFocusedWindow", "AXMainWindow"):
            return 0, "win"
        if attr == "AXTitle":
            return 0, "Orca"
        if attr == "AXChildren":
            return 0, ["a", "b"]
        if attr == "AXWindows":
            return 0, ["win"]
        return -25205, None

    def set_attr(el, attr, val):
        sets.append((attr, val))
        return 0

    return types.SimpleNamespace(
        AXUIElementCreateApplication=lambda pid: "ax-app",
        AXUIElementCopyAttributeValue=copy_attr,
        AXUIElementSetAttributeValue=set_attr,
    )


def _poll(ax, front_pid=PID):
    logs = []
    front = types.SimpleNamespace(processIdentifier=lambda: front_pid)
    with patch.dict(sys.modules, {"ApplicationServices": ax}), \
            patch.object(target._time, "sleep"), \
            patch.object(target, "_log", logs.append), \
            patch("heyvox.input.frontmost.frontmost_app", return_value=front):
        result = target._poll_live_focus(PID)
    return result, logs


def test_immediate_focus_does_not_toggle_or_dump():
    sets = []
    (focused, role), logs = _poll(_ax(focus_after=0, sets=sets))
    assert (focused, role) == ("focused-elem", "AXTextArea")
    assert sets == [("AXManualAccessibility", True)]
    assert not any("diag:" in m for m in logs)


def test_frontmost_app_without_focus_toggles_flag_once_then_dumps_diag():
    sets = []
    (focused, role), logs = _poll(_ax(focus_after=None, sets=sets))
    assert (focused, role) == (None, "")
    assert sets == [
        ("AXManualAccessibility", True),
        ("AXManualAccessibility", False),
        ("AXManualAccessibility", True),
    ]
    diag = [m for m in logs if "[live-focus] diag:" in m]
    assert len(diag) == 1
    assert f"front_pid={PID}" in diag[0] and "AXFocusedWindow=ok" in diag[0]
    assert any("a11y-toggled" in m for m in logs)


def test_focus_that_appears_after_the_toggle_is_returned():
    (focused, role), logs = _poll(_ax(focus_after=12))
    assert (focused, role) == ("focused-elem", "AXTextArea")
    assert any("a11y-toggled" in m for m in logs)
    assert not any("diag:" in m for m in logs)


def test_background_app_is_not_toggled():
    sets = []
    (focused, _), logs = _poll(_ax(focus_after=None, sets=sets), front_pid=PID + 1)
    assert focused is None
    assert sets == [("AXManualAccessibility", True)]
    assert any("front_pid=%d" % (PID + 1) in m for m in logs if "diag:" in m)


def test_diag_never_raises_when_ax_calls_blow_up():
    ax = _ax(focus_after=None)
    real = ax.AXUIElementCopyAttributeValue

    def flaky(el, attr, _):
        if attr in ("AXFocusedWindow", "AXMainWindow", "AXWindows"):
            raise RuntimeError("boom")
        return real(el, attr, _)

    ax.AXUIElementCopyAttributeValue = flaky
    (focused, _), logs = _poll(ax)
    assert focused is None
    assert any("[live-focus] diag:" in m and "exc(" in m for m in logs)
