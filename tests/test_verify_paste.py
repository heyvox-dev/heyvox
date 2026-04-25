"""Unit tests for heyvox.input.target.verify_paste (Plan 15-06).

Covers:
- _normalize_text whitespace/case behaviour
- First-try content match
- Drift + retry path (persistent miss)
- Retry-success path (second try lands)
- Non-AX focus-unchanged path (Terminal/iTerm2)
- Non-AX focus-moved path
- W3: Tier-2 element-None triggers _acquire_focused_element + full AX
- W3 graceful degradation: acquire-fail → focus-unchanged fallback
- W7: profile=None emits explicit log line
- W11: retry path calls _set_clipboard with transcript before Cmd+V
- VerifyResult frozen invariant
"""

from dataclasses import FrozenInstanceError, dataclass
from unittest.mock import MagicMock

import pytest


def _make_lock(
    app_name="TestApp",
    app_bundle_id="com.test.app",
    app_pid=1234,
    window_number=42,
    focused_was_text_field=True,
):
    from heyvox.input.target import TargetLock

    return TargetLock(
        app_bundle_id=app_bundle_id,
        app_pid=app_pid,
        window_number=window_number,
        ax_role_path=(),
        leaf_role="AXTextArea",
        focused_was_text_field=focused_was_text_field,
        app_name=app_name,
    )


@dataclass
class FakeProfile:
    name: str = "TestApp"
    focus_shortcut: str = "l"
    enter_count: int = 1
    settle_delay: float = 0.0
    is_electron: bool = False
    workspace_switch_cmd: str = ""
    workspace_db: str = ""
    has_session_detection: bool = False
    supports_ax_verify: bool = True
    ax_settle_before_verify: float = 0.0


def _make_profile(**kwargs):
    return FakeProfile(**kwargs)


# ---------------------------------------------------------------------------
# _normalize_text
# ---------------------------------------------------------------------------


def test_normalize_collapses_whitespace():
    from heyvox.input.target import _normalize_text

    assert _normalize_text("  hello\n\n  world  ") == "hello world"


def test_normalize_preserves_case():
    from heyvox.input.target import _normalize_text

    assert _normalize_text("Hello World") == "Hello World"


def test_normalize_handles_none():
    from heyvox.input.target import _normalize_text

    assert _normalize_text(None) == ""


# ---------------------------------------------------------------------------
# VerifyResult frozen
# ---------------------------------------------------------------------------


def test_verify_result_is_frozen():
    from heyvox.input.target import VerifyResult

    r = VerifyResult(verified=True, retried=False, drift=False)
    with pytest.raises(FrozenInstanceError):
        r.verified = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# AX path — first try match
# ---------------------------------------------------------------------------


def test_verify_succeeds_first_try_when_ax_value_contains_transcript(monkeypatch):
    from heyvox.input.target import verify_paste

    fake_element = object()
    monkeypatch.setattr(
        "heyvox.input.target._read_ax_value",
        lambda elem: "User said: hello world. Then continued.",
    )
    monkeypatch.setattr("heyvox.input.target._time.sleep", lambda s: None)

    lock = _make_lock()
    profile = _make_profile(supports_ax_verify=True)
    result = verify_paste(lock, fake_element, "hello world", profile)

    assert result.verified is True
    assert result.retried is False
    assert result.drift is False


# ---------------------------------------------------------------------------
# AX path — drift + retry (W11 clipboard re-set)
# ---------------------------------------------------------------------------


def test_verify_drift_after_retry_resets_clipboard(monkeypatch):
    """W11: retry path MUST re-set clipboard from transcript before Cmd+V."""
    from heyvox.input.target import verify_paste

    clipboard_sets = []
    monkeypatch.setattr(
        "heyvox.input.target._read_ax_value", lambda elem: ""
    )
    monkeypatch.setattr("heyvox.input.target._time.sleep", lambda s: None)

    def _fake_set_clipboard(text):
        clipboard_sets.append(text)
        return (True, 1)

    def _fake_get_frontmost():
        return "TestApp"

    # Patch on the source modules (re-imported inside verify_paste's retry branch)
    import heyvox.input.injection as injection_mod
    monkeypatch.setattr(injection_mod, "_set_clipboard", _fake_set_clipboard)
    monkeypatch.setattr(injection_mod, "_get_frontmost_app", _fake_get_frontmost)
    monkeypatch.setattr(
        "heyvox.input.target.subprocess.run",
        lambda *a, **kw: MagicMock(returncode=0),
    )

    lock = _make_lock()
    profile = _make_profile(supports_ax_verify=True)
    result = verify_paste(lock, object(), "hello", profile)

    assert result.verified is False
    assert result.retried is True
    assert result.drift is True
    assert clipboard_sets == ["hello"], (
        f"W11: clipboard should be re-set with transcript before retry; "
        f"got {clipboard_sets!r}"
    )


