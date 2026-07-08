"""Herald TTS helpers — pure functions shared by worker.py and watcher.py.

Closes DEFECT-LOG pattern **P-producer-parity**: when two code paths can
produce the same artifact (TTS WAV file), they drift silently until a user
reports the difference. Symptoms over the past months:

- DEF-111: workspace-label prepend lived in watcher.py for months before
  worker.py grew it.
- watcher.py:282 has a ``detect_mood_voice`` whose docstring literally says
  "Match the mood detection in worker.py" — a comment is not enforcement.
- ``_apply_verbosity`` / ``_get_verbosity`` were duplicated with slightly
  different code paths.

This module is the single source of truth. Both producers import from here.

Scope intentionally narrow: only the *pure* shared logic
(mood detection, verbosity filtering, last-TTS-block extraction, voice
mapping). The Kokoro/Qwen IPC, queue enqueue, sidecar writes, and timing
sidecar continue to live in their respective producers — those have
diverged enough that sharing them would require a bigger refactor and the
existing dedup (TTS_DEDUP_SECS) covers them anyway.

Watcher.py remains in the codebase as an explicit fallback producer
(catches TTS when the Claude Code Stop hook didn't fire, e.g. unmanaged
Claude installations). Once we have N weeks of production data showing
WATCHER_FIRED is rare, the watcher can be retired and worker.py becomes
the sole producer.
"""

from __future__ import annotations

import os
import re
import tempfile
from typing import Optional


# ---------------------------------------------------------------------------
# Mood detection
# ---------------------------------------------------------------------------

# Source of truth for mood → Kokoro voice mapping. Both producers consume.
MOOD_VOICES: dict[str, str] = {
    "neutral": "af_sarah",
    "cheerful": "af_heart",
    "alert": "af_nova",
    "thoughtful": "af_sky",
}

DEFAULT_VOICE: str = "af_sarah"

# Keyword lists kept here so they stay aligned across producers. Adding a
# word to one path used to require remembering to mirror it in the other —
# the drift was the bug, not the keywords themselves.
ALERT_WORDS = (
    "error", "fail", "broke", "crash", "warning", "careful",
    "danger", "critical", "urgent", "problem", "bug",
)

CHEERFUL_WORDS = (
    "done", "success", "passed", "complete", "fixed", "great",
    "perfect", "working", "deployed", "shipped", "merged",
    "awesome", "congrats", "excellent",
)

THOUGHTFUL_WORDS = (
    "should we", "want me to", "would you", "what do you",
    "how about", "shall i", "let me know", "hmm", "consider", "interesting",
)


def detect_mood(text: str) -> str:
    """Detect emotional mood. Returns 'alert' | 'cheerful' | 'thoughtful' | 'neutral'.

    Priority order is intentional: alert wins over cheerful when both fire,
    because alert is the higher-urgency signal. Empty input → 'neutral'.
    """
    if not text:
        return "neutral"
    t = text.lower()
    if any(w in t for w in ALERT_WORDS):
        return "alert"
    if any(w in t for w in CHEERFUL_WORDS):
        return "cheerful"
    if any(w in t for w in THOUGHTFUL_WORDS):
        return "thoughtful"
    return "neutral"


def mood_voice(text: str) -> str:
    """Shortcut: detect mood and return the matching Kokoro voice.

    Replaces watcher.py's ``detect_mood_voice`` — same end result, but routed
    through the shared mood vocabulary so future keyword additions land in
    both producers automatically.
    """
    return MOOD_VOICES.get(detect_mood(text), DEFAULT_VOICE)


# ---------------------------------------------------------------------------
# Verbosity
# ---------------------------------------------------------------------------

VERBOSITY_LEVELS = ("full", "short", "skip")


def _verbosity_file_path() -> str:
    """Re-resolve VERBOSITY_FILE on every call (testable via monkeypatch)."""
    try:
        from heyvox.constants import VERBOSITY_FILE
        return VERBOSITY_FILE
    except ImportError:
        # Watcher runs as a standalone script in some shim configurations.
        _tmp = os.environ.get("TMPDIR", tempfile.gettempdir()).rstrip("/")
        return f"{_tmp}/heyvox-verbosity"


