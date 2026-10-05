"""Silent failure paths made visible / live (T-004, T-008)."""

from __future__ import annotations

from unittest.mock import patch

from heyvox.adapters.last_agent import LastAgentAdapter
from heyvox.audio import cues
from heyvox.input.frontmost import FrontmostApp


def _adapter(agents=("conductor", "orca")):
    adapter = LastAgentAdapter.__new__(LastAgentAdapter)  # no observer thread
    import threading
    adapter._agents = list(agents)
    adapter._lock = threading.Lock()
    adapter._last_agent_name = None
    return adapter


class TestLastAgentTracksLiveFrontmost:
    """T-008 / DEF-260: last_agent_name stayed None because NSWorkspace's value
    was frozen at listener start."""

    def test_tracks_agent_from_live_value(self):
        adapter = _adapter()
        live = FrontmostApp(pid=1, bundle_id="com.stablyai.orca", name="Orca")
        with patch("heyvox.input.frontmost.frontmost_app", return_value=live), \
             patch("heyvox.adapters.last_agent._safe_stderr"):
            first = adapter._track_once(True)
        assert adapter.last_agent_name == "Orca"
        assert first is False

    def test_ignores_non_agent_app_and_keeps_previous(self):
        adapter = _adapter()
        adapter._last_agent_name = "Orca"
        live = FrontmostApp(pid=2, bundle_id="com.apple.Safari", name="Safari")
        with patch("heyvox.input.frontmost.frontmost_app", return_value=live):
            first = adapter._track_once(True)
        assert adapter.last_agent_name == "Orca"
        assert first is True

    def test_no_frontmost_app_is_a_noop(self):
        adapter = _adapter()
        with patch("heyvox.input.frontmost.frontmost_app", return_value=None):
            assert adapter._track_once(True) is True
        assert adapter.last_agent_name is None


class TestCueSystemMuteWarning:
    """T-004: a cue dispatched while the macOS output is muted warns (rate-limited)."""

    def setup_method(self):
        cues._last_system_mute_warn = 0.0

    def test_warns_when_system_muted(self):
        with patch("heyvox.herald.coreaudio.is_system_muted", return_value=True), \
             patch.object(cues, "_log") as log:
            cues._warn_if_system_muted("listening")
        assert any("WARNING system output is muted" in c.args[0] for c in log.call_args_list)

    def test_silent_when_not_muted(self):
        with patch("heyvox.herald.coreaudio.is_system_muted", return_value=False), \
             patch.object(cues, "_log") as log:
            cues._warn_if_system_muted("listening")
        log.assert_not_called()

    def test_rate_limited(self):
        with patch("heyvox.herald.coreaudio.is_system_muted", return_value=True), \
             patch.object(cues, "_log") as log:
            cues._warn_if_system_muted("a")
            cues._warn_if_system_muted("b")
        assert log.call_count == 1

    def test_check_failure_never_raises(self):
        with patch("heyvox.herald.coreaudio.is_system_muted", side_effect=RuntimeError("x")):
            cues._warn_if_system_muted("listening")  # must not raise
