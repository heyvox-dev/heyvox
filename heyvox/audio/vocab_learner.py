"""
Vocabulary learner for STT initial_prompt biasing (Phase 16).

Off-the-hot-path batch pipeline that:
  1. Prefilters transcripts (gibberish, wake-word forms).
  2. Sends batches to Claude Haiku (subscription CLI primary, API-key secondary).
  3. Validates every item with Pydantic.
  4. Merges into a cumulative corpus-frequency store (idempotent).
  5. Gates by frequency + confidence, then caps to the Whisper 223-token limit.

Privacy contract: when cfg.enabled is False this module performs ZERO subprocess/
network calls. The fail-closed guard is the very first statement in learn_vocab().

Usage (from heyvox CLI):
    heyvox learn-vocab [--dry-run] [--reset] [--eval]

Requirements: Phase 16 plan 03.
"""

import json
import logging
import re
import subprocess                # MODULE-LEVEL so monkeypatch.setattr("...vocab_learner.subprocess.run") works
from pathlib import Path
from typing import Any, Literal

from platformdirs import user_data_dir
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from heyvox.text_processing import _WAKE_WORD_PHRASES, strip_wake_words

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level data paths (mirrors heyvox/history.py pattern)
# ---------------------------------------------------------------------------

_DATA_DIR = Path(user_data_dir("heyvox"))
_VOCAB_STORE = _DATA_DIR / "vocab_store.json"
_PROMPT_FILE = _DATA_DIR / "stt_initial_prompt.txt"

# ---------------------------------------------------------------------------
# Module-level whisper tokenizer cache (Open Question 3 — avoid per-call load)
# ---------------------------------------------------------------------------

_TOKENIZER = None


def _get_whisper_tokenizer():
    """Return the module-level cached Whisper tokenizer, loading it on first call."""
    global _TOKENIZER
    if _TOKENIZER is None:
        from mlx_whisper.tokenizer import get_tokenizer
        _TOKENIZER = get_tokenizer(multilingual=True)
    return _TOKENIZER


# ---------------------------------------------------------------------------
# Wake-word guardrail vocabulary (belt-and-suspenders, non-negotiable 1)
# ---------------------------------------------------------------------------

# Canonical variants from _WAKE_WORD_PHRASES (lowercased).
_WAKE_VARIANTS: frozenset[str] = frozenset(
    p for variants in _WAKE_WORD_PHRASES.values() for p in variants
)

# Spike-observed garbled wake forms NOT in the canonical trigger list.
# These are GLOSSARY-side rejections only — they supplement the canonical
# list for the glossary guardrail but are NOT a second wake-word source.
_EXTRA_WAKE_FORMS: frozenset[str] = frozenset({
    "apox", "heybox", "haywax", "haywalk", "hembox",
    "hoi box", "hand box", "wox", "wux",
})

# Separator-stripped ("squished") wake forms. The variant lists above are all
# SPACED ("hey vox"), so concatenated / hyphenated spellings — "HeyVox",
# "heyvox", "hey-vox" — slipped past every check and leaked into initial_prompt
# (DEF-145, found in UAT). Comparing a separator-free form closes that class.
_WAKE_SQUISHED: frozenset[str] = frozenset(
    re.sub(r"[\s\-_.,!?]+", "", w) for w in (_WAKE_VARIANTS | _EXTRA_WAKE_FORMS)
) | {"heyvox", "heyjarvis"}


def is_wake_word(text: str) -> bool:
    """Return True if *text* is (or starts/ends with) a wake-word form.

    Belt-and-suspenders:
    1. Exact-variant check against the canonical _WAKE_WORD_PHRASES vocabulary.
    2. Extra garbled forms from the spike's 25+ variant audit.
    3. Fuzzy strip_wake_words() pass — catches the "Hey X" family via regex.

    Used pre-LLM (transcript filter), in the GlossaryItem validator, and at
    merge time. A wake form must NEVER reach initial_prompt (Failure Mode 1).
    """
    if not text:
        return False
    norm = text.strip().lower().strip(".,!?")
    if norm in _WAKE_VARIANTS or norm in _EXTRA_WAKE_FORMS:
        return True
    # Separator-stripped match — catches "HeyVox" / "heyvox" / "hey-vox" (DEF-145)
    if re.sub(r"[\s\-_.,!?]+", "", norm) in _WAKE_SQUISHED:
        return True
    # Any canonical variant as a whole-phrase prefix/suffix
    for v in _WAKE_VARIANTS:
        if norm == v or norm.startswith(v + " ") or norm.endswith(" " + v):
            return True
    # Fuzzy fallback: if strip_wake_words removes everything → it was a wake form
    stripped = strip_wake_words(text, "hey_vox", "hey_jarvis").strip()
    return stripped == ""


