"""
Tests for TrainingCollector labeling and pruning (DEF-155/156/157).

The collector feeds wake-word retraining — mislabeled clips poison the
training set silently, so the labeling rules and prune order are pinned
here.
"""

import os

import numpy as np
import pytest

from heyvox.audio.training_collector import (
    TrainingCollector,
    classify_stop_outcome,
)

SR = 16000


def _speech(secs: float) -> np.ndarray:
    """Noise burst well above the collector's speech RMS gate (300)."""
    rng = np.random.default_rng(42)
    return rng.integers(-3000, 3000, int(secs * SR)).astype(np.int16)


def _silence(secs: float) -> np.ndarray:
    return np.zeros(int(secs * SR), dtype=np.int16)


@pytest.fixture
def collector(tmp_path):
    return TrainingCollector(base_dir=str(tmp_path), max_clips_per_category=1000)


# ---------------------------------------------------------------------------
# classify_stop_outcome — the DEF-155/156 labeling rules
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "stop_reason,end_stripped,has_text,expected",
    [
        # stop_wake ended the recording = confirmed detection, even when the
        # transcript still needed a strip (DEF-091 imperfect trim, was DEF-155
        # mislabeled as fn).
        ("stop_wake", True, True, "tp"),
        ("stop_wake", False, True, "tp"),
        # stop_wake with no usable text → likely false trigger, FP paths own it.
        ("stop_wake", True, False, None),
        ("stop_wake", False, False, None),
        # Non-stop_wake end + end-strip proof = the detector missed a spoken
        # stop word.
        ("silence_timeout", True, True, "fn"),
        ("ptt_interrupt", True, False, "fn"),
        ("max_duration", True, True, "fn"),
        # No end-strip = no evidence a stop word was ever spoken.
        ("silence_timeout", False, True, None),
        ("ptt", False, True, None),
        ("other", False, False, None),
    ],
)
def test_classify_stop_outcome(stop_reason, end_stripped, has_text, expected):
    assert (
        classify_stop_outcome(stop_reason, end_stripped=end_stripped, has_text=has_text)
        == expected
    )


# ---------------------------------------------------------------------------
# _extract_speech_tail — trailing-silence skip (DEF-155)
# ---------------------------------------------------------------------------

def test_fn_stop_survives_trailing_silence(collector, tmp_path):
    """A timeout-ended recording carries ~5s of silence after the missed wake
    word. The verbatim 2s tail would be pure silence and the RMS gate would
    discard it — the speech-tail extractor must reach back to the speech."""
    chunks = [_speech(1.0), _silence(5.0)]
    assert collector.save_fn_stop(chunks, SR, score=0.31) is True

    clips = list((tmp_path / "fn").glob("fn_stop_*.wav"))
    assert len(clips) == 1
    import soundfile as sf
    audio, _ = sf.read(str(clips[0]), dtype="int16")
    assert len(audio) <= int(2.0 * SR)
    rms = np.sqrt(np.mean(audio.astype(np.float64) ** 2))
    assert rms >= 300, "extracted window must contain the speech, not silence"


def test_fn_stop_pure_silence_discarded(collector, tmp_path):
    assert collector.save_fn_stop([_silence(6.0)], SR) is False
    assert list((tmp_path / "fn").glob("*.wav")) == []


def test_tp_stop_uses_speech_tail(collector, tmp_path):
    chunks = [_speech(1.5), _silence(0.4)]
    assert collector.save_tp_stop(chunks, SR, score=0.97) is True
    assert len(list((tmp_path / "tp").glob("tp_stop_*.wav"))) == 1


# ---------------------------------------------------------------------------
# Observed score lands in the filename (DEF-155 diagnostics)
# ---------------------------------------------------------------------------

def test_fn_stop_score_in_filename(collector, tmp_path):
    collector.save_fn_stop([_speech(2.0)], SR, score=0.42)
    assert list((tmp_path / "fn").glob("*score0.42.wav")), (
        "observed stop-score must be in the filename so model-blind vs "
        "gate-blocked misses are distinguishable without the logs"
    )


# ---------------------------------------------------------------------------
# _prune — mtime order + subtype balance (DEF-157)
# ---------------------------------------------------------------------------

