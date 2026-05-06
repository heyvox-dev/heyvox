"""Unit tests for heyvox.input.target.resolve_lock (Plan 15-05).

Covers:
- Three-tier ladder (role-path -> profile shortcut -> fail-closed)
- Fail-closed reason taxonomy (NO_TEXT_FIELD_AT_START, MULTI_FIELD_NO_SHORTCUT,
  TARGET_UNREACHABLE) + W13 message-format consistency
- Yank-back argv shape (--id + unconditional --session + --force)
- History-unconditional (W5, patches heyvox.history.save — the actual symbol)
- PasteOutcome frozen invariant
- [PASTE] log line emission per tier
"""

from dataclasses import FrozenInstanceError, dataclass
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _make_lock(
    focused_was_text_field=True,
    ax_role_path=(("AXTextArea", 0),),
    app_bundle_id="com.test.app",
    app_pid=1234,
    window_number=42,
    conductor_workspace_id=None,
    conductor_session_id=None,
    app_name="TestApp",
    leaf_role="AXTextArea",
):
    from heyvox.input.target import TargetLock
    return TargetLock(
        app_bundle_id=app_bundle_id,
        app_pid=app_pid,
        window_number=window_number,
        ax_role_path=ax_role_path,
        leaf_role=leaf_role,
        focused_was_text_field=focused_was_text_field,
        conductor_workspace_id=conductor_workspace_id,
        conductor_session_id=conductor_session_id,
        app_name=app_name,
    )


@dataclass
class FakeProfile:
    name: str = "TestApp"
    focus_shortcut: str = ""
    enter_count: int = 1
    settle_delay: float = 0.0  # 0 so tests don't actually sleep
    is_electron: bool = False
    workspace_switch_cmd: str = ""
    workspace_db: str = "/tmp/fake.db"
    has_session_detection: bool = False
    supports_ax_verify: bool = True
    ax_settle_before_verify: float = 0.1


@dataclass
class FakeConfig:
    _p: FakeProfile = None

    def get_app_profile(self, name):
        return self._p


def _make_config(**profile_kwargs):
    return FakeConfig(_p=FakeProfile(**profile_kwargs))


# ---------------------------------------------------------------------------
# Pre-tier short-circuit
# ---------------------------------------------------------------------------


def test_focused_was_text_field_false_fails_closed(monkeypatch):
    from heyvox.input.target import FailReason, resolve_lock

    lock = _make_lock(focused_was_text_field=False, app_name="AcmeApp")
    # Patch yank_back so the test doesn't hit AppKit
    monkeypatch.setattr(
        "heyvox.input.target._yank_back_app_and_workspace",
        lambda lock, profile, config: None,
    )
    outcome = resolve_lock(lock, config=_make_config())
    assert outcome.ok is False
    assert outcome.tier_used == 0
    assert outcome.reason is FailReason.NO_TEXT_FIELD_AT_START
    assert "AcmeApp" in outcome.message


# ---------------------------------------------------------------------------
# W13 — every reason formats with .format(app_name=...)
# ---------------------------------------------------------------------------


def test_all_fail_reason_messages_format_with_app_name():
    from heyvox.input.target import FailReason, _REASON_MESSAGES

    for reason in FailReason:
        formatted = _REASON_MESSAGES[reason].format(app_name="SpecialApp")
        assert (
            "SpecialApp" in formatted
        ), f"{reason.value} failed to interpolate app_name"


# ---------------------------------------------------------------------------
# PasteOutcome invariants
# ---------------------------------------------------------------------------


def test_paste_outcome_is_frozen():
    from heyvox.input.target import PasteOutcome

    out = PasteOutcome(ok=True, tier_used=1)
    with pytest.raises(FrozenInstanceError):
        out.tier_used = 2  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Tier 1 — role-path success
# ---------------------------------------------------------------------------


