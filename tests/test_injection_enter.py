"""Tests for text injection and Enter keystroke targeting.

Covers Bug #2: Enter not pressed after paste (adapter/focus issues).
The Enter keystroke must target the specific app process via
`tell process "AppName"` in System Events, not rely on frontmost app.
"""

import pytest
from unittest.mock import patch, MagicMock


class TestPressEnterTargeting:
    """Verify press_enter() targets the correct application."""

    def test_enter_targets_specific_app(self, monkeypatch):
        """press_enter(app_name="Conductor") must use 'tell process' in osascript."""
        from heyvox.input import injection

        captured_scripts = []

        def mock_run(cmd, **kwargs):
            if cmd[0] == "osascript":
                captured_scripts.append(cmd[2])
            return MagicMock(returncode=0)

        monkeypatch.setattr("subprocess.run", mock_run)

        injection.press_enter(count=1, app_name="Conductor")

        assert len(captured_scripts) == 1
        script = captured_scripts[0]
        assert 'tell process "Conductor"' in script, \
            f"Expected 'tell process \"Conductor\"' in osascript, got: {script}"

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
            return MagicMock(returncode=0)

        monkeypatch.setattr("subprocess.run", mock_run)

        injection.press_enter(count=3, app_name="Cursor")

        script = captured_scripts[0]
        assert script.count("keystroke return") == 3

    def test_enter_delay_between_keystrokes(self, monkeypatch):
        """Delay between Enter keystrokes must be included in the script."""
        from heyvox.input import injection

        captured_scripts = []

        def mock_run(cmd, **kwargs):
            if cmd[0] == "osascript":
                captured_scripts.append(cmd[2])
            return MagicMock(returncode=0)

        monkeypatch.setattr("subprocess.run", mock_run)

        injection.press_enter(count=2, app_name="Conductor")

        script = captured_scripts[0]
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
        """type_text() must set clipboard before Cmd-V."""
        from heyvox.input import injection

        commands_run = []
        text_to_paste = "hello world"

        def mock_run(cmd, **kwargs):
            commands_run.append(" ".join(cmd[:3]) if len(cmd) >= 3 else " ".join(cmd))
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr("subprocess.run", mock_run)
        # Stub out clipboard verify so type_text doesn't abort early.
        # The real get_clipboard_text() calls osascript; in tests pbcopy is
        # mocked so the clipboard is never actually set.
        monkeypatch.setattr(injection, "get_clipboard_text", lambda: text_to_paste)

        injection.type_text(text_to_paste)

        # Should have at minimum: pbcopy (set clipboard) + osascript (Cmd-V paste)
        pbcopy_calls = [c for c in commands_run if "pbcopy" in c]
        osascript_calls = [c for c in commands_run if "osascript" in c]
        assert len(pbcopy_calls) >= 1, f"Expected pbcopy call, got: {commands_run}"
        assert len(osascript_calls) >= 1, f"Expected osascript paste call, got: {commands_run}"

    def test_type_text_escapes_quotes(self, monkeypatch):
        """type_text() uses pbcopy for clipboard — special chars including quotes are safe."""
        from heyvox.input import injection

        text_to_paste = 'He said "hello"'
        pbcopy_inputs = []

        original_run = __import__("subprocess").run

        def mock_run(cmd, **kwargs):
            if cmd[0] == "pbcopy":
                # Capture the raw bytes sent to pbcopy
                pbcopy_inputs.append(kwargs.get("input", b""))
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr("subprocess.run", mock_run)
        # Stub clipboard verify to return the exact text so paste proceeds
        monkeypatch.setattr(injection, "get_clipboard_text", lambda: text_to_paste)

        injection.type_text(text_to_paste)

        # pbcopy receives raw UTF-8 bytes — no escaping needed (unlike osascript)
        assert len(pbcopy_inputs) >= 1, "pbcopy must be called to set clipboard"
        assert text_to_paste.encode("utf-8") in pbcopy_inputs, \
            f"pbcopy must receive the verbatim text bytes, got: {pbcopy_inputs}"
