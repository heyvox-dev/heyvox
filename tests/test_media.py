"""Tests for heyvox.audio.media — media pause/resume control."""

import os
import pytest
from unittest.mock import patch, MagicMock

import heyvox.audio.media as media


@pytest.fixture(autouse=True)
def clean_flags(tmp_path, monkeypatch):
    """Use tmp_path for flag files to avoid polluting /tmp."""
    flag = str(tmp_path / "heyvox-media-paused-rec")
    monkeypatch.setattr(media, "_PAUSE_FLAG", flag)
    monkeypatch.setattr(media, "_mr_lib", None)
    # Never drive real players (VLC, Music, ...) from tests: tests that want
    # players patch _scriptable_players themselves.
    monkeypatch.setattr(media, "_scriptable_players", lambda: [])
    yield
    try:
        os.unlink(flag)
    except FileNotFoundError:
        pass


class TestIsMediaPlaying:
    """_is_media_playing_native() wraps nowplaying-cli."""

    @patch("heyvox.audio.media.subprocess.run")
    def test_returns_true_when_playing(self, mock_run):
        mock_run.return_value = MagicMock(stdout="1\n")
        assert media._is_media_playing_native() is True

    @patch("heyvox.audio.media.subprocess.run")
    def test_returns_false_when_paused(self, mock_run):
        mock_run.return_value = MagicMock(stdout="0\n")
        assert media._is_media_playing_native() is False

    @patch("heyvox.audio.media.subprocess.run")
    def test_returns_none_when_null(self, mock_run):
        mock_run.return_value = MagicMock(stdout="null\n")
        assert media._is_media_playing_native() is None

    @patch("heyvox.audio.media.subprocess.run")
    def test_returns_none_when_empty(self, mock_run):
        mock_run.return_value = MagicMock(stdout="\n")
        assert media._is_media_playing_native() is None

    @patch("heyvox.audio.media.subprocess.run", side_effect=FileNotFoundError)
    def test_returns_none_when_cli_missing(self, mock_run):
        assert media._is_media_playing_native() is None

    @patch("heyvox.audio.media.subprocess.run", side_effect=media.subprocess.TimeoutExpired(cmd="", timeout=0.5))
    def test_returns_none_on_timeout(self, mock_run):
        assert media._is_media_playing_native() is None


class TestPauseMedia:
    """pause_media() should create flag file and use correct method."""

    def setup_method(self):
        # Reset no-media cache from previous tests
        media._no_media_cache_until = 0.0

    @patch("heyvox.audio.media._hush_command", return_value=None)
    @patch("heyvox.audio.media._is_media_playing_native", return_value=None)
    def test_noop_when_no_session(self, mock_state, mock_hush):
        """No native session and no browser media → returns False, no flag created."""
        result = media.pause_media()
        assert result is False
        assert not os.path.exists(media._PAUSE_FLAG)

    @patch("heyvox.audio.media._hush_command", return_value=None)
    @patch("heyvox.audio.media._is_media_playing_native", return_value=False)
    def test_noop_when_already_paused_by_user(self, mock_state, mock_hush):
        result = media.pause_media()
        assert result is False
        assert not os.path.exists(media._PAUSE_FLAG)

    def test_noop_when_already_paused_by_us(self):
        # Create the flag to simulate we already paused
        with open(media._PAUSE_FLAG, "w") as f:
            f.write("mr")
        result = media.pause_media()
        assert result is True  # Returns True but doesn't re-pause

    @patch("heyvox.audio.media._hush_command", return_value=None)
    @patch("heyvox.audio.media._get_mr")
    @patch("heyvox.audio.media._is_media_playing_native", return_value=True)
    def test_uses_mediaremote_when_playing(self, mock_state, mock_mr, mock_hush):
        mr_lib = MagicMock()
        mr_lib.MRMediaRemoteSendCommand.return_value = True
        mock_mr.return_value = mr_lib
        result = media.pause_media()
        assert result is True
        mr_lib.MRMediaRemoteSendCommand.assert_called_once_with(media._MR_PAUSE, None)
        assert open(media._PAUSE_FLAG).read() == "mr"

    @patch("heyvox.audio.media._hush_command", return_value=None)
    @patch("heyvox.audio.media._get_mr", return_value=None)
    @patch("heyvox.audio.media._is_media_playing_native", return_value=True)
    def test_falls_back_gracefully_when_mr_unavailable(self, mock_state, mock_mr, mock_hush):
        """When MediaRemote unavailable and no browser video, pause returns False."""
        result = media.pause_media()
        # MediaRemote unavailable + no browser media → cannot pause → False
        assert result is False


