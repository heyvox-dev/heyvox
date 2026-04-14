"""
Text processing utilities for STT output.

Provides wake word stripping and garbled transcription detection.
These are pure functions (no global state, no side effects) extracted from
heyvox/main.py to enable isolated testing and reuse.

Requirements: DECOMP-03
"""
import re


# Common transcription variants of wake word model names.
# Whisper may transcribe "hey_jarvis_v0.1" as "Hey Jarvis", "hey jarvis",
# "Hey, Jarvis", "Hey Travis", "Hey Chavez", etc.
_WAKE_WORD_PHRASES: dict[str, list[str]] = {
    "hey_jarvis": [
        "hey jarvis", "hey, jarvis",
        "hey travis", "hey, travis",
        "hey chavez", "hey, chavez",
        "hey chavis", "hey, chavis",
        "hey charmis", "hey, charmis",
        "hey charvis", "hey, charvis",
        "hey charles", "hey, charles",
        "hey javis", "hey, javis",
        "hey javi", "hey, javi",
        "hey java", "hey, java",
        "hey job is", "hey job",
        "hey charisma",
        "hey javas", "hey, javas",
        "h-arvis", "h arvis",
        "jarvis", "jarvis.",
        "hrvs", "hrs", "hr",
        "j.a.r.v.i.s", "jar",
    ],
    "hey_vox": [
        "hey vox", "hey, vox",
        "hey box", "hey, box",
        "hey fox", "hey, fox",
        "hey vocs", "hey, vocs",
        "hey vokes", "hey, vokes",
        "hey vos", "hey, vos",
        "hey boks", "hey, boks",
        "hey vaux", "hey, vaux",
        "hey voxx", "hey, voxx",
        "hey rocks", "hey, rocks",
        "hey docs", "hey, docs",
        "hey locks", "hey, locks",
        "hey socks", "hey, socks",
        "he walks", "he vox", "he box",  # "hey vox" without the y
        "vox", "vox.",
    ],
}


def is_garbled(text: str) -> bool:
    """Detect garbled/nonsensical STT output from accidental wake word triggers.

    Catches common Whisper hallucination patterns:
    - Excessive repeated words/phrases
    - Mostly non-alphanumeric characters
    - Known Whisper filler hallucinations
    """
    cleaned = text.strip()
    if not cleaned:
        return False

    # Too short to be useful (single word that isn't a command)
    words = cleaned.split()
    if len(words) <= 1 and len(cleaned) < 4:
        return True

    # High ratio of repeated words (e.g. "the the the the")
    if len(words) >= 4:
        unique = set(w.lower() for w in words)
        if len(unique) / len(words) < 0.25:
            return True

    # Repeated phrases: split into bigrams and check repetition
    if len(words) >= 6:
        bigrams = [f"{words[i]} {words[i+1]}" for i in range(len(words) - 1)]
        unique_bigrams = set(b.lower() for b in bigrams)
        if len(unique_bigrams) / len(bigrams) < 0.3:
            return True

    # Mostly non-letter characters (Unicode garbage)
    alpha_chars = sum(1 for c in cleaned if c.isalpha())
    if len(cleaned) > 3 and alpha_chars / len(cleaned) < 0.4:
        return True

    # Known Whisper hallucination patterns
    hallucination_patterns = [
        r"^\.+$",                          # Just dots
        r"^[\s.,:;!?]+$",                  # Just punctuation
        r"(?i)^(thanks? for watching|subscribe)",  # YouTube artifacts
        r"(?i)^(music|applause|laughter)\s*$",     # Sound descriptions
        r"(?i)^you$",                       # Common short hallucination
    ]
    for pattern in hallucination_patterns:
        if re.match(pattern, cleaned):
            return True

    return False


def strip_wake_words(text: str, start_model: str, stop_model: str) -> str:
    """Remove wake word phrases from the beginning and end of transcription.

    Whisper transcribes the wake word along with the user's speech. Since the
    wake word is just a trigger mechanism, it should not appear in the injected
    text. Uses both an explicit phrase list AND a fuzzy regex fallback to catch
    novel Whisper mistranscriptions (e.g. "Hey Chavis", "Hey Job is").

    Args:
        text: Raw transcription from STT.
        start_model: Wake word model name for start trigger (e.g. "hey_jarvis_v0.1").
        stop_model: Wake word model name for stop trigger.

    Returns:
        Cleaned text with wake word phrases removed from start/end.
    """
    if not text:
        return text

    # Collect all known phrases for the configured wake word models
    phrases = set()
    for model in (start_model, stop_model):
        # Strip version suffix: "hey_jarvis_v0.1" → "hey_jarvis"
        # Only strip _v followed by a digit to avoid mangling names like "hey_vox"
        base = re.sub(r'_v\d[\d.]*$', '', model)
        if base in _WAKE_WORD_PHRASES:
            phrases.update(_WAKE_WORD_PHRASES[base])
        # Also add the raw model name as a phrase (underscores → spaces)
        phrases.add(base.replace("_", " "))

    # Sort longest first so "hey, jarvis" matches before "hey"
    sorted_phrases = sorted(phrases, key=len, reverse=True)

    cleaned = text.strip()

    # --- Pass 1: Exact phrase matching (handles known variants) ---
    stripped = False

    # Strip from end first (stop wake word)
    for phrase in sorted_phrases:
        lower = cleaned.lower().rstrip(" .,!?")
        if lower.endswith(phrase):
            idx = len(cleaned.rstrip(" .,!?")) - len(phrase)
            cleaned = cleaned[:idx].rstrip(" .,!?")
            stripped = True
            break

    # Strip from start (start wake word — happens with toggle mode)
    for phrase in sorted_phrases:
        lower = cleaned.lower().lstrip(" .,!?")
        if lower.startswith(phrase):
            cleaned = cleaned[len(phrase):].lstrip(" .,!?")
            stripped = True
            break

    # --- Pass 2: Fuzzy regex fallback (catches novel Whisper variants) ---
    # Matches "Hey <1-2 words>" at start/end that look like wake word attempts.
    # Only runs if the explicit list didn't already catch something.
    if not stripped:
        # Start: "Hey Jarvis/Chavis/Travis/etc." — 1-2 short words after "hey"
        cleaned = re.sub(
            r'^[Hh]ey[,.]?\s+\w{2,8}(\s+\w{2,5})?\s*[.,!?]*\s*',
            '', cleaned, count=1
        ).strip()
        # End: same pattern at the end of the text
        cleaned = re.sub(
            r'\s*[.,!?]*\s*[Hh]ey[,.]?\s+\w{2,8}(\s+\w{2,5})?[.,!?]*\s*$',
            '', cleaned, count=1
        ).strip()

    return cleaned.strip()