def _make_clip(cat_dir, name: str, mtime: float) -> None:
    p = cat_dir / name
    p.write_bytes(b"")
    os.utime(p, (mtime, mtime))


def test_prune_protects_minority_subtype(tmp_path):
    """Alphabetical pruning deleted every tp_start before any tp_stop
    (start < stop) — the rarer subtype was silently erased. Deletions must
    come from the largest subtype group, oldest first."""
    collector = TrainingCollector(base_dir=str(tmp_path), max_clips_per_category=4)
    tp = tmp_path / "tp"
    _make_clip(tp, "tp_start_mic_20260101_000000_score0.90.wav", 1000.0)  # oldest overall
    for i in range(4):
        _make_clip(tp, f"tp_stop_mic_2026010{i + 1}_000001_score0.00.wav", 2000.0 + i)

    collector._prune("tp")

    remaining = sorted(p.name for p in tp.glob("*.wav"))
    assert len(remaining) == 4
    assert any(n.startswith("tp_start_") for n in remaining), (
        "minority subtype must survive pruning"
    )
    assert "tp_stop_mic_20260101_000001_score0.00.wav" not in remaining, (
        "oldest clip of the largest subtype must be the one deleted"
    )


def test_prune_deletes_oldest_by_mtime_not_name(tmp_path):
    collector = TrainingCollector(base_dir=str(tmp_path), max_clips_per_category=2)
    tn = tmp_path / "tn"
    _make_clip(tn, "tn_aaa_20260301_000000_score0.50.wav", 3000.0)  # alphabetically first, newest
    _make_clip(tn, "tn_mmm_20260201_000000_score0.50.wav", 2000.0)
    _make_clip(tn, "tn_zzz_20260101_000000_score0.50.wav", 1000.0)  # alphabetically last, oldest

    collector._prune("tn")

    remaining = {p.name for p in tn.glob("*.wav")}
    assert remaining == {
        "tn_aaa_20260301_000000_score0.50.wav",
        "tn_mmm_20260201_000000_score0.50.wav",
    }, "deletion must follow mtime (oldest first), not filename order"


def test_prune_noop_under_limit(tmp_path):
    collector = TrainingCollector(base_dir=str(tmp_path), max_clips_per_category=10)
    tn = tmp_path / "tn"
    for i in range(3):
        _make_clip(tn, f"tn_x_2026010{i + 1}_000000_score0.50.wav", 1000.0 + i)
    collector._prune("tn")
    assert len(list(tn.glob("*.wav"))) == 3


# ---------------------------------------------------------------------------
# DEF-167 — retroactive relabelers removed (no evidence-free reclassification)
# ---------------------------------------------------------------------------

def test_save_tp_start_has_no_reclassify_method(collector):
    assert not hasattr(collector, "reclassify_tp_start_as_fp")


def test_save_tn_has_no_reclassify_method(collector):
    assert not hasattr(collector, "reclassify_fn_start")


def test_aborted_trigger_leaves_tp_start_clip_alone(collector, tmp_path):
    """An aborted trigger must not relabel its tp_start clip as fp/ — the
    method that used to do this (reclassify_tp_start_as_fp) is gone."""
    collector.feed(_speech(3.0))
    assert collector.save_tp_start(0.85) is True

    tp_clips = list((tmp_path / "tp").glob("tp_start_*.wav"))
    assert len(tp_clips) == 1
    fp_clips = list((tmp_path / "fp").glob("*.wav"))
    assert fp_clips == [], "no reclassification path exists to move tp_start into fp/"


def test_tn_save_not_relabeled_by_subsequent_trigger(collector, tmp_path):
    """A trigger following a recent TN save must not relabel that TN clip as
    fn/ — the method that used to do this (reclassify_fn_start) is gone."""
    collector.feed(_speech(3.0))
    assert collector.save_tn(0.4) is True  # within default tn_score_range (0.1, 0.7)

    collector.feed(_speech(3.0))
    assert collector.save_tp_start(0.9) is True  # simulate "a trigger followed"

    tn_clips = list((tmp_path / "tn").glob("*.wav"))
    assert len(tn_clips) == 1, "tn/ clip must still be present, unrelabeled"
    fn_clips = list((tmp_path / "fn").glob("*.wav"))
    assert fn_clips == [], "no reclassification path exists to move tn into fn/"