class TestResumeMedia:
    """resume_media() should only resume if we paused, respecting other flags."""

    def test_noop_when_no_flag(self):
        assert media.resume_media() is False

    @patch("heyvox.audio.media.glob.glob", return_value=[])
    @patch("heyvox.audio.media._get_mr")
    @patch("heyvox.audio.media.time.sleep")
    def test_resumes_via_mediaremote(self, mock_sleep, mock_mr, mock_glob):
        """resume_media() with a 'mr' flag resumes via MediaRemote."""
        mr_lib = MagicMock()
        mr_lib.MRMediaRemoteSendCommand.return_value = True
        mock_mr.return_value = mr_lib
        with open(media._PAUSE_FLAG, "w") as f:
            f.write("mr")
        result = media.resume_media()
        assert result is True
        mr_lib.MRMediaRemoteSendCommand.assert_called_once_with(media._MR_PLAY, None)
        mock_sleep.assert_called_with(media.RESUME_DELAY)
        assert not os.path.exists(media._PAUSE_FLAG)

    @patch("heyvox.audio.media.glob.glob")
    @patch("heyvox.audio.media.time.sleep")
    def test_skips_resume_when_other_flags_exist(self, mock_sleep, mock_glob):
        with open(media._PAUSE_FLAG, "w") as f:
            f.write("key")
        mock_glob.return_value = ["/tmp/heyvox-media-paused-orch"]
        result = media.resume_media()
        assert result is False
        assert not os.path.exists(media._PAUSE_FLAG)  # Our flag still removed


class TestFlagLifecycle:
    """Flag file creation and cleanup."""

    @patch("heyvox.audio.media._hush_command", return_value=None)
    @patch("heyvox.audio.media._get_mr")
    @patch("heyvox.audio.media._is_media_playing_native", return_value=True)
    def test_pause_creates_flag(self, mock_state, mock_mr, mock_hush):
        mr_lib = MagicMock()
        mr_lib.MRMediaRemoteSendCommand.return_value = True
        mock_mr.return_value = mr_lib
        assert not os.path.exists(media._PAUSE_FLAG)
        media.pause_media()
        assert os.path.exists(media._PAUSE_FLAG)

    @patch("heyvox.audio.media.glob.glob", return_value=[])
    @patch("heyvox.audio.media._get_mr")
    @patch("heyvox.audio.media.time.sleep")
    def test_resume_removes_flag(self, mock_sleep, mock_mr, mock_glob):
        mr_lib = MagicMock()
        mr_lib.MRMediaRemoteSendCommand.return_value = True
        mock_mr.return_value = mr_lib
        with open(media._PAUSE_FLAG, "w") as f:
            f.write("mr")
        media.resume_media()
        assert not os.path.exists(media._PAUSE_FLAG)


class _Proc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


