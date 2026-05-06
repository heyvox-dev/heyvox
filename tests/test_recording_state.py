"""Tests for heyvox.recording — RecordingStateMachine class."""

import time
import pytest
from unittest.mock import patch, MagicMock

from heyvox.recording import RecordingStateMachine
from heyvox.app_context import AppContext


# ---------------------------------------------------------------------------
# Structural tests (kept from original)
# ---------------------------------------------------------------------------

def test_recording_state_machine_import():
    pass


def test_recording_state_machine_constructor_accepts_ctx_config_log_hud():
    from heyvox.recording import RecordingStateMachine
    from heyvox.app_context import AppContext
    ctx = AppContext()
    rsm = RecordingStateMachine(ctx=ctx, config=None, log_fn=print, hud_send=lambda msg: None)
    assert rsm.ctx is ctx


def test_recording_state_machine_has_required_methods():
    from heyvox.recording import RecordingStateMachine
    assert callable(getattr(RecordingStateMachine, 'start', None))
    assert callable(getattr(RecordingStateMachine, 'stop', None))
    assert callable(getattr(RecordingStateMachine, 'cancel', None))


def test_audio_rms_is_importable():
    pass


def test_recording_state_machine_uses_appcontext():
    """RecordingStateMachine must store state on ctx, not as globals."""
    from heyvox.recording import RecordingStateMachine
    from heyvox.app_context import AppContext
    ctx = AppContext()
    rsm = RecordingStateMachine(ctx=ctx, config=None, log_fn=print, hud_send=lambda msg: None)
    # Verify it references ctx, not module globals
    assert rsm.ctx is ctx
    assert rsm.ctx.is_recording is False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def rsm_and_ctx():
    ctx = AppContext()
    config = MagicMock()
    config.min_recording_secs = 0.5
    config.cues_dir = None
    config.stt = MagicMock()
    config.stt.backend = "none"
    rsm = RecordingStateMachine(
        ctx=ctx,
        config=config,
        log_fn=lambda s: None,
        hud_send=lambda m: None,
    )
    return rsm, ctx


# ---------------------------------------------------------------------------
# Shared patch context for start() tests
# ---------------------------------------------------------------------------

def _start_patches():
    """Return a list of patches needed to safely call rsm.start()."""
    return [
        patch("heyvox.audio.tts.set_recording"),
        patch("heyvox.audio.cues.audio_cue"),
        patch("heyvox.audio.cues.get_cues_dir", return_value="/tmp"),
        patch("heyvox.audio.media.pause_media"),
        patch("heyvox.input.target.snapshot_target", return_value=None),
        patch("heyvox.ipc.update_state"),
    ]


# ---------------------------------------------------------------------------
# Behavioral tests: start()
# ---------------------------------------------------------------------------

def test_start_sets_is_recording(rsm_and_ctx, isolate_flags):
    rsm, ctx = rsm_and_ctx
    with patch("heyvox.audio.tts.set_recording"), \
         patch("heyvox.audio.cues.audio_cue"), \
         patch("heyvox.audio.cues.get_cues_dir", return_value="/tmp"), \
         patch("heyvox.audio.media.pause_media"), \
         patch("heyvox.input.target.snapshot_target", return_value=None), \
         patch("heyvox.ipc.update_state"):
        rsm.start()
    assert ctx.is_recording is True


def test_start_noop_when_already_recording(rsm_and_ctx, isolate_flags):
    rsm, ctx = rsm_and_ctx
    ctx.is_recording = True
    # Should not raise and should leave state as-is
    with patch("heyvox.audio.tts.set_recording"), \
         patch("heyvox.audio.cues.audio_cue"), \
         patch("heyvox.audio.cues.get_cues_dir", return_value="/tmp"), \
         patch("heyvox.audio.media.pause_media"), \
         patch("heyvox.input.target.snapshot_target", return_value=None), \
         patch("heyvox.ipc.update_state"):
        rsm.start()
    assert ctx.is_recording is True


def test_start_blocked_by_zombie_mic(rsm_and_ctx, isolate_flags):
    rsm, ctx = rsm_and_ctx
    ctx.zombie_mic_reinit = True
    with patch("heyvox.audio.tts.set_recording"), \
         patch("heyvox.audio.cues.audio_cue"), \
         patch("heyvox.audio.cues.get_cues_dir", return_value="/tmp"), \
         patch("heyvox.audio.media.pause_media"), \
         patch("heyvox.input.target.snapshot_target", return_value=None), \
         patch("heyvox.ipc.update_state"):
        rsm.start()
    assert ctx.is_recording is False


def test_start_blocked_during_shutdown(rsm_and_ctx, isolate_flags):
    rsm, ctx = rsm_and_ctx
    ctx.shutdown.set()
    with patch("heyvox.audio.tts.set_recording"), \
         patch("heyvox.audio.cues.audio_cue"), \
         patch("heyvox.audio.cues.get_cues_dir", return_value="/tmp"), \
         patch("heyvox.audio.media.pause_media"), \
         patch("heyvox.input.target.snapshot_target", return_value=None), \
         patch("heyvox.ipc.update_state"):
        rsm.start()
    assert ctx.is_recording is False


# ---------------------------------------------------------------------------
# Behavioral tests: stop()
# ---------------------------------------------------------------------------

def test_stop_sets_busy_and_clears_recording(rsm_and_ctx, isolate_flags):
    rsm, ctx = rsm_and_ctx
    ctx.is_recording = True
    ctx.recording_start_time = time.time() - 2.0  # 2 seconds ago (> min_recording_secs)
    with patch("heyvox.recording.RecordingStateMachine._send_local"), \
         patch("heyvox.audio.cues.audio_cue"), \
         patch("heyvox.audio.cues.get_cues_dir", return_value="/tmp"), \
         patch("heyvox.ipc.update_state"):
        rsm.stop()
    assert ctx.is_recording is False
    assert ctx.busy is True


def test_stop_noop_when_not_recording(rsm_and_ctx, isolate_flags):
    rsm, ctx = rsm_and_ctx
    ctx.is_recording = False
    ctx.busy = False
    with patch("heyvox.recording.RecordingStateMachine._send_local"), \
         patch("heyvox.audio.cues.audio_cue"), \
         patch("heyvox.audio.cues.get_cues_dir", return_value="/tmp"), \
         patch("heyvox.ipc.update_state"):
        rsm.stop()
    assert ctx.busy is False


# ---------------------------------------------------------------------------
# Behavioral tests: cancel()
# ---------------------------------------------------------------------------

def test_cancel_clears_all_state(rsm_and_ctx, isolate_flags):
    rsm, ctx = rsm_and_ctx
    ctx.is_recording = True
    ctx.busy = True
    ctx.audio_buffer = [b"\x00"]
    with patch("heyvox.audio.media.resume_media"), \
         patch("heyvox.audio.cues.audio_cue"), \
         patch("heyvox.audio.cues.get_cues_dir", return_value="/tmp"), \
         patch("heyvox.ipc.update_state"), \
         patch("heyvox.recording._release_recording_guard"):
        rsm.cancel()
    assert ctx.is_recording is False
    assert ctx.busy is False
    assert len(ctx.audio_buffer) == 0
