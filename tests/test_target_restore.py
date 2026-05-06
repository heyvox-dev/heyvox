"""Tests for heyvox.input.target — workspace detection and restore_target().

Covers:
- Workspace switch is skipped when already on correct workspace
- Workspace switch triggers when workspace changed during recording
- focus_shortcut is only used after actual workspace switch
- _detect_app_workspace returns correct workspace from AX tree
"""
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch, MagicMock


@dataclass
class FakeTargetSnapshot:
    app_name: str = "Conductor"
    app_pid: int = 1234
    ax_element: Any = None
    element_role: str = "AXTextArea"
    window_title: str = "Conductor"
    detected_workspace: str = ""
    app_bundle_id: str = ""
    _workspace_switched: bool = False


@dataclass
class FakeAppProfile:
    name: str = "Conductor"
    focus_shortcut: str = "l"
    enter_count: int = 1
    is_electron: bool = True
    settle_delay: float = 0.3
    enter_delay: float = 0.15
    has_workspace_detection: bool = True
    workspace_db: str = ""
    workspace_list_query: str = ""
    workspace_switch_cmd: str = "/usr/local/bin/conductor-switch-workspace"


@dataclass
class FakeConfig:
    _profile: FakeAppProfile = field(default_factory=FakeAppProfile)

    def get_app_profile(self, app_name):
        if app_name and self._profile.name.lower() == app_name.lower():
            return self._profile
        return None


class TestRestoreTargetWorkspaceSkip:
    """restore_target() skips workspace switch when already on correct workspace."""

    @patch("heyvox.input.target._time")
    @patch("heyvox.input.target._find_window_text_fields", return_value=[])
    @patch("heyvox.input.target._activate_app")
    @patch("heyvox.input.target._switch_app_workspace")
    @patch("heyvox.input.target._detect_app_workspace")
    def test_skips_switch_when_already_on_workspace(
        self, mock_detect, mock_switch, mock_activate, mock_find, mock_time,
    ):
        """When current workspace matches snapshot, switch is NOT called."""
        mock_detect.return_value = "seattle"

        mock_appkit = MagicMock()
        mock_app = MagicMock()
        mock_app.localizedName.return_value = "Conductor"
        mock_app.processIdentifier.return_value = 1234
        mock_appkit.NSWorkspace.sharedWorkspace.return_value.frontmostApplication.return_value = mock_app

        mock_ax = MagicMock()
        mock_ax.AXUIElementCreateApplication.return_value = MagicMock()
        mock_ax.AXUIElementSetAttributeValue.return_value = 0
        mock_ax.AXUIElementCopyAttributeValue = MagicMock()

        mock_cf = MagicMock()
        mock_cf.kCFBooleanTrue = True

        snap = FakeTargetSnapshot(detected_workspace="seattle")
        config = FakeConfig()

        with patch.dict("sys.modules", {
            "AppKit": mock_appkit,
            "ApplicationServices": mock_ax,
            "CoreFoundation": mock_cf,
        }):
            from heyvox.input.target import restore_target
            restore_target(snap, config=config)

        mock_switch.assert_not_called()
        assert snap._workspace_switched is False

    @patch("heyvox.input.target._time")
    @patch("heyvox.input.target._find_window_text_fields", return_value=[])
    @patch("heyvox.input.target._activate_app")
    @patch("heyvox.input.target._switch_app_workspace")
    @patch("heyvox.input.target._detect_app_workspace")
    def test_switches_when_workspace_changed(
        self, mock_detect, mock_switch, mock_activate, mock_find, mock_time,
    ):
        """When current workspace differs from snapshot, switch IS called."""
        mock_detect.return_value = "dakar"

        mock_appkit = MagicMock()
        mock_app = MagicMock()
        mock_app.localizedName.return_value = "Conductor"
        mock_app.processIdentifier.return_value = 1234
        mock_appkit.NSWorkspace.sharedWorkspace.return_value.frontmostApplication.return_value = mock_app

        mock_ax = MagicMock()
        mock_ax.AXUIElementCreateApplication.return_value = MagicMock()
        mock_ax.AXUIElementSetAttributeValue.return_value = 0

        mock_cf = MagicMock()
        mock_cf.kCFBooleanTrue = True

        snap = FakeTargetSnapshot(detected_workspace="seattle")
        config = FakeConfig()

        with patch.dict("sys.modules", {
            "AppKit": mock_appkit,
            "ApplicationServices": mock_ax,
            "CoreFoundation": mock_cf,
        }):
            from heyvox.input.target import restore_target
            restore_target(snap, config=config)

        mock_switch.assert_called_once_with("seattle", config._profile)
        assert snap._workspace_switched is True

    @patch("heyvox.input.target._time")
    @patch("heyvox.input.target._find_window_text_fields", return_value=[])
    @patch("heyvox.input.target._activate_app")
    @patch("heyvox.input.target._switch_app_workspace")
    @patch("heyvox.input.target._detect_app_workspace")
    def test_switches_when_detection_returns_empty(
        self, mock_detect, mock_switch, mock_activate, mock_find, mock_time,
    ):
        """When workspace detection fails (empty), switch happens as fallback."""
        mock_detect.return_value = ""

        mock_appkit = MagicMock()
        mock_app = MagicMock()
        mock_app.localizedName.return_value = "Conductor"
        mock_app.processIdentifier.return_value = 1234
        mock_appkit.NSWorkspace.sharedWorkspace.return_value.frontmostApplication.return_value = mock_app

        mock_ax = MagicMock()
        mock_ax.AXUIElementCreateApplication.return_value = MagicMock()
        mock_ax.AXUIElementSetAttributeValue.return_value = 0

        mock_cf = MagicMock()
        mock_cf.kCFBooleanTrue = True

        snap = FakeTargetSnapshot(detected_workspace="seattle")
        config = FakeConfig()

        with patch.dict("sys.modules", {
            "AppKit": mock_appkit,
            "ApplicationServices": mock_ax,
            "CoreFoundation": mock_cf,
        }):
            from heyvox.input.target import restore_target
            restore_target(snap, config=config)

        mock_switch.assert_called_once_with("seattle", config._profile)
        assert snap._workspace_switched is True


