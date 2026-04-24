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


_INTRA_TOKEN_REPEAT = re.compile(r"(.{2,3})\1{3,}")


def is_garbled(
    text: str,
    *,
    stt_secs: float | None = None,
    audio_secs: float | None = None,
) -> bool:
    """Detect garbled/nonsensical STT output from accidental wake word triggers.

    Catches common Whisper hallucination patterns:
    - Excessive repeated words/phrases (global)
    - Consecutive duplicate words (local run-length — catches tail repetition
      that a coherent prefix would otherwise dilute in the global ratio)
    - Intra-token substring repetition (e.g. "P's's's's's's")
    - Tail-window bigram repetition (clean prefix + garbled suffix)
    - Mostly non-alphanumeric characters
    - Known Whisper filler hallucinations

    Args:
        text: The STT transcription.
        stt_secs: Optional STT inference elapsed time (seconds). Combined with
            audio_secs, an abnormally high ratio is near-certain evidence that
            Whisper's temperature-fallback loop fired — the output is
            hallucinated even when repetition signals slip past text checks.
            Guarded by audio_secs ≥ 5.0 to avoid false-positives on cold-load
            transcriptions of short recordings.
        audio_secs: Optional audio duration (seconds). See stt_secs.
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

    # DEF-083: Consecutive duplicate words (run-length).
    # A coherent prefix can dilute the global unique-ratio check, so a local
    # run of identical words (e.g. "can can can can can can can" at the tail
    # of an otherwise sensible sentence) is a slam-dunk garbled signal.
    if len(words) >= 4:
        _norm = lambda w: w.lower().strip(".,!?'\"")  # noqa: E731
        run_len = 1
        for i in range(1, len(words)):
            if _norm(words[i]) and _norm(words[i]) == _norm(words[i - 1]):
                run_len += 1
                if run_len >= 4:
                    return True
            else:
                run_len = 1

    # DEF-083: Intra-token substring repetition. MLX Whisper's temperature
    # fallback occasionally emits a single token where a 2-3 char substring
    # repeats 4+ times (e.g. "P's's's's's's's's's's's's"). Legit contractions
    # ("surpass's") and onomatopoeia ("sooo") are unaffected because the
    # regex requires ≥ 4 consecutive copies of the captured group.
    for word in words:
        if len(word) >= 8 and _INTRA_TOKEN_REPEAT.search(word):
            return True

    # DEF-083: Tail-window bigram repetition. For longer outputs, compute the
    # unique-bigram ratio over the last 40% of words. Catches the "clean
    # prefix + garbled suffix" pattern that the global bigram check misses
    # because the coherent start dominates the denominator.
    if len(words) >= 10:
        tail_start = int(len(words) * 0.6)
        tail_words = words[tail_start:]
        if len(tail_words) >= 4:
            tail_bigrams = [
                f"{tail_words[i]} {tail_words[i + 1]}".lower()
                for i in range(len(tail_words) - 1)
            ]
            if tail_bigrams and len(set(tail_bigrams)) / len(tail_bigrams) < 0.4:
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

    # DEF-083: Abnormally slow STT on non-trivial audio. MLX whisper-small on
    # Apple Silicon runs ~10-25x realtime when warm, so a ratio > 0.3 over a
    # ≥ 5 s recording (excluding cold-load territory) means the temperature
    # fallback looped through multiple decoding attempts — near-certain
    # hallucination even when the text-level checks above didn't trip.
    if (
        stt_secs is not None and audio_secs is not None
        and stt_secs >= 5.0 and audio_secs >= 5.0
        and stt_secs / audio_secs > 0.3
    ):
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
        start_model: Wake word model name for start trigger (e.g. "hey_vox").
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

    # Strip from end (stop wake word) — repeat to catch multiple trailing instances
    # e.g. "some text. Hey box. Hey box" when detector misses first attempts
    for _ in range(5):  # Cap iterations to avoid infinite loop
        matched = False
        for phrase in sorted_phrases:
            lower = cleaned.lower().rstrip(" .,!?")
            if lower.endswith(phrase):
                idx = len(cleaned.rstrip(" .,!?")) - len(phrase)
                cleaned = cleaned[:idx].rstrip(" .,!?")
                stripped = True
                matched = True
                break
        if not matched:
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
