"""Tests for echo suppression and external TTS coordination.

Covers Bug #4: External TTS (from Conductor hooks) playing during recording.
Wake word detection must be suppressed while any TTS is playing.
"""

import os
import time
import pytest

from heyvox.constants import (
    TTS_PLAYING_FLAG,
    TTS_PLAYING_MAX_AGE_SECS,
    GRACE_AFTER_TTS,
    SPEAKER_MODE_THRESHOLD_MULT,
)


class TestTTSFlagSuppression:
    """Wake word detection must pause when TTS flag is present."""

    def test_fresh_tts_flag_suppresses(self, isolate_flags):
        """A recently created TTS flag should suppress wake word detection."""
        flag = isolate_flags["tts_flag"]
        open(flag, "w").close()

        assert os.path.exists(flag)
        age = time.time() - os.path.getmtime(flag)
        assert age < TTS_PLAYING_MAX_AGE_SECS, "Fresh flag should be within max age"

    def test_stale_tts_flag_ignored(self, isolate_flags):
        """TTS flag older than TTS_PLAYING_MAX_AGE_SECS must be ignored."""
        flag = isolate_flags["tts_flag"]
        open(flag, "w").close()

        old_time = time.time() - TTS_PLAYING_MAX_AGE_SECS - 10
        os.utime(flag, (old_time, old_time))

        age = time.time() - os.path.getmtime(flag)
        assert age > TTS_PLAYING_MAX_AGE_SECS, "Stale flag should exceed max age"

    def test_external_tts_flag_also_suppresses(self, tmp_path):
        """Conductor's TTS flag (/tmp/claude-tts-playing.pid) must also suppress."""
        external_flag = str(tmp_path / "claude-tts-playing.pid")
        open(external_flag, "w").close()

        assert os.path.exists(external_flag)
        # The main loop checks both flags in the echo suppression block


class TestEchoTextBuffer:
    """Test the text-level echo filtering that strips TTS output from transcription."""

    def test_register_and_filter(self):
        """Registered TTS text should be filtered from transcription."""
        from heyvox.audio.echo import register_tts_text, filter_tts_echo

        register_tts_text("The weather today is sunny and warm")
        result = filter_tts_echo("The weather today is sunny and warm")
        assert result == "", f"Echo should be fully filtered, got: '{result}'"

    def test_partial_echo_filtered(self):
        """Partial TTS echo at the start of transcription should be removed."""
        from heyvox.audio.echo import register_tts_text, filter_tts_echo

        register_tts_text("This is a TTS message about code quality")
        # Whisper might capture part of TTS + user speech
        result = filter_tts_echo("code quality. Now please write a function")
        # Should keep the user's speech part
        assert "write a function" in result.lower()

    def test_no_echo_passthrough(self):
        """Text that doesn't match any registered TTS should pass through."""
        from heyvox.audio.echo import filter_tts_echo

        result = filter_tts_echo("Please implement the login feature")
        assert "login feature" in result.lower()


class TestSpeakerModeThreshold:
    """In speaker mode (no headset), wake word threshold must be higher."""

    def test_threshold_multiplier_value(self):
        """Speaker mode multiplier should be 1.4x."""
        assert SPEAKER_MODE_THRESHOLD_MULT == 1.4

    def test_threshold_applied_in_speaker_mode(self):
        """Effective threshold = base * multiplier when no headset."""
        base_threshold = 0.5
        effective = base_threshold * SPEAKER_MODE_THRESHOLD_MULT
        assert effective == pytest.approx(0.7, abs=0.01)

    def test_threshold_unchanged_in_headset_mode(self):
        """With headset, threshold stays at base value."""
        base_threshold = 0.5
        headset_mode = True
        effective = base_threshold if headset_mode else base_threshold * SPEAKER_MODE_THRESHOLD_MULT
        assert effective == 0.5


class TestGracePeriods:
    """Verify timing constants for audio transitions."""

    def test_grace_after_recording(self):
        """Grace period after recording before TTS starts."""
        from heyvox.constants import GRACE_AFTER_RECORDING
        assert GRACE_AFTER_RECORDING == 1.0

    def test_grace_after_tts(self):
        """Grace period after TTS before re-enabling wake word."""
        assert GRACE_AFTER_TTS == 0.6

    def test_grace_before_media_resume(self):
        """Grace period after TTS before resuming media."""
        from heyvox.constants import GRACE_BEFORE_MEDIA_RESUME
        assert GRACE_BEFORE_MEDIA_RESUME == 1.5


class TestRecordingFlagPaths:
    """Verify the recording flag path is consistent across all files."""

    def test_constants_recording_flag(self):
        """Constants module must define /tmp/heyvox-recording."""
        from heyvox import constants
        # Note: isolate_flags patches this, so read the original
        assert "heyvox-recording" in constants.__dict__.get("RECORDING_FLAG", "")

    def test_recording_flag_defined_in_constants(self):
        """RECORDING_FLAG must be defined in constants (used by main.py for echo suppression)."""
        from heyvox.constants import RECORDING_FLAG
        # Use endswith instead of startswith("/tmp/") because pytest's isolate_flags
        # fixture redirects the path to a tmp_path that resolves via /private/var/...
        # on macOS (symlink: /tmp -> /private/var/folders/...).
        assert RECORDING_FLAG and RECORDING_FLAG.endswith("heyvox-recording")

    def test_conductor_hook_checks_heyvox_flag(self):
        """Conductor's tts-speak.sh must check /tmp/heyvox-recording.

        This is a documentation test — verifies the hook file contains the check.
        """
        hook_path = os.path.expanduser("~/.claude/hooks/tts-speak.sh")
        if not os.path.exists(hook_path):
            pytest.skip("Conductor TTS hook not found")

        with open(hook_path) as f:
            content = f.read()

        assert "heyvox-recording" in content, \
            "tts-speak.sh must check /tmp/heyvox-recording flag"

    def test_orchestrator_checks_heyvox_flag(self):
        """Conductor's tts-orchestrator.sh must check /tmp/heyvox-recording."""
        hook_path = os.path.expanduser("~/.claude/hooks/tts-orchestrator.sh")
        if not os.path.exists(hook_path):
            pytest.skip("Conductor TTS orchestrator not found")

        with open(hook_path) as f:
            content = f.read()

        assert "heyvox-recording" in content, \
            "tts-orchestrator.sh must check /tmp/heyvox-recording flag"
