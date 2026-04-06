"""Tests for heyvox.input.injection — text injection via clipboard + osascript."""

from unittest.mock import patch, MagicMock

from heyvox.input.injection import (
    type_text, press_enter, focus_app, focus_input,
    clipboard_is_image, get_clipboard_text,
)


class TestTypeText:
    """type_text() — clipboard-based text injection."""

    @patch("heyvox.input.injection.subprocess.run")
    @patch("heyvox.input.injection.time.sleep")
    @patch("heyvox.input.injection.get_clipboard_text", return_value="hello")
    def test_basic_paste(self, mock_clip, mock_sleep, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        type_text("hello")
        # pbcopy (set) + osascript (Cmd-V) = 2 subprocess calls
        assert mock_run.call_count == 2

    @patch("heyvox.input.injection.subprocess.run")
    @patch("heyvox.input.injection.time.sleep")
    @patch("heyvox.input.injection.get_clipboard_text", return_value="hello")
    def test_no_clipboard_restore(self, mock_clip, mock_sleep, mock_run):
        """Clipboard is NOT restored after paste — prevents Electron race condition."""
        mock_run.return_value = MagicMock(returncode=0)
        type_text("hello")
        # Only 2 calls: pbcopy + Cmd-V. No third call to restore.
        assert mock_run.call_count == 2

    @patch("heyvox.input.injection.subprocess.run")
    @patch("heyvox.input.injection.time.sleep")
    @patch("heyvox.input.injection.get_clipboard_text", return_value="wrong")
    def test_aborts_on_verify_mismatch(self, mock_clip, mock_sleep, mock_run):
        """If clipboard doesn't match after pbcopy, abort without pasting."""
        mock_run.return_value = MagicMock(returncode=0)
        type_text("hello")
        # Only pbcopy call — Cmd-V should NOT be sent
        assert mock_run.call_count == 1

    @patch("heyvox.input.injection.subprocess.run")
    @patch("heyvox.input.injection.time.sleep")
    @patch("heyvox.input.injection.get_clipboard_text", return_value="hello")
    def test_aborts_on_pbcopy_failure(self, mock_clip, mock_sleep, mock_run):
        """If pbcopy fails (non-zero exit), abort without pasting."""
        mock_run.return_value = MagicMock(returncode=1)
        type_text("hello")
        # pbcopy called once but failed — no Cmd-V
        assert mock_run.call_count == 1

    @patch("heyvox.input.injection.subprocess.run")
    @patch("heyvox.input.injection.time.sleep")
    @patch("heyvox.input.injection.get_clipboard_text", return_value='say "hello"')
    def test_handles_special_chars(self, mock_clip, mock_sleep, mock_run):
        """pbcopy handles quotes/unicode via stdin — no escaping needed."""
        mock_run.return_value = MagicMock(returncode=0)
        type_text('say "hello"')
        # pbcopy receives raw bytes via stdin
        pbcopy_call = mock_run.call_args_list[0]
        assert pbcopy_call[1].get("input") == b'say "hello"'


class TestPressEnter:
    """press_enter() — keystroke simulation."""

    @patch("heyvox.input.injection.subprocess.run")
    def test_single_enter(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        press_enter(1)
        mock_run.assert_called_once()

    @patch("heyvox.input.injection.subprocess.run")
    def test_multiple_enter(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        press_enter(3)
        mock_run.assert_called_once()
        osascript_arg = mock_run.call_args[0][0][2]
        assert osascript_arg.count("keystroke return") == 3


class TestFocusApp:
    """focus_app() — application activation."""

    @patch("heyvox.input.injection.subprocess.run")
    def test_activates_app(self, mock_run):
        focus_app("Cursor")
        mock_run.assert_called_once()
        osascript_arg = mock_run.call_args[0][0][2]
        assert "Cursor" in osascript_arg
        assert "activate" in osascript_arg


class TestFocusInput:
    """focus_input() — keyboard shortcut to focus input field."""

    @patch("heyvox.input.injection.subprocess.run")
    def test_sends_shortcut_when_configured(self, mock_run):
        focus_input("Cursor", {"cursor": "l"})
        mock_run.assert_called_once()

    @patch("heyvox.input.injection.subprocess.run")
    def test_noop_when_no_shortcut(self, mock_run):
        focus_input("Unknown", {"cursor": "l"})
        mock_run.assert_not_called()

    @patch("heyvox.input.injection.subprocess.run")
    def test_case_insensitive_matching(self, mock_run):
        focus_input("CURSOR", {"cursor": "l"})
        mock_run.assert_called_once()


class TestClipboardIsImage:
    """clipboard_is_image() — detect image clipboard content."""

    @patch("heyvox.input.injection.subprocess.run")
    def test_detects_png(self, mock_run):
        mock_run.return_value = MagicMock(stdout="«class PNGf», 12345")
        assert clipboard_is_image() is True

    @patch("heyvox.input.injection.subprocess.run")
    def test_detects_tiff(self, mock_run):
        mock_run.return_value = MagicMock(stdout="«class TIFF», 12345")
        assert clipboard_is_image() is True

    @patch("heyvox.input.injection.subprocess.run")
    def test_no_image(self, mock_run):
        mock_run.return_value = MagicMock(stdout="«class utf8», 50")
        assert clipboard_is_image() is False


class TestGetClipboardText:
    """get_clipboard_text() — read text from clipboard."""

    @patch("heyvox.input.injection.subprocess.run")
    def test_returns_text(self, mock_run):
        mock_run.return_value = MagicMock(stdout="hello world\n")
        assert get_clipboard_text() == "hello world"

    @patch("heyvox.input.injection.subprocess.run")
    def test_returns_empty_on_error(self, mock_run):
        mock_run.return_value = MagicMock(stdout="\n")
        assert get_clipboard_text() == ""
