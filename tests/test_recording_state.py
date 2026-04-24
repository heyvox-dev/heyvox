"""Tests for heyvox.recording — RecordingStateMachine class."""

import time
import pytest
from unittest.mock import patch, MagicMock

from heyvox.recording import RecordingStateMachine, _audio_rms
from heyvox.app_context import AppContext


# ---------------------------------------------------------------------------
# Structural tests (kept from original)
# ---------------------------------------------------------------------------

def test_recording_state_machine_import():
    from heyvox.recording import RecordingStateMachine


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
    from heyvox.recording import _audio_rms


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


# ---------------------------------------------------------------------------
# DEF-084: cancel_transcription must not leak across recordings
# ---------------------------------------------------------------------------

def test_start_clears_stale_cancel_transcription(rsm_and_ctx, isolate_flags):
    """DEF-084: if a prior STT's early-return left cancel_transcription set
    (e.g. Escape-during-STT → garbled filter → return without clear), the next
    start() must reset it so the new recording isn't spuriously cancelled."""
    rsm, ctx = rsm_and_ctx
    # Simulate the leaked state after a garbled-filter early return during
    # which the user had pressed Escape.
    ctx.cancel_transcription.set()
    assert ctx.cancel_transcription.is_set()
    with patch("heyvox.audio.tts.set_recording"), \
         patch("heyvox.audio.cues.audio_cue"), \
         patch("heyvox.audio.cues.get_cues_dir", return_value="/tmp"), \
         patch("heyvox.audio.media.pause_media"), \
         patch("heyvox.input.target.snapshot_target", return_value=None), \
         patch("heyvox.ipc.update_state"):
        rsm.start()
    assert ctx.cancel_transcription.is_set() is False
    assert ctx.is_recording is True


def test_send_local_finally_clears_cancel_transcription(rsm_and_ctx, isolate_flags):
    """DEF-084: _send_local's finally block must clear cancel_transcription
    on every exit path (garbled, empty-stt, voice-command, happy path,
    exception). This guards against future early-return paths forgetting to
    reset the flag locally."""
    rsm, ctx = rsm_and_ctx
    # Arrange: Pretend Escape was pressed during STT.
    ctx.cancel_transcription.set()
    # Force the `_send_local` entry to take an early return by tripping the
    # low-energy gate — raw_rms_db well below _MIN_AUDIO_DBFS. This exercises
    # the shallowest path through the try/finally and doesn't require us to
    # stub MLX or injection.
    with patch("heyvox.audio.cues.audio_cue"), \
         patch("heyvox.audio.cues.get_cues_dir", return_value="/tmp"), \
         patch("heyvox.ipc.update_state"), \
         patch("heyvox.recording._release_recording_guard"), \
         patch("heyvox.audio.media.resume_media"):
        rsm._send_local(
            duration=1.0,
            audio_chunks=[],
            raw_rms_db=-90.0,  # below _MIN_AUDIO_DBFS, triggers early return
            ptt=False,
            recording_target=None,
            stop_time=0.0,
        )
    # The finally block must have cleared the flag even though the low-energy
    # path returned before the in-body consumer checks.
    assert ctx.cancel_transcription.is_set() is False


def test_send_local_exception_path_clears_cancel_transcription(rsm_and_ctx, isolate_flags):
    """DEF-084: even if _send_local throws mid-pipeline, the finally block
    must still reset cancel_transcription. Protects against the pattern where
    a future filter raises and skips every local clear() call."""
    rsm, ctx = rsm_and_ctx
    ctx.cancel_transcription.set()
    # Force an exception by making the cue dir lookup blow up deep in the
    # pipeline. The outer except/finally in _send_local should absorb it.
    with patch("heyvox.audio.cues.get_cues_dir", side_effect=RuntimeError("boom")), \
         patch("heyvox.audio.cues.audio_cue"), \
         patch("heyvox.ipc.update_state"), \
         patch("heyvox.recording._release_recording_guard"), \
         patch("heyvox.audio.media.resume_media"):
        rsm._send_local(
            duration=1.0,
            audio_chunks=[],
            raw_rms_db=-90.0,
            ptt=False,
            recording_target=None,
            stop_time=0.0,
        )
    assert ctx.cancel_transcription.is_set() is False
