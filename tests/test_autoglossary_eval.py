"""Deterministic scorer eval for Phase 16 — STT Auto-Glossary.

Scores the COMMITTED ceiling.json against ground_truth.json without any live LLM call.
Asserts recall >= 0.90 and precision >= 0.85 on the ceiling fixture.
Also guards that wake-word forms never leak into the content-hit bucket.

The scorer (classify_eval / score_glossary) lives in heyvox.audio.vocab_learner so the
`heyvox learn-vocab --eval` harness and this CI test share ONE implementation
(no duplication). NEVER shells out to `claude` — proven by test_eval_does_not_shell_out.
"""

import json
from pathlib import Path

from heyvox.audio.vocab_learner import classify_eval, score_glossary

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "autoglossary"

_GT = json.loads((FIXTURE_DIR / "ground_truth.json").read_text())
_CANON = {c.lower(): c for c in _GT["canonical_set"]}
_WAKE_RIGHT = {"heyvox", "hey vox", "vox", "hey-vox"}


def test_ceiling_recall_and_precision():
    """Deterministic — scores committed ceiling.json against ground_truth.json.
    NEVER calls the live `claude` CLI. Asserts recall >= 0.90 and precision >= 0.85."""
    ceiling = json.loads((FIXTURE_DIR / "ceiling.json").read_text())
    s = score_glossary(ceiling["glossary"], _GT)
    assert s["recall"] >= 0.90, f"recall {s['recall']:.2f} < 0.90; missed {set(_GT['canonical_set']) - s['canon_hit']}"
    assert s["precision"] >= 0.85, f"precision {s['precision']:.2f} < 0.85 (content={s['content']}, fp={s['fp']})"


def test_no_wake_words_in_promoted():
    """The 7 HeyVox entries in ceiling.json MUST classify as wakeword, never as content.
    Guards non-negotiable 1 + the initial_prompt-poisoning threat: a wake form must never be
    scored as a useful content correction."""
    ceiling = json.loads((FIXTURE_DIR / "ceiling.json").read_text())
    s = score_glossary(ceiling["glossary"], _GT)
    assert s["wake"] >= 7, f"expected >=7 wake entries bucketed, got {s['wake']}"
    # No content-hit may itself be a wake form.
    for wrong, right in [(str(i.get("wrong", "")), str(i.get("right", ""))) for i in ceiling["glossary"]]:
        if classify_eval(wrong, right, _CANON) == "content-hit":
            assert right.strip().lower() not in _WAKE_RIGHT


def test_eval_does_not_shell_out():
    # Guard: the shared scorer must never invoke the live LLM. Inspect the ACTUAL
    # source of the factored functions (they live in vocab_learner.py now, so a
    # test-file text scan would no longer prove anything).
    import inspect

    from heyvox.audio import vocab_learner

    for fn in (vocab_learner.score_glossary, vocab_learner.classify_eval):
        src = inspect.getsource(fn)
        assert "subprocess" not in src, f"{fn.__name__} must not shell out — no subprocess allowed"
        assert "claude" not in src, f"{fn.__name__} must not call the live LLM"
