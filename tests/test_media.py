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

    @patch("heyvox.audio.media._hush_command", return_value=None)
    @patch("heyvox.audio.media._is_media_playing_native", return_value=None)
    @patch("heyvox.audio.media._browser_has_video_tab", return_value=False)
    @patch("heyvox.audio.media._test_chrome_js_access", return_value=False)
    def test_noop_when_no_session(self, mock_js, mock_video, mock_state, mock_hush):
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
    @patch("heyvox.audio.media._send_media_key", return_value=True)
    @patch("heyvox.audio.media._get_mr")
    @patch("heyvox.audio.media._is_media_playing_native", return_value=True)
    def test_uses_mediaremote_when_playing(self, mock_state, mock_mr, mock_key, mock_hush):
        mr_lib = MagicMock()
        mr_lib.MRMediaRemoteSendCommand.return_value = True
        mock_mr.return_value = mr_lib
        result = media.pause_media()
        assert result is True
        mr_lib.MRMediaRemoteSendCommand.assert_called_once_with(media._MR_PAUSE, None)
        assert open(media._PAUSE_FLAG).read() == "mr"
        mock_key.assert_not_called()

    @patch("heyvox.audio.media._hush_command", return_value=None)
    @patch("heyvox.audio.media._browser_has_video_tab", return_value=False)
    @patch("heyvox.audio.media._test_chrome_js_access", return_value=False)
    @patch("heyvox.audio.media._get_mr", return_value=None)
    @patch("heyvox.audio.media._is_media_playing_native", return_value=True)
    def test_falls_back_gracefully_when_mr_unavailable(self, mock_state, mock_mr, mock_js, mock_video, mock_hush):
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
