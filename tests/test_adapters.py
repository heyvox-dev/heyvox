"""Tests for heyvox.adapters — text injection adapters."""

from unittest.mock import patch

from heyvox.adapters.generic import GenericAdapter


class TestGenericAdapterAlwaysFocused:
    """GenericAdapter with no target_app (always-focused mode)."""

    def test_should_not_auto_send(self):
        adapter = GenericAdapter()
        assert adapter.should_auto_send() is False

    def test_enter_count_default(self):
        adapter = GenericAdapter()
        assert adapter.enter_count == 2


class TestGenericAdapterPinnedApp:
    """GenericAdapter with target_app set (pinned-app mode)."""

    def test_should_auto_send(self):
        adapter = GenericAdapter(target_app="Cursor")
        assert adapter.should_auto_send() is True

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

    @patch("heyvox.adapters.last_agent.threading.Thread")
    def test_inject_text_without_last_agent(self, mock_thread):
        from heyvox.adapters.last_agent import LastAgentAdapter
        adapter = LastAgentAdapter(agents=["Claude"])
        adapter._last_agent_name = None
        with patch("heyvox.input.injection.type_text") as mock_type, \
             patch("heyvox.input.injection.focus_app"):
            adapter.inject_text("test text")
            mock_type.assert_called_once_with("test text", app_name=None)

    @patch("heyvox.adapters.last_agent.threading.Thread")
    def test_inject_text_with_last_agent(self, mock_thread):
        from heyvox.adapters.last_agent import LastAgentAdapter
        adapter = LastAgentAdapter(agents=["Claude"])
        adapter._last_agent_name = "Claude"
        with patch("heyvox.input.injection.type_text") as mock_type, \
             patch("heyvox.input.injection.focus_app") as mock_focus, \
             patch("heyvox.adapters.last_agent.LastAgentAdapter._try_conductor_injection", return_value=False), \
             patch("heyvox.adapters.last_agent.time.sleep"):
            adapter.inject_text("test text")
            mock_focus.assert_called_once_with("Claude")
            mock_type.assert_called_once_with("test text", app_name="Claude")

    @patch("heyvox.adapters.last_agent.threading.Thread")
    def test_enter_count_configurable(self, mock_thread):
        from heyvox.adapters.last_agent import LastAgentAdapter
        adapter = LastAgentAdapter(agents=["Claude"], enter_count=3)
        assert adapter.enter_count == 3