def is_wake_tail(word: str) -> bool:
    """DEF-219: True if *word* alone is the tail of a known "hey <word>" wake
    form (e.g. "job" from a transcript ending "...Hey, job." — a garbled "Hey
    Vox"/"Hey Jarvis").

    is_wake_word() only recognises a FULL phrase ("hey job") — but the LLM
    extractor strips the leading "hey" before emitting a wrong/right pair, so
    a bare "job" slipped past every existing check even though "hey job" is
    already a known variant in _WAKE_WORD_PHRASES (confirmed live: 35 corpus
    hits, all "Hey, job."/"Hey, John." wake-word tails, none a real content
    correction — the resulting "job"->"John" entry reached the live
    initial_prompt). Checked against the curated _WAKE_VARIANTS set directly —
    deliberately NOT is_wake_word()'s fuzzy regex fallback (text_processing.py
    strip_wake_words Pass 2), which matches "hey" + almost any short word and
    would reject legitimate short glossary terms too (verified: it flagged
    "chat", "Nemo", "Muslima" as false positives in testing).
    """
    norm = word.strip().lower().strip(".,!?")
    return f"hey {norm}" in _WAKE_VARIANTS or f"hey, {norm}" in _WAKE_VARIANTS


# ---------------------------------------------------------------------------
# Pydantic schema
# ---------------------------------------------------------------------------

class GlossaryItem(BaseModel):
    """One STT mis-transcription correction entry extracted by the LLM.

    Validator rejects any wake-word form in `wrong` or `right`
    (belt-and-suspenders, non-negotiable 1).
    """
    wrong: str = Field(min_length=1, description="mis-transcribed form Whisper produced")
    right: str = Field(min_length=1, description="correct canonical spelling")
    kind: Literal["private", "public", "tech"]
    confidence: float = Field(ge=0.0, le=1.0)
    # User-curated entries bypass the corpus_freq/confidence gate and rank
    # first in the prompt. Never set by LLM extraction — manual store edits only.
    pinned: bool = False
    model_config = ConfigDict(extra="ignore")

    @field_validator("wrong", "right")
    @classmethod
    def strip_and_reject_wake(cls, v: str) -> str:
        v = v.strip()
        if is_wake_word(v) or is_wake_tail(v):
            raise ValueError(f"wake-word form rejected: {v!r}")
        return v


class GlossaryBatch(BaseModel):
    """Wrapper for the API-key forced-tool path."""
    items: list[GlossaryItem]


# ---------------------------------------------------------------------------
# Gibberish prefilter (guardrail layer 3)
# ---------------------------------------------------------------------------

_FILLER: frozenset[str] = frozenset({
    "hihi", "haha", "uhm", "uhm uhm", "hmm", "uh", "um", "ah",
})


def is_gibberish(text: str) -> bool:
    """Return True for transcripts that must never reach the LLM.

    Blocks: non-Latin script (< 50% ASCII letters), pure repetition, filler.
    Real spike examples: a pure-Sinhala line, "hihi", "Uhm Uhm".
    """
    cleaned = text.strip()
    if not cleaned:
        return True
    low = cleaned.lower().strip(".,!?")
    if low in _FILLER:
        return True
    letters = [c for c in cleaned if c.isalpha()]
    if letters:
        ascii_letters = [c for c in letters if c.isascii()]
        if len(ascii_letters) / len(letters) < 0.5:
            return True
    # Pure repetition: every word is the same token AND length >= 3
    words = cleaned.lower().split()
    if len(set(words)) == 1 and len(words) >= 3:
        return True
    return False


# ---------------------------------------------------------------------------
# Context Window / token-cap helpers
# ---------------------------------------------------------------------------