def test_verify_succeeds_on_retry(monkeypatch):
    """First read returns empty, second read contains transcript."""
    from heyvox.input.target import verify_paste

    reads = {"count": 0}

    def _fake_read(elem):
        reads["count"] += 1
        return "" if reads["count"] == 1 else "now: hello world"

    monkeypatch.setattr("heyvox.input.target._read_ax_value", _fake_read)
    monkeypatch.setattr("heyvox.input.target._time.sleep", lambda s: None)

    import heyvox.input.injection as injection_mod
    monkeypatch.setattr(
        injection_mod, "_set_clipboard", lambda t: (True, 1)
    )
    monkeypatch.setattr(
        injection_mod, "_get_frontmost_app", lambda: "TestApp"
    )
    monkeypatch.setattr(
        "heyvox.input.target.subprocess.run",
        lambda *a, **kw: MagicMock(returncode=0),
    )

    lock = _make_lock()
    profile = _make_profile(supports_ax_verify=True)
    result = verify_paste(lock, object(), "hello world", profile)

    assert result.verified is True
    assert result.retried is True
    assert result.drift is False


# ---------------------------------------------------------------------------
# Non-AX path
# ---------------------------------------------------------------------------


def test_non_ax_profile_focus_unchanged_verifies(monkeypatch):
    from heyvox.input.target import verify_paste

    monkeypatch.setattr(
        "heyvox.input.target._focus_unchanged", lambda lock: True
    )
    monkeypatch.setattr("heyvox.input.target._time.sleep", lambda s: None)

    lock = _make_lock(app_bundle_id="com.apple.Terminal")
    profile = _make_profile(supports_ax_verify=False, name="Terminal")
    result = verify_paste(lock, None, "hello", profile)

    assert result.verified is True
    assert result.retried is False
    assert result.drift is False
    assert result.detail == "focus-unchanged"


def test_non_ax_profile_focus_moved_drift(monkeypatch):
    from heyvox.input.target import verify_paste

    monkeypatch.setattr(
        "heyvox.input.target._focus_unchanged", lambda lock: False
    )
    monkeypatch.setattr("heyvox.input.target._time.sleep", lambda s: None)

    lock = _make_lock()
    profile = _make_profile(supports_ax_verify=False, name="Terminal")
    result = verify_paste(lock, None, "hello", profile)

    assert result.verified is False
    assert result.drift is True
    assert result.detail == "focus-moved"


# ---------------------------------------------------------------------------
# W3 — Tier-2 element=None re-acquire
# ---------------------------------------------------------------------------


def test_tier2_acquires_focused_element_when_element_none(monkeypatch):
    """W3: Tier 2 returns element=None; verify_paste re-acquires via AXFocusedUIElement."""
    from heyvox.input.target import verify_paste

    fake_acquired = object()
    monkeypatch.setattr(
        "heyvox.input.target._acquire_focused_element",
        lambda lock: fake_acquired,
    )
    monkeypatch.setattr(
        "heyvox.input.target._read_ax_value",
        lambda elem: "hello world" if elem is fake_acquired else None,
    )
    monkeypatch.setattr("heyvox.input.target._time.sleep", lambda s: None)

    lock = _make_lock()
    profile = _make_profile(supports_ax_verify=True)
    result = verify_paste(lock, None, "hello world", profile)

    assert result.verified is True
    assert result.retried is False


def test_tier2_acquire_fail_falls_back_to_focus_unchanged(monkeypatch):
    """W3 graceful-degradation: _acquire returns None → focus-unchanged fallback."""
    from heyvox.input.target import verify_paste

    monkeypatch.setattr(
        "heyvox.input.target._acquire_focused_element", lambda lock: None
    )
    monkeypatch.setattr(
        "heyvox.input.target._focus_unchanged", lambda lock: True
    )
    monkeypatch.setattr("heyvox.input.target._time.sleep", lambda s: None)

    lock = _make_lock()
    profile = _make_profile(supports_ax_verify=True)
    result = verify_paste(lock, None, "hello", profile)

    assert result.verified is True
    assert result.detail == "tier2-acquire-fail-focus-unchanged"