class TestScriptablePlayers:
    """AppleScript tier for players MediaRemote cannot see (DEF-264)."""

    @pytest.fixture
    def vlc(self, monkeypatch):
        from heyvox.config import MediaPlayerConfig
        player = MediaPlayerConfig(
            name="VLC", bundle_id="org.videolan.vlc",
            is_playing="playing", pause="play", resume="play",
        )
        monkeypatch.setattr(media, "_scriptable_players", lambda: [player])
        monkeypatch.setattr(media, "_no_media_cache_until", 0.0)
        monkeypatch.setattr(media, "_hush_command", lambda *a, **k: None)
        monkeypatch.setattr(media, "_is_media_playing_native", lambda: None)
        return player

    @staticmethod
    def _osa(monkeypatch, state):
        """Fake osascript: state probe answers `state`, commands are recorded."""
        calls = []

        def fake(script):
            calls.append(script)
            if "is running" in script:
                return _Proc(stdout=state + "\n")
            return _Proc()
        monkeypatch.setattr(media, "_run_osa", fake)
        return calls

    def test_pauses_playing_player_and_flags_it(self, vlc, monkeypatch):
        calls = self._osa(monkeypatch, "true")
        assert media.pause_media() is True
        assert open(media._PAUSE_FLAG).read() == "app:org.videolan.vlc"
        assert calls[-1] == 'tell application id "org.videolan.vlc" to play'

    def test_does_not_touch_player_paused_by_user(self, vlc, monkeypatch):
        calls = self._osa(monkeypatch, "false")
        assert media.pause_media() is False
        assert len(calls) == 1  # probe only, no toggle command
        assert not os.path.exists(media._PAUSE_FLAG)

    def test_closed_player_is_not_launched(self, vlc, monkeypatch):
        calls = self._osa(monkeypatch, "off")
        assert media.pause_media() is False
        assert all("is running" in c for c in calls)

    def test_probe_script_checks_running_before_telling_app(self, vlc, monkeypatch):
        calls = self._osa(monkeypatch, "off")
        media.pause_media()
        script = calls[0]
        assert script.index("is running") < script.index("tell application id")

    def test_automation_denied_warns_once_and_returns_none(self, vlc, monkeypatch):
        monkeypatch.setattr(media, "_osa_denied_warned", set())
        logs = []
        monkeypatch.setattr(media, "_log", logs.append)
        monkeypatch.setattr(
            media, "_run_osa",
            lambda s: _Proc(stderr="execution error: Not authorized (-1743)", returncode=1),
        )
        assert media._player_state(vlc) is None
        assert media._player_state(vlc) is None
        assert sum("Automation permission denied" in m for m in logs) == 1

    @patch("heyvox.audio.media.glob.glob", return_value=[])
    @patch("heyvox.audio.media.time.sleep")
    def test_resume_plays_when_still_paused(self, mock_sleep, mock_glob, vlc, monkeypatch):
        calls = self._osa(monkeypatch, "false")
        with open(media._PAUSE_FLAG, "w") as f:
            f.write("app:org.videolan.vlc")
        assert media.resume_media() is True
        assert calls[-1] == 'tell application id "org.videolan.vlc" to play'
        assert not os.path.exists(media._PAUSE_FLAG)

    @patch("heyvox.audio.media.glob.glob", return_value=[])
    @patch("heyvox.audio.media.time.sleep")
    def test_resume_does_not_toggle_player_playing_again(self, mock_sleep, mock_glob, vlc, monkeypatch):
        """VLC's play toggles: resuming an already-playing player would pause it."""
        calls = self._osa(monkeypatch, "true")
        with open(media._PAUSE_FLAG, "w") as f:
            f.write("app:org.videolan.vlc")
        assert media.resume_media() is False
        assert len(calls) == 1

    @patch("heyvox.audio.media.glob.glob", return_value=[])
    @patch("heyvox.audio.media.time.sleep")
    def test_resume_handles_hush_and_player_together(self, mock_sleep, mock_glob, vlc, monkeypatch):
        calls = self._osa(monkeypatch, "false")
        hush = []
        monkeypatch.setattr(media, "_hush_command", lambda *a, **k: hush.append(a) or {"state": "playing"})
        with open(media._PAUSE_FLAG, "w") as f:
            f.write("hush,app:org.videolan.vlc")
        assert media.resume_media() is True
        assert hush and calls[-1].endswith("to play")

    def test_pause_combines_hush_and_player(self, vlc, monkeypatch):
        self._osa(monkeypatch, "true")
        monkeypatch.setattr(media, "_hush_command", lambda *a, **k: {"pausedCount": 1, "tabs": []})
        assert media.pause_media() is True
        assert open(media._PAUSE_FLAG).read() == "hush,app:org.videolan.vlc"


class TestMediaPlayerConfig:
    def test_defaults_include_vlc_and_quicktime(self):
        from heyvox.config import TTSConfig
        names = {p.name for p in TTSConfig().media_players}
        assert {"VLC", "QuickTime Player", "Music", "Spotify"} <= names

    def test_user_entry_overrides_default_by_name(self):
        from heyvox.config import TTSConfig
        cfg = TTSConfig(media_players=[{"name": "vlc", "enabled": False}])
        vlc = [p for p in cfg.media_players if p.name.lower() == "vlc"]
        assert len(vlc) == 1 and vlc[0].enabled is False
