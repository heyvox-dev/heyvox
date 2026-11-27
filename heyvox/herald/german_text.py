"""Text preparation for German TTS (kikiri Kokoro + espeak-de G2P).

espeak-ng reads digits and most German words well. It fails on three things
Herald's agent output is full of:

1. English loanwords ("Headset", "Cache", "Daemon") — fixed by respelling
   via the pronunciation lexicon (``lexicon_de.yaml`` + user overrides).
2. Abbreviations with periods ("z. B.", "bzw.") — read letter by letter and,
   worse, split into separate sentences by the daemon's sentence splitter.
3. Clock times and decimals ("09:20", "0,6 s") — read as "null neun Doppelpunkt
   zwanzig" / "null, sechs s".

``prepare(text)`` applies all three. It runs inside the Kokoro daemon right
before sentence splitting, so the text Herald registers for echo suppression
(DEF-078) stays the original wording.
"""

from __future__ import annotations

import logging
import os
import re
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

_SHIPPED_LEXICON = Path(__file__).with_name("lexicon_de.yaml")
USER_LEXICON_NAME = "tts-lexicon-de.yaml"

# Written abbreviation -> spoken form. Matched with flexible inner spacing.
_ABBREVIATIONS = {
    "z. B.": "zum Beispiel",
    "d. h.": "das heißt",
    "u. a.": "unter anderem",
    "u. U.": "unter Umständen",
    "o. Ä.": "oder Ähnliches",
    "bzw.": "beziehungsweise",
    "usw.": "und so weiter",
    "ggf.": "gegebenenfalls",
    "inkl.": "inklusive",
    "exkl.": "exklusive",
    "ca.": "circa",
    "evtl.": "eventuell",
    "vgl.": "vergleiche",
    "Nr.": "Nummer",
    "max.": "maximal",
    "min.": "mindestens",
}

# Unit after a number -> spoken unit.
_UNITS = {
    "ms": "Millisekunden",
    "s": "Sekunden",
    "min": "Minuten",
    "h": "Stunden",
    "KB": "Kilobyte",
    "MB": "Megabyte",
    "GB": "Gigabyte",
    "TB": "Terabyte",
    "%": "Prozent",
}


def _abbrev_pattern(abbr: str) -> re.Pattern:
    # "z. B." also matches "z.B." and "z.  B."
    parts = [re.escape(p) for p in abbr.split()]
    body = r"\s*".join(parts)
    return re.compile(rf"(?<![\w.]){body}(?!\w)")


_ABBREV_RES = [(_abbrev_pattern(a), spoken) for a, spoken in _ABBREVIATIONS.items()]

_TIME_RE = re.compile(r"(?<![\d:])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])(\s*Uhr\b)?")
_DECIMAL_RE = re.compile(r"(?<![\d,])(\d+),(\d+)(?![\d,])")
_UNIT_RE = re.compile(
    r"(?<=\d)\s*(" + "|".join(re.escape(u) for u in sorted(_UNITS, key=len, reverse=True)) + r")(?![\w%])"
)


def _speak_time(m: re.Match) -> str:
    hour, minute = int(m.group(1)), int(m.group(2))
    return f"{hour} Uhr" if minute == 0 else f"{hour} Uhr {minute}"


def normalize(text: str) -> str:
    """Expand abbreviations, clock times, decimals and units."""
    for pattern, spoken in _ABBREV_RES:
        text = pattern.sub(spoken, text)
    text = _TIME_RE.sub(_speak_time, text)
    text = _UNIT_RE.sub(lambda m: " " + _UNITS[m.group(1)], text)
    text = _DECIMAL_RE.sub(r"\1 Komma \2", text)
    return text


def _load_yaml(path: Path) -> dict[str, str]:
    import yaml

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log.warning("German lexicon %s unreadable: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        log.warning("German lexicon %s is not a mapping, ignored", path)
        return {}
    return {str(k): ("" if v is None else str(v)) for k, v in data.items()}


def _user_lexicon_path() -> Path | None:
    env = os.environ.get("HEYVOX_TTS_LEXICON_DE")
    if env:
        return Path(env)
    try:
        from heyvox.config import CONFIG_DIR

        return Path(CONFIG_DIR) / USER_LEXICON_NAME
    except Exception:
        return None


def load_lexicon(user_path: Path | None = None) -> dict[str, str]:
    """Shipped lexicon merged with the user's; a user value of "" deletes."""
    entries = _load_yaml(_SHIPPED_LEXICON)
    path = user_path if user_path is not None else _user_lexicon_path()
    if path is not None:
        for key, value in _load_yaml(path).items():
            if value:
                entries[key] = value
            else:
                entries.pop(key, None)
    return entries


def _compile(entries: dict[str, str]) -> list[tuple[re.Pattern, str]]:
    compiled = []
    # Longest first so "Pull Request" wins over a hypothetical "Pull".
    for key in sorted(entries, key=len, reverse=True):
        words = [re.escape(w) for w in key.split()]
        body = r"[\s-]+".join(words)
        # All-caps keys (acronyms) match case-sensitively: "UK" but not "uk".
        flags = 0 if key.isupper() else re.IGNORECASE
        pattern = re.compile(rf"(?<!\w)(?:{body})(?P<suffix>e?s)?(?!\w)", flags)
        compiled.append((pattern, entries[key]))
    return compiled


@lru_cache(maxsize=4)
def _compiled_for(user_path_str: str, mtime: float) -> list[tuple[re.Pattern, str]]:
    path = Path(user_path_str) if user_path_str else None
    return _compile(load_lexicon(path if path is not None else Path("/nonexistent")))


def _current_rules() -> list[tuple[re.Pattern, str]]:
    path = _user_lexicon_path()
    try:
        mtime = path.stat().st_mtime if path is not None else 0.0
    except OSError:
        mtime = 0.0
    # Keyed on the user file's mtime so edits apply without a daemon restart.
    return _compiled_for(str(path) if path is not None else "", mtime)


def apply_lexicon(text: str, rules: list[tuple[re.Pattern, str]] | None = None) -> str:
    """Replace lexicon words with their respellings, keeping an s/es suffix."""
    for pattern, respelling in rules if rules is not None else _current_rules():
        text = pattern.sub(lambda m, r=respelling: r + (m.group("suffix") or ""), text)
    return text


def prepare(text: str) -> str:
    """Full German TTS text preparation: normalize, then lexicon."""
    return apply_lexicon(normalize(text))
