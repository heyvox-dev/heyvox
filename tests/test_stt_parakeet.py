"""Tests for the Parakeet STT engine (sherpa-onnx NeMo TDT, bench 2026-10-16).

The real model is 650 MB and not in CI, so the recognizer is faked; what is
tested is HeyVox's own logic around it: silence padding, pause-aligned
chunking, the MLX fallback on empty results, and that recording.py's
engine-agnostic preload/warm calls do not touch MLX.
"""
from types import SimpleNamespace

import numpy as np
import pytest

from heyvox.audio import stt

SR = 16000


class _FakeRecognizer:
    def __init__(self, text="hallo welt"):
        self.text = text
        self.lengths = []

    def create_stream(self):
        rec = self

        class _Stream:
            def __init__(self):
                self.result = SimpleNamespace(text="")

            def accept_waveform(self, sr, samples):
                assert sr == SR
                rec.lengths.append(len(samples))

        return _Stream()

    def decode_stream(self, stream):
        stream.result.text = self.text


@pytest.fixture
def parakeet(monkeypatch):
    rec = _FakeRecognizer()
    monkeypatch.setattr(stt, "_engine", "parakeet")
    monkeypatch.setattr(stt, "_parakeet", rec)
    monkeypatch.setattr(stt, "_parakeet_fallback", True)
    monkeypatch.setattr(stt, "_mlx_model_id", "mlx-community/whisper-large-v3-turbo-german-f16-q4")
    return rec


def _chunks(secs):
    return [np.full(int(secs * SR), 1000, dtype=np.int16)]


def test_pads_half_a_second_on_both_ends(parakeet):
    assert stt.transcribe_audio(_chunks(3), engine="parakeet") == "hallo welt"
    pad = int(stt._PARAKEET_PAD_SECS * SR)
    assert parakeet.lengths == [3 * SR + 2 * pad]


def test_long_audio_is_cut_at_the_pause_into_bounded_chunks():
    speech = np.full(SR * 27, 0.3, dtype=np.float32)
    pause = np.zeros(SR // 2, dtype=np.float32)
    audio = np.concatenate([speech, pause, np.full(SR * 40, 0.3, dtype=np.float32)])
    chunks = stt._split_at_pauses(audio, SR)
    assert all(len(c) <= stt._PARAKEET_CHUNK_SECS * SR for c in chunks)
    assert sum(len(c) for c in chunks) == len(audio)
    first_cut = len(chunks[0])
    assert 27 * SR <= first_cut <= 27 * SR + SR // 2, "cut must land inside the pause"


def test_short_audio_is_one_chunk():
    audio = np.ones(SR * 10, dtype=np.float32)
    assert len(stt._split_at_pauses(audio, SR)) == 1


def _with_fake_mlx(monkeypatch, result="vom fallback"):
    calls = []
    real = stt.transcribe_audio

    def fake(chunks, engine="mlx", **kw):
        if engine == "mlx":
            calls.append(kw.get("mlx_model"))
            return result
        return real(chunks, engine=engine, **kw)

    monkeypatch.setattr(stt, "transcribe_audio", fake)
    return fake, calls


def test_empty_result_falls_back_to_mlx(parakeet, monkeypatch):
    parakeet.text = ""
    fake, calls = _with_fake_mlx(monkeypatch)
    assert fake(_chunks(9), engine="parakeet") == "vom fallback"
    assert calls == ["mlx-community/whisper-large-v3-turbo-german-f16-q4"]


def test_no_fallback_for_sub_second_noise(parakeet, monkeypatch):
    parakeet.text = ""
    fake, calls = _with_fake_mlx(monkeypatch)
    assert fake(_chunks(0.5), engine="parakeet") == ""
    assert calls == []


def test_no_fallback_when_disabled(parakeet, monkeypatch):
    parakeet.text = ""
    monkeypatch.setattr(stt, "_parakeet_fallback", False)
    fake, calls = _with_fake_mlx(monkeypatch)
    assert fake(_chunks(9), engine="parakeet") == ""
    assert calls == []


def test_preload_does_not_load_mlx_for_parakeet(parakeet, monkeypatch):
    loads = []
    monkeypatch.setattr(stt, "_load_mlx_model", lambda: loads.append(1))
    monkeypatch.setattr(stt.threading, "Thread",
                        lambda target, daemon: SimpleNamespace(start=target))
    stt._mlx_loaded.clear()
    stt.preload_model()
    assert loads == []
    assert stt.model_loaded() is True


def test_bpe_vocab_from_tokens(tmp_path):
    tokens = tmp_path / "tokens.txt"
    tokens.write_text("<unk> 0\n▁Or 1\nca 2\n<blk> 3\n", encoding="utf-8")
    out = stt._parakeet_bpe_vocab(str(tokens), str(tmp_path / "cache"))
    assert open(out, encoding="utf-8").read() == "<unk>\t0\n▁Or\t-1\nca\t-2\n<blk>\t-3\n"
