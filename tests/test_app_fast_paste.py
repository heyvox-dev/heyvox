"""Unit tests for heyvox.input.injection.app_fast_paste (Plan 15-03).

Covers profile-driven keystroke building, live-frontmost-name usage (DEF-027
preservation), clipboard-write failures, osascript rc handling.

Uses a FakeProfile dataclass instead of Pydantic AppProfileConfig to keep the
unit tests fast and free of Pydantic validation noise.
"""

from dataclasses import dataclass
from unittest.mock import MagicMock, patch



@dataclass
class FakeProfile:
    name: str = "TestApp"
    focus_shortcut: str = ""
    enter_count: int = 1
    settle_delay: float = 0.3
    is_electron: bool = True


def _script_arg(mock_run) -> str:
    """Return the AppleScript string passed to `osascript -e <script>`."""
    call = mock_run.call_args
    # call.args[0] is the argv list: ['osascript', '-e', '<script>']
    return call.args[0][2]


# ---------------------------------------------------------------------------
# Success path — keystroke order + profile-driven content
# ---------------------------------------------------------------------------


def test_profile_with_focus_shortcut_builds_correct_order():
    from heyvox.input.injection import app_fast_paste

    profile = FakeProfile(
        name="TestApp", focus_shortcut="l", enter_count=1, settle_delay=0.3
    )

    with patch("heyvox.input.injection._set_clipboard", return_value=(True, 5)), \
         patch("heyvox.input.injection.get_clipboard_text", return_value="hello"), \
         patch("heyvox.input.injection._get_frontmost_app", return_value="testapp"), \
         patch("heyvox.input.injection.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr=b"")
        ok = app_fast_paste(profile, "hello")

    assert ok is True
    script = _script_arg(mock_run)
    assert 'keystroke "l" using command down' in script
    assert 'keystroke "v" using command down' in script
    assert "keystroke return" in script
    # Cmd+focus before Cmd+V before return
    assert script.index('keystroke "l"') < script.index('keystroke "v"')
    assert script.index('keystroke "v"') < script.index("keystroke return")


def test_profile_without_focus_shortcut_skips_focus_key():
    from heyvox.input.injection import app_fast_paste

    profile = FakeProfile(
        name="Terminal", focus_shortcut="", enter_count=1, settle_delay=0.1
    )

    with patch("heyvox.input.injection._set_clipboard", return_value=(True, 5)), \
         patch("heyvox.input.injection.get_clipboard_text", return_value="hi"), \
         patch("heyvox.input.injection._get_frontmost_app", return_value="Terminal"), \
         patch("heyvox.input.injection.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr=b"")
        ok = app_fast_paste(profile, "hi")

    assert ok is True
    script = _script_arg(mock_run)
    assert 'using command down' in script  # Cmd+V still present
    assert 'keystroke "v"' in script
    # No keystroke uses command down except Cmd+V
    assert script.count("using command down") == 1


def test_enter_count_two_emits_two_returns():
    from heyvox.input.injection import app_fast_paste

    profile = FakeProfile(
        name="TestApp", focus_shortcut="l", enter_count=2, settle_delay=0.3
    )

    with patch("heyvox.input.injection._set_clipboard", return_value=(True, 5)), \
         patch("heyvox.input.injection.get_clipboard_text", return_value="hi"), \
         patch("heyvox.input.injection._get_frontmost_app", return_value="testapp"), \
         patch("heyvox.input.injection.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr=b"")
        app_fast_paste(profile, "hi")

    script = _script_arg(mock_run)
    assert script.count("keystroke return") == 2


def test_enter_count_zero_emits_no_return():
    from heyvox.input.injection import app_fast_paste

    profile = FakeProfile(
        name="TestApp", focus_shortcut="", enter_count=0, settle_delay=0.1
    )

    with patch("heyvox.input.injection._set_clipboard", return_value=(True, 5)), \
         patch("heyvox.input.injection.get_clipboard_text", return_value="hi"), \
         patch("heyvox.input.injection._get_frontmost_app", return_value="Terminal"), \
         patch("heyvox.input.injection.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr=b"")
        app_fast_paste(profile, "hi")

    script = _script_arg(mock_run)
    assert "keystroke return" not in script


def test_settle_delay_read_from_profile():
    from heyvox.input.injection import app_fast_paste

    profile = FakeProfile(
        name="Conductor", focus_shortcut="l", enter_count=1, settle_delay=0.42
    )

    with patch("heyvox.input.injection._set_clipboard", return_value=(True, 5)), \
         patch("heyvox.input.injection.get_clipboard_text", return_value="hi"), \
         patch("heyvox.input.injection._get_frontmost_app", return_value="conductor"), \
         patch("heyvox.input.injection.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr=b"")
        app_fast_paste(profile, "hi")

    script = _script_arg(mock_run)
    assert "delay 0.42" in script


# ---------------------------------------------------------------------------
# Live frontmost name — preserves DEF-027 lowercase fix
# ---------------------------------------------------------------------------


def test_tell_process_uses_live_frontmost_name_lowercase():
    from heyvox.input.injection import app_fast_paste

    profile = FakeProfile(name="Conductor", focus_shortcut="l", enter_count=1)

    with patch("heyvox.input.injection._set_clipboard", return_value=(True, 5)), \
         patch("heyvox.input.injection.get_clipboard_text", return_value="hi"), \
         patch("heyvox.input.injection._get_frontmost_app", return_value="conductor"), \
         patch("heyvox.input.injection.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr=b"")
        app_fast_paste(profile, "hi")

    script = _script_arg(mock_run)
    # tell process uses LOWERCASE process name from live frontmost, not profile.name
    assert 'tell process "conductor"' in script
    assert 'tell process "Conductor"' not in script


def test_tell_process_uses_live_frontmost_for_different_app():
    from heyvox.input.injection import app_fast_paste

    profile = FakeProfile(name="Cursor", focus_shortcut="l", enter_count=1)

    with patch("heyvox.input.injection._set_clipboard", return_value=(True, 5)), \
         patch("heyvox.input.injection.get_clipboard_text", return_value="hi"), \
         patch("heyvox.input.injection._get_frontmost_app", return_value="Cursor"), \
         patch("heyvox.input.injection.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr=b"")
        app_fast_paste(profile, "hi")

    script = _script_arg(mock_run)
    assert 'tell process "Cursor"' in script


def test_frontmost_unknown_falls_back_to_profile_name():
    from heyvox.input.injection import app_fast_paste

    profile = FakeProfile(name="TestApp", focus_shortcut="l", enter_count=1)

    with patch("heyvox.input.injection._set_clipboard", return_value=(True, 5)), \
         patch("heyvox.input.injection.get_clipboard_text", return_value="hi"), \
         patch("heyvox.input.injection._get_frontmost_app", return_value="?"), \
         patch("heyvox.input.injection.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr=b"")
        app_fast_paste(profile, "hi")

    script = _script_arg(mock_run)
    assert 'tell process "TestApp"' in script


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_clipboard_write_failure_returns_false():
    from heyvox.input.injection import app_fast_paste

    profile = FakeProfile(name="TestApp", focus_shortcut="l", enter_count=1)

    with patch("heyvox.input.injection._set_clipboard", return_value=(False, 0)), \
         patch("heyvox.input.injection.audio_cue") as mock_cue, \
         patch("heyvox.input.injection.subprocess.run") as mock_run:
        ok = app_fast_paste(profile, "hi")

    assert ok is False
    mock_cue.assert_called_with("error")
    mock_run.assert_not_called()


def test_clipboard_verify_mismatch_returns_false():
    from heyvox.input.injection import app_fast_paste

    profile = FakeProfile(name="TestApp", focus_shortcut="l", enter_count=1)

    with patch("heyvox.input.injection._set_clipboard", return_value=(True, 5)), \
         patch("heyvox.input.injection.get_clipboard_text", return_value="wrongtext"), \
         patch("heyvox.input.injection.audio_cue") as mock_cue, \
         patch("heyvox.input.injection.subprocess.run") as mock_run:
        ok = app_fast_paste(profile, "hello")

    assert ok is False
    mock_cue.assert_called_with("error")
    mock_run.assert_not_called()


def test_osascript_nonzero_returns_false():
    from heyvox.input.injection import app_fast_paste

    profile = FakeProfile(name="TestApp", focus_shortcut="l", enter_count=1)

    with patch("heyvox.input.injection._set_clipboard", return_value=(True, 5)), \
         patch("heyvox.input.injection.get_clipboard_text", return_value="hi"), \
         patch("heyvox.input.injection._get_frontmost_app", return_value="testapp"), \
         patch("heyvox.input.injection.audio_cue") as mock_cue, \
         patch("heyvox.input.injection.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stderr=b"not authorized")
        ok = app_fast_paste(profile, "hi")

    assert ok is False
    mock_cue.assert_called_with("error")


# ---------------------------------------------------------------------------
# Generality — works for any app profile, no app-specific branching
# ---------------------------------------------------------------------------


def test_fictional_app_with_cmd_k_works_identically():
    """Proves app_fast_paste is fully profile-driven with no app-specific
    branches. A fictional app with focus_shortcut='k' builds the same shape
    of script as Conductor with focus_shortcut='l'."""
    from heyvox.input.injection import app_fast_paste

    profile = FakeProfile(
        name="FictionalApp", focus_shortcut="k", enter_count=1, settle_delay=0.2
    )

    with patch("heyvox.input.injection._set_clipboard", return_value=(True, 5)), \
         patch("heyvox.input.injection.get_clipboard_text", return_value="hi"), \
         patch("heyvox.input.injection._get_frontmost_app", return_value="FictionalApp"), \
         patch("heyvox.input.injection.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr=b"")
        app_fast_paste(profile, "hi")

    script = _script_arg(mock_run)
    assert 'keystroke "k" using command down' in script
    assert 'keystroke "v" using command down' in script
    assert "delay 0.2" in script
    assert 'tell process "FictionalApp"' in script


# ---------------------------------------------------------------------------
# Special characters in text don't crash (clipboard path, not script path)
# ---------------------------------------------------------------------------


def test_special_chars_in_text_dont_crash():
    from heyvox.input.injection import app_fast_paste

    profile = FakeProfile(name="TestApp", focus_shortcut="l", enter_count=1)
    weird = 'hello "world"\nwith\\backslashes and $dollar'

    with patch("heyvox.input.injection._set_clipboard", return_value=(True, 5)), \
         patch("heyvox.input.injection.get_clipboard_text", return_value=weird), \
         patch("heyvox.input.injection._get_frontmost_app", return_value="testapp"), \
         patch("heyvox.input.injection.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr=b"")
        ok = app_fast_paste(profile, weird)

    assert ok is True