def test_tier1_succeeds_when_role_path_walks_cleanly(monkeypatch, capsys):
    from heyvox.input.target import resolve_lock

    monkeypatch.setattr(
        "heyvox.input.target._yank_back_app_and_workspace",
        lambda lock, profile, config: None,
    )
    fake_window = object()
    fake_leaf = object()
    monkeypatch.setattr(
        "heyvox.input.target._find_window_by_number",
        lambda ax_app, wn: fake_window,
    )
    monkeypatch.setattr(
        "heyvox.input.target._walk_role_path",
        lambda window, path: fake_leaf,
    )

    mock_ax = MagicMock()
    mock_ax.AXUIElementCreateApplication.return_value = MagicMock()
    mock_ax.AXUIElementSetAttributeValue.return_value = 0
    mock_cf = MagicMock()
    mock_cf.kCFBooleanTrue = True

    lock = _make_lock()
    with patch.dict("sys.modules", {
        "ApplicationServices": mock_ax, "CoreFoundation": mock_cf,
    }):
        outcome = resolve_lock(lock, config=_make_config())

    assert outcome.ok is True
    assert outcome.tier_used == 1
    assert outcome.element is fake_leaf
    captured = capsys.readouterr()
    assert "[PASTE] tier_used=1" in captured.err


# ---------------------------------------------------------------------------
# Tier 2 — profile shortcut when tier 1 fails
# ---------------------------------------------------------------------------


def test_tier2_fires_with_focus_shortcut(monkeypatch, capsys):
    """DEF-089: Tier 2 with focus_shortcut returns ok=True without firing
    any osascript itself. The actual focus+paste+Enter osascript is fired
    by `app_fast_paste` — the caller — once. Firing it here too caused
    duplicate Cmd+L races and ~1.5–2.5 s of extra latency per paste.
    """
    from heyvox.input.target import resolve_lock

    monkeypatch.setattr(
        "heyvox.input.target._yank_back_app_and_workspace",
        lambda lock, profile, config: None,
    )
    # Tier 1 returns None
    monkeypatch.setattr(
        "heyvox.input.target._find_window_by_number",
        lambda ax_app, wn: None,
    )

    captured_argv = []

    def _fake_run(argv, *args, **kwargs):
        captured_argv.append(argv)
        return MagicMock(returncode=0, stderr=b"")

    monkeypatch.setattr("heyvox.input.target.subprocess.run", _fake_run)
    monkeypatch.setattr(
        "heyvox.input.injection._get_frontmost_app", lambda: "TestApp"
    )

    mock_ax = MagicMock()
    mock_ax.AXUIElementCreateApplication.return_value = MagicMock()
    mock_cf = MagicMock()
    mock_cf.kCFBooleanTrue = True

    lock = _make_lock()
    config = _make_config(focus_shortcut="l")
    with patch.dict("sys.modules", {
        "ApplicationServices": mock_ax, "CoreFoundation": mock_cf,
    }):
        outcome = resolve_lock(lock, config=config)

    assert outcome.ok is True
    assert outcome.tier_used == 2
    # DEF-089: resolve_lock no longer fires osascript in Tier 2.
    # _get_frontmost_app may still be called by other paths, but
    # there must be no osascript invocation owned by this function.
    osa = [a for a in captured_argv if a and a[0] == "osascript"]
    assert osa == [], (
        f"Tier 2 should defer keystrokes to app_fast_paste; "
        f"got osascript calls: {osa}"
    )
    captured = capsys.readouterr()
    assert "[PASTE] tier_used=2" in captured.err
    assert "deferred to app_fast_paste" in captured.err


# ---------------------------------------------------------------------------
# Tier 3 — fail-closed paths
# ---------------------------------------------------------------------------


