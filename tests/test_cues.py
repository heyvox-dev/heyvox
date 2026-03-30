"""Tests for heyvox.audio.cues — audio cue playback and suppression."""

import os
import time
import pytest
from unittest.mock import patch, MagicMock

from heyvox.audio.cues import get_cues_dir, audio_cue, is_suppressed, _cue_suppress_until
import heyvox.audio.cues as cues_module


class TestGetCuesDir:
    """Cue directory resolution."""

    def test_config_dir_used_when_exists(self, tmp_path):
        cues_dir = str(tmp_path / "my_cues")
        os.makedirs(cues_dir)
        assert get_cues_dir(cues_dir) == cues_dir

    def test_config_dir_ignored_when_missing(self):
        result = get_cues_dir("/nonexistent/cues/dir")
        # Falls back to package-relative path
        assert "cues" in result

    def test_empty_config_uses_package_path(self):
        result = get_cues_dir("")
        assert "cues" in result

    def test_package_path_is_absolute(self):
        result = get_cues_dir("")
        assert os.path.isabs(result)


class TestAudioCue:
    """Audio cue playback via afplay."""

    @patch("heyvox.audio.cues.subprocess.Popen")
    def test_plays_existing_cue(self, mock_popen, tmp_path):
        cue_file = tmp_path / "listening.aiff"
        cue_file.touch()
        audio_cue("listening", str(tmp_path))
        mock_popen.assert_called_once()
        call_args = mock_popen.call_args
        assert call_args[0][0][0] == "afplay"
        assert str(cue_file) in call_args[0][0][1]

    @patch("heyvox.audio.cues.subprocess.Popen")
    def test_skips_missing_cue(self, mock_popen, tmp_path):
        audio_cue("nonexistent", str(tmp_path))
        mock_popen.assert_not_called()

    @patch("heyvox.audio.cues.subprocess.Popen")
    def test_sets_suppression_window(self, mock_popen, tmp_path):
        cue_file = tmp_path / "ok.aiff"
        cue_file.touch()
        before = time.time()
        audio_cue("ok", str(tmp_path))
        # Suppression should be set ~1.5s into the future
        assert cues_module._cue_suppress_until > before + 1.0


class TestIsSuppressed:
    """Wake word suppression timing."""

    def test_not_suppressed_by_default(self):
        cues_module._cue_suppress_until = 0.0
        assert is_suppressed() is False

    def test_suppressed_when_in_window(self):
        cues_module._cue_suppress_until = time.time() + 10.0
        assert is_suppressed() is True

    def test_not_suppressed_after_window(self):
        cues_module._cue_suppress_until = time.time() - 1.0
        assert is_suppressed() is False
