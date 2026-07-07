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
        # DEF-154: single-word + "wax" variants observed live with
        # large-v3-turbo-german-f16-q4 ("Heybox!") and whisper-small ("Hey, Wax").
        # Deliberately NOT "heyvox": that is the product's written name and may
        # legitimately end a silence-stopped dictation.
        "heybox", "hey wax", "hey, wax",
        "vox", "vox.",
    ],
}


_INTRA_TOKEN_REPEAT = re.compile(r"(.{2,3})\1{3,}")


# DEF-137: Known full-string "silence hallucinations". whisper-large-v3 / turbo
# emit a small fixed set of credit-roll / filler phrases from NON-speech audio
# (silence, breath, Bluetooth-HFP line noise) where whisper-small returned "".
# The time-based guards below were tuned for small (~10-25x realtime); turbo
# hallucinates these FAST (ratio ~0.1) and short+coherent, so neither the
# repetition checks nor the DEF-133 yield gate fire. Matched as the ENTIRE
# normalised output (lower-cased, surrounding punctuation stripped) so the same
# words inside a real sentence ("vielen Dank, weiter geht's") are unaffected.
_SILENCE_HALLUCINATIONS = {
    # German (large-v3 / turbo)
    "vielen dank",
    "untertitel der amara.org-community",
    "untertitelung des zdf für funk, 2017",
    # English
    "thank you",
    "thanks for watching",
    "thank you for watching",
}


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
            audio_secs, a *catastrophic* ratio (> 0.6) on ≥ 5 s audio still
            forces discard as a belt-and-suspenders guard for hallucination
            shapes the text checks don't recognise (cf. DEF-075 with ratio
            0.66). Moderate ratios (0.3–0.6) are NOT a discard signal on their
            own — DEF-081's tighter compression/logprob thresholds intentionally
            invoke temperature fallback on borderline audio, which costs time
            but produces clean output. Discarding clean output because
            inference was slow throws away exactly the cases DEF-081 was
            designed to rescue (cf. DEF-093). Text-level checks above remain
            authoritative for most hallucination shapes.
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

    # DEF-137: full-string silence hallucination (see _SILENCE_HALLUCINATIONS).
    # Time-independent — catches the turbo/large-v3 case the time-based guards
    # below miss because turbo emits these fast (low ratio) rather than thrashing.
    if cleaned.lower().strip(" .,!?") in _SILENCE_HALLUCINATIONS:
        return True

    # DEF-083 / DEF-093: Catastrophic STT ratio guard. MLX whisper-small on
    # Apple Silicon runs ~10-25x realtime when warm. A ratio > 0.6 on ≥ 5 s
    # audio means temperature fallback exhausted multiple passes without
    # converging — near-certain hallucination even when text-level checks
    # didn't trip (cf. DEF-075: 13 s audio → 8.6 s inference → "doc doc doc").
    #
    # Threshold deliberately raised from 0.3 (DEF-083 original) to 0.6
    # (DEF-093). The 0.3 floor was discarding clean transcriptions whenever
    # DEF-081's tighter compression/logprob thresholds invoked temperature
    # fallback on borderline audio (low SNR, complex compounds, GPU pressure
    # from parallel Kokoro inference). The fallback there is success — the
    # final pass converged on clean output — but the wall-clock cost falsely
    # tripped DEF-083's hallucination heuristic. Examples observed:
    #   - 2026-04-27 07:28: 15.2 s audio → 5.3 s STT (ratio 0.35) → clean
    #     German sentence DISCARDED.
    #   - Multiple other 3-5 s spikes throughout the morning that escaped only
    #     because audio_secs was < 5 s or ratio was just under threshold.
    # Text-level checks above remain authoritative for the dominant hallucination
    # shapes (repetition, intra-token, low alpha, known patterns); the catastrophic
    # ratio is kept as a belt-and-suspenders guard for novel shapes.
    if (
        stt_secs is not None and audio_secs is not None
        and stt_secs >= 5.0 and audio_secs >= 5.0
        and stt_secs / audio_secs > 0.6
    ):
        # DEF-191: the catastrophic-ratio guard is a TIME-only proxy for
        # "temperature fallback thrashed → hallucination". But slow-yet-COHERENT
        # STT (system load / quiet mic / GPU contention from parallel Conductor
        # sessions) trips it too and silently discards real dictation. Observed
        # 2026-07-06: 17.4 s audio → 11.4 s STT (ratio 0.66), text = "Lies bitte
        # meine Konversation die letzten mit Andrew und hilf mir eine gute Frage"
        # — fully coherent, DISCARDED, then re-verify burned another 21.2 s and
        # also discarded it → the whole utterance was lost. A genuine thrash that
        # the text checks above missed still has a degenerate SHAPE: low word
        # diversity or very few words. A sentence that cleared every text check
        # AND shows high word diversity is real speech that was merely slow — do
        # NOT let wall-clock cost alone kill it. Only the still-suspicious grey
        # zone (short output or repetitive) is discarded on the time signal.
        _gwords = cleaned.split()
        _uniq = (
            len(set(w.lower() for w in _gwords)) / len(_gwords)
            if _gwords else 0.0
        )
        if not (len(_gwords) >= 5 and _uniq >= 0.5):
            return True

    # DEF-133: "struggled and gave up" — Whisper spent many multiples of its
    # healthy decode time yet returned almost no text from several seconds of
    # audio. This is the shape the text checks and the catastrophic-ratio guard
    # both miss: a *short* output ("k nud", 2 words) keeps the unique-ratio and
    # repetition checks quiet, and a sub-5 s / sub-0.6 inference time slips
    # under the catastrophic-ratio guard (observed 2026-06-02: 11.3 s audio →
    # 3.5 s STT → "k nud", ratio 0.31, injected as-is).
    #
    # The differentiator from the DEF-093 slow-but-CLEAN case is text *yield*
    # (chars per second of audio), NOT the time ratio alone — DEF-093's false
    # positive was a full German sentence (high yield) that merely decoded slow.
    # Warm whisper-small runs ~10-25x realtime (ratio ~0.04-0.10); a ratio
    # > 0.25 means it thrashed, and < 1 char/s of audio means it produced
    # essentially nothing. Both together = a decode collapse that bailed early.
    # A legitimate short reply ("ja, mach das") is safe: it decodes fast (low
    # ratio), so the ratio gate excludes it even though its yield is low.
    if (
        stt_secs is not None and audio_secs is not None
        and audio_secs >= 4.0
        and stt_secs / audio_secs > 0.25
        and len(cleaned) / audio_secs < 1.0
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
            base = cleaned.rstrip(" .,!?")
            if base.lower().endswith(phrase):
                idx = len(base) - len(phrase)
                # DEF-154: word-boundary guard — "HeyVox" must not lose its
                # "vox" suffix, "14 Uhr" must not lose "hr" (jarvis variant).
                if idx > 0 and base[idx - 1].isalnum():
                    continue
                cleaned = base[:idx].rstrip(" .,!?")
                stripped = True
                matched = True
                break
        if not matched:
            break

    # Strip from start (start wake word — happens with toggle mode)
    for phrase in sorted_phrases:
        lower = cleaned.lower().lstrip(" .,!?")
        if lower.startswith(phrase):
            # DEF-154: word-boundary guard — "Voxel rendering" must not
            # lose its "vox" prefix.
            if len(phrase) < len(lower) and lower[len(phrase)].isalnum():
                continue
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
