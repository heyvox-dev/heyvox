"""Tests for text injection and Enter keystroke targeting.

Covers Bug #2: Enter not pressed after paste (adapter/focus issues).
The Enter keystroke must target the specific app process via
`tell process "AppName"` in System Events, not rely on frontmost app.
"""

from unittest.mock import MagicMock


class TestPressEnterTargeting:
    """Verify press_enter() targets the correct application."""

    def test_enter_targets_specific_app(self, monkeypatch):
        """press_enter(app_name="Conductor") must use 'tell process' in osascript."""
        from heyvox.input import injection

        captured_scripts = []

        def mock_run(cmd, **kwargs):
            if cmd[0] == "osascript":
                captured_scripts.append(cmd[2])
            mock_result = MagicMock(returncode=0)
            mock_result.stdout.strip.return_value = "conductor"
            return mock_result

        monkeypatch.setattr("subprocess.run", mock_run)

        injection.press_enter(count=1, app_name="Conductor")

        # Find the actual Enter script (contains "keystroke return")
        enter_scripts = [s for s in captured_scripts if "keystroke return" in s]
        assert len(enter_scripts) == 1, f"Expected 1 Enter script, got {len(enter_scripts)}"
        script = enter_scripts[0]
        assert 'tell process "conductor"' in script, \
            f"Expected 'tell process \"conductor\"' in osascript, got: {script}"

    def test_enter_uses_frontmost_when_no_app(self, monkeypatch):
        """press_enter() without app_name must NOT use 'tell process'."""
        from heyvox.input import injection

        captured_scripts = []

        def mock_run(cmd, **kwargs):
            if cmd[0] == "osascript":
                captured_scripts.append(cmd[2])
            return MagicMock(returncode=0)

        monkeypatch.setattr("subprocess.run", mock_run)

        injection.press_enter(count=1)

        assert len(captured_scripts) == 1
        script = captured_scripts[0]
        assert "tell process" not in script, \
            f"Expected no 'tell process' in osascript when no app, got: {script}"

    def test_enter_count_respected(self, monkeypatch):
        """press_enter(count=2) must emit 2 keystroke return commands."""
        from heyvox.input import injection

        captured_scripts = []

        def mock_run(cmd, **kwargs):
            if cmd[0] == "osascript":
                captured_scripts.append(cmd[2])
            mock_result = MagicMock(returncode=0)
            mock_result.stdout.strip.return_value = "Cursor"
            return mock_result

        monkeypatch.setattr("subprocess.run", mock_run)

        injection.press_enter(count=3, app_name="Cursor")

        # Find the actual Enter script (contains "keystroke return")
        enter_scripts = [s for s in captured_scripts if "keystroke return" in s]
        assert len(enter_scripts) == 1
        script = enter_scripts[0]
        assert script.count("keystroke return") == 3

    def test_enter_delay_between_keystrokes(self, monkeypatch):
        """Delay between Enter keystrokes must be included in the script."""
        from heyvox.input import injection

        captured_scripts = []

        def mock_run(cmd, **kwargs):
            if cmd[0] == "osascript":
                captured_scripts.append(cmd[2])
            mock_result = MagicMock(returncode=0)
            mock_result.stdout.strip.return_value = "conductor"
            return mock_result

        monkeypatch.setattr("subprocess.run", mock_run)

        injection.press_enter(count=2, app_name="Conductor")

        # Find the actual Enter script (contains "keystroke return")
        enter_scripts = [s for s in captured_scripts if "keystroke return" in s]
        assert len(enter_scripts) == 1
        script = enter_scripts[0]
        assert "delay" in script, "Must have delay between keystrokes"


