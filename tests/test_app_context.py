"""Tests for heyvox.app_context — AppContext dataclass."""
import pytest
import threading

pytestmark = pytest.mark.skip(reason="module not yet created — Plan 01 will unskip")


def test_appcontext_creates_with_defaults():
    from heyvox.app_context import AppContext
    ctx = AppContext()
    assert ctx.is_recording is False
    assert ctx.busy is False
    assert isinstance(ctx.lock, type(threading.Lock()))
    assert isinstance(ctx.shutdown, threading.Event)


def test_appcontext_mutable_defaults_are_independent():
    from heyvox.app_context import AppContext
    ctx1 = AppContext()
    ctx2 = AppContext()
    # Each instance must have its own lock and buffer
    assert ctx1.lock is not ctx2.lock
    assert ctx1.audio_buffer is not ctx2.audio_buffer
    assert ctx1.shutdown is not ctx2.shutdown


def test_appcontext_has_all_state_fields():
    from heyvox.app_context import AppContext
    ctx = AppContext()
    # Recording state
    assert hasattr(ctx, 'is_recording')
    assert hasattr(ctx, 'audio_buffer')
    assert hasattr(ctx, 'triggered_by_ptt')
    assert hasattr(ctx, 'cancel_transcription')
    assert hasattr(ctx, 'shutdown')
    # Device state
    assert hasattr(ctx, 'consecutive_failed_recordings')
    assert hasattr(ctx, 'zombie_mic_reinit')
    assert hasattr(ctx, 'last_good_audio_time')
    # HUD state
    assert hasattr(ctx, 'hud_client')
    assert hasattr(ctx, 'hud_last_reconnect')
