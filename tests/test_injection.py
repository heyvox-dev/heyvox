"""Tests for heyvox.input.injection — text injection via clipboard + osascript."""

import pytest
from unittest.mock import patch, MagicMock, call

from heyvox.input.injection import (
    type_text, press_enter, focus_app, focus_input,
    clipboard_is_image, get_clipboard_text,
)


class TestTypeText:
    """type_text() — clipboard-based text injection."""

    @patch("heyvox.input.injection.subprocess.run")
    @patch("heyvox.input.injection.time.sleep")
    @patch("heyvox.input.injection.clipboard_is_image", return_value=False)
    @patch("heyvox.input.injection.get_clipboard_text", return_value="")
    def test_basic_paste(self, mock_clip, mock_img, mock_sleep, mock_run):
        type_text("hello")
        # At least: set clipboard, cmd-v
        assert mock_run.call_count >= 2

    @patch("heyvox.input.injection.subprocess.run")
    @patch("heyvox.input.injection.time.sleep")
    @patch("heyvox.input.injection.clipboard_is_image", return_value=False)
    @patch("heyvox.input.injection.get_clipboard_text", return_value="old clipboard")
    def test_restores_clipboard(self, mock_clip, mock_img, mock_sleep, mock_run):
        type_text("new text")
        # Should call subprocess.run 3 times: set, paste, restore
        assert mock_run.call_count == 3

    @patch("heyvox.input.injection.subprocess.run")
    @patch("heyvox.input.injection.time.sleep")
    @patch("heyvox.input.injection.clipboard_is_image", return_value=True)
    @patch("heyvox.input.injection.get_clipboard_text", return_value="")
    def test_skips_restore_for_image_clipboard(self, mock_clip, mock_img, mock_sleep, mock_run):
        type_text("test")
        # Only set clipboard + cmd-v (no restore for image clipboard)
        assert mock_run.call_count == 2

    @patch("heyvox.input.injection.subprocess.run")
    @patch("heyvox.input.injection.time.sleep")
    @patch("heyvox.input.injection.clipboard_is_image", return_value=False)
    @patch("heyvox.input.injection.get_clipboard_text", return_value="")
    def test_escapes_quotes(self, mock_clip, mock_img, mock_sleep, mock_run):
        type_text('say "hello"')
        first_call = mock_run.call_args_list[0]
        osascript_arg = first_call[0][0][2]  # The -e argument
        assert '\\"hello\\"' in osascript_arg

    @patch("heyvox.input.injection.subprocess.run")
    @patch("heyvox.input.injection.time.sleep")
    @patch("heyvox.input.injection.clipboard_is_image", return_value=False)
    @patch("heyvox.input.injection.get_clipboard_text", return_value="")
    def test_escapes_backslashes(self, mock_clip, mock_img, mock_sleep, mock_run):
        type_text("path\\to\\file")
        first_call = mock_run.call_args_list[0]
        osascript_arg = first_call[0][0][2]
        assert "path\\\\to\\\\file" in osascript_arg


class TestPressEnter:
    """press_enter() — keystroke simulation."""

    @patch("heyvox.input.injection.subprocess.run")
    def test_single_enter(self, mock_run):
        press_enter(1)
        mock_run.assert_called_once()

    @patch("heyvox.input.injection.subprocess.run")
    def test_multiple_enter(self, mock_run):
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
