"""DEF-255: the energy gate must not silently drop quiet-but-speech recordings.

The gate compares the MEAN level of a recording; a mic whose gain sagged 10-15 dB (DEF-101 G435
state) or a long dictation with pauses fell below it and was dropped without any user-visible
message. These tests pin the speech-level admission, the quiet-text guard and the banner rules.
"""

import inspect
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from heyvox import recording
from heyvox.app_context import AppContext
from heyvox.recording import (
    RecordingStateMachine,
    _admit_quiet_recording,
    _audio_rms,
    _speech_level_db,
)

SR = 16000
FRAME = SR // 50  # 20 ms


def _frames(levels_dbfs, seed=0):
    """Build int16 audio: one 20 ms frame per entry, noise scaled to that RMS (None = digital silence)."""
    rng = np.random.default_rng(seed)
    parts = []
    for lvl in levels_dbfs:
        if lvl is None:
            parts.append(np.zeros(FRAME, dtype=np.int16))
        else:
            rms = 32768.0 * 10 ** (lvl / 20.0)
            parts.append(np.clip(rng.standard_normal(FRAME) * rms, -32768, 32767).astype(np.int16))
    return [np.concatenate(parts)]


# ---------------------------------------------------------------------------
# _speech_level_db
# ---------------------------------------------------------------------------

