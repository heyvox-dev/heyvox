"""Pipeline unit-test scaffold for Phase 16 — STT Auto-Glossary.

All 11 test functions referenced in 16-VALIDATION.md.

These tests import from heyvox.audio.vocab_learner / heyvox.audio.stt
(created in plans 02/03), so they fail at collection until those land.
This is the correct Wave-0 RED state. Tests contain real assertions so they
go GREEN automatically when the implementation arrives.

Monkeypatch targets use the module where the symbol is used, per PATTERNS Pitfall 4:
  patch("heyvox.audio.vocab_learner.subprocess.run", ...)
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_run_return():
    """Return value shape verified in 16-PATTERNS.md line 455."""
    return MagicMock(
        stdout='{"type":"result","result":"[{\\"wrong\\":\\"Harold\\",\\"right\\":\\"Herald\\",\\"kind\\":\\"private\\",\\"confidence\\":0.95}]"}',
        returncode=0,
    )


def _make_vocab_cfg(**kwargs) -> SimpleNamespace:
    defaults = {
        "enabled": True,
        "provider": "subscription",
        "model": "claude-haiku-4-5",
        "max_terms": 30,
        "min_frequency": 2,
        "min_confidence": 0.6,
        "seeds": ["ngrid", "Herald", "Hush", "HeyVox", "Conductor"],
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# STT wiring tests (plans 02/03)
# ---------------------------------------------------------------------------

def test_init_local_stt_stores_prompt():
    """init_local_stt() must accept initial_prompt and store it as a module global
    for a prompt-robust turbo-class model (DEF-152 model-gate keeps it; a fragile
    model would clear it — covered by test_init_local_stt_gates_glossary_for_fragile_model)."""
    from heyvox.audio import stt
    stt.init_local_stt(engine="mlx", mlx_model="mlx-community/whisper-large-v3-turbo",
                       initial_prompt="Herald Hush")
    assert stt._mlx_initial_prompt == "Herald Hush"


def test_transcribe_kwargs_include_prompt():
    """transcribe_audio() passes initial_prompt kwarg to mlx_whisper when non-empty."""
    mlx_whisper = pytest.importorskip("mlx_whisper")
    from heyvox.audio import stt

    stt._mlx_initial_prompt = "Claude Xero"
    stt._mlx_model_id = "mlx-community/whisper-large-v3-turbo"  # turbo class → glossary active (DEF-152)
    stt._mlx_loaded.set()

    captured_kwargs = {}

    def fake_transcribe(audio, **kwargs):
        captured_kwargs.update(kwargs)
        return {"text": "test output", "segments": []}

    with patch.object(mlx_whisper, "transcribe", side_effect=fake_transcribe):
        stt.transcribe_audio([np.zeros(16000, dtype=np.int16)], engine="mlx")

    assert "initial_prompt" in captured_kwargs, "initial_prompt must be passed when non-empty"
    assert captured_kwargs["initial_prompt"] == "Claude Xero"


def test_transcribe_kwargs_no_prompt_when_empty():
    """transcribe_audio() must NOT pass initial_prompt kwarg when it is empty string."""
    mlx_whisper = pytest.importorskip("mlx_whisper")
    from heyvox.audio import stt

    stt._mlx_initial_prompt = ""
    stt._mlx_loaded.set()

    captured_kwargs = {}

    def fake_transcribe(audio, **kwargs):
        captured_kwargs.update(kwargs)
        return {"text": "test output", "segments": []}

    with patch.object(mlx_whisper, "transcribe", side_effect=fake_transcribe):
        stt.transcribe_audio([np.zeros(16000, dtype=np.int16)], engine="mlx")

    assert "initial_prompt" not in captured_kwargs, \
        "initial_prompt must NOT be in kwargs when empty — avoids biasing whisper with empty string"


# ---------------------------------------------------------------------------
# GlossaryItem validation tests (plan 02)
# ---------------------------------------------------------------------------

def test_glossary_item_rejects_wake_words():
    """GlossaryItem must reject wake-word forms in wrong or right (non-negotiable 1)."""
    from heyvox.audio.vocab_learner import GlossaryItem
    with pytest.raises(Exception):
        GlossaryItem(wrong="hey vox", right="Herald", kind="private", confidence=0.9)
    with pytest.raises(Exception):
        GlossaryItem(wrong="Heybox", right="HeyVox", kind="private", confidence=0.9)


def test_glossary_item_accepts_valid():
    """GlossaryItem accepts a well-formed, non-wake-word entry."""
    from heyvox.audio.vocab_learner import GlossaryItem
    item = GlossaryItem(wrong="Harold", right="Herald", kind="private", confidence=0.95)
    assert item.right == "Herald"


# ---------------------------------------------------------------------------
# Prefilter tests (plan 02)
# ---------------------------------------------------------------------------

def test_gibberish_prefilter():
    """is_gibberish blocks non-Latin script, pure repetition, and filler."""
    from heyvox.audio.vocab_learner import is_gibberish
    assert is_gibberish("hihi"), "pure repetition must be filtered"
    assert is_gibberish("ගන"), "non-Latin script must be filtered"
    assert not is_gibberish("There is something to volume"), "normal sentence must not be filtered"


# ---------------------------------------------------------------------------
# Cap discipline test (plan 02)
# ---------------------------------------------------------------------------

def test_cap_discipline():
    """build_initial_prompt with 200 items stays <= 223 whisper tokens."""
    pytest.importorskip("mlx_whisper")
    from heyvox.audio.vocab_learner import build_initial_prompt
    from mlx_whisper.tokenizer import get_tokenizer

    items = [
        {"right": f"Term{i}", "corpus_freq": 200 - i, "confidence": 0.9}
        for i in range(200)
    ]
    result = build_initial_prompt(items, max_terms=30)
    tok = get_tokenizer(multilingual=True)
    assert len(tok.encode(result)) <= 223, \
        f"initial_prompt exceeds 223 whisper tokens: {len(tok.encode(result))}"


# ---------------------------------------------------------------------------
# Merge store idempotency test (plan 02)
# ---------------------------------------------------------------------------

def test_merge_store_idempotent():
    """merge_store() is idempotent: running it twice produces the same result as once."""
    from heyvox.audio.vocab_learner import merge_store

    items = [
        {"wrong": "Harold", "right": "Herald", "kind": "private", "confidence": 0.95},
        {"wrong": "Cloud", "right": "Claude", "kind": "public", "confidence": 0.9},
    ]
    corpus = ["Harold was here", "Harold again", "Cloud code is great"]

    store_a: dict = {}
    merge_store(items, corpus, store_a)
    store_after_one = json.loads(json.dumps(store_a))

    merge_store(items, corpus, store_a)
    store_after_two = json.loads(json.dumps(store_a))

    assert store_after_one == store_after_two, \
        "merge_store() must be idempotent: second run must not change result"


# ---------------------------------------------------------------------------
# Privacy / extraction gating tests (plan 02)
# ---------------------------------------------------------------------------

def test_disabled_skips_extraction():
    """When enabled=False, learn_vocab must never call subprocess.run (privacy fail-closed)."""
    from heyvox.audio.vocab_learner import learn_vocab

    cfg = _make_vocab_cfg(enabled=False)
    with patch("heyvox.audio.vocab_learner.subprocess.run") as mock_run:
        learn_vocab(cfg=cfg)
        assert mock_run.call_count == 0, \
            "subprocess.run must not be called when vocab_learner.enabled=False"


def test_extract_batch_argv_is_hardened():
    """extract_batch() argv must include hardened security flags and must NOT use shell=True."""
    from heyvox.audio.vocab_learner import extract_batch

    with patch("heyvox.audio.vocab_learner.subprocess.run") as mock_run:
        mock_run.return_value = _make_mock_run_return()
        extract_batch("some test transcript text")

    assert mock_run.call_count >= 1, "subprocess.run must be called by extract_batch"
    call_kwargs = mock_run.call_args

    # argv is the first positional arg
    argv = call_kwargs[0][0] if call_kwargs[0] else call_kwargs.args[0]
    argv_str = " ".join(str(a) for a in argv)

    # Security hardening flags (non-negotiable 4 + AI-SPEC §3)
    assert "--setting-sources" in argv, "missing --setting-sources flag (prevents CLAUDE.md contamination)"
    assert "--tools" in argv, "missing --tools flag (must disable all built-in tools)"
    assert "--permission-mode" in argv, "missing --permission-mode flag"
    assert "bypassPermissions" in argv_str, "missing bypassPermissions value"
    assert "--system-prompt" in argv, "missing --system-prompt flag"
    assert "--output-format" in argv, "missing --output-format flag"
    assert "json" in argv_str, "missing json value for --output-format"

    # Must NOT use shell=True (command injection prevention)
    kw = call_kwargs[1] if call_kwargs[1] else call_kwargs.kwargs
    assert kw.get("shell", False) is not True, "shell=True is forbidden (command injection risk)"


# ---------------------------------------------------------------------------
# CLI registration test (plan 03)
# ---------------------------------------------------------------------------

def test_cli_learn_vocab_registered():
    """'learn-vocab' subcommand must be registered and the handler must exist."""
    from heyvox import cli
    assert hasattr(cli, "_cmd_learn_vocab"), \
        "_cmd_learn_vocab handler must be defined in heyvox.cli"


# ---------------------------------------------------------------------------
# Config-insertion test (plan 04 — Open Question 2 / Pitfall 2)
# ---------------------------------------------------------------------------

def test_update_config_inserts_initial_prompt_fresh(tmp_path, monkeypatch):
    """Open Question 2 / Pitfall 2: writing stt.local.initial_prompt into a config that lacks
    the key must INSERT it inside the existing stt.local: block, not append at top level."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "stt:\n"
        "  backend: local\n"
        "  local:\n"
        "    engine: mlx\n"
        "    language: \"\"\n"
    )
    monkeypatch.setattr("heyvox.config.CONFIG_FILE", cfg_file)
    from heyvox.config import update_config
    update_config(**{"stt.local.initial_prompt": "Claude Xero Herald"})
    text = cfg_file.read_text()
    # The key must be indented under stt.local (>=4 spaces), never at column 0.
    assert "initial_prompt: Claude Xero Herald" in text or 'initial_prompt: "Claude Xero Herald"' in text
    lines = [ln for ln in text.splitlines() if "initial_prompt:" in ln]
    assert lines, "initial_prompt not written"
    assert lines[0].startswith("    "), f"initial_prompt not nested under stt.local: {lines[0]!r}"


