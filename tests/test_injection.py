"""Tests for heyvox.input.injection — text injection via clipboard + osascript."""

import sys
from unittest.mock import patch, MagicMock, call

from heyvox.input.injection import (
    type_text, press_enter, focus_app, focus_input,
    clipboard_is_image, get_clipboard_text,
    _settle_delay_for,
    _clipboard_still_ours,
    _verify_target_focused,
    _ax_inject_text,
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
        # Subprocess calls: get_frontmost_before + already_frontmost_check + Cmd-V + get_frontmost_after = 4.
        # No additional call to write clipboard content back (no restore).
        # NSPasteboard.setString_forType_ is never called a second time.
        assert mock_run.call_count == 4
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


# ---------------------------------------------------------------------------
# NEW: Plan 02 — changeCount race detection, focus verification, AX fast-path
# ---------------------------------------------------------------------------


def _make_snap(element_role: str = "AXTextField", ax_element=None, app_bundle_id: str = "com.example.App"):
    """Create a minimal TargetLock-like object for tests.

    Phase 15-02 migration: returns an object with both old (element_role,
    ax_element) and new (leaf_role, app_pid, conductor_workspace_id) field
    shapes so both the historical and current consumers see valid data.
    """
    snap = MagicMock()
    # Old-shape fields (kept for any tests that still poke them)
    snap.element_role = element_role
    snap.ax_element = ax_element if ax_element is not None else MagicMock()
    # New-shape fields (TargetLock)
    snap.leaf_role = element_role
    snap.app_pid = 1234
    snap.app_name = "TestApp"
    snap.conductor_workspace_id = None
    snap.app_bundle_id = app_bundle_id
    return snap


class TestClipboardRace:
    """changeCount-based clipboard corruption detection (PASTE-02)."""

    def test_clipboard_still_ours_true(self):
        """Returns True when changeCount matches expected value."""
        mock_appkit = MagicMock()
        mock_pb = MagicMock()
        mock_pb.changeCount.return_value = 42
        mock_appkit.NSPasteboard.generalPasteboard.return_value = mock_pb
        with patch.dict("sys.modules", {"AppKit": mock_appkit}):
            assert _clipboard_still_ours(42) is True

    def test_clipboard_still_ours_false(self):
        """Returns False when changeCount does not match — clipboard was stolen."""
        mock_appkit = MagicMock()
        mock_pb = MagicMock()
        mock_pb.changeCount.return_value = 43
        mock_appkit.NSPasteboard.generalPasteboard.return_value = mock_pb
        with patch.dict("sys.modules", {"AppKit": mock_appkit}):
            assert _clipboard_still_ours(42) is False

    def test_clipboard_still_ours_exception_returns_false(self):
        """Returns False on exception (fail-safe)."""
        mock_appkit = MagicMock()
        mock_appkit.NSPasteboard.generalPasteboard.side_effect = RuntimeError("no AppKit")
        with patch.dict("sys.modules", {"AppKit": mock_appkit}):
            result = _clipboard_still_ours(42)
        assert result is False

    def test_paste_aborts_on_stolen_clipboard(self):
        """If clipboard changeCount changes after write, abort paste and play error cue."""
        mock_appkit, mock_pb = _make_mock_appkit("hello")
        # changeCount returns 42 on write, then 99 on verify check (stolen)
        mock_pb.changeCount.return_value = 42
        with patch.dict("sys.modules", {"AppKit": mock_appkit}):
            with patch("heyvox.input.injection.subprocess.run") as mock_run:
                with patch("heyvox.input.injection.time.sleep"):
                    with patch("heyvox.input.injection._clipboard_still_ours", return_value=False):
                        with patch("heyvox.input.injection.audio_cue") as mock_cue:
                            mock_run.return_value = MagicMock(returncode=0)
                            result = type_text("hello")
        # Paste should fail — Cmd-V not sent
        mock_cue.assert_called_with("error")
        assert result is False

    def test_paste_retries_on_stolen_clipboard(self):
        """Retries up to max_retries times; succeeds on second attempt."""
        mock_appkit, mock_pb = _make_mock_appkit("hello")
        mock_pb.changeCount.return_value = 42
        # First call returns False (stolen), second returns True (ok)
        side_effects = [False, True]
        call_idx = {"n": 0}
        def still_ours(count):
            result = side_effects[call_idx["n"]]
            call_idx["n"] = min(call_idx["n"] + 1, len(side_effects) - 1)
            return result
        with patch.dict("sys.modules", {"AppKit": mock_appkit}):
            with patch("heyvox.input.injection.subprocess.run") as mock_run:
                with patch("heyvox.input.injection.time.sleep"):
                    with patch("heyvox.input.injection._clipboard_still_ours", side_effect=still_ours):
                        with patch("heyvox.input.injection.audio_cue"):
                            mock_run.return_value = MagicMock(returncode=0)
                            result = type_text("hello", max_retries=2)
        assert result is True


class TestFocusVerification:
    """Proactive focus verification before paste (PASTE-05)."""

    def test_correct_app_focused_returns_true(self):
        """Returns True when frontmost app bundle ID matches expected."""
        mock_appkit = MagicMock()
        mock_app = MagicMock()
        mock_app.bundleIdentifier.return_value = "com.conductor.app"
        mock_appkit.NSWorkspace.sharedWorkspace.return_value.frontmostApplication.return_value = mock_app
        with patch.dict("sys.modules", {"AppKit": mock_appkit}):
            assert _verify_target_focused("com.conductor.app") is True

    def test_wrong_app_focused_returns_false(self):
        """Returns False when a different app is focused."""
        mock_appkit = MagicMock()
        mock_app = MagicMock()
        mock_app.bundleIdentifier.return_value = "com.apple.Safari"
        mock_appkit.NSWorkspace.sharedWorkspace.return_value.frontmostApplication.return_value = mock_app
        with patch.dict("sys.modules", {"AppKit": mock_appkit}):
            assert _verify_target_focused("com.conductor.app") is False

    def test_none_skips_check(self):
        """None expected_bundle_id skips check and returns True (no-op)."""
        assert _verify_target_focused(None) is True

    def test_exception_fails_open(self):
        """Returns True on exception (fail-open — don't block paste on check failure)."""
        mock_appkit = MagicMock()
        mock_appkit.NSWorkspace.sharedWorkspace.side_effect = RuntimeError("no workspace")
        with patch.dict("sys.modules", {"AppKit": mock_appkit}):
            assert _verify_target_focused("com.conductor.app") is True

    def test_paste_aborts_on_wrong_focus(self):
        """Focus mismatch aborts paste: no Cmd-V, error cue played."""
        mock_appkit, mock_pb = _make_mock_appkit("hello")
        with patch.dict("sys.modules", {"AppKit": mock_appkit}):
            with patch("heyvox.input.injection.subprocess.run") as mock_run:
                with patch("heyvox.input.injection.time.sleep"):
                    with patch("heyvox.input.injection._verify_target_focused", return_value=False):
                        with patch("heyvox.input.injection.audio_cue") as mock_cue:
                            mock_run.return_value = MagicMock(returncode=0)
                            result = type_text("hello")
        mock_cue.assert_called_with("error")
        assert result is False


class TestAXFastPath:
    """AX fast-path for native AppKit text fields (PASTE-04)."""

    def test_injects_into_axtextfield(self):
        """Returns True for AXTextField with successful AXUIElementSetAttributeValue."""
        snap = _make_snap("AXTextField")
        fake_focused = object()
        mock_ax = MagicMock()
        mock_ax.AXUIElementCreateApplication.return_value = MagicMock()
        mock_ax.AXUIElementCopyAttributeValue = MagicMock(return_value=(0, fake_focused))
        mock_ax.AXUIElementSetAttributeValue = MagicMock(return_value=0)
        with patch.dict("sys.modules", {"ApplicationServices": mock_ax}):
            result = _ax_inject_text(snap, "hello")
        assert result is True

    def test_injects_into_axtextarea(self):
        """Returns True for AXTextArea with successful AXUIElementSetAttributeValue."""
        snap = _make_snap("AXTextArea")
        fake_focused = object()
        mock_ax = MagicMock()
        mock_ax.AXUIElementCreateApplication.return_value = MagicMock()
        mock_ax.AXUIElementCopyAttributeValue = MagicMock(return_value=(0, fake_focused))
        mock_ax.AXUIElementSetAttributeValue = MagicMock(return_value=0)
        with patch.dict("sys.modules", {"ApplicationServices": mock_ax}):
            result = _ax_inject_text(snap, "hello")
        assert result is True

    def test_skips_webarea(self):
        """Returns False for AXWebArea — not a native text field."""
        snap = _make_snap("AXWebArea")
        assert _ax_inject_text(snap, "hello") is False

    def test_skips_none_snap(self):
        """Returns False when snap is None."""
        assert _ax_inject_text(None, "hello") is False

    def test_skips_none_ax_element(self):
        """Returns False when ax_element is None."""
        snap = _make_snap("AXTextField", ax_element=None)
        # We need ax_element to actually be None, not a MagicMock
        snap.ax_element = None
        assert _ax_inject_text(snap, "hello") is False

    def test_returns_false_on_ax_error(self):
        """Returns False when AXUIElementSetAttributeValue returns non-zero."""
        snap = _make_snap("AXTextField")
        fake_focused = object()
        mock_ax = MagicMock()
        mock_ax.AXUIElementCreateApplication.return_value = MagicMock()
        mock_ax.AXUIElementCopyAttributeValue = MagicMock(return_value=(0, fake_focused))
        mock_ax.AXUIElementSetAttributeValue = MagicMock(return_value=-25200)
        with patch.dict("sys.modules", {"ApplicationServices": mock_ax}):
            result = _ax_inject_text(snap, "hello")
        assert result is False

    def test_type_text_tries_ax_fastpath_first(self):
        """type_text tries AX fast-path before clipboard when snap has AXTextField."""
        snap = _make_snap("AXTextField")
        mock_appkit, mock_pb = _make_mock_appkit("hello")
        with patch.dict("sys.modules", {"AppKit": mock_appkit}):
            with patch("heyvox.input.injection._ax_inject_text", return_value=True) as mock_ax:
                with patch("heyvox.input.injection.subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=0)
                    result = type_text("hello", snap=snap)
        mock_ax.assert_called_once_with(snap, "hello")
        assert result is True
        # Clipboard path (osascript) should NOT have been called
        mock_run.assert_not_called()


class TestErrorCue:
    """Error cue played on paste failure (PASTE-05)."""

    def test_error_cue_on_clipboard_write_failure(self):
        """audio_cue('error') called when NSPasteboard write fails."""
        mock_appkit, mock_pb = _make_mock_appkit("hello")
        mock_pb.setString_forType_.return_value = False
        with patch.dict("sys.modules", {"AppKit": mock_appkit}):
            with patch("heyvox.input.injection.subprocess.run") as mock_run:
                with patch("heyvox.input.injection.time.sleep"):
                    with patch("heyvox.input.injection.audio_cue") as mock_cue:
                        mock_run.return_value = MagicMock(returncode=0)
                        result = type_text("hello")
        mock_cue.assert_called_with("error")
        assert result is False

    def test_error_cue_on_osascript_failure(self):
        """audio_cue('error') called when osascript returns non-zero."""
        mock_appkit, mock_pb = _make_mock_appkit("hello")
        with patch.dict("sys.modules", {"AppKit": mock_appkit}):
            with patch("heyvox.input.injection.subprocess.run") as mock_run:
                with patch("heyvox.input.injection.time.sleep"):
                    with patch("heyvox.input.injection._clipboard_still_ours", return_value=True):
                        with patch("heyvox.input.injection.audio_cue") as mock_cue:
                            # Calls: get_frontmost_before, already_frontmost_check, Cmd-V (fails), get_frontmost_after
                            mock_run.side_effect = [
                                MagicMock(returncode=0, stdout="Cursor"),  # get frontmost before
                                MagicMock(returncode=0, stdout="Cursor"),  # already_frontmost check
                                MagicMock(returncode=1, stderr=b"error"),  # osascript Cmd-V fails
                                MagicMock(returncode=0, stdout="Cursor"),  # get frontmost after
                            ]
                            result = type_text("hello")
        mock_cue.assert_called_with("error")
        assert result is False

    def test_type_text_returns_true_on_success(self):
        """type_text returns True on successful paste."""
        mock_appkit, mock_pb = _make_mock_appkit("hello")
        with patch.dict("sys.modules", {"AppKit": mock_appkit}):
            with patch("heyvox.input.injection.subprocess.run") as mock_run:
                with patch("heyvox.input.injection.time.sleep"):
                    with patch("heyvox.input.injection._clipboard_still_ours", return_value=True):
                        mock_run.return_value = MagicMock(returncode=0)
                        result = type_text("hello")
        assert result is True
