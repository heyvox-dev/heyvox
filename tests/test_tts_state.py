"""
Tests for TTS state management: mute, verbosity, style, system mute.

Covers bug-audit patterns:
- State desync between in-memory flags and file flags
- Cross-process file sync for verbosity/style
- System mute detection via osascript
- Stale flag cleanup on startup
"""

import os
import threading
import unittest
from unittest.mock import patch, MagicMock

from heyvox.audio import tts
from heyvox.constants import (
    CLAUDE_TTS_MUTE_FLAG,
    HERALD_MUTE_FLAG,
    VERBOSITY_FILE,
    TTS_STYLE_FILE,
)


class TestSystemMuteDetection(unittest.TestCase):
    """_is_system_muted() should correctly parse osascript output."""

    @patch("heyvox.audio.tts.subprocess.run")
    def test_system_muted_true(self, mock_run):
        mock_run.return_value = MagicMock(stdout="true\n", returncode=0)
        assert tts._is_system_muted() is True

    @patch("heyvox.audio.tts.subprocess.run")
    def test_system_not_muted(self, mock_run):
        mock_run.return_value = MagicMock(stdout="false\n", returncode=0)
        assert tts._is_system_muted() is False

    @patch("heyvox.audio.tts.subprocess.run")
    def test_system_mute_osascript_error(self, mock_run):
        """On subprocess failure, assume NOT muted (don't block audio)."""
        mock_run.side_effect = OSError("osascript not found")
        assert tts._is_system_muted() is False

    @patch("heyvox.audio.tts.subprocess.run")
    def test_system_mute_timeout(self, mock_run):
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired("osascript", 2)
        assert tts._is_system_muted() is False

    @patch("heyvox.audio.tts.subprocess.run")
    def test_system_mute_unexpected_output(self, mock_run):
        mock_run.return_value = MagicMock(stdout="missing value\n", returncode=0)
        assert tts._is_system_muted() is False


class TestIsMuted(unittest.TestCase):
    """is_muted() combines: in-memory flag, file flag, system mute."""

    def setUp(self):
        tts._muted = False
        # Clean file flags
        for f in [CLAUDE_TTS_MUTE_FLAG]:
            try:
                os.remove(f)
            except FileNotFoundError:
                pass

    def tearDown(self):
        tts._muted = False
        for f in [CLAUDE_TTS_MUTE_FLAG]:
            try:
                os.remove(f)
            except FileNotFoundError:
                pass

    @patch("heyvox.audio.tts._is_system_muted", return_value=False)
    def test_not_muted_by_default(self, _):
        assert tts.is_muted() is False

    @patch("heyvox.audio.tts._is_system_muted", return_value=False)
    def test_muted_by_memory_flag(self, _):
        tts._muted = True
        assert tts.is_muted() is True

    @patch("heyvox.audio.tts._is_system_muted", return_value=False)
    def test_muted_by_file_flag(self, _):
        open(CLAUDE_TTS_MUTE_FLAG, "w").close()
        assert tts.is_muted() is True

    @patch("heyvox.audio.tts._is_system_muted", return_value=True)
    def test_muted_by_system(self, _):
        assert tts.is_muted() is True


class TestSetMuted(unittest.TestCase):
    """set_muted() should sync in-memory + file flags."""

    def setUp(self):
        tts._muted = False
        self._cleanup_flags()

    def tearDown(self):
        tts._muted = False
        self._cleanup_flags()

    def _cleanup_flags(self):
        for f in [CLAUDE_TTS_MUTE_FLAG, HERALD_MUTE_FLAG]:
            try:
                os.remove(f)
            except FileNotFoundError:
                pass

    @patch("heyvox.audio.tts._herald")
    def test_mute_creates_flags(self, mock_herald):
        tts.set_muted(True)
        assert tts._muted is True
        assert os.path.exists(CLAUDE_TTS_MUTE_FLAG)
        assert os.path.exists(HERALD_MUTE_FLAG)
        mock_herald.assert_called_once_with("stop")

    @patch("heyvox.audio.tts._herald")
    def test_unmute_removes_flags(self, _):
        # First mute
        tts.set_muted(True)
        # Then unmute
        tts.set_muted(False)
        assert tts._muted is False
        assert not os.path.exists(CLAUDE_TTS_MUTE_FLAG)
        assert not os.path.exists(HERALD_MUTE_FLAG)

    @patch("heyvox.audio.tts._herald")
    def test_unmute_tolerates_missing_flags(self, _):
        """Unmuting when flags don't exist should not raise."""
        tts.set_muted(False)  # No flags to remove — should not error
        assert tts._muted is False