# ---------------------------------------------------------------------------
# Code-review regression guards (Phase 16 review — WR-02, WR-04)
# ---------------------------------------------------------------------------

def test_parse_json_array_ignores_bracket_prose():
    """WR-02: the parser must not be fooled by bracketed prose around the real
    array. The old greedy r'\\[.*\\]' spanned first-'[' to last-']', so leading
    '[note]' or trailing 'see [1]' corrupted the match."""
    from heyvox.audio.vocab_learner import _parse_json_array

    text = (
        "Here is the result [note: 2 items found]:\n"
        "```json\n"
        '[{"wrong":"Cloud","right":"Claude","kind":"public","confidence":0.9}]\n'
        "```\n"
        "(done — see [1])"
    )
    items = _parse_json_array(text)
    assert items == [{"wrong": "Cloud", "right": "Claude", "kind": "public", "confidence": 0.9}]


def test_parse_json_array_raises_when_no_array():
    """WR-02: still raises ValueError (caught by the retry loop) when no JSON array exists."""
    from heyvox.audio.vocab_learner import _parse_json_array

    with pytest.raises(ValueError):
        _parse_json_array("no array here, just prose")


def test_update_config_returns_false_on_missing_section(tmp_path, monkeypatch):
    """WR-04: writing a nested key whose intermediate section is absent must NOT
    silently succeed — update_config returns False so the CLI can warn instead of
    printing a misleading success line."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("wake_words:\n  start: hey_vox\n")  # no stt: section at all
    monkeypatch.setattr("heyvox.config.CONFIG_FILE", cfg_file)
    from heyvox.config import update_config

    result = update_config(**{"stt.local.initial_prompt": "Claude"})
    assert result is False
    assert "initial_prompt" not in cfg_file.read_text()


def test_update_config_returns_true_on_success(tmp_path, monkeypatch):
    """WR-04: a write into an existing section returns True."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("stt:\n  local:\n    engine: mlx\n")
    monkeypatch.setattr("heyvox.config.CONFIG_FILE", cfg_file)
    from heyvox.config import update_config

    result = update_config(**{"stt.local.initial_prompt": "Claude"})
    assert result is True
    assert "initial_prompt:" in cfg_file.read_text()


