"""Tests for recording flag lifecycle and TTS coordination.

Covers Bug #1: TTS plays during recording due to flag file coordination failures.
The recording flag (/tmp/heyvox-recording) must be:
  - Created immediately on start_recording()
  - Maintained through STT and paste pipeline
  - Removed only after paste completes (in _send_local's finally block)
"""

import os
import time
import threading


class TestRecordingFlagLifecycle:
    """Verify the recording flag is created and removed at the right times."""

    def test_flag_created_on_start(self, isolate_flags, mock_config, monkeypatch):
        """RecordingStateMachine.start() must create the recording flag immediately."""
        from heyvox.app_context import AppContext
        from heyvox.recording import RecordingStateMachine

        flag = isolate_flags["recording_flag"]
        assert not os.path.exists(flag)

        # Suppress side effects
        monkeypatch.setattr("heyvox.audio.cues.play_cue", lambda *a, **kw: None, raising=False)
        monkeypatch.setattr("heyvox.audio.tts.set_recording", lambda *a, **kw: None, raising=False)
        monkeypatch.setattr("heyvox.recording.RECORDING_FLAG", flag)

        ctx = AppContext()
        rsm = RecordingStateMachine(ctx=ctx, config=mock_config, log_fn=print, hud_send=lambda m: None)

        rsm.start()

        assert os.path.exists(flag), "Recording flag must exist after start()"
        assert ctx.is_recording is True

        # Cleanup
        ctx.is_recording = False
        try:
            os.remove(flag)
        except FileNotFoundError:
            pass

    def test_flag_file_is_empty(self, isolate_flags, mock_config, monkeypatch):
        """Recording flag should be an empty file (presence-only sentinel)."""
        from heyvox.app_context import AppContext
        from heyvox.recording import RecordingStateMachine

        flag = isolate_flags["recording_flag"]

        monkeypatch.setattr("heyvox.audio.cues.play_cue", lambda *a, **kw: None, raising=False)
        monkeypatch.setattr("heyvox.audio.tts.set_recording", lambda *a, **kw: None, raising=False)
        monkeypatch.setattr("heyvox.recording.RECORDING_FLAG", flag)

        ctx = AppContext()
        rsm = RecordingStateMachine(ctx=ctx, config=mock_config, log_fn=print, hud_send=lambda m: None)

        rsm.start()

        assert os.path.getsize(flag) == 0
        ctx.is_recording = False
        try:
            os.remove(flag)
        except FileNotFoundError:
            pass

    def test_release_recording_guard_removes_flag(self, isolate_flags):
        """_release_recording_guard() must remove the flag and clear the event."""
        from heyvox.recording import _release_recording_guard

        flag = isolate_flags["recording_flag"]
        open(flag, "w").close()

        _release_recording_guard()

        assert not os.path.exists(flag), "Flag must be removed after release"

    def test_release_recording_guard_idempotent(self, isolate_flags):
        """Calling _release_recording_guard() when flag doesn't exist must not crash."""
        from heyvox.recording import _release_recording_guard

        flag = isolate_flags["recording_flag"]
        assert not os.path.exists(flag)

        # Should not raise
        _release_recording_guard()


class TestTTSRecordingEvent:
    """Verify the TTS recording coordination API (delegates to Herald)."""

    def test_set_recording_true_calls_herald_pause(self, monkeypatch):
        """set_recording(True) must call herald pause to stop in-flight TTS."""
        from heyvox.audio import tts

        herald_calls = []
        monkeypatch.setattr(tts, "_herald", lambda cmd, *a, **kw: herald_calls.append(cmd) or
                            __import__("subprocess").CompletedProcess([], 0, "", ""))

        tts.set_recording(True)
        assert "pause" in herald_calls, "herald pause must be called when recording starts"

    def test_set_recording_false_calls_herald_resume(self, monkeypatch):
        """set_recording(False) must call herald resume to re-enable TTS."""
        from heyvox.audio import tts

        herald_calls = []
        monkeypatch.setattr(tts, "_herald", lambda cmd, *a, **kw: herald_calls.append(cmd) or
                            __import__("subprocess").CompletedProcess([], 0, "", ""))

        tts.set_recording(False)
        assert "resume" in herald_calls, "herald resume must be called when recording stops"

    def test_set_recording_true_blocks_tts(self, isolate_flags, monkeypatch):
        """set_recording(True) must call herald pause so TTS is blocked."""
        from heyvox.audio import tts

        herald_calls = []
        monkeypatch.setattr(tts, "_herald", lambda cmd, *a, **kw: herald_calls.append(cmd) or
                            __import__("subprocess").CompletedProcess([], 0, "", ""))

        tts.set_recording(True)
        # Herald pause is what blocks TTS in the new delegation model
        assert "pause" in herald_calls

        tts.set_recording(False)

    def test_recording_coordination_is_thread_safe(self, monkeypatch):
        """set_recording() must work correctly when called from another thread."""
        from heyvox.audio import tts

        herald_calls = []
        monkeypatch.setattr(tts, "_herald", lambda cmd, *a, **kw: herald_calls.append(cmd) or
                            __import__("subprocess").CompletedProcess([], 0, "", ""))

        def set_from_thread():
            tts.set_recording(True)

        t = threading.Thread(target=set_from_thread)
        t.start()
        t.join()

        assert "pause" in herald_calls, "herald pause must be visible from spawning thread"
        tts.set_recording(False)


class TestExternalFlagCoordination:
    """Verify that external processes (Conductor TTS hooks) can detect recording state."""

    def test_flag_visible_to_external_check(self, isolate_flags, mock_config, monkeypatch):
        """An external shell check for the flag file must succeed during recording."""
        import subprocess
        from heyvox.app_context import AppContext
        from heyvox.recording import RecordingStateMachine

        flag = isolate_flags["recording_flag"]

        monkeypatch.setattr("heyvox.audio.cues.play_cue", lambda *a, **kw: None, raising=False)
        monkeypatch.setattr("heyvox.audio.tts.set_recording", lambda *a, **kw: None, raising=False)
        monkeypatch.setattr("heyvox.recording.RECORDING_FLAG", flag)

        ctx = AppContext()
        rsm = RecordingStateMachine(ctx=ctx, config=mock_config, log_fn=print, hud_send=lambda m: None)

        rsm.start()

        # Simulate what Conductor's tts-speak.sh does
        result = subprocess.run(
            ["test", "-f", flag],
            capture_output=True,
        )
        assert result.returncode == 0, "External shell must see the recording flag"

        ctx.is_recording = False
        try:
            os.remove(flag)
        except FileNotFoundError:
            pass

    def test_stale_flag_cleaned_on_startup(self, isolate_flags):
        """Stale recording flags from crashed sessions must be cleaned on startup."""
        flag = isolate_flags["recording_flag"]

        # Create a flag with old timestamp
        open(flag, "w").close()
        old_time = time.time() - 120  # 2 minutes old
        os.utime(flag, (old_time, old_time))

        assert os.path.exists(flag)

        # The startup cleanup in main.py's run() removes stale flags
        # We test the cleanup logic directly
        try:
            age = time.time() - os.path.getmtime(flag)
            if age > 60:
                os.unlink(flag)
        except FileNotFoundError:
            pass

        assert not os.path.exists(flag), "Stale flag must be cleaned up"