class TestVerbosityFileSync(unittest.TestCase):
    """Verbosity set/get should stay in sync via shared file."""

    def setUp(self):
        tts._verbosity = tts.Verbosity.FULL
        tts._muted = False
        self._cleanup()

    def tearDown(self):
        tts._verbosity = tts.Verbosity.FULL
        tts._muted = False
        self._cleanup()

    def _cleanup(self):
        for f in [VERBOSITY_FILE, CLAUDE_TTS_MUTE_FLAG, HERALD_MUTE_FLAG]:
            try:
                os.remove(f)
            except FileNotFoundError:
                pass

    def test_set_full_removes_file(self):
        """'full' = default, so file should not exist."""
        # Create the file first
        with open(VERBOSITY_FILE, "w") as f:
            f.write("short")
        tts.set_verbosity("full")
        assert not os.path.exists(VERBOSITY_FILE)
        assert tts.get_verbosity() == "full"

    def test_set_short_writes_file(self):
        tts.set_verbosity("short")
        assert os.path.exists(VERBOSITY_FILE)
        with open(VERBOSITY_FILE) as f:
            assert f.read().strip() == "short"
        assert tts.get_verbosity() == "short"

    def test_set_skip_creates_mute_flags(self):
        """'skip' verbosity should also create mute flags for Herald."""
        tts.set_verbosity("skip")
        assert tts._muted is True
        assert os.path.exists(CLAUDE_TTS_MUTE_FLAG)
        assert os.path.exists(HERALD_MUTE_FLAG)

    def test_set_full_clears_mute_flags(self):
        """Going back to 'full' from 'skip' should clear mute flags."""
        tts.set_verbosity("skip")
        tts.set_verbosity("full")
        assert tts._muted is False
        assert not os.path.exists(CLAUDE_TTS_MUTE_FLAG)

    def test_get_verbosity_reads_file(self):
        """get_verbosity() should read from file, not just in-memory."""
        # Simulate another process writing the file
        with open(VERBOSITY_FILE, "w") as f:
            f.write("short")
        assert tts.get_verbosity() == "short"

    def test_get_verbosity_invalid_file_falls_back(self):
        """Invalid file content should fall back to in-memory value."""
        with open(VERBOSITY_FILE, "w") as f:
            f.write("bogus")
        tts._verbosity = tts.Verbosity.FULL
        assert tts.get_verbosity() == "full"


class TestTtsStyleFileSync(unittest.TestCase):
    """Style set/get should stay in sync via shared file."""

    def setUp(self):
        tts._style = "detailed"
        self._cleanup()

    def tearDown(self):
        tts._style = "detailed"
        self._cleanup()

    def _cleanup(self):
        try:
            os.remove(TTS_STYLE_FILE)
        except FileNotFoundError:
            pass

    @patch("heyvox.audio.tts.update_config", create=True)
    def test_set_style_writes_file(self, _):
        tts.set_tts_style("concise")
        with open(TTS_STYLE_FILE) as f:
            assert f.read().strip() == "concise"

    @patch("heyvox.audio.tts.update_config", create=True)
    def test_set_default_style_removes_file(self, _):
        """'detailed' = default, so file should be removed."""
        with open(TTS_STYLE_FILE, "w") as f:
            f.write("concise")
        tts.set_tts_style("detailed")
        assert not os.path.exists(TTS_STYLE_FILE)

    def test_set_invalid_style_ignored(self):
        tts.set_tts_style("nonexistent_style")
        assert tts._style == "detailed"

    def test_get_style_reads_file(self):
        """get_tts_style() should read from file for cross-process consistency."""
        with open(TTS_STYLE_FILE, "w") as f:
            f.write("technical")
        assert tts.get_tts_style() == "technical"

    def test_get_style_invalid_file_falls_back(self):
        with open(TTS_STYLE_FILE, "w") as f:
            f.write("invalid")
        tts._style = "casual"
        assert tts.get_tts_style() == "casual"

    def test_get_style_prompt_matches_style(self):
        with open(TTS_STYLE_FILE, "w") as f:
            f.write("technical")
        prompt = tts.get_tts_style_prompt()
        assert "function names" in prompt.lower() or "file paths" in prompt.lower()