# ---------------------------------------------------------------------------
# UAT regression guards (DEF-145 — found in live Test 2 run)
# ---------------------------------------------------------------------------

def test_is_wake_word_catches_concatenated_forms():
    """DEF-145: concatenated/hyphenated wake spellings must be caught, not just spaced
    ones. "HeyVox" leaked into initial_prompt 4x because only "hey vox" was matched."""
    from heyvox.audio.vocab_learner import is_wake_word

    for form in ("HeyVox", "heyvox", "hey-vox", "Hey Vox", "hey vox", "HeyJarvis", "Vox"):
        assert is_wake_word(form), f"{form!r} should be flagged as a wake form"
    # Real corrections must still pass through.
    for term in ("Claude", "Herald", "Xero", "Geminicap", "Blackhole"):
        assert not is_wake_word(term), f"{term!r} should NOT be flagged as a wake form"


def test_build_initial_prompt_dedupes_and_skips_wake():
    """DEF-145: duplicate right-spellings collapse to one slot, and a wake form is
    dropped as the final belt-and-suspenders gate."""
    pytest.importorskip("mlx_whisper")  # build_initial_prompt needs the whisper tokenizer
    from heyvox.audio.vocab_learner import build_initial_prompt

    items = [
        {"right": "Claude", "corpus_freq": 10},
        {"right": "claude", "corpus_freq": 9},   # case-insensitive duplicate
        {"right": "HeyVox", "corpus_freq": 8},   # wake form → must be skipped
        {"right": "Xero", "corpus_freq": 7},
        {"right": "Xero", "corpus_freq": 6},      # duplicate
    ]
    toks = build_initial_prompt(items, max_terms=30).split()
    assert toks.count("Claude") == 1, f"Claude not deduped: {toks}"
    assert toks.count("Xero") == 1, f"Xero not deduped: {toks}"
    assert "HeyVox" not in toks, f"wake form leaked: {toks}"
    assert {t.lower() for t in toks} == {"claude", "xero"}