def build_initial_prompt(items: list[dict], max_terms: int) -> str:
    """Rank by corpus_freq, pack DISTINCT `right`-spellings until the 223-token cap.

    Uses the WHISPER tokenizer (not len(str)) — proper nouns and German
    compounds tokenise to more sub-tokens than character length implies.
    Safety margin: stop at 220 (3 tokens under the hard 223 clamp).

    Deduplicates by canonical spelling (case-insensitive): several mis-transcription
    forms map to the same `right`, and emitting it twice wastes the token budget
    (DEF-145 — e.g. "Claude Claude HeyVox HeyVox"). Also skips any wake form as a
    final belt-and-suspenders gate — a wake form must NEVER reach initial_prompt
    (non-negotiable 1), even if a stale store entry slipped past earlier filters.
    """
    tok = _get_whisper_tokenizer()
    # Pinned (user-curated) terms first, then by corpus frequency — pinned
    # entries must never be displaced by the max_terms/token cap.
    ranked = sorted(
        items,
        key=lambda r: (1 if r.get("pinned", False) else 0, r.get("corpus_freq", 0)),
        reverse=True,
    )
    out: list[str] = []
    seen: set[str] = set()
    used = 0
    for r in ranked:
        if len(out) >= max_terms:
            break
        piece = r["right"]
        # DEF-219: is_wake_tail must also see the ORIGINAL `wrong` form — the
        # leaked "job"->"John" pair only reveals itself as a wake-word tail
        # via "job" ("hey job"); "John" alone looks like a plausible term.
        if is_wake_word(piece) or is_wake_tail(piece) or is_wake_tail(r.get("wrong", "")):
            continue  # last gate: never let a wake form into the prompt
        key = piece.strip().lower()
        if key in seen:
            continue  # one slot per canonical spelling
        n = len(tok.encode(" " + piece))
        if used + n > 220:
            break
        out.append(piece)
        seen.add(key)
        used += n
    return " ".join(out)


# ---------------------------------------------------------------------------
# Deterministic eval scorer (shared by tests + `learn-vocab --eval`)
# ---------------------------------------------------------------------------
# Single source of truth: tests/test_autoglossary_eval.py imports these so the
# `--eval` CLI harness and the CI eval use identical scoring logic. Pure set
# arithmetic — NO subprocess / LLM (guarded by test_eval_does_not_shell_out).

_EVAL_WAKE_RIGHT: frozenset[str] = frozenset({"heyvox", "hey vox", "vox", "hey-vox"})
_EVAL_WAKE_WRONG = re.compile(
    r"\b(hey|hay|hai|high|hem|hand|a|ah|hoi)[ ,!-]*"
    r"(vox|box|wox|wux|wops|wax|walk|works|folks|john|jarvis|travis|chav\w*|"
    r"charlie|charvis|charmis|chuck|ciao|job|avis|hr\w*|child)\b",
    re.I,
)


def classify_eval(wrong: str, right: str, canonical: dict[str, str]) -> str:
    """Classify one glossary entry for eval scoring.

    Returns ``"wakeword"`` (a wake form leaked in — always a miss),
    ``"content-hit"`` (``right`` is a known canonical correction), or
    ``"other-FP"`` (a correction not in the ground-truth canonical set).
    ``canonical`` maps lowercased canonical spelling -> canonical spelling.
    """
    r, w = right.strip().lower(), wrong.strip().lower()
    if r in _EVAL_WAKE_RIGHT or _EVAL_WAKE_WRONG.search(w) or _EVAL_WAKE_WRONG.search(r):
        return "wakeword"
    if r in canonical:
        return "content-hit"
    return "other-FP"