def get_verbosity() -> str:
    """Read verbosity from the shared state file. Returns 'full' if absent."""
    try:
        with open(_verbosity_file_path()) as f:
            level = f.read().strip()
    except (FileNotFoundError, OSError):
        return "full"
    return level if level in VERBOSITY_LEVELS else "full"


def apply_verbosity(text: str, verbosity: str) -> Optional[str]:
    """Apply playback filtering. Returns None when the TTS should be dropped.

    - 'skip' → None
    - 'short' → first sentence (or first 100 chars)
    - 'full' / 'summary' → unchanged
    """
    if verbosity == "skip":
        return None
    if verbosity == "short":
        match = re.search(r"[.!?]", text)
        if match:
            return text[:match.end()].strip()
        return text[:100]
    return text


# ---------------------------------------------------------------------------
# TTS block extraction (watcher's polling-friendly version)
# ---------------------------------------------------------------------------

_TTS_BLOCK_RE = re.compile(r"<tts>(.*?)</tts>", re.DOTALL)

_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_SPEECH_MARKUP_RE = re.compile(r"[*`#\\]")


def strip_code_spans(text: str) -> str:
    """Remove markdown code (fenced blocks + inline `spans`) from text.

    DEF-194: when the assistant explains the TTS mechanism it writes
    ``<tts>``/``</tts>`` literally inside backticks. Matching
    ``<tts>...</tts>`` anywhere then spliced the prose between two such
    literals into the spoken text, so Kokoro voiced raw markdown aloud
    ("backtick", "asterisk", "backslash"). Real <tts> blocks are never
    wrapped in code formatting, so dropping code spans first leaves only
    genuine blocks.
    """
    text = _CODE_FENCE_RE.sub("", text)
    return _INLINE_CODE_RE.sub("", text)


def strip_speech_markup(text: str) -> str:
    """Strip markdown/escape marks a TTS engine would mis-voice (DEF-194).

    Fenced code is dropped (code isn't speech); stray emphasis, heading,
    backtick and backslash characters are removed so Kokoro never reads
    "asterisk"/"backtick"/"backslash" literally. Word content is kept.
    """
    text = _CODE_FENCE_RE.sub("", text)
    text = _SPEECH_MARKUP_RE.sub("", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def extract_last_tts_block(
    text: str,
    *,
    min_chars: int = 5,
    max_trailing_chars: int = 50,
    require_at_end_fraction: float = 0.5,
) -> Optional[str]:
    """Return the last ``<tts>...</tts>`` block iff it sits near the tail.

    Used by watcher.py's polling path — it tails JSONL transcripts and
    catches TTS that the Stop-hook race already missed or hasn't yet
    fired. The "near the tail" heuristic exists so we don't speak a
    transcript message that the user moved past long ago.

    Returns None when:
    - no <tts> block found
    - content empty / ``SKIP`` / shorter than ``min_chars``
    - opening tag sits in the first half of the transcript
    - trailing text after the block exceeds ``max_trailing_chars``

    Worker.py's HeraldWorker._extract_tts_blocks is more sophisticated
    (handles multi-block, position-aware) — it stays in worker.py.
    """
    if not text:
        return None
    text = strip_code_spans(text)  # DEF-194: drop literal `<tts>` mentions in code
    matches = _TTS_BLOCK_RE.findall(text)
    if not matches:
        return None
    speech = matches[-1].strip()
    if not speech or speech == "SKIP" or len(speech) < min_chars:
        return None
    last_open = text.rfind("<tts>")
    if last_open < len(text) * require_at_end_fraction:
        return None
    last_close = text.rfind("</tts>")
    remaining = text[last_close + len("</tts>"):].strip() if last_close >= 0 else ""
    if len(remaining) > max_trailing_chars:
        return None
    return speech


__all__ = [
    "MOOD_VOICES",
    "strip_code_spans",
    "strip_speech_markup",
    "DEFAULT_VOICE",
    "ALERT_WORDS",
    "CHEERFUL_WORDS",
    "THOUGHTFUL_WORDS",
    "VERBOSITY_LEVELS",
    "detect_mood",
    "mood_voice",
    "get_verbosity",
    "apply_verbosity",
    "extract_last_tts_block",
]
