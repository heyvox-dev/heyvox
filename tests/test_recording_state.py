"""Tests for heyvox.recording — RecordingStateMachine class."""
import pytest

pytestmark = pytest.mark.skip(reason="module not yet created — Plan 03 will unskip")


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