def score_glossary(glossary: list[dict], ground_truth: dict) -> dict:
    """Deterministically score *glossary* against *ground_truth*'s canonical set.

    Pure set arithmetic — no live model or shell call. Returns recall,
    precision, the bucket counts (content / fp / wake), and the set of
    canonical terms hit.
    """
    canonical = {c.lower(): c for c in ground_truth.get("canonical_set", [])}
    buckets: dict[str, list] = {"content-hit": [], "wakeword": [], "other-FP": []}
    canon_hit: set[str] = set()
    for it in glossary:
        wrong, right = str(it.get("wrong", "")), str(it.get("right", ""))
        if not wrong or not right:
            continue
        cat = classify_eval(wrong, right, canonical)
        buckets[cat].append((wrong, right))
        if cat == "content-hit":
            canon_hit.add(canonical[right.strip().lower()])
    n_content = len(buckets["content-hit"])
    n_fp = len(buckets["other-FP"])
    total_canon = len(canonical)
    recall = len(canon_hit) / total_canon if total_canon else 0.0
    precision = n_content / (n_content + n_fp) if (n_content + n_fp) else 0.0
    return {
        "recall": recall,
        "precision": precision,
        "wake": len(buckets["wakeword"]),
        "content": n_content,
        "fp": n_fp,
        "canon_hit": canon_hit,
    }


# ---------------------------------------------------------------------------
# LLM extraction — primary (subscription CLI) path
# ---------------------------------------------------------------------------

SYSTEM = (
    "You are a JSON data-extraction function. "
    "Output ONLY a JSON array of objects, no prose, no markdown fences."
)


def _parse_json_array(text: str) -> list[dict]:
    """Strip markdown fences / prose and parse the first JSON *array* in *text*.

    Even with the JSON-only system prompt, Haiku wraps output in ```json … ```.
    Observed in the spike and verified at runtime on this machine.

    Uses an incremental decoder rather than a greedy ``\\[.*\\]`` regex: the
    greedy form spans from the first ``[`` to the LAST ``]``, so any bracketed
    prose before/after the real array (e.g. "see [1]") corrupted the match.
    We instead try ``raw_decode`` at each ``[`` until one yields a JSON list,
    which also ignores trailing prose after a valid array.
    """
    decoder = json.JSONDecoder()
    idx = text.find("[")
    while idx != -1:
        try:
            value, _ = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, list):
            return value
        idx = text.find("[", idx + 1)
    raise ValueError(f"no JSON array in CLI output: {text[:200]!r}")


def extract_batch(batch_text: str, model: str = "claude-haiku-4-5") -> list[dict]:
    """Call the claude CLI (subscription path) and return raw item dicts.

    Argv is a hardened list (shell=False is the default; never shell=True).
    --setting-sources "" drops the user's global CLAUDE.md (German + TTS).
    --tools ""           disables all built-in tools (no fs/bash/web).
    --permission-mode bypassPermissions keeps the empty toolset.
    --output-format json returns an envelope; model text is in .result.
    """
    proc = subprocess.run(
        [
            "claude", "-p", batch_text,
            "--model", model,
            "--output-format", "json",
            "--system-prompt", SYSTEM,
            "--setting-sources", "",
            "--tools", "",
            "--permission-mode", "bypassPermissions",
        ],
        capture_output=True, text=True, timeout=120, check=True,
    )
    envelope = json.loads(proc.stdout)
    return _parse_json_array(envelope["result"])


# ---------------------------------------------------------------------------
# LLM extraction — secondary (API-key) path
# ---------------------------------------------------------------------------

def extract_batch_api(batch_text: str, model: str = "claude-haiku-4-5") -> list[dict]:
    """Call the Anthropic API directly (API-key path — secondary for OSS users).

    Uses a single forced tool so structured output is guaranteed without
    fence-stripping. Imports `anthropic` INSIDE the function — it is an
    optional dep (heyvox[learn-vocab] extra).

    Raises RuntimeError if the `anthropic` package is not installed or
    ANTHROPIC_API_KEY is not set.
    """
    import os
    try:
        import anthropic
    except ImportError:
        raise RuntimeError(
            "anthropic package not installed. "
            "Install it with: pip install 'heyvox[learn-vocab]'"
        ) from None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. "
            "Set the environment variable or use the subscription path (provider=subscription)."
        )
    client = anthropic.Anthropic(api_key=api_key)
    SCHEMA = {
        "type": "object",
        "properties": {"items": {"type": "array", "items": GlossaryItem.model_json_schema()}},
        "required": ["items"],
    }
    resp = client.messages.create(
        model=model,
        max_tokens=2048,
        temperature=0,
        system=SYSTEM,
        tools=[{
            "name": "emit_glossary",
            "description": "Return extracted STT mis-transcriptions.",
            "input_schema": SCHEMA,
        }],
        tool_choice={"type": "tool", "name": "emit_glossary"},
        messages=[{"role": "user", "content": batch_text}],
    )
    items_raw = next(
        b.input for b in resp.content if b.type == "tool_use"
    )["items"]
    return items_raw


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