def test_tier1_fail_with_no_profile_returns_target_unreachable(monkeypatch, capsys):
    """DEF-089: with the keystroke removed from Tier 2, the only path to
    TARGET_UNREACHABLE through Tier 3 is "Tier 1 walk failed AND no profile
    provided" (no Tier 2 to defer to). Replaces the obsolete test that
    forced the Tier 2 osascript to return rc=1 — that path no longer exists.
    """
    from heyvox.input.target import FailReason, resolve_lock

    monkeypatch.setattr(
        "heyvox.input.target._yank_back_app_and_workspace",
        lambda lock, profile, config: None,
    )
    monkeypatch.setattr(
        "heyvox.input.target._find_window_by_number",
        lambda ax_app, wn: None,
    )

    mock_ax = MagicMock()
    mock_ax.AXUIElementCreateApplication.return_value = MagicMock()
    mock_cf = MagicMock()
    mock_cf.kCFBooleanTrue = True

    lock = _make_lock(app_name="TargetApp")
    # No config -> profile lookup returns None -> Tier 2 skipped, Tier 3
    # fail-closed routes through TARGET_UNREACHABLE.
    with patch.dict("sys.modules", {
        "ApplicationServices": mock_ax, "CoreFoundation": mock_cf,
    }):
        outcome = resolve_lock(lock, config=None)

    assert outcome.ok is False
    assert outcome.tier_used == 0
    assert outcome.reason is FailReason.TARGET_UNREACHABLE
    assert "TargetApp" in outcome.message
    captured = capsys.readouterr()
    assert "tier_used=fail_closed" in captured.err
    assert "target_unreachable" in captured.err


def test_no_focus_shortcut_gives_multi_field_no_shortcut(monkeypatch):
    from heyvox.input.target import FailReason, resolve_lock

    monkeypatch.setattr(
        "heyvox.input.target._yank_back_app_and_workspace",
        lambda lock, profile, config: None,
    )
    monkeypatch.setattr(
        "heyvox.input.target._find_window_by_number",
        lambda ax_app, wn: None,
    )

    mock_ax = MagicMock()
    mock_ax.AXUIElementCreateApplication.return_value = MagicMock()
    mock_cf = MagicMock()
    mock_cf.kCFBooleanTrue = True

    lock = _make_lock(app_name="NoShortcutApp")
    config = _make_config(focus_shortcut="")  # profile has NO shortcut
    with patch.dict("sys.modules", {
        "ApplicationServices": mock_ax, "CoreFoundation": mock_cf,
    }):
        outcome = resolve_lock(lock, config=config)

    assert outcome.ok is False
    assert outcome.reason is FailReason.MULTI_FIELD_NO_SHORTCUT
    assert "NoShortcutApp" in outcome.message
    assert "multiple inputs" in outcome.message


# ---------------------------------------------------------------------------
# Yank-back: conductor-switch-workspace invocation shape (B4)
# ---------------------------------------------------------------------------


def test_yank_back_uses_id_flag(monkeypatch):
    from heyvox.input.target import _yank_back_app_and_workspace

    captured_argv = []

    def _fake_run(argv, *a, **kw):
        captured_argv.append(argv)
        return MagicMock(returncode=0)

    monkeypatch.setattr("heyvox.input.target.subprocess.run", _fake_run)

    mock_appkit = MagicMock()
    mock_appkit.NSRunningApplication.runningApplicationsWithBundleIdentifier_.return_value = []

    lock = _make_lock(
        conductor_workspace_id="ws-uuid-1", conductor_session_id=None
    )
    profile = FakeProfile(workspace_switch_cmd="/usr/local/bin/test-switch")
    with patch.dict("sys.modules", {"AppKit": mock_appkit}):
        _yank_back_app_and_workspace(lock, profile, config=None)

    switch_calls = [
        a for a in captured_argv if a and a[0] == "/usr/local/bin/test-switch"
    ]
    assert len(switch_calls) == 1
    argv = switch_calls[0]
    assert "--id" in argv
    assert "ws-uuid-1" in argv
    assert "--force" in argv
    assert "--session" not in argv