# ---------------------------------------------------------------------------
# Model-gate (DEF-152 option a — glossary only for prompt-robust turbo class)
# ---------------------------------------------------------------------------

def test_model_supports_glossary_predicate():
    """DEF-152: only the turbo class is prompt-robust; small/large-v3 collapse."""
    from heyvox.audio.stt import _model_supports_glossary

    for ok in ("mlx-community/whisper-large-v3-turbo",
               "mlx-community/whisper-large-v3-turbo-german-f16-q4"):
        assert _model_supports_glossary(ok), ok
    for bad in ("mlx-community/whisper-small-mlx", "mlx-community/whisper-large-v3", "", None):
        assert not _model_supports_glossary(bad), repr(bad)


def test_init_local_stt_gates_glossary_for_fragile_model():
    """DEF-152: init_local_stt drops the glossary for a prompt-fragile model and
    keeps it for a turbo-class model. Restores module globals to avoid pollution."""
    import heyvox.audio.stt as stt

    saved = (stt._mlx_initial_prompt, stt._mlx_model_id, stt._mlx_language)
    try:
        logs: list[str] = []
        stt.init_local_stt(engine="mlx", mlx_model="mlx-community/whisper-small-mlx",
                           initial_prompt="Claude Xero Herald", log_fn=logs.append)
        assert stt._mlx_initial_prompt == "", "glossary must be gated off for whisper-small"
        assert any("glossary DISABLED" in m for m in logs)

        stt.init_local_stt(engine="mlx",
                           mlx_model="mlx-community/whisper-large-v3-turbo-german-f16-q4",
                           initial_prompt="Claude Xero Herald", log_fn=logs.append)
        assert stt._mlx_initial_prompt == "Claude Xero Herald", "glossary must survive on turbo"
    finally:
        stt._mlx_initial_prompt, stt._mlx_model_id, stt._mlx_language = saved


def test_pinned_bypasses_promotion_gate():
    """User-curated (pinned) entries promote regardless of corpus_freq/confidence."""
    from heyvox.audio.vocab_learner import passes_promotion_gate

    fresh = {"wrong": "Cloud MD", "right": "CLAUDE.md", "corpus_freq": 1,
             "confidence": 0.95, "pinned": True}
    unpinned = {"wrong": "Cloud MD", "right": "CLAUDE.md", "corpus_freq": 1,
                "confidence": 0.95}
    organic = {"wrong": "Xerox", "right": "Xero", "corpus_freq": 5,
               "confidence": 0.9}
    assert passes_promotion_gate(fresh, min_frequency=2, min_confidence=0.6)
    assert not passes_promotion_gate(unpinned, min_frequency=2, min_confidence=0.6)
    assert passes_promotion_gate(organic, min_frequency=2, min_confidence=0.6)


def test_pinned_ranks_before_high_frequency_terms():
    """Pinned terms must not be displaced by the max_terms cap."""
    pytest.importorskip("mlx_whisper")  # build_initial_prompt needs the whisper tokenizer
    from heyvox.audio.vocab_learner import build_initial_prompt

    items = [
        {"wrong": f"w{i}", "right": f"Organic{i}", "corpus_freq": 100 + i,
         "confidence": 0.9}
        for i in range(5)
    ]
    items.append({"wrong": "Wispers", "right": "Whisper", "corpus_freq": 1,
                  "confidence": 0.95, "pinned": True})
    toks = build_initial_prompt(items, max_terms=3).split()
    assert "Whisper" in toks, f"pinned term displaced by cap: {toks}"
    assert len(toks) == 3


def test_glossary_item_accepts_pinned_field():
    from heyvox.audio.vocab_learner import GlossaryItem

    plain = GlossaryItem.model_validate(
        {"wrong": "Freshhold", "right": "Threshold", "kind": "tech", "confidence": 0.9}
    )
    assert plain.pinned is False
    pinned = GlossaryItem.model_validate(
        {"wrong": "Freshhold", "right": "Threshold", "kind": "tech",
         "confidence": 0.9, "pinned": True}
    )
    assert pinned.pinned is True