def build_extraction_prompt(batch: list[str], seeds: list[str]) -> str:
    """Assemble the user-turn prompt for one extraction batch.

    Includes:
    - Known correct spellings (seed list — guardrail layer 2)
    - Inline corpus-real few-shot examples (public + private)
    - One negative example (ordinary words must not be corrected)
    - Numbered transcript batch
    """
    seed_block = ", ".join(seeds) if seeds else "(none)"
    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(batch))
    return (
        f"Known correct spellings (private terms — always use exactly as spelled):\n"
        f"{seed_block}\n\n"
        f"Examples (correct):\n"
        f'  {{"wrong":"Cloud","right":"Claude","kind":"public","confidence":0.9}}\n'
        f'  {{"wrong":"zero","right":"Xero","kind":"public","confidence":0.85}}\n'
        f'  {{"wrong":"Harold","right":"Herald","kind":"private","confidence":0.95}}\n'
        f'  {{"wrong":"Engrid","right":"ngrid","kind":"private","confidence":0.9}}\n\n'
        f"Negative example: do NOT correct ordinary words like Mitte, Java.\n\n"
        f"Transcripts to analyse:\n{numbered}\n\n"
        f"Return a JSON array of {{wrong, right, kind, confidence}} objects for real "
        f"recurring STT mis-transcriptions only. Empty array [] if none found."
    )


# ---------------------------------------------------------------------------
# Corpus frequency counter
# ---------------------------------------------------------------------------

def count_in_corpus(wrong: str, transcripts: list[str]) -> int:
    """Return the number of transcripts containing *wrong* as a whole word/phrase.

    DEF-218: a plain substring match over-counts any short `wrong` that occurs
    inside longer words — "jet" inside "jetzt"/"project"/"subject" inflated its
    corpus_freq to 371+ vs. 3 real standalone occurrences, making it outrank
    every other glossary term for the initial_prompt token budget. Word-boundary
    matching (case-insensitive) fixes short substrings while still matching
    multi-word phrases like "black hole" as a unit.
    """
    pattern = re.compile(r"\b" + re.escape(wrong) + r"\b", re.IGNORECASE)
    return sum(1 for t in transcripts if pattern.search(t))


# ---------------------------------------------------------------------------
# Store load / save
# ---------------------------------------------------------------------------

def passes_promotion_gate(rec: dict, min_frequency: int, min_confidence: float) -> bool:
    """Guardrail layer 4: corpus_freq + confidence gate for prompt promotion.

    Pinned (user-curated) entries bypass the gate — they exist precisely
    because the term has not (yet) been heard often enough to promote itself.
    """
    if rec.get("pinned", False):
        return True
    return (
        rec.get("corpus_freq", 0) >= min_frequency
        and rec.get("confidence", 0.0) >= min_confidence
    )


