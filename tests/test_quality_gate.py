"""Guard tests for tools/quality_gate.py.

Cover the score-aware / dry-run / hard-brake redesign (DEF-167 follow-up) plus
the never-delete / never-overwrite guarantee. Silent wavs are used so the RMS
pre-gate short-circuits before any mlx_whisper call -- the move logic is
exercised with no model load, keeping the suite fast and deterministic.

Semantics under test:
- A positive-dir clip is quarantined ONLY when Whisper finds no wake word AND
  its filename model-score is below _TRUST_SCORE. High-score and no-score
  (curated) clips are never quarantined -- the STT must not overrule the model.
- Nothing moves unless apply=True (dry run is the default).
- If the positives-quarantine rate exceeds the hard brake, moves are refused
  unless force=True.
"""
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import quality_gate  # noqa: E402


def _write_silent_wav(path: Path, seconds: float = 0.5, sample_rate: int = 16000) -> None:
    """Write a near-silent int16 wav (RMS well below quality_gate._SILENCE_RMS)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = np.zeros(int(seconds * sample_rate), dtype=np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(frames.tobytes())


def _low(name_stem: str) -> str:
    return f"{name_stem}_score0.30.wav"   # below _TRUST_SCORE -> gate-eligible


def _high(name_stem: str) -> str:
    return f"{name_stem}_score1.00.wav"   # >= _TRUST_SCORE -> trusted


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def test_safe_dest_avoids_collision(tmp_path: Path):
    d = tmp_path / "q"
    d.mkdir()
    assert quality_gate._safe_dest(d, "a.wav") == d / "a.wav"
    (d / "a.wav").touch()
    assert quality_gate._safe_dest(d, "a.wav") == d / "a_dup1.wav"
    (d / "a_dup1.wav").touch()
    assert quality_gate._safe_dest(d, "a.wav") == d / "a_dup2.wav"


def test_parse_score():
    assert quality_gate._parse_score("tp_start_mic_20260101_000000_score1.00.wav") == 1.0
    assert quality_gate._parse_score("fn_start_mic_x_score0.42.wav") == 0.42
    assert quality_gate._parse_score("some_curated_recording.wav") is None


def test_positive_should_quarantine_decision():
    q = quality_gate._positive_should_quarantine
    # has wake word -> never quarantine, regardless of score
    assert q(True, 0.1) is False
    # no wake word + low score -> quarantine
    assert q(False, 0.30) is True
    # no wake word + high score -> TRUSTED, do not quarantine
    assert q(False, 0.99) is False
    # no wake word + no score (curated) -> TRUSTED
    assert q(False, None) is False


# --------------------------------------------------------------------------- #
# positives score gate + apply/dry-run
# --------------------------------------------------------------------------- #

def _dirs(tmp_path):
    return (tmp_path / "positives", tmp_path / "quarantine", tmp_path / "state")


def test_low_score_positive_quarantined_on_apply_not_deleted(tmp_path: Path):
    pos = tmp_path / "tp"
    positives, quarantine, state = _dirs(tmp_path)
    _write_silent_wav(pos / _low("tp_start_target"))
    # Trusted (high-score) clips keep the quarantine rate under the hard brake
    # so the real apply path runs without needing --force.
    for i in range(9):
        _write_silent_wav(pos / _high(f"tp_start_trusted{i}"))

    s = quality_gate.run_gate([pos], [], positives, quarantine, state_dir=state, apply=True)

    name = _low("tp_start_target")
    assert s["brake_tripped"] is False        # 1/10 = 10% < 15%
    assert s["applied"] and s["moves_made"] == 1
    assert not (pos / name).exists()          # gone from source
    assert (quarantine / name).exists()       # present in quarantine (never deleted)
    assert len(list(pos.glob("*.wav"))) == 9  # trusted clips untouched


def test_high_score_positive_never_quarantined(tmp_path: Path):
    pos = tmp_path / "tp"
    positives, quarantine, state = _dirs(tmp_path)
    _write_silent_wav(pos / _high("tp_start_mic"))

    s = quality_gate.run_gate([pos], [], positives, quarantine, state_dir=state, apply=True)

    assert s["would_quarantine"] == 0 and s["moves_made"] == 0
    assert (pos / _high("tp_start_mic")).exists()   # trusted, untouched


def test_no_score_positive_never_quarantined(tmp_path: Path):
    pos = tmp_path / "recordings"
    positives, quarantine, state = _dirs(tmp_path)
    _write_silent_wav(pos / "curated_clip.wav")

    s = quality_gate.run_gate([pos], [], positives, quarantine, state_dir=state, apply=True)

    assert s["would_quarantine"] == 0
    assert (pos / "curated_clip.wav").exists()


def test_dry_run_is_default_and_moves_nothing(tmp_path: Path):
    pos = tmp_path / "tp"
    positives, quarantine, state = _dirs(tmp_path)
    _write_silent_wav(pos / _low("tp_start_mic"))

    s = quality_gate.run_gate([pos], [], positives, quarantine, state_dir=state)  # apply defaults False

    assert s["applied"] is False and s["moves_made"] == 0
    assert s["would_quarantine"] == 1
    assert (pos / _low("tp_start_mic")).exists()    # nothing moved on a dry run


def test_collision_preserves_both_clips(tmp_path: Path):
    pos_a = tmp_path / "tp"
    pos_b = tmp_path / "fn"
    positives, quarantine, state = _dirs(tmp_path)
    _write_silent_wav(pos_a / _low("same"))
    _write_silent_wav(pos_b / _low("same"))

    # force past the brake -- this test is about collision safety, not the rate.
    quality_gate.run_gate([pos_a, pos_b], [], positives, quarantine,
                          state_dir=state, apply=True, force=True)

    survivors = sorted(p.name for p in quarantine.glob("*.wav"))
    assert survivors == [_low("same"), _low("same").replace(".wav", "_dup1.wav")], survivors


# --------------------------------------------------------------------------- #
# hard brake
# --------------------------------------------------------------------------- #

def test_brake_refuses_moves_above_threshold(tmp_path: Path):
    pos = tmp_path / "tp"
    positives, quarantine, state = _dirs(tmp_path)
    # All low-score + silent => all no-WW => 100% would-quarantine, well over 15%.
    for i in range(5):
        _write_silent_wav(pos / _low(f"tp_start_mic{i}"))

    s = quality_gate.run_gate([pos], [], positives, quarantine, state_dir=state, apply=True)

    assert s["brake_tripped"] is True
    assert s["applied"] is False and s["moves_made"] == 0
    assert len(list(pos.glob("*.wav"))) == 5        # nothing moved under the brake


def test_force_overrides_brake(tmp_path: Path):
    pos = tmp_path / "tp"
    positives, quarantine, state = _dirs(tmp_path)
    for i in range(5):
        _write_silent_wav(pos / _low(f"tp_start_mic{i}"))

    s = quality_gate.run_gate([pos], [], positives, quarantine,
                              state_dir=state, apply=True, force=True)

    assert s["brake_tripped"] is True and s["applied"] is True
    assert s["moves_made"] == 5
    assert len(list(quarantine.glob("*.wav"))) == 5


def test_resumable_second_run_no_new_moves(tmp_path: Path):
    pos = tmp_path / "tp"
    positives, quarantine, state = _dirs(tmp_path)
    _write_silent_wav(pos / _low("tp_start_mic"))

    # force past the brake so the single low-score clip actually moves in run 1.
    quality_gate.run_gate([pos], [], positives, quarantine,
                          state_dir=state, apply=True, force=True)
    s2 = quality_gate.run_gate([pos], [], positives, quarantine,
                               state_dir=state, apply=True, force=True)
    # Clip already moved out of the gated dir; nothing new to do.
    assert s2["moves_made"] == 0
    assert (quarantine / _low("tp_start_mic")).exists()
