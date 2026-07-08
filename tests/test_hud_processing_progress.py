"""Tests for estimated HUD transcription progress helpers."""

from heyvox.hud.overlay import (
    _estimate_transcription_secs,
    _processing_progress_label,
    _processing_progress_snapshot,
    _processing_status_title,
)


def test_transcription_eta_uses_warm_model_lower_base():
    warm = _estimate_transcription_secs(8.0, warm=True)
    cold = _estimate_transcription_secs(8.0, warm=False)

    assert warm >= 1.4
    assert cold > warm


def test_processing_progress_caps_before_completion():
    progress, remaining = _processing_progress_snapshot(
        started_at=10.0,
        estimate_secs=2.0,
        now=30.0,
    )

    assert progress == 0.95
    assert remaining == 1


def test_processing_progress_label_shows_percent_and_eta():
    assert _processing_progress_label(0.42, 3) == "42%  ~3s"


def test_processing_status_title_shows_percent():
    assert _processing_status_title(0.42) == "\U0001f7e1 42%"