def load_store(path: Path = _VOCAB_STORE) -> dict:
    """Load the cumulative vocab store. Returns {} on any error (swallow-and-log)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("vocab_store read error (%s): %s — starting empty", path, e)
        return {}


def save_store(store: dict, path: Path = _VOCAB_STORE) -> None:
    """Persist the cumulative vocab store to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(store, fh, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Idempotent merge
# ---------------------------------------------------------------------------

def merge_store(
    raw_items: list[dict],
    transcripts: list[str],
    store: dict,
) -> None:
    """Merge validated items into *store* IN-PLACE (idempotent).

    corpus_freq is RECOMPUTED from the corpus on every run — never blindly
    incremented — so re-running yields the same result (non-negotiable /
    Failure Mode handling).

    Belt-and-suspenders: wake-word forms are post-filtered again even if they
    somehow passed Pydantic validation.

    Schema per RESEARCH: {wrong, right, kind, confidence, corpus_freq, first_seen}.
    """
    from datetime import date as _date
    today = _date.today().isoformat()
    for obj in raw_items:
        try:
            item = GlossaryItem.model_validate(obj)
        except ValidationError as exc:
            log.warning("merge_store: dropped malformed item %r: %s", obj, exc)
            continue
        wrong = item.wrong
        right = item.right
        # Belt-and-suspenders post-filter (non-negotiable 1)
        if is_wake_word(wrong) or is_wake_word(right) or is_wake_tail(wrong) or is_wake_tail(right):
            log.debug("merge_store: dropped wake-word item wrong=%r right=%r", wrong, right)
            continue
        freq = count_in_corpus(wrong, transcripts)
        if wrong in store:
            rec = store[wrong]
            rec["right"] = right
            rec["kind"] = item.kind
            rec["confidence"] = item.confidence
            rec["corpus_freq"] = freq  # recomputed, not incremented
            # preserve first_seen
        else:
            store[wrong] = {
                "wrong": wrong,
                "right": right,
                "kind": item.kind,
                "confidence": item.confidence,
                "corpus_freq": freq,
                "first_seen": today,
            }


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------

def learn_vocab(
    cfg: Any,
    dry_run: bool = False,
    run_eval: bool = False,
    model_override: str | None = None,
    max_terms_override: int | None = None,
    min_frequency_override: int | None = None,
    reset: bool = False,
    store_path: Path = _VOCAB_STORE,
    transcripts_path: Path | None = None,
) -> dict:
    """Off-hot-path batch orchestrator for vocabulary extraction.

    Privacy fail-closed contract: if cfg.enabled is False this function
    returns immediately — ZERO subprocess/network/file-read of transcripts
    occurs before this guard.

    Returns a summary dict:
        {enabled, extracted, dropped, skipped_batches, promoted, token_count, prompt}
    """
    # -------------------------------------------------------------------------
    # PRIVACY FAIL-CLOSED — this MUST be the first check (non-negotiable 2)
    # -------------------------------------------------------------------------
    if not getattr(cfg, "enabled", False):
        log.info(
            "vocab_learner disabled (config.vocab_learner.enabled=False) — skipping extraction"
        )
        return {"enabled": False, "extracted": 0, "promoted": 0}

    # -------------------------------------------------------------------------
    # Resolve effective parameters
    # -------------------------------------------------------------------------
    model = model_override or getattr(cfg, "model", "claude-haiku-4-5")
    max_terms = max_terms_override if max_terms_override is not None else getattr(cfg, "max_terms", 30)
    min_frequency = min_frequency_override if min_frequency_override is not None else getattr(cfg, "min_frequency", 2)
    min_confidence = getattr(cfg, "min_confidence", 0.6)
    seeds = list(getattr(cfg, "seeds", []))
    provider = getattr(cfg, "provider", "subscription")

    # -------------------------------------------------------------------------
    # Load transcripts
    # -------------------------------------------------------------------------
    if transcripts_path is None:
        transcripts_path = _DATA_DIR / "transcripts.jsonl"
    transcripts_path = Path(transcripts_path)
    texts: list[str] = []
    if transcripts_path.exists():
        try:
            with open(transcripts_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        t = rec.get("text", "")
                        if t:
                            texts.append(t)
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            log.warning("learn_vocab: could not read transcripts from %s: %s", transcripts_path, e)
    else:
        log.info("learn_vocab: transcripts file not found at %s — nothing to learn from", transcripts_path)

    # -------------------------------------------------------------------------
    # Prefilter (guardrail layers 1 + 3)
    # -------------------------------------------------------------------------
    candidates = [t for t in texts if not is_gibberish(t) and not is_wake_word(t)]
    log.info(
        "learn_vocab: %d raw transcripts → %d after prefilter",
        len(texts), len(candidates),
    )

    # -------------------------------------------------------------------------
    # Load (or reset) the cumulative store
    # -------------------------------------------------------------------------
    store: dict = {} if reset else load_store(store_path)

    # -------------------------------------------------------------------------
    # Batch extraction
    # -------------------------------------------------------------------------
    _chunk_size = 50
    chunks = [candidates[i:i + _chunk_size] for i in range(0, len(candidates), _chunk_size)]

    extracted = 0
    dropped = 0
    skipped_batches = 0

    for chunk in chunks:
        prompt_text = build_extraction_prompt(chunk, seeds)
        raw_items: list[dict] = []
        for attempt in range(2):
            try:
                if provider == "api":
                    raw_items = extract_batch_api(prompt_text, model=model)
                else:
                    raw_items = extract_batch(prompt_text, model=model)
                break
            except Exception as exc:
                if attempt == 0:
                    log.warning(
                        "learn_vocab: batch failed (%s), retrying once with nudge …", exc
                    )
                    prompt_text = (
                        prompt_text
                        + "\n\nReturn ONLY a JSON array, no prose."
                    )
                else:
                    log.error(
                        "learn_vocab: batch failed twice, skipping: %s", exc
                    )
                    skipped_batches += 1
                    raw_items = []

        for obj in raw_items:
            try:
                GlossaryItem.model_validate(obj)
                extracted += 1
            except ValidationError as exc:
                log.warning("learn_vocab: dropped malformed item %r: %s", obj, exc)
                dropped += 1
                continue

        # Merge validated items from this batch
        valid_items = []
        for obj in raw_items:
            try:
                valid_items.append(GlossaryItem.model_validate(obj).model_dump())
            except ValidationError:
                pass
        merge_store(valid_items, texts, store)

    # -------------------------------------------------------------------------
    # Gate: corpus_freq + confidence (guardrail layer 4)
    # -------------------------------------------------------------------------
    kept = [
        r for r in store.values()
        if passes_promotion_gate(r, min_frequency, min_confidence)
    ]

    # -------------------------------------------------------------------------
    # Cap to top-N (guardrail layer 5) and render
    # -------------------------------------------------------------------------
    prompt_str = build_initial_prompt(kept, max_terms=max_terms)
    tok = _get_whisper_tokenizer()
    token_count = len(tok.encode(prompt_str)) if prompt_str else 0

    # -------------------------------------------------------------------------
    # Optional eval (--eval): score the promoted glossary against the bundled
    # ground-truth fixture. Meaningful when learning from the frozen eval/spike
    # corpus; in a from-source checkout the fixture lives under tests/fixtures.
    # Never crashes the run — failures are reported, not raised.
    # -------------------------------------------------------------------------
    eval_result: dict | None = None
    if run_eval:
        gt_path = (
            Path(__file__).resolve().parents[2]
            / "tests" / "fixtures" / "autoglossary" / "ground_truth.json"
        )
        if gt_path.exists():
            try:
                gt = json.loads(gt_path.read_text(encoding="utf-8"))
                es = score_glossary(kept, gt)
                eval_result = {
                    "recall": round(es["recall"], 3),
                    "precision": round(es["precision"], 3),
                    "content": es["content"],
                    "fp": es["fp"],
                    "wake": es["wake"],
                }
            except Exception as e:
                log.warning("learn_vocab: eval scoring failed: %s", e)
                eval_result = {"error": str(e)}
        else:
            eval_result = {"available": False, "reason": f"ground_truth fixture not found at {gt_path}"}

    # -------------------------------------------------------------------------
    # Persist results (unless dry_run)
    # -------------------------------------------------------------------------
    if not dry_run:
        save_store(store, store_path)
        # Diagnostic artifact ONLY — NOT read at runtime. STT biasing reads
        # config.stt.local.initial_prompt (written by `heyvox learn-vocab`).
        # This file lets the user inspect the rendered prompt (`cat stt_initial_prompt.txt`).
        try:
            _PROMPT_FILE.parent.mkdir(parents=True, exist_ok=True)
            _PROMPT_FILE.write_text(prompt_str, encoding="utf-8")
        except Exception as e:
            log.warning("learn_vocab: could not write diagnostic prompt file: %s", e)

    log.info(
        "learn_vocab: extracted=%d dropped=%d skipped_batches=%d "
        "promoted=%d token_count=%d",
        extracted, dropped, skipped_batches, len(kept), token_count,
    )

    return {
        "enabled": True,
        "extracted": extracted,
        "dropped": dropped,
        "skipped_batches": skipped_batches,
        "promoted": len(kept),
        "token_count": token_count,
        "prompt": prompt_str,
        "eval": eval_result,
    }
