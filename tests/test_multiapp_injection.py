"""
TestMultiAppInjection — integration tests for the full injection pipeline.

Exercises type_text() + _settle_delay_for() + InjectionConfig across all
supported app targets (Conductor, Cursor, iTerm2, Chrome, unknown apps,
AX fast-path, and failure signaling).

These tests mock AppKit / ApplicationServices / subprocess at the boundary
so no real clipboard, app, or osascript calls are made.

Requirement coverage: PASTE-01, PASTE-02, PASTE-03, PASTE-04, PASTE-05
"""
import sys
from unittest.mock import patch, MagicMock, call

import pytest

from heyvox.input.injection import (
    type_text,
    _settle_delay_for,
)
from heyvox.config import InjectionConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_appkit(clipboard_text: str = "hello", change_count: int = 42):
    """Return (mock_appkit, mock_pb) with NSPasteboard configured."""
    mock_pb = MagicMock()
    mock_pb.clearContents.return_value = None
    mock_pb.setString_forType_.return_value = True
    mock_pb.changeCount.return_value = change_count
    mock_pb.stringForType_.return_value = clipboard_text

    mock_appkit = MagicMock()
    mock_appkit.NSPasteboard.generalPasteboard.return_value = mock_pb
    mock_appkit.NSPasteboardTypeString = "public.utf8-plain-text"
    return mock_appkit, mock_pb


def _make_snap(
    app_name: str = "Cursor",
    app_pid: int = 1234,
    element_role: str = "AXWebArea",
    app_bundle_id: str = "com.cursor.app",
    ax_element=None,
    conductor_workspace: str = "",
):
    """Return a minimal TargetLock-like mock (Phase 15-02 field shape)."""
    snap = MagicMock()
    snap.app_name = app_name
    snap.app_pid = app_pid
    # Old-shape (retained for legacy consumers)
    snap.element_role = element_role
    snap.ax_element = ax_element if ax_element is not None else MagicMock()
    # TargetLock fields (new)
    snap.leaf_role = element_role
    snap.conductor_workspace_id = None
    snap.app_bundle_id = app_bundle_id
    snap.conductor_workspace = conductor_workspace
    return snap


def _run_type_text(text: str, snap=None, settle_secs: float = 0.1, max_retries: int = 2):
    """Call type_text with standard mocking scaffolding. Returns (result, mock_run, mock_cue).

    Focus verification is patched to return True so tests focus on the
    clipboard/injection path rather than app focus state.
    """
    mock_appkit, mock_pb = _make_appkit(text)
    with patch.dict("sys.modules", {"AppKit": mock_appkit}):
        with patch("heyvox.input.injection.subprocess.run") as mock_run:
            with patch("heyvox.input.injection.time.sleep"):
                with patch("heyvox.input.injection._clipboard_still_ours", return_value=True):
                    with patch("heyvox.input.injection._verify_target_focused", return_value=True):
                        with patch("heyvox.input.injection.audio_cue") as mock_cue:
                            mock_run.return_value = MagicMock(returncode=0)
                            result = type_text(
                                text,
                                app_name=snap.app_name if snap else None,
                                snap=snap,
                                settle_secs=settle_secs,
                                max_retries=max_retries,
                            )
    return result, mock_run, mock_cue


# ---------------------------------------------------------------------------
# TestMultiAppInjection — per-app settle delay resolution via InjectionConfig
# ---------------------------------------------------------------------------