class TestApplyVerbosity(unittest.TestCase):
    """apply_verbosity() text filtering."""

    def test_full_returns_all(self):
        assert tts.apply_verbosity("Hello world.", tts.Verbosity.FULL) == "Hello world."

    def test_summary_returns_all(self):
        """Summary is treated as full."""
        assert tts.apply_verbosity("Hello world.", tts.Verbosity.SUMMARY) == "Hello world."

    def test_skip_returns_none(self):
        assert tts.apply_verbosity("Hello world.", tts.Verbosity.SKIP) is None

    def test_short_returns_first_sentence(self):
        result = tts.apply_verbosity("First sentence. Second sentence.", tts.Verbosity.SHORT)
        assert result == "First sentence."

    def test_short_no_period_truncates(self):
        long_text = "A" * 200
        result = tts.apply_verbosity(long_text, tts.Verbosity.SHORT)
        assert len(result) == 100

    def test_string_verbosity(self):
        """Should accept string values, not just Verbosity enum."""
        assert tts.apply_verbosity("Hello.", "full") == "Hello."
        assert tts.apply_verbosity("Hello.", "skip") is None


class TestTtsThreadSafety(unittest.TestCase):
    """Concurrent access to TTS state should not corrupt or raise."""

    def setUp(self):
        tts._muted = False
        tts._verbosity = tts.Verbosity.FULL
        tts._style = "detailed"

    def tearDown(self):
        tts._muted = False
        tts._verbosity = tts.Verbosity.FULL
        tts._style = "detailed"
        for f in [VERBOSITY_FILE, TTS_STYLE_FILE,
                  CLAUDE_TTS_MUTE_FLAG, HERALD_MUTE_FLAG]:
            try:
                os.remove(f)
            except FileNotFoundError:
                pass

    @patch("heyvox.audio.tts._herald")
    def test_concurrent_mute_toggle(self, _):
        """Rapid mute/unmute from multiple threads should not raise."""
        errors = []

        def toggle(n):
            try:
                for _ in range(50):
                    tts.set_muted(n % 2 == 0)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=toggle, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == [], f"Thread errors: {errors}"

    def test_concurrent_verbosity_changes(self):
        """Rapid verbosity changes from multiple threads should not raise."""
        errors = []
        levels = ["full", "short", "skip", "full"]

        def change(idx):
            try:
                for _ in range(50):
                    tts.set_verbosity(levels[idx])
                    tts.get_verbosity()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=change, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == [], f"Thread errors: {errors}"

    @patch("heyvox.audio.tts.update_config", create=True)
    def test_concurrent_style_changes(self, _):
        """Rapid style changes from multiple threads should not raise."""
        errors = []
        styles = ["detailed", "concise", "technical", "casual"]

        def change(idx):
            try:
                for _ in range(50):
                    tts.set_tts_style(styles[idx])
                    tts.get_tts_style()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=change, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == [], f"Thread errors: {errors}"


class TestVoiceCommands(unittest.TestCase):
    """check_voice_command() parsing."""

    def test_known_commands(self):
        assert tts.check_voice_command("mute") == ("tts-mute", "Toggling mute")
        assert tts.check_voice_command("skip") == ("tts-skip", "Skipping")
        assert tts.check_voice_command("stop all") == ("tts-stop", "Stopping all audio")
        assert tts.check_voice_command("be quiet") == ("verbosity-short", "First sentence mode")
        assert tts.check_voice_command("shut up") == ("verbosity-skip", "Muted")

    def test_unknown_command(self):
        assert tts.check_voice_command("what is the weather") is None

    def test_trailing_punctuation_stripped(self):
        assert tts.check_voice_command("mute.") == ("tts-mute", "Toggling mute")
        assert tts.check_voice_command("skip!") == ("tts-skip", "Skipping")

    def test_case_insensitive(self):
        assert tts.check_voice_command("MUTE") == ("tts-mute", "Toggling mute")
        assert tts.check_voice_command("Be Quiet") == ("verbosity-short", "First sentence mode")


if __name__ == "__main__":
    unittest.main()
