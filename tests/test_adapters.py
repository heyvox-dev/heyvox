"""Tests for heyvox.adapters — text injection adapters."""

import pytest
from unittest.mock import patch, MagicMock

from heyvox.adapters.generic import GenericAdapter


class TestGenericAdapterAlwaysFocused:
    """GenericAdapter with no target_app (always-focused mode)."""

    def test_should_not_auto_send(self):
        adapter = GenericAdapter()
        assert adapter.should_auto_send() is False

    def test_enter_count_default(self):
        adapter = GenericAdapter()
        assert adapter.enter_count == 2

    @patch("heyvox.adapters.generic.type_text")
    def test_inject_text_pastes_directly(self, mock_type):
        adapter = GenericAdapter()
        adapter.inject_text("hello world")
        mock_type.assert_called_once_with("hello world")

    @patch("heyvox.adapters.generic.type_text")
    def test_inject_text_no_focus_call(self, mock_type):
        with patch("heyvox.input.injection.focus_app") as mock_focus:
            adapter = GenericAdapter()
            adapter.inject_text("test")
            mock_focus.assert_not_called()


class TestGenericAdapterPinnedApp:
    """GenericAdapter with target_app set (pinned-app mode)."""

    def test_should_auto_send(self):
        adapter = GenericAdapter(target_app="Cursor")
        assert adapter.should_auto_send() is True

    @patch("heyvox.adapters.generic.type_text")
    @patch("heyvox.adapters.generic.time.sleep")
    def test_inject_focuses_app_first(self, mock_sleep, mock_type):
        with patch("heyvox.input.injection.focus_app") as mock_focus:
            adapter = GenericAdapter(target_app="Cursor")
            adapter.inject_text("hello")
            mock_focus.assert_called_once_with("Cursor")
            mock_type.assert_called_once_with("hello")

    def test_custom_enter_count(self):
        adapter = GenericAdapter(target_app="Claude", enter_count=1)
        assert adapter.enter_count == 1


class TestLastAgentAdapter:
    """LastAgentAdapter — tracks frontmost agent app."""

    @patch("heyvox.adapters.last_agent.threading.Thread")
    def test_should_always_auto_send(self, mock_thread):
        from heyvox.adapters.last_agent import LastAgentAdapter
        adapter = LastAgentAdapter(agents=["Claude", "Cursor"])
        assert adapter.should_auto_send() is True

    @patch("heyvox.adapters.last_agent.threading.Thread")
    def test_agents_stored_lowercase(self, mock_thread):
        from heyvox.adapters.last_agent import LastAgentAdapter
        adapter = LastAgentAdapter(agents=["Claude", "CURSOR"])
        assert adapter._agents == ["claude", "cursor"]

    @patch("heyvox.adapters.last_agent.type_text")
    @patch("heyvox.adapters.last_agent.threading.Thread")
    def test_inject_text_without_last_agent(self, mock_thread, mock_type):
        from heyvox.adapters.last_agent import LastAgentAdapter
        adapter = LastAgentAdapter(agents=["Claude"])
        adapter._last_agent_name = None
        adapter.inject_text("test text")
        mock_type.assert_called_once_with("test text")

    @patch("heyvox.adapters.last_agent.focus_input")
    @patch("heyvox.adapters.last_agent.focus_app")
    @patch("heyvox.adapters.last_agent.type_text")
    @patch("heyvox.adapters.last_agent.time.sleep")
    @patch("heyvox.adapters.last_agent.threading.Thread")
    def test_inject_text_with_last_agent(self, mock_thread, mock_sleep, mock_type, mock_focus, mock_focus_input):
        from heyvox.adapters.last_agent import LastAgentAdapter
        adapter = LastAgentAdapter(agents=["Claude"])
        adapter._last_agent_name = "Claude"
        adapter.inject_text("test text")
        mock_focus.assert_called_once_with("Claude")
        mock_focus_input.assert_called_once_with("Claude")
        mock_type.assert_called_once_with("test text")

    @patch("heyvox.adapters.last_agent.threading.Thread")
    def test_enter_count_configurable(self, mock_thread):
        from heyvox.adapters.last_agent import LastAgentAdapter
        adapter = LastAgentAdapter(agents=["Claude"], enter_count=3)
        assert adapter.enter_count == 3