class TestMultiAppInjection:
    """Integration tests: full injection pipeline for different app targets."""

    # Default InjectionConfig settle delays (same as production defaults)
    CFG = InjectionConfig()

    # --- Conductor ---

    def test_conductor_settle_delay(self):
        """Conductor gets 0.3s settle delay (Tauri Electron wrapper, needs extra time)."""
        delay = _settle_delay_for("Conductor", self.CFG.app_delays, self.CFG.focus_settle_secs)
        assert delay == 0.3

    def test_conductor_injection_succeeds(self):
        """Full type_text call succeeds for Conductor app target."""
        snap = _make_snap("Conductor", app_bundle_id="com.conductor.app")
        result, mock_run, _ = _run_type_text("hello world", snap=snap, settle_secs=0.3)
        assert result is True
        # osascript Cmd-V was called
        assert mock_run.call_count >= 1

    def test_conductor_osascript_targets_process(self):
        """osascript script targets actual process name (case-sensitive) when app_name is set."""
        snap = _make_snap("Conductor", app_bundle_id="com.conductor.app")
        mock_appkit, _ = _make_appkit("hello")
        with patch.dict("sys.modules", {"AppKit": mock_appkit}):
            with patch("heyvox.input.injection.subprocess.run") as mock_run:
                with patch("heyvox.input.injection.time.sleep"):
                    with patch("heyvox.input.injection._clipboard_still_ours", return_value=True):
                        with patch("heyvox.input.injection._verify_target_focused", return_value=True):
                            with patch("heyvox.input.injection._get_frontmost_app", return_value="conductor"):
                                mock_run.return_value = MagicMock(returncode=0)
                                type_text("hello", app_name="Conductor", snap=snap, settle_secs=0.3)
        # Find the Cmd-V call (contains "keystroke")
        cmdv_calls = [
            c for c in mock_run.call_args_list
            if "keystroke" in str(c)
        ]
        assert len(cmdv_calls) == 1
        # Uses actual process name "conductor" (lowercase), not display name "Conductor"
        assert "conductor" in str(cmdv_calls[0])

    # --- Cursor ---

    def test_cursor_settle_delay(self):
        """Cursor gets 0.15s settle delay (Electron app, lighter than Tauri)."""
        delay = _settle_delay_for("Cursor", self.CFG.app_delays, self.CFG.focus_settle_secs)
        assert delay == 0.15

    def test_cursor_injection_succeeds(self):
        """Full type_text call succeeds for Cursor app target."""
        snap = _make_snap("Cursor", app_bundle_id="com.cursor.app")
        result, mock_run, _ = _run_type_text("fix the bug", snap=snap, settle_secs=0.15)
        assert result is True

    def test_windsurf_settle_delay(self):
        """Windsurf gets 0.15s settle delay (same Electron tier as Cursor)."""
        delay = _settle_delay_for("Windsurf", self.CFG.app_delays, self.CFG.focus_settle_secs)
        assert delay == 0.15

    def test_vscode_settle_delay(self):
        """Visual Studio Code gets 0.15s settle delay (Electron, case-insensitive match)."""
        delay = _settle_delay_for("Visual Studio Code", self.CFG.app_delays, self.CFG.focus_settle_secs)
        assert delay == 0.15

    # --- iTerm2 / Terminal ---

    def test_iterm2_settle_delay(self):
        """iTerm2 gets 0.03s settle delay (native AppKit, no Electron overhead)."""
        delay = _settle_delay_for("iTerm2", self.CFG.app_delays, self.CFG.focus_settle_secs)
        assert delay == 0.03

    def test_terminal_settle_delay(self):
        """Terminal gets 0.03s settle delay (native AppKit)."""
        delay = _settle_delay_for("Terminal", self.CFG.app_delays, self.CFG.focus_settle_secs)
        assert delay == 0.03

    def test_iterm2_injection_succeeds(self):
        """Full type_text call succeeds for iTerm2 (native AppKit, short settle)."""
        snap = _make_snap("iTerm2", app_bundle_id="com.googlecode.iterm2")
        result, mock_run, _ = _run_type_text("ls -la", snap=snap, settle_secs=0.03)
        assert result is True

    # --- Unknown / default ---

    def test_unknown_app_gets_default_settle_delay(self):
        """Unrecognized app name gets the default settle delay (0.1s)."""
        delay = _settle_delay_for("SomeUnknownApp", self.CFG.app_delays, self.CFG.focus_settle_secs)
        assert delay == self.CFG.focus_settle_secs

    def test_none_app_name_gets_default_delay(self):
        """None app_name (frontmost) gets the default settle delay."""
        delay = _settle_delay_for(None, self.CFG.app_delays, self.CFG.focus_settle_secs)
        assert delay == self.CFG.focus_settle_secs

    def test_unknown_app_injection_succeeds(self):
        """type_text succeeds for unknown app using default settle delay."""
        snap = _make_snap("SomeEditor", app_bundle_id="com.some.editor")
        result, _, _ = _run_type_text("hello", snap=snap, settle_secs=0.1)
        assert result is True

    # --- Chrome / Hush path ---

    def test_chrome_injection_uses_hush_socket(self):
        """Chrome injection tries Hush socket first (skips clipboard path on success)."""
        snap = _make_snap("Google Chrome", app_bundle_id="com.google.Chrome")
        mock_appkit, mock_pb = _make_appkit("hello")
        with patch.dict("sys.modules", {"AppKit": mock_appkit}):
            with patch("heyvox.input.injection._chrome_type_text", return_value=True) as mock_chrome:
                with patch("heyvox.input.injection.subprocess.run") as mock_run:
                    result = type_text("hello", snap=snap)
        assert result is True
        mock_chrome.assert_called_once_with("hello")
        # Clipboard + osascript path was NOT used (no subprocess.run needed)
        mock_run.assert_not_called()
        mock_pb.setString_forType_.assert_not_called()

    def test_chrome_injection_falls_back_to_clipboard(self):
        """When Hush socket fails, falls back to clipboard + Cmd-V."""
        snap = _make_snap("Google Chrome", app_bundle_id="com.google.Chrome")
        mock_appkit, mock_pb = _make_appkit("hello")
        with patch.dict("sys.modules", {"AppKit": mock_appkit}):
            with patch("heyvox.input.injection._chrome_type_text", return_value=False):
                with patch("heyvox.input.injection.subprocess.run") as mock_run:
                    with patch("heyvox.input.injection.time.sleep"):
                        with patch("heyvox.input.injection._clipboard_still_ours", return_value=True):
                            with patch("heyvox.input.injection._verify_target_focused", return_value=True):
                                mock_run.return_value = MagicMock(returncode=0)
                                result = type_text("hello", snap=snap)
        assert result is True
        # NSPasteboard was used as fallback
        mock_pb.setString_forType_.assert_called_once_with("hello", "public.utf8-plain-text")

    # --- AX fast-path ---

    def test_ax_fastpath_for_native_textfield(self):
        """Native AppKit AXTextField: AX fast-path used, skips clipboard entirely."""
        snap = _make_snap("Xcode", element_role="AXTextField", app_bundle_id="com.apple.dt.Xcode")
        mock_appkit, mock_pb = _make_appkit("hello")
        mock_ax = MagicMock()
        mock_ax.AXUIElementSetAttributeValue = MagicMock(return_value=0)
        with patch.dict("sys.modules", {"AppKit": mock_appkit, "ApplicationServices": mock_ax}):
            with patch("heyvox.input.injection._chrome_type_text", return_value=False):
                with patch("heyvox.input.injection.subprocess.run") as mock_run:
                    result = type_text("hello", snap=snap)
        assert result is True
        # Clipboard was NOT used (no osascript subprocess)
        mock_run.assert_not_called()
        mock_pb.setString_forType_.assert_not_called()

    def test_ax_fastpath_skipped_for_webarea(self):
        """AXWebArea (Electron/WebKit) skips AX fast-path, uses clipboard."""
        snap = _make_snap("Cursor", element_role="AXWebArea", app_bundle_id="com.cursor.app")
        mock_appkit, mock_pb = _make_appkit("hello")
        with patch.dict("sys.modules", {"AppKit": mock_appkit}):
            with patch("heyvox.input.injection._chrome_type_text", return_value=False):
                with patch("heyvox.input.injection.subprocess.run") as mock_run:
                    with patch("heyvox.input.injection.time.sleep"):
                        with patch("heyvox.input.injection._clipboard_still_ours", return_value=True):
                            with patch("heyvox.input.injection._verify_target_focused", return_value=True):
                                mock_run.return_value = MagicMock(returncode=0)
                                result = type_text("hello", snap=snap)
        assert result is True
        # Clipboard path was used (NSPasteboard written)
        mock_pb.setString_forType_.assert_called_once()

    def test_ax_fastpath_falls_back_to_clipboard_on_ax_error(self):
        """AX fast-path failure (non-zero error code) falls back to clipboard."""
        snap = _make_snap("Xcode", element_role="AXTextField", app_bundle_id="com.apple.dt.Xcode")
        mock_appkit, mock_pb = _make_appkit("hello")
        mock_ax = MagicMock()
        mock_ax.AXUIElementSetAttributeValue = MagicMock(return_value=-25200)  # kAXErrorCannotComplete
        with patch.dict("sys.modules", {"AppKit": mock_appkit, "ApplicationServices": mock_ax}):
            with patch("heyvox.input.injection._chrome_type_text", return_value=False):
                with patch("heyvox.input.injection.subprocess.run") as mock_run:
                    with patch("heyvox.input.injection.time.sleep"):
                        with patch("heyvox.input.injection._clipboard_still_ours", return_value=True):
                            with patch("heyvox.input.injection._verify_target_focused", return_value=True):
                                mock_run.return_value = MagicMock(returncode=0)
                                result = type_text("hello", snap=snap)
        assert result is True
        # Clipboard fallback was used
        mock_pb.setString_forType_.assert_called_once_with("hello", "public.utf8-plain-text")

    # --- Failure signaling ---

    def test_returns_false_on_osascript_failure(self):
        """type_text returns False when osascript Cmd-V fails (non-zero rc)."""
        snap = _make_snap("Cursor", app_bundle_id="com.cursor.app")
        mock_appkit, mock_pb = _make_appkit("hello")
        with patch.dict("sys.modules", {"AppKit": mock_appkit}):
            with patch("heyvox.input.injection._chrome_type_text", return_value=False):
                with patch("heyvox.input.injection._ax_inject_text", return_value=False):
                    with patch("heyvox.input.injection._verify_target_focused", return_value=True):
                        with patch("heyvox.input.injection.subprocess.run") as mock_run:
                            with patch("heyvox.input.injection.time.sleep"):
                                with patch("heyvox.input.injection._clipboard_still_ours", return_value=True):
                                    with patch("heyvox.input.injection.audio_cue") as mock_cue:
                                        mock_run.side_effect = [
                                            MagicMock(returncode=0),  # get_frontmost_before
                                            MagicMock(returncode=0),  # already_frontmost check
                                            MagicMock(returncode=1, stderr=b"osascript: error"),  # Cmd-V fails
                                            MagicMock(returncode=0),  # get_frontmost_after
                                        ]
                                        result = type_text("hello", snap=snap)
        assert result is False
        mock_cue.assert_called_with("error")

    def test_returns_false_on_clipboard_stolen(self):
        """type_text returns False when clipboard is stolen during settle (max retries exhausted)."""
        snap = _make_snap("Cursor", app_bundle_id="com.cursor.app")
        mock_appkit, mock_pb = _make_appkit("hello")
        with patch.dict("sys.modules", {"AppKit": mock_appkit}):
            with patch("heyvox.input.injection._chrome_type_text", return_value=False):
                with patch("heyvox.input.injection._ax_inject_text", return_value=False):
                    with patch("heyvox.input.injection._verify_target_focused", return_value=True):
                        with patch("heyvox.input.injection.subprocess.run") as mock_run:
                            with patch("heyvox.input.injection.time.sleep"):
                                with patch("heyvox.input.injection._clipboard_still_ours", return_value=False):
                                    with patch("heyvox.input.injection.audio_cue") as mock_cue:
                                        mock_run.return_value = MagicMock(returncode=0)
                                        result = type_text("hello", snap=snap, max_retries=0)
        assert result is False
        mock_cue.assert_called_with("error")

    def test_returns_false_on_focus_mismatch(self):
        """type_text returns False when focus verification fails (wrong app in front)."""
        snap = _make_snap("Conductor", app_bundle_id="com.conductor.app")
        mock_appkit, _ = _make_appkit("hello")
        # Simulate wrong app focused (Safari instead of Conductor)
        mock_app = MagicMock()
        mock_app.bundleIdentifier.return_value = "com.apple.Safari"
        mock_appkit.NSWorkspace.sharedWorkspace.return_value.frontmostApplication.return_value = mock_app
        with patch.dict("sys.modules", {"AppKit": mock_appkit}):
            with patch("heyvox.input.injection._chrome_type_text", return_value=False):
                with patch("heyvox.input.injection._ax_inject_text", return_value=False):
                    with patch("heyvox.input.injection.subprocess.run") as mock_run:
                        with patch("heyvox.input.injection.time.sleep"):
                            with patch("heyvox.input.injection.audio_cue") as mock_cue:
                                mock_run.return_value = MagicMock(returncode=0)
                                result = type_text("hello", snap=snap)
        assert result is False
        mock_cue.assert_called_with("error")

    def test_returns_true_on_all_paths_succeed(self):
        """type_text returns True when clipboard write + Cmd-V both succeed."""
        snap = _make_snap("Cursor", app_bundle_id="com.cursor.app")
        result, _, _ = _run_type_text("all good", snap=snap, settle_secs=0.15)
        assert result is True

    # --- InjectionConfig integration ---

    def test_injection_config_defaults_match_production(self):
        """InjectionConfig default delays match expected production values."""
        cfg = InjectionConfig()
        assert cfg.app_delays["conductor"] == 0.3
        assert cfg.app_delays["cursor"] == 0.15
        assert cfg.app_delays["windsurf"] == 0.15
        assert cfg.app_delays["visual studio code"] == 0.15
        assert cfg.app_delays["iterm2"] == 0.03
        assert cfg.app_delays["terminal"] == 0.03
        assert cfg.focus_settle_secs == 0.1
        assert cfg.max_retries == 2

    def test_settle_delay_for_uses_injection_config(self):
        """_settle_delay_for correctly resolves delays from InjectionConfig.app_delays."""
        cfg = InjectionConfig()
        # All production targets
        assert _settle_delay_for("Conductor", cfg.app_delays, cfg.focus_settle_secs) == 0.3
        assert _settle_delay_for("Cursor", cfg.app_delays, cfg.focus_settle_secs) == 0.15
        assert _settle_delay_for("Windsurf", cfg.app_delays, cfg.focus_settle_secs) == 0.15
        assert _settle_delay_for("Visual Studio Code", cfg.app_delays, cfg.focus_settle_secs) == 0.15
        assert _settle_delay_for("iTerm2", cfg.app_delays, cfg.focus_settle_secs) == 0.03
        assert _settle_delay_for("Terminal", cfg.app_delays, cfg.focus_settle_secs) == 0.03
        # Fallback
        assert _settle_delay_for("NotebookApp", cfg.app_delays, cfg.focus_settle_secs) == 0.1
