"""DEF-258: dictation into Orca (Electron) failed closed with no_text_field_at_start.

Orca had no app profile, and Chromium only exposes AXFocusedUIElement for the
ACTIVE app, so with the mouse over Orca and another app frontmost the capture
saw no text field. The Orca profile opts into activate_on_mismatch and
_try_activate_and_recapture now switches Chromium's a11y tree on and polls.
"""
import sys
import types
from unittest.mock import MagicMock, patch

from heyvox.config import HeyvoxConfig
from heyvox.input import target


def test_orca_default_profile_recovers_on_mismatch():
    profile = HeyvoxConfig().get_app_profile("Orca")
    assert profile is not None
    assert profile.activate_on_mismatch is True
    assert profile.is_electron is True
    # No focus shortcut: Orca restores focus on activation, so the recovery
    # path (not Tier 2 + app_fast_paste) carries the paste.
    assert profile.focus_shortcut == ""


def _fake_ax(focused_after_polls: int, role: str = "AXTextArea"):
    """Fake ApplicationServices: AXFocusedUIElement empty for N polls, then set."""
    calls = {"focused": 0, "set": []}

    def copy_attr(elem, attr, _):
        if attr == "AXFocusedUIElement":
            calls["focused"] += 1
            if calls["focused"] > focused_after_polls:
                return 0, "focused-elem"
            return -25212, None
        if attr == "AXRole":
            return 0, role
        return -25205, None

    def set_attr(elem, attr, value):
        calls["set"].append((attr, value))
        return 0

    ax = types.SimpleNamespace(
        AXUIElementCreateApplication=lambda pid: "ax-app",
        AXUIElementCopyAttributeValue=copy_attr,
        AXUIElementSetAttributeValue=set_attr,
    )
    return ax, calls


def _run_recapture(ax, lock):
    appkit = MagicMock()
    running = appkit.NSRunningApplication.runningApplicationWithProcessIdentifier_.return_value
    with patch.dict(sys.modules, {"AppKit": appkit, "ApplicationServices": ax}), \
            patch.object(target._time, "sleep"):
        return target._try_activate_and_recapture(lock), running


def test_recapture_enables_manual_accessibility_and_polls_until_focused():
    ax, calls = _fake_ax(focused_after_polls=5)
    lock = types.SimpleNamespace(app_pid=123, app_name="Orca")

    ok, running = _run_recapture(ax, lock)

    assert ok is True
    running.activateWithOptions_.assert_called_once()
    assert ("AXManualAccessibility", True) in calls["set"]
    assert calls["focused"] == 6  # 5 empty polls, success on the 6th
    assert lock.focused_was_text_field is True
    assert lock.leaf_role == "AXTextArea"


def test_recapture_gives_up_when_focus_never_appears():
    ax, calls = _fake_ax(focused_after_polls=10_000)
    lock = types.SimpleNamespace(app_pid=123, app_name="Orca")

    ok, _ = _run_recapture(ax, lock)

    assert ok is False
    assert calls["focused"] == 20  # bounded poll (~1s)
    assert not hasattr(lock, "focused_was_text_field")


def test_recapture_rejects_non_text_role():
    ax, _ = _fake_ax(focused_after_polls=0, role="AXButton")
    lock = types.SimpleNamespace(app_pid=123, app_name="Orca")

    ok, _ = _run_recapture(ax, lock)

    assert ok is False


# --- Tier 1b: live focus matched against lock tie-breakers -----------------

def _lock(**kw):
    base = dict(
        app_bundle_id="com.stablyai.orca", app_pid=123, window_number=0,
        ax_role_path=(), leaf_role="AXTextArea", leaf_axid=None,
        leaf_title=None, leaf_description="Send a message…",
        focused_was_text_field=True, app_name="Orca",
    )
    base.update(kw)
    return target.TargetLock(**base)


def _ax_live(role="AXTextArea", desc="Send a message…", title=None, axid=None):
    attrs = {"AXRole": role, "AXDescription": desc, "AXTitle": title,
             "AXIdentifier": axid}

    def copy_attr(elem, attr, _):
        if attr == "AXFocusedUIElement":
            return 0, "focused-elem"
        v = attrs.get(attr)
        return (0, v) if v else (-25212, None)

    return types.SimpleNamespace(
        AXUIElementCreateApplication=lambda pid: "ax-app",
        AXUIElementCopyAttributeValue=copy_attr,
        AXUIElementSetAttributeValue=lambda *a: 0,
    )


def _match(ax, lock):
    with patch.dict(sys.modules, {"ApplicationServices": ax}), \
            patch.object(target._time, "sleep"):
        return target._live_focus_matches_lock(lock)


def test_live_focus_matches_same_field():
    assert _match(_ax_live(), _lock()) is True


def test_live_focus_rejects_other_field_in_same_app():
    assert _match(_ax_live(desc="Find files"), _lock()) is False


def test_live_focus_rejects_other_role():
    assert _match(_ax_live(role="AXTextField"), _lock()) is False


def test_live_focus_requires_a_tiebreaker():
    # Nothing identifies the captured field -> cannot tell fields apart.
    assert _match(_ax_live(), _lock(leaf_description=None)) is False


def test_resolve_lock_uses_live_focus_when_role_path_unreachable():
    cfg = HeyvoxConfig()
    ax = _ax_live()
    appkit = MagicMock()
    with patch.dict(sys.modules, {"AppKit": appkit, "ApplicationServices": ax,
                                  "CoreFoundation": MagicMock()}), \
            patch.object(target._time, "sleep"), \
            patch.object(target, "_yank_back_app_and_workspace"):
        ok = target.resolve_lock(_lock(), config=cfg)
        bad = target.resolve_lock(_lock(leaf_description="Find files"), config=cfg)

    assert ok.ok is True and ok.tier_used == 2
    assert bad.ok is False
    assert bad.reason == target.FailReason.MULTI_FIELD_NO_SHORTCUT
