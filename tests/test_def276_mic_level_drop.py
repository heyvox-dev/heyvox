"""DEF-276: a sagged mic cut hands-free dictations mid-sentence and nobody said why.

2026-11-05: the G435 (USB) dropped ~15 dB with no device event. Speech chunk
peaks fell under the static silence threshold (500), the silence watchdog
counted 86 % of the last 4 s as quiet and stopped five dictations after ~6 s.
Power-cycling the headset restored the level.

These tests pin (1) the relative silence threshold that keeps quiet speech from
counting as silence, (2) that it never raises the threshold or drops it into
ambient noise, (3) the per-mic level baseline and its drop warning, and (4) the
wiring in main.py / recording.py.
"""

import inspect
import json

import numpy as np
import pytest

from heyvox.audio import level
from heyvox.audio.level import (
    MicLevelBaseline,
    chunk_peak,
    effective_silence_threshold,
)

CH = 1280
WIN = 50  # 4 s of 80 ms chunks


def _peaks(speech_peak, pause_peak, n_speech=60, n_pause=0, seed=0):
    """Chunk peaks of a dictation: speech chunks around ``speech_peak`` with short gaps, then a pause."""
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n_speech):
        # every 4th chunk is a gap between words
        base = pause_peak if i % 4 == 3 else speech_peak
        out.append(int(base * rng.uniform(0.6, 1.4)))
    out += [int(pause_peak * rng.uniform(0.5, 1.5)) for _ in range(n_pause)]
    return out


def _watchdog_stops(peaks, static):
    """Replay of the main.py post-speech watchdog: True when the last 4 s are >= 85 % quiet."""
    thr = effective_silence_threshold(peaks, static)
    recent = peaks[-WIN:]
    return np.mean(np.array(recent) < thr) >= 0.85


# ---------------------------------------------------------------------------
# relative silence threshold
# ---------------------------------------------------------------------------

def test_sagged_speech_is_not_silence():
    # 2026-11-05 sagged state: speech chunk peaks ~180-600, pauses ~5, static threshold 500.
    peaks = _peaks(speech_peak=300, pause_peak=5)
    assert np.mean(np.array(peaks[-WIN:]) < 500) >= 0.85  # the static threshold stopped here
    assert not _watchdog_stops(peaks, 500)


def test_sagged_pause_still_stops():
    peaks = _peaks(speech_peak=300, pause_peak=5, n_pause=WIN)
    assert _watchdog_stops(peaks, 500)


def test_healthy_mic_keeps_static_threshold():
    peaks = _peaks(speech_peak=3000, pause_peak=100)
    assert effective_silence_threshold(peaks, 500) == 500


def test_never_raises_threshold():
    peaks = _peaks(speech_peak=20000, pause_peak=2000)
    assert effective_silence_threshold(peaks, 200) == 200


def test_stays_above_ambient():
    # Quiet speech in a noisy room: the threshold must stay above the ambient level.
    peaks = _peaks(speech_peak=900, pause_peak=150, n_pause=20)
    thr = effective_silence_threshold(peaks, 500)
    assert thr >= int(np.percentile(peaks, level.NOISE_PEAK_PERCENTILE) * level.NOISE_MARGIN)
    assert thr <= 500


def test_floor():
    peaks = [3] * 40
    assert effective_silence_threshold(peaks, 500) == level.RELATIVE_SILENCE_FLOOR


def test_too_few_chunks_uses_static():
    assert effective_silence_threshold([10] * (level.RELATIVE_MIN_CHUNKS - 1), 500) == 500


def test_disabled_static_threshold_passes_through():
    assert effective_silence_threshold([10] * 40, 0) == 0


def test_chunk_peak():
    assert chunk_peak(np.array([1, -700, 30], dtype=np.int16)) == 700
    assert chunk_peak(np.array([-32768], dtype=np.int16)) == 32768
    assert chunk_peak(np.array([], dtype=np.int16)) == 0


# ---------------------------------------------------------------------------
# per-mic baseline and drop detection
# ---------------------------------------------------------------------------

def _fill(b, mic, db, n):
    for _ in range(n):
        assert b.observe(mic, db, 10.0) is None