def test_tier2_acquire_fail_focus_moved_drift(monkeypatch):
    from heyvox.input.target import verify_paste

    monkeypatch.setattr(
        "heyvox.input.target._acquire_focused_element", lambda lock: None
    )
    monkeypatch.setattr(
        "heyvox.input.target._focus_unchanged", lambda lock: False
    )
    monkeypatch.setattr("heyvox.input.target._time.sleep", lambda s: None)

    lock = _make_lock()
    profile = _make_profile(supports_ax_verify=True)
    result = verify_paste(lock, None, "hello", profile)

    assert result.verified is False
    assert result.drift is True
    assert result.detail == "tier2-acquire-fail-focus-moved"


# ---------------------------------------------------------------------------
# W7 — profile=None explicit log
# ---------------------------------------------------------------------------


def test_verify_with_profile_none_emits_explicit_log(monkeypatch, capsys):
    from heyvox.input.target import verify_paste

    monkeypatch.setattr(
        "heyvox.input.target._acquire_focused_element", lambda lock: None
    )
    monkeypatch.setattr(
        "heyvox.input.target._focus_unchanged", lambda lock: True
    )
    monkeypatch.setattr("heyvox.input.target._time.sleep", lambda s: None)

    lock = _make_lock(app_name="MyApp")
    verify_paste(lock, None, "hello", profile=None)

    captured = capsys.readouterr()
    assert "profile=None" in captured.err, (
        f"W7: expected explicit profile=None log; got {captured.err!r}"
    )


# ---------------------------------------------------------------------------
# Log line variants (all four outcomes emit [PASTE] verified=...)
# ---------------------------------------------------------------------------


def test_verified_true_first_try_log(monkeypatch, capsys):
    from heyvox.input.target import verify_paste

    monkeypatch.setattr(
        "heyvox.input.target._read_ax_value", lambda elem: "hello world"
    )
    monkeypatch.setattr("heyvox.input.target._time.sleep", lambda s: None)

    verify_paste(_make_lock(), object(), "hello", _make_profile())
    out = capsys.readouterr().err
    assert "[PASTE] verified=true retried=false drift=false" in out


def test_drift_log_format(monkeypatch, capsys):
    from heyvox.input.target import verify_paste

    monkeypatch.setattr(
        "heyvox.input.target._read_ax_value", lambda elem: ""
    )
    monkeypatch.setattr("heyvox.input.target._time.sleep", lambda s: None)
    import heyvox.input.injection as injection_mod
    monkeypatch.setattr(
        injection_mod, "_set_clipboard", lambda t: (True, 1)
    )
    monkeypatch.setattr(
        injection_mod, "_get_frontmost_app", lambda: "TestApp"
    )
    monkeypatch.setattr(
        "heyvox.input.target.subprocess.run",
        lambda *a, **kw: MagicMock(returncode=0),
    )

    verify_paste(_make_lock(), object(), "hello", _make_profile())
    out = capsys.readouterr().err
    assert "[PASTE] verified=false retried=true drift=true" in out


# ---------------------------------------------------------------------------
# W12 — defensive guard (belongs to recording.py, tested via import check)
# ---------------------------------------------------------------------------


def test_recording_send_local_has_defensive_outcome_guard():
    """W12: recording._send_local must gate verify_paste on
    `outcome is not None` AND `outcome.ok` to avoid AttributeError when
    recording_target was None upstream. DEF-090 split the guard across
    multiple lines (and added the auto-Enter skip), so we check the
    two clauses appear in the same conditional block instead of one
    contiguous substring."""
    from pathlib import Path
    import re

    src = Path("heyvox/recording.py").read_text()
    # Find any conditional/expression block that mentions both clauses
    # within ~12 lines of each other — robust against line-formatting
    # changes while still proving the defensive gate is present.
    block_pattern = re.compile(
        r"outcome is not None[^\n]*(?:\n[^\n]*){0,12}outcome\.ok",
        re.MULTILINE,
    )
    assert block_pattern.search(src), (
        "W12: expected defensive gate (`outcome is not None` AND "
        "`outcome.ok`) somewhere in _send_local"
    )
