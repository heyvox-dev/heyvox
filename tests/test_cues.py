"""Tests for heyvox.audio.cues — audio cue playback and suppression."""

import os
import time
import wave
from unittest.mock import patch

import pytest
import soundfile

from heyvox.audio.cues import get_cues_dir, audio_cue, is_suppressed
import heyvox.audio.cues as cues_module


def _write_valid_cue_wav(path: str, frames: int = 100, samplerate: int = 16000) -> None:
    """Write a minimal valid mono 16-bit PCM WAV to `path`.

    Named with a `.aiff` extension by callers to match audio_cue()'s path
    construction (f"{name}.aiff") -- soundfile.read() sniffs the file header
    to determine container format, not the extension, so WAV-formatted bytes
    behind a `.aiff` filename decode correctly (verified manually against
    soundfile 0.13.1 before adopting this approach; see plan Task 2 note).
    """
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(samplerate)
        wf.writeframes(b"\x00\x00" * frames)


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
    """Audio cue playback via sounddevice (cached), with afplay fallback."""

    def setup_method(self):
        # Prevent cached entries from one test leaking into the next.
        cues_module._cue_cache.clear()

    @pytest.fixture(autouse=True)
    def _default_not_muted(self):
        # Isolate from real is_muted() state (tts._muted global, HERALD_MUTE_FLAG
        # file) so these tests are deterministic; muted-path tests override this.
        with patch("heyvox.audio.tts.is_muted", return_value=False):
            yield

    @patch("sounddevice.play")
    @patch("heyvox.audio.cues.subprocess.run")
    @patch("heyvox.audio.cues.subprocess.Popen")
    def test_plays_existing_cue_via_sounddevice(self, mock_popen, mock_run, mock_play, tmp_path):
        # afconvert (DEF-207 resample) reports failure -> falls back to the
        # file's raw rate, keeping this test focused on the sounddevice path.
        mock_run.return_value.returncode = 1
        cue_file = tmp_path / "listening.aiff"
        _write_valid_cue_wav(str(cue_file))

        audio_cue("listening", str(tmp_path))

        mock_play.assert_called_once()
        mock_popen.assert_not_called()

    @patch("heyvox.audio.cues.subprocess.Popen")
    def test_skips_missing_cue(self, mock_popen, tmp_path):
        audio_cue("nonexistent", str(tmp_path))
        mock_popen.assert_not_called()

    @patch("sounddevice.play")
    @patch("heyvox.audio.cues.subprocess.Popen")
    def test_sets_suppression_window(self, mock_popen, mock_play, tmp_path):
        cue_file = tmp_path / "ok.aiff"
        _write_valid_cue_wav(str(cue_file))
        before = time.time()
        audio_cue("ok", str(tmp_path))
        # Suppression should be set ~1.5s into the future
        assert cues_module._cue_suppress_until > before + 1.0

    @patch("sounddevice.play")
    @patch("soundfile.read", wraps=soundfile.read)
    def test_cue_cache_reuse(self, mock_read, mock_play, tmp_path):
        cue_file = tmp_path / "listening.aiff"
        _write_valid_cue_wav(str(cue_file))

        audio_cue("listening", str(tmp_path))
        audio_cue("listening", str(tmp_path))

        mock_read.assert_called_once()
        assert mock_play.call_count == 2

    @patch("sounddevice.play")
    @patch("sounddevice.query_devices")
    @patch("heyvox.audio.cues._resolve_sounddevice_output_index", return_value=7)
    def test_def207_resamples_cue_to_device_rate(self, mock_resolve, mock_query, mock_play, tmp_path):
        """DEF-207: cue files are recorded at 22050Hz-ish; feeding that raw
        into a device running at a different native rate (e.g. built-in
        speakers at 48000Hz) crackled. Must resample to the resolved
        device's rate (via the real afconvert) before playing."""
        mock_query.return_value = {"default_samplerate": 48000.0}
        cue_file = tmp_path / "listening.aiff"
        _write_valid_cue_wav(str(cue_file), samplerate=16000)

        audio_cue("listening", str(tmp_path))

        mock_play.assert_called_once()
        assert mock_play.call_args[0][1] == 48000
        assert mock_play.call_args.kwargs["device"] == 7

    @patch("sounddevice.play")
    @patch("sounddevice.query_devices")
    @patch("heyvox.audio.cues._resolve_sounddevice_output_index", return_value=7)
    def test_def207_cache_keyed_by_target_rate(self, mock_resolve, mock_query, mock_play, tmp_path):
        """Switching output device (different native rate) must not reuse a
        cache entry that was resampled for a different rate."""
        cue_file = tmp_path / "listening.aiff"
        _write_valid_cue_wav(str(cue_file), samplerate=16000)

        mock_query.return_value = {"default_samplerate": 48000.0}
        audio_cue("listening", str(tmp_path))
        mock_query.return_value = {"default_samplerate": 44100.0}
        audio_cue("listening", str(tmp_path))

        assert mock_play.call_count == 2
        rates_played = [c[0][1] for c in mock_play.call_args_list]
        assert rates_played == [48000, 44100]

    @patch("sounddevice.play", side_effect=RuntimeError("device busy"))
    @patch("heyvox.audio.cues.subprocess.run")
    @patch("heyvox.audio.cues.subprocess.Popen")
    def test_afplay_fallback_on_sounddevice_failure(self, mock_popen, mock_run, mock_play, tmp_path):
        # afconvert (DEF-207 resample) reports failure -> falls back to the
        # file's raw rate; sounddevice.play then fails too -> afplay fallback.
        mock_run.return_value.returncode = 1
        cue_file = tmp_path / "listening.aiff"
        _write_valid_cue_wav(str(cue_file))

        audio_cue("listening", str(tmp_path))

        mock_popen.assert_called_once()
        call_args = mock_popen.call_args
        assert call_args[0][0][0] == "afplay"
        assert str(cue_file) in call_args[0][0][1]

    @patch("heyvox.audio.tts.is_muted", return_value=True)
    @patch("sounddevice.play")
    @patch("heyvox.audio.cues.subprocess.Popen")
    def test_noop_when_muted(self, mock_popen, mock_play, mock_muted, tmp_path):
        """DEF-233: cues must respect the same mute state Herald TTS does."""
        cue_file = tmp_path / "listening.aiff"
        _write_valid_cue_wav(str(cue_file))

        audio_cue("listening", str(tmp_path))

        mock_play.assert_not_called()
        mock_popen.assert_not_called()

    @patch("heyvox.audio.tts.is_muted")
    def test_mute_check_skips_system_mute(self, mock_muted, tmp_path):
        """The wake-word cue path must not pay for the osascript system-mute
        check (WW_LATENCY-critical) — only the fast in-memory/file flags."""
        mock_muted.return_value = True
        audio_cue("listening", str(tmp_path))
        mock_muted.assert_called_once_with(check_system=False)


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