def test_no_warning_without_baseline(tmp_path):
    b = MicLevelBaseline(tmp_path)
    _fill(b, "G435", -27.0, level.BASELINE_MIN_SAMPLES - 1)
    assert b.observe("G435", -45.0, 10.0) is None  # baseline not established yet


def test_drop_detected(tmp_path):
    b = MicLevelBaseline(tmp_path)
    _fill(b, "G435 Wireless Gaming Headset", -27.0, level.BASELINE_MIN_SAMPLES)
    drop = b.observe("G435 Wireless Gaming Headset", -42.0, 6.0)
    assert drop == pytest.approx(15.0)


def test_normal_spread_is_no_drop(tmp_path):
    b = MicLevelBaseline(tmp_path)
    _fill(b, "G435", -27.0, 10)
    assert b.observe("G435", -33.0, 10.0) is None


def test_short_recordings_ignored(tmp_path):
    b = MicLevelBaseline(tmp_path)
    _fill(b, "G435", -27.0, 10)
    assert b.observe("G435", -60.0, level.MIN_DICTATION_SECS - 0.1) is None
    assert len(json.loads((tmp_path / "mic-levels.json").read_text())["g435"]) == 10


def test_baseline_per_mic(tmp_path):
    b = MicLevelBaseline(tmp_path)
    _fill(b, "G435", -27.0, 10)
    _fill(b, "MacBook Pro Microphone", -45.0, 10)
    assert b.observe("MacBook Pro Microphone", -46.0, 10.0) is None
    assert b.observe("G435", -45.0, 10.0) is not None


def test_persisted_across_instances(tmp_path):
    _fill(MicLevelBaseline(tmp_path), "G435", -27.0, 10)
    assert MicLevelBaseline(tmp_path).observe("G435", -42.0, 10.0) is not None


def test_permanent_change_stops_warning(tmp_path):
    b = MicLevelBaseline(tmp_path)
    _fill(b, "G435", -27.0, level.BASELINE_WINDOW)
    warnings = [b.observe("G435", -42.0, 10.0) is not None for _ in range(level.BASELINE_WINDOW)]
    assert warnings[0] and not warnings[-1]


def test_corrupt_cache_is_ignored(tmp_path):
    (tmp_path / "mic-levels.json").write_text("{not json")
    b = MicLevelBaseline(tmp_path)
    assert b.observe("G435", -27.0, 10.0) is None


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------

def test_main_watchdog_uses_relative_threshold():
    from heyvox import main
    src = inspect.getsource(main._run_loop)
    assert "effective_silence_threshold(_rec_peaks, silence_threshold)" in src
    assert "_rec_peaks.append(_level)" in src
    assert "_rec_peaks = []" in src


def test_recording_checks_level_drop():
    from heyvox import recording
    src = inspect.getsource(recording.RecordingStateMachine)
    assert "self._check_mic_level_drop(raw_speech_db, duration)" in src


def _machine():
    from unittest.mock import MagicMock
    from heyvox.recording import RecordingStateMachine
    from heyvox.app_context import AppContext
    logs = []
    sm = RecordingStateMachine(AppContext(), MagicMock(), logs.append, MagicMock())
    return sm, logs


def test_drop_raises_banner_and_normal_clears(tmp_path):
    from heyvox import constants
    from heyvox.hud.surface import HUDSurface
    with open(constants.ACTIVE_MIC_FILE, "w") as f:
        f.write("G435 Wireless Gaming Headset\n")
    sm, logs = _machine()
    for _ in range(level.BASELINE_MIN_SAMPLES):
        sm._check_mic_level_drop(-27.0, 10.0)
    assert not any(r["source"] == "mic-level-drop" for r in HUDSurface.read_active())

    sm._check_mic_level_drop(-42.0, 6.0)
    banners = [r for r in HUDSurface.read_active() if r["source"] == "mic-level-drop"]
    assert banners and "15 dB below normal" in banners[0]["text"]
    assert "off and on" in banners[0]["text"]
    assert any("[mic-level] drop" in m for m in logs)

    sm._check_mic_level_drop(-28.0, 10.0)
    assert not any(r["source"] == "mic-level-drop" for r in HUDSurface.read_active())


def test_no_active_mic_is_silent():
    sm, logs = _machine()
    sm._check_mic_level_drop(-42.0, 6.0)  # ACTIVE_MIC_FILE missing in the test tmpdir
    assert not logs