class TestLastAgentAdapterEnter:
    """Verify LastAgentAdapter passes app name to press_enter."""

    def test_adapter_tracks_agent_name(self):
        """LastAgentAdapter must store the agent name from AppKit polling."""
        from heyvox.adapters.last_agent import LastAgentAdapter

        # Create adapter but don't start polling (it needs AppKit)
        adapter = LastAgentAdapter.__new__(LastAgentAdapter)
        adapter._agents = ["conductor"]
        adapter._enter_count = 2
        adapter._last_agent_name = None

        # Simulate what the poll thread does
        name = "Conductor"
        name_lower = name.lower()
        for agent in adapter._agents:
            if agent in name_lower:
                adapter._last_agent_name = name
                break

        assert adapter._last_agent_name == "Conductor"

    def test_adapter_should_auto_send(self):
        """LastAgentAdapter.should_auto_send() must return True."""
        from heyvox.adapters.last_agent import LastAgentAdapter

        adapter = LastAgentAdapter.__new__(LastAgentAdapter)
        adapter._enter_count = 2
        adapter._last_injected_via_conductor = False  # set by __init__, required for should_auto_send()
        adapter._last_agent_name = None  # set by __init__, used in should_auto_send() log output
        assert adapter.should_auto_send() is True


class TestGenericAdapterEnter:
    """Verify GenericAdapter behavior for comparison."""

    def test_generic_adapter_no_auto_send_by_default(self):
        """GenericAdapter.should_auto_send() returns False (no target app = no Enter)."""
        from heyvox.adapters.generic import GenericAdapter

        adapter = GenericAdapter(enter_count=2)
        # GenericAdapter without target_app doesn't auto-send — it just pastes
        assert adapter.should_auto_send() is False


class TestTypeText:
    """Verify clipboard-based text injection."""

    def test_type_text_sets_clipboard(self, monkeypatch):
        """type_text() must set clipboard via NSPasteboard before Cmd-V.

        NSPasteboard replaced pbcopy in Plan 01 (PASTE-01). The mock patches
        AppKit.NSPasteboard and verifies the write call was made, then the
        osascript Cmd-V is also invoked.
        """
        from heyvox.input import injection
        import sys

        text_to_paste = "hello world"

        mock_pb = MagicMock()
        mock_pb.clearContents.return_value = None
        mock_pb.setString_forType_.return_value = True
        mock_pb.changeCount.return_value = 42
        mock_pb.stringForType_.return_value = text_to_paste

        mock_appkit = MagicMock()
        mock_appkit.NSPasteboard.generalPasteboard.return_value = mock_pb
        mock_appkit.NSPasteboardTypeString = "public.utf8-plain-text"

        osascript_calls = []

        def mock_run(cmd, **kwargs):
            if cmd[0] == "osascript":
                osascript_calls.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr=b"")

        monkeypatch.setattr("subprocess.run", mock_run)
        monkeypatch.setattr(injection, "time", MagicMock(sleep=lambda s: None))

        with __import__("unittest.mock", fromlist=["patch"]).patch.dict(
            sys.modules, {"AppKit": mock_appkit}
        ):
            injection.type_text(text_to_paste)

        # NSPasteboard write must have been called
        mock_pb.setString_forType_.assert_called_once_with(text_to_paste, "public.utf8-plain-text")
        # osascript Cmd-V must have been called
        assert len(osascript_calls) >= 1, f"Expected osascript paste call, got: {osascript_calls}"

    def test_type_text_special_chars(self, monkeypatch):
        """type_text() via NSPasteboard handles special chars including quotes safely."""
        from heyvox.input import injection
        import sys

        text_to_paste = 'He said "hello"'

        mock_pb = MagicMock()
        mock_pb.clearContents.return_value = None
        mock_pb.setString_forType_.return_value = True
        mock_pb.changeCount.return_value = 42
        mock_pb.stringForType_.return_value = text_to_paste

        mock_appkit = MagicMock()
        mock_appkit.NSPasteboard.generalPasteboard.return_value = mock_pb
        mock_appkit.NSPasteboardTypeString = "public.utf8-plain-text"

        def mock_run(cmd, **kwargs):
            return MagicMock(returncode=0, stdout="", stderr=b"")

        monkeypatch.setattr("subprocess.run", mock_run)
        monkeypatch.setattr(injection, "time", MagicMock(sleep=lambda s: None))

        with __import__("unittest.mock", fromlist=["patch"]).patch.dict(
            sys.modules, {"AppKit": mock_appkit}
        ):
            injection.type_text(text_to_paste)

        # NSPasteboard receives the raw string — no escaping needed
        mock_pb.setString_forType_.assert_called_once_with(text_to_paste, "public.utf8-plain-text")