class TestFocusShortcutOnlyAfterSwitch:
    """focus_shortcut should only be included in paste when workspace actually switched."""

    def test_no_focus_shortcut_when_no_switch(self):
        """When _workspace_switched is False, focus_shortcut should be empty."""
        snap = FakeTargetSnapshot(detected_workspace="seattle")
        snap._workspace_switched = False
        profile = FakeAppProfile()

        ws_switched = getattr(snap, '_workspace_switched', False)
        focus = profile.focus_shortcut if ws_switched else ""
        assert focus == ""

    def test_focus_shortcut_when_switch_happened(self):
        """When _workspace_switched is True, focus_shortcut should be set."""
        snap = FakeTargetSnapshot(detected_workspace="seattle")
        snap._workspace_switched = True
        profile = FakeAppProfile()

        ws_switched = getattr(snap, '_workspace_switched', False)
        focus = profile.focus_shortcut if ws_switched else ""
        assert focus == "l"


class TestFocusShortcutInOsascript:
    """focus_shortcut is included in the osascript keystroke block."""

    def test_focus_shortcut_prepended_to_keystrokes(self):
        """When focus_shortcut='l', Cmd+L is in the osascript before Cmd+V."""
        from heyvox.input.injection import _osascript_type_text
        mock_appkit, _ = _make_mock_appkit("test text")

        with patch.dict("sys.modules", {"AppKit": mock_appkit}):
            with patch("heyvox.input.injection.subprocess.run") as mock_run:
                with patch("heyvox.input.injection.time.sleep"):
                    with patch("heyvox.input.injection._clipboard_still_ours", return_value=True):
                        mock_run.return_value = MagicMock(returncode=0, stdout="conductor")
                        _osascript_type_text(
                            "test text",
                            app_name="conductor",
                            enter_count=1,
                            enter_delay=0.15,
                            focus_shortcut="l",
                        )

        # Find the osascript call (the one with keystroke, not the frontmost checks)
        osascript_calls = [
            c for c in mock_run.call_args_list
            if c[0] and len(c[0][0]) >= 3 and "keystroke" in str(c[0][0][2])
        ]
        assert len(osascript_calls) >= 1
        script = str(osascript_calls[0][0][0][2])
        # Cmd+L should appear BEFORE Cmd+V
        l_pos = script.find('keystroke "l" using command down')
        v_pos = script.find('keystroke "v" using command down')
        assert l_pos >= 0, "Cmd+L not found in script"
        assert v_pos >= 0, "Cmd+V not found in script"
        assert l_pos < v_pos, "Cmd+L should come before Cmd+V"

    def test_no_focus_shortcut_when_empty(self):
        """When focus_shortcut='', no Cmd+shortcut in the osascript."""
        from heyvox.input.injection import _osascript_type_text
        mock_appkit, _ = _make_mock_appkit("test text")

        with patch.dict("sys.modules", {"AppKit": mock_appkit}):
            with patch("heyvox.input.injection.subprocess.run") as mock_run:
                with patch("heyvox.input.injection.time.sleep"):
                    with patch("heyvox.input.injection._clipboard_still_ours", return_value=True):
                        mock_run.return_value = MagicMock(returncode=0, stdout="conductor")
                        _osascript_type_text(
                            "test text",
                            app_name="conductor",
                            enter_count=1,
                            focus_shortcut="",
                        )

        osascript_calls = [
            c for c in mock_run.call_args_list
            if c[0] and len(c[0][0]) >= 3 and "keystroke" in str(c[0][0][2])
        ]
        assert len(osascript_calls) >= 1
        script = str(osascript_calls[0][0][0][2])
        assert 'keystroke "l" using command down' not in script


# ---------------------------------------------------------------------------
# Helper (copied from test_injection.py)
# ---------------------------------------------------------------------------

def _make_mock_appkit(clipboard_text: str = "hello"):
    mock_pb = MagicMock()
    mock_pb.clearContents.return_value = None
    mock_pb.setString_forType_.return_value = True
    mock_pb.changeCount.return_value = 42
    mock_pb.stringForType_.return_value = clipboard_text

    mock_appkit = MagicMock()
    mock_appkit.NSPasteboard.generalPasteboard.return_value = mock_pb
    mock_appkit.NSPasteboardTypeString = "public.utf8-plain-text"
    return mock_appkit, mock_pb
