"""Guard tests for the TTS daemons' inlined model pins (DEF-179).

The Kokoro and Qwen daemons run in a separate interpreter that cannot import
heyvox, so each inlines its model's pinned commit SHA. These tests:
  1. exercise the Qwen QWEN_TTS_MODEL override validation, and
  2. guard the inlined SHAs against drifting from the canonical registry in
     heyvox/model_pins.py.

Net-free: the daemon modules are loaded from file path (their module tops are
stdlib + numpy only; mlx-audio is imported lazily inside load_model). No model
load, no network.

References: .planning/DEFECT-LOG.md (DEF-179)
"""

import importlib.util
from pathlib import Path

import pytest

import heyvox
from heyvox import model_pins

_DAEMON_DIR = Path(heyvox.__file__).parent / "herald" / "daemon"


def _load_daemon(filename):
    path = _DAEMON_DIR / filename
    modname = "heyvox_test_" + filename.replace("-", "_").removesuffix(".py")
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def kokoro():
    return _load_daemon("kokoro-daemon.py")


@pytest.fixture(scope="module")
def qwen():
    return _load_daemon("qwen-daemon.py")


def test_kokoro_inlined_revision_matches_registry(kokoro):
    assert kokoro.MLX_MODEL_ID in model_pins.MODEL_REVISIONS
    assert kokoro.MLX_MODEL_REVISION == model_pins.MODEL_REVISIONS[kokoro.MLX_MODEL_ID], (
        "kokoro-daemon MLX_MODEL_REVISION drifted from heyvox/model_pins.py — "
        "update both together."
    )


def test_qwen_inlined_revision_matches_registry(qwen):
    default = qwen._QWEN_DEFAULT_MODEL
    assert default in model_pins.MODEL_REVISIONS
    assert qwen._MODEL_REVISIONS[default] == model_pins.MODEL_REVISIONS[default], (
        "qwen-daemon _MODEL_REVISIONS drifted from heyvox/model_pins.py — "
        "update both together."
    )


def test_qwen_default_when_env_unset(qwen, monkeypatch):
    monkeypatch.delenv("QWEN_TTS_MODEL", raising=False)
    model_id, revision = qwen._resolve_qwen_model()
    assert model_id == qwen._QWEN_DEFAULT_MODEL
    assert revision == qwen._MODEL_REVISIONS[qwen._QWEN_DEFAULT_MODEL]


def test_qwen_trusted_override_accepted_but_unpinned(qwen, monkeypatch):
    monkeypatch.setenv("QWEN_TTS_MODEL", "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16")
    model_id, revision = qwen._resolve_qwen_model()
    assert model_id == "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16"
    assert revision is None  # not a pinned default — loads unpinned (as intended)


def test_qwen_untrusted_override_rejected(qwen, monkeypatch):
    monkeypatch.setenv("QWEN_TTS_MODEL", "evil/backdoor-tts")
    model_id, revision = qwen._resolve_qwen_model()
    assert model_id == qwen._QWEN_DEFAULT_MODEL
    assert revision == qwen._MODEL_REVISIONS[qwen._QWEN_DEFAULT_MODEL]


def test_qwen_local_dir_override_accepted(qwen, monkeypatch, tmp_path):
    monkeypatch.setenv("QWEN_TTS_MODEL", str(tmp_path))
    model_id, revision = qwen._resolve_qwen_model()
    assert model_id == str(tmp_path)
    assert revision is None