def test_speech_level_empty_and_silent_are_floor():
    assert _speech_level_db([], SR) == -96.0
    assert _speech_level_db(_frames([None] * 100), SR) == -96.0
    assert _speech_level_db(_frames([-30] * 10), 0) == -96.0
    assert _speech_level_db([np.zeros(FRAME // 2, dtype=np.int16)], SR) == -96.0


def test_speech_level_matches_mean_for_steady_signal():
    chunks = _frames([-30] * 200)
    assert _speech_level_db(chunks, SR) == pytest.approx(_audio_rms(chunks, SR), abs=1.5)


def test_speech_level_ignores_pauses():
    """15 % speech frames at -42 dBFS + 85 % silence: mean is below the gate, speech level is not."""
    levels = ([-42] * 15 + [None] * 85) * 4
    chunks = _frames(levels)
    assert _audio_rms(chunks, SR) < recording._MIN_AUDIO_DBFS
    assert _speech_level_db(chunks, SR) == pytest.approx(-42, abs=1.5)


def test_speech_level_ignores_sparse_loud_blip():
    """A 2 % click at -20 dBFS lifts the mean above the gate but is not speech."""
    chunks = _frames([-20] * 2 + [None] * 98)
    assert _audio_rms(chunks, SR) >= recording._MIN_AUDIO_DBFS
    assert _speech_level_db(chunks, SR) == -96.0


def test_speech_level_full_scale_int16_does_not_overflow():
    chunks = [np.full(SR, 32767, dtype=np.int16)]
    level = _speech_level_db(chunks, SR)
    assert np.isfinite(level)
    assert -1.0 < level <= 0.0


# ---------------------------------------------------------------------------
# _admit_quiet_recording
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "speech_db, duration, expected",
    [
        (None, 45.0, False),        # caller did not measure it: old behaviour
        (-60.0, 45.0, False),       # noise floor, not speech
        (-48.1, 45.0, False),
        (-48.0, 3.0, True),         # both boundaries inclusive
        (-44.0, 42.3, True),        # the 09-19 dictations
        (-44.0, 2.9, False),        # too short to be a dictation
    ],
)
def test_admit_quiet_recording(speech_db, duration, expected):
    assert _admit_quiet_recording(speech_db, duration) is expected


def test_thresholds_are_pinned():
    assert recording._MIN_AUDIO_DBFS == -48.0
    assert recording._MIN_SPEECH_DBFS == -48.0
    assert recording._QUIET_ADMIT_MIN_SECS == 3.0
    assert recording._QUIET_ADMIT_MIN_WORDS == 4
    assert recording._QUIET_BANNER_MIN_SECS == 3.0


# ---------------------------------------------------------------------------
# _send_local wiring
# ---------------------------------------------------------------------------

@pytest.fixture
def rsm_hud():
    ctx = AppContext()
    config = MagicMock()
    config.cues_dir = None
    config.audio.sample_rate = SR
    config.stt.local.engine = "mlx"
    config.stt.local.mlx_model = "test-model"
    config.stt.local.language = "de"
    config.echo_suppression.stt_echo_filter = False
    config.wake_words.start = "hey vox"
    config.wake_words.stop = "hey vox"
    config.transcription_prefix = ""
    hud = MagicMock()
    rsm = RecordingStateMachine(ctx=ctx, config=config, log_fn=lambda s: None, hud_send=hud)
    return rsm, ctx, hud


def _run_send_local(rsm, *, raw_rms_db, raw_speech_db, duration, stt_text=""):
    """Run _send_local against mocks; returns (transcribe_audio mock, banner mock, audio_cue mock)."""
    with patch("heyvox.recording._resolve_min_audio_dbfs", return_value=-48.0), \
         patch("heyvox.recording._save_debug_audio", return_value=None), \
         patch("heyvox.recording._release_recording_guard"), \
         patch("heyvox.audio.stt.transcribe_audio", return_value=stt_text) as stt, \
         patch("heyvox.audio.cues.audio_cue") as cue, \
         patch("heyvox.audio.cues.get_cues_dir", return_value="/tmp"), \
         patch("heyvox.audio.media.resume_media"), \
         patch("heyvox.history.save"), \
         patch("heyvox.ipc.update_state"), \
         patch("heyvox.hud.surface.HUDSurface.banner") as banner:
        rsm._send_local(
            duration=duration,
            audio_chunks=[np.zeros(int(SR * duration), dtype=np.int16)],
            raw_rms_db=raw_rms_db,
            raw_speech_db=raw_speech_db,
            ptt=True,
            recording_target=None,
            stop_time=0.0,
        )
    return stt, banner, cue


def test_send_local_default_raw_speech_db_is_none():
    assert inspect.signature(RecordingStateMachine._send_local).parameters["raw_speech_db"].default is None


def test_quiet_recording_with_speech_level_is_transcribed(rsm_hud):
    rsm, _ctx, _hud = rsm_hud
    stt, banner, _cue = _run_send_local(rsm, raw_rms_db=-51.4, raw_speech_db=-45.7, duration=42.3)
    stt.assert_called_once()
    banner.assert_not_called()
    assert rsm._quiet_streak == 0


@pytest.mark.parametrize(
    "raw_rms_db, raw_speech_db, duration",
    [
        (-70.0, -60.0, 45.0),   # speech level below the floor
        (-51.4, -44.0, 2.0),    # speech level fine but too short for a dictation
        (-51.4, None, 45.0),    # caller passed no speech level: pre-DEF-255 behaviour
    ],
)
def test_quiet_recording_without_speech_level_is_still_skipped(rsm_hud, raw_rms_db, raw_speech_db, duration):
    rsm, _ctx, _hud = rsm_hud
    stt, _banner, cue = _run_send_local(rsm, raw_rms_db=raw_rms_db, raw_speech_db=raw_speech_db, duration=duration)
    stt.assert_not_called()
    assert rsm._quiet_streak == 1
    cue.assert_called()


def test_recording_above_mean_gate_never_needs_speech_level(rsm_hud):
    rsm, _ctx, _hud = rsm_hud
    stt, banner, _cue = _run_send_local(rsm, raw_rms_db=-33.7, raw_speech_db=None, duration=17.0)
    stt.assert_called_once()
    banner.assert_not_called()


def test_first_long_dropped_recording_shows_banner(rsm_hud):
    rsm, _ctx, _hud = rsm_hud
    _stt, banner, _cue = _run_send_local(rsm, raw_rms_db=-70.0, raw_speech_db=-70.0, duration=45.0)
    banner.assert_called_once()
    kwargs = banner.call_args.kwargs
    assert kwargs["source"] == "recording-quiet"
    assert kwargs["level"] == "warn"
    assert "Mic too quiet (-70 dBFS)" in kwargs["text"]


def test_first_short_dropped_recording_shows_no_banner(rsm_hud):
    rsm, _ctx, _hud = rsm_hud
    _stt, banner, _cue = _run_send_local(rsm, raw_rms_db=-70.0, raw_speech_db=-70.0, duration=1.0)
    banner.assert_not_called()


def test_second_consecutive_short_drop_shows_banner(rsm_hud):
    rsm, _ctx, _hud = rsm_hud
    rsm._quiet_streak = 1
    _stt, banner, _cue = _run_send_local(rsm, raw_rms_db=-70.0, raw_speech_db=-70.0, duration=1.0)
    banner.assert_called_once()
    assert rsm._quiet_streak == 2


def test_quiet_admitted_short_text_is_discarded_with_banner(rsm_hud):
    rsm, _ctx, hud = rsm_hud
    stt, banner, cue = _run_send_local(
        rsm, raw_rms_db=-51.4, raw_speech_db=-45.7, duration=5.0, stt_text="Ja gut."
    )
    stt.assert_called_once()
    banner.assert_called_once()
    assert not any(c.args and c.args[0].get("type") == "transcript" for c in hud.call_args_list)
    cue.assert_called()


def test_quiet_admitted_long_text_is_kept(rsm_hud):
    rsm, ctx, hud = rsm_hud
    ctx.cancel_transcription.set()  # stop right after the transcript step, before injection
    text = "das ist ein ganz normaler satz"
    _stt, banner, _cue = _run_send_local(
        rsm, raw_rms_db=-51.4, raw_speech_db=-45.7, duration=5.0, stt_text=text
    )
    banner.assert_not_called()
    assert any(c.args and c.args[0] == {"type": "transcript", "text": text} for c in hud.call_args_list)


def test_loud_recording_keeps_short_text(rsm_hud):
    """The short-text guard applies to quiet-admitted recordings only."""
    rsm, ctx, hud = rsm_hud
    ctx.cancel_transcription.set()
    _stt, banner, _cue = _run_send_local(
        rsm, raw_rms_db=-33.0, raw_speech_db=-28.0, duration=5.0, stt_text="Ja gut."
    )
    banner.assert_not_called()
    assert any(c.args and c.args[0] == {"type": "transcript", "text": "Ja gut."} for c in hud.call_args_list)


def test_stop_computes_and_forwards_speech_level():
    """stop() is too heavy to run here: pin that it measures the speech level and hands it to _send_local."""
    src = inspect.getsource(RecordingStateMachine.stop)
    assert "_speech_level_db(recorded_chunks" in src
    assert '"raw_speech_db": raw_speech_db' in src