def test_session_id_triggers_session_flag(monkeypatch):
    """B4 + iter-3 W-fix: resolver appends --session unconditionally when
    lock.conductor_session_id is set. Asserts argv (runtime) not source text."""
    from heyvox.input.target import _yank_back_app_and_workspace

    captured_argv = []

    def _fake_run(argv, *args, **kwargs):
        captured_argv.append(argv)
        return MagicMock(returncode=0, stderr=b"")

    monkeypatch.setattr("heyvox.input.target.subprocess.run", _fake_run)
    mock_appkit = MagicMock()
    mock_appkit.NSRunningApplication.runningApplicationsWithBundleIdentifier_.return_value = []

    lock = _make_lock(
        conductor_workspace_id="ws-uuid-1",
        conductor_session_id="sess-uuid-7",
    )
    profile = FakeProfile(workspace_switch_cmd="/usr/local/bin/test-switch")
    with patch.dict("sys.modules", {"AppKit": mock_appkit}):
        _yank_back_app_and_workspace(lock, profile, config=None)

    switch_calls = [
        a for a in captured_argv if a and a[0] == "/usr/local/bin/test-switch"
    ]
    assert len(switch_calls) == 1
    argv = switch_calls[0]
    assert "--id" in argv and "ws-uuid-1" in argv
    assert "--session" in argv
    session_idx = argv.index("--session")
    assert (
        argv[session_idx + 1] == "sess-uuid-7"
    ), f"--session UUID mismatch: argv[{session_idx+1}] = {argv[session_idx+1]!r}"


def test_no_session_id_omits_session_flag(monkeypatch):
    """When lock.conductor_session_id is None, --session is NOT appended."""
    from heyvox.input.target import _yank_back_app_and_workspace

    captured_argv = []
    monkeypatch.setattr(
        "heyvox.input.target.subprocess.run",
        lambda argv, *a, **kw: captured_argv.append(argv) or MagicMock(returncode=0),
    )
    mock_appkit = MagicMock()
    mock_appkit.NSRunningApplication.runningApplicationsWithBundleIdentifier_.return_value = []

    lock = _make_lock(
        conductor_workspace_id="ws-uuid-1", conductor_session_id=None
    )
    profile = FakeProfile(workspace_switch_cmd="/usr/local/bin/test-switch")
    with patch.dict("sys.modules", {"AppKit": mock_appkit}):
        _yank_back_app_and_workspace(lock, profile, config=None)

    switch_calls = [
        a for a in captured_argv if a and a[0] == "/usr/local/bin/test-switch"
    ]
    assert len(switch_calls) == 1
    assert "--session" not in switch_calls[0]


def test_yank_back_noop_when_no_workspace_id(monkeypatch):
    from heyvox.input.target import _yank_back_app_and_workspace

    captured_argv = []
    monkeypatch.setattr(
        "heyvox.input.target.subprocess.run",
        lambda argv, *a, **kw: captured_argv.append(argv)
        or MagicMock(returncode=0),
    )
    mock_appkit = MagicMock()
    mock_appkit.NSRunningApplication.runningApplicationsWithBundleIdentifier_.return_value = []

    lock = _make_lock(conductor_workspace_id=None)
    profile = FakeProfile(workspace_switch_cmd="/usr/local/bin/test-switch")
    with patch.dict("sys.modules", {"AppKit": mock_appkit}):
        _yank_back_app_and_workspace(lock, profile, config=None)

    switch_calls = [
        a for a in captured_argv if a and a[0] == "/usr/local/bin/test-switch"
    ]
    assert switch_calls == []


# ---------------------------------------------------------------------------
# W5 — history unconditional (patches heyvox.history.save, the real symbol)
# ---------------------------------------------------------------------------


def test_heyvox_history_save_is_the_patch_target():
    """W5: guards against re-introducing the nonexistent _add_history method
    as a patch target. The only history-write site is `heyvox.history.save`
    (Fact 2); tests MUST patch that path."""
    import heyvox.history as history_mod

    assert hasattr(
        history_mod, "save"
    ), "heyvox.history.save must exist (W5 patch target)"
    import heyvox.recording as rec_mod

    # `rsm._add_history` does NOT exist on RecordingStateMachine — patches
    # targeting it would silently AttributeError.
    assert not hasattr(
        rec_mod.RecordingStateMachine, "_add_history"
    ), "RecordingStateMachine._add_history must NOT exist (W5 guard)"
