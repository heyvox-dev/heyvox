"""Deterministic scorer eval for Phase 16 — STT Auto-Glossary.

Scores the COMMITTED ceiling.json against ground_truth.json without any live LLM call.
Asserts recall >= 0.90 and precision >= 0.85 on the ceiling fixture.
Also guards that wake-word forms never leak into the content-hit bucket.

NEVER shells out to `claude` — proven by test_eval_does_not_shell_out.
"""

import json
import re
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "autoglossary"

_GT = json.loads((FIXTURE_DIR / "ground_truth.json").read_text())
_CANON = {c.lower(): c for c in _GT["canonical_set"]}
_WAKE_RIGHT = {"heyvox", "hey vox", "vox", "hey-vox"}
_WAKE_WRONG = re.compile(
    r"\b(hey|hay|hai|high|hem|hand|a|ah|hoi)[ ,!-]*"
    r"(vox|box|wox|wux|wops|wax|walk|works|folks|john|jarvis|travis|chav\w*|"
    r"charlie|charvis|charmis|chuck|ciao|job|avis|hr\w*|child)\b",
    re.I,
)


def _classify(wrong: str, right: str):
    r, w = right.strip().lower(), wrong.strip().lower()
    if r in _WAKE_RIGHT or _WAKE_WRONG.search(w) or _WAKE_WRONG.search(r):
        return "wakeword"
    if r in _CANON:
        return "content-hit"
    return "other-FP"


def _score(glossary: list[dict]):
    buckets = {"content-hit": [], "wakeword": [], "other-FP": []}
    canon_hit = set()
    for it in glossary:
        wrong, right = str(it.get("wrong", "")), str(it.get("right", ""))
        if not wrong or not right:
            continue
        cat = _classify(wrong, right)
        buckets[cat].append((wrong, right))
        if cat == "content-hit":
            canon_hit.add(_CANON[right.strip().lower()])
    n_content = len(buckets["content-hit"])
    n_fp = len(buckets["other-FP"])
    recall = len(canon_hit) / len(_GT["canonical_set"])
    precision = n_content / (n_content + n_fp) if (n_content + n_fp) else 0.0
    return {"recall": recall, "precision": precision,
            "wake": len(buckets["wakeword"]), "content": n_content, "fp": n_fp,
            "canon_hit": canon_hit}


def test_ceiling_recall_and_precision():
    """Deterministic — scores committed ceiling.json against ground_truth.json.
    NEVER calls the live `claude` CLI. Asserts recall >= 0.90 and precision >= 0.85."""
    ceiling = json.loads((FIXTURE_DIR / "ceiling.json").read_text())
    s = _score(ceiling["glossary"])
    assert s["recall"] >= 0.90, f"recall {s['recall']:.2f} < 0.90; missed {set(_GT['canonical_set']) - s['canon_hit']}"
    assert s["precision"] >= 0.85, f"precision {s['precision']:.2f} < 0.85 (content={s['content']}, fp={s['fp']})"


def test_no_wake_words_in_promoted():
    """The 7 HeyVox entries in ceiling.json MUST classify as wakeword, never as content.
    Guards non-negotiable 1 + the initial_prompt-poisoning threat: a wake form must never be
    scored as a useful content correction."""
    ceiling = json.loads((FIXTURE_DIR / "ceiling.json").read_text())
    s = _score(ceiling["glossary"])
    assert s["wake"] >= 7, f"expected >=7 wake entries bucketed, got {s['wake']}"
    # No content-hit may itself be a wake form.
    for wrong, right in [(str(i.get("wrong", "")), str(i.get("right", ""))) for i in ceiling["glossary"]]:
        if _classify(wrong, right) == "content-hit":
            assert right.strip().lower() not in _WAKE_RIGHT


def test_eval_does_not_shell_out():
    # Guard: the IMPLEMENTATION section of this module must contain no shell-out.
    # We check the lines ABOVE this function (the module-level code that does scoring).
    src = Path(__file__).read_text()
    # Find where this function starts; check only the code before it.
    guard_fn = "def test_eval_does_not_shell_out"
    impl_section = src[: src.index(guard_fn)]
    _sub = "subprocess"
    _claude = "claude -p"
    assert _sub not in impl_section, f"found '{_sub}' in scorer implementation — no shell-out allowed"
    assert _claude not in impl_section, f"found '{_claude}' in scorer implementation — no live LLM allowed"
