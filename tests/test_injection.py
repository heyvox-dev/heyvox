"""Tests for heyvox.input.injection — text injection via clipboard + osascript."""

import sys
from unittest.mock import patch, MagicMock, call

from heyvox.input.injection import (
    type_text, press_enter, focus_app, focus_input,
    clipboard_is_image, get_clipboard_text,
    _settle_delay_for,
)


def _make_mock_appkit(clipboard_text: str = "hello"):
    """Create a mock AppKit module with a fully functional NSPasteboard mock."""
    mock_pb = MagicMock()
    mock_pb.clearContents.return_value = None
    mock_pb.setString_forType_.return_value = True
    mock_pb.changeCount.return_value = 42
    mock_pb.stringForType_.return_value = clipboard_text

    mock_appkit = MagicMock()
    mock_appkit.NSPasteboard.generalPasteboard.return_value = mock_pb
    mock_appkit.NSPasteboardTypeString = "public.utf8-plain-text"
    return mock_appkit, mock_pb


class TestTypeText:
    """type_text() — clipboard-based text injection."""

    def test_basic_paste(self):
        """NSPasteboard.setString_forType_ is called with the text (no pbcopy subprocess)."""
        mock_appkit, mock_pb = _make_mock_appkit("hello")
        with patch.dict("sys.modules", {"AppKit": mock_appkit}):
            with patch("heyvox.input.injection.subprocess.run") as mock_run:
                with patch("heyvox.input.injection.time.sleep"):
                    mock_run.return_value = MagicMock(returncode=0)
                    type_text("hello")
        # NSPasteboard write was called
        mock_pb.setString_forType_.assert_called_once_with("hello", "public.utf8-plain-text")
        # osascript Cmd-V call was made
        assert mock_run.call_count >= 1

    def test_no_clipboard_restore(self):
        """Clipboard is NOT restored after paste — prevents Electron race condition."""
        mock_appkit, mock_pb = _make_mock_appkit("hello")
        with patch.dict("sys.modules", {"AppKit": mock_appkit}):
            with patch("heyvox.input.injection.subprocess.run") as mock_run:
                with patch("heyvox.input.injection.time.sleep"):
                    mock_run.return_value = MagicMock(returncode=0)
                    type_text("hello")
        # Subprocess calls: get_frontmost_before + Cmd-V + get_frontmost_after = 3.
        # No additional call to write clipboard content back (no restore).
        # NSPasteboard.setString_forType_ is never called a second time.
        assert mock_run.call_count == 3
        # Verify NSPasteboard was only written once (no restore write)
        mock_pb.setString_forType_.assert_called_once()

    def test_aborts_on_verify_mismatch(self):
        """If clipboard read returns wrong text, abort without pasting."""
        mock_appkit, mock_pb = _make_mock_appkit("wrong")
        with patch.dict("sys.modules", {"AppKit": mock_appkit}):
            with patch("heyvox.input.injection.subprocess.run") as mock_run:
                with patch("heyvox.input.injection.time.sleep"):
                    mock_run.return_value = MagicMock(returncode=0)
                    type_text("hello")
        # Cmd-V should NOT be sent when clipboard verify fails
        assert mock_run.call_count == 0

    def test_aborts_on_clipboard_write_failure(self):
        """If NSPasteboard.setString_forType_ returns False, abort without pasting."""
        mock_appkit, mock_pb = _make_mock_appkit("hello")
        mock_pb.setString_forType_.return_value = False
        with patch.dict("sys.modules", {"AppKit": mock_appkit}):
            with patch("heyvox.input.injection.subprocess.run") as mock_run:
                with patch("heyvox.input.injection.time.sleep"):
                    mock_run.return_value = MagicMock(returncode=0)
                    type_text("hello")
        # No osascript paste call on clipboard write failure
        assert mock_run.call_count == 0

    def test_handles_special_chars(self):
        """NSPasteboard receives special chars string directly (no escaping)."""
        text = 'say "hello"'
        mock_appkit, mock_pb = _make_mock_appkit(text)
        with patch.dict("sys.modules", {"AppKit": mock_appkit}):
            with patch("heyvox.input.injection.subprocess.run") as mock_run:
                with patch("heyvox.input.injection.time.sleep"):
                    mock_run.return_value = MagicMock(returncode=0)
                    type_text(text)
        mock_pb.setString_forType_.assert_called_once_with(text, "public.utf8-plain-text")


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
    """get_clipboard_text() — read text from clipboard via NSPasteboard."""

    def test_returns_text(self):
        mock_appkit, mock_pb = _make_mock_appkit("hello world")
        with patch.dict("sys.modules", {"AppKit": mock_appkit}):
            result = get_clipboard_text()
        assert result == "hello world"

    def test_returns_empty_when_no_text(self):
        mock_appkit, mock_pb = _make_mock_appkit("")
        mock_pb.stringForType_.return_value = None
        with patch.dict("sys.modules", {"AppKit": mock_appkit}):
            result = get_clipboard_text()
        assert result == ""


class TestSettleDelay:
    """_settle_delay_for() — per-app focus settle delay resolution."""

    DELAYS: dict[str, float] = {"conductor": 0.3, "cursor": 0.15, "iterm2": 0.03}

    def test_exact_match(self):
        assert _settle_delay_for("cursor", self.DELAYS, 0.1) == 0.15

    def test_substring_match(self):
        assert _settle_delay_for("Cursor Editor", self.DELAYS, 0.1) == 0.15

    def test_case_insensitive(self):
        assert _settle_delay_for("CONDUCTOR", self.DELAYS, 0.1) == 0.3

    def test_default_delay(self):
        assert _settle_delay_for("UnknownApp", self.DELAYS, 0.1) == 0.1

    def test_none_app(self):
        assert _settle_delay_for(None, self.DELAYS, 0.1) == 0.1
