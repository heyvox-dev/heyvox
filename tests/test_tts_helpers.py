"""Tests for heyvox.herald.tts_helpers — P-producer-parity guard.

These tests are the single source of truth for the shared mood / verbosity /
extraction logic. If either worker.py or watcher.py reintroduces a local copy,
the guard test at the bottom of this file will fail.
"""

from __future__ import annotations

import pytest

from heyvox.herald.tts_helpers import (
    ALERT_WORDS,
    CHEERFUL_WORDS,
    DEFAULT_VOICE,
    MOOD_VOICES,
    THOUGHTFUL_WORDS,
    VERBOSITY_LEVELS,
    apply_verbosity,
    detect_mood,
    extract_last_tts_block,
    get_verbosity,
    mood_voice,
)


# ---------------------------------------------------------------------------
# detect_mood
# ---------------------------------------------------------------------------


class TestDetectMood:
    @pytest.mark.parametrize("word", ALERT_WORDS)
    def test_alert_words_route_to_alert(self, word):
        assert detect_mood(f"the build {word}ed") == "alert"

    @pytest.mark.parametrize("word", CHEERFUL_WORDS)
    def test_cheerful_words_route_to_cheerful(self, word):
        assert detect_mood(f"task {word}") == "cheerful"

    @pytest.mark.parametrize("phrase", THOUGHTFUL_WORDS)
    def test_thoughtful_phrases_route_to_thoughtful(self, phrase):
        assert detect_mood(f"hey, {phrase} keep going") == "thoughtful"

    def test_no_keywords_returns_neutral(self):
        assert detect_mood("just a status update") == "neutral"

    def test_empty_returns_neutral(self):
        assert detect_mood("") == "neutral"
        assert detect_mood(None) == "neutral"  # defensive: caller may pass None

    def test_alert_wins_over_cheerful_when_both_match(self):
        """Priority order: alert > cheerful > thoughtful > neutral."""
        assert detect_mood("the deploy failed, but tests passed") == "alert"

    def test_cheerful_wins_over_thoughtful(self):
        assert detect_mood("how about that — task done") == "cheerful"

    def test_case_insensitive(self):
        assert detect_mood("ERROR detected") == "alert"
        assert detect_mood("DONE!") == "cheerful"


class TestMoodVoice:
    def test_each_mood_maps_to_voice(self):
        for mood, voice in MOOD_VOICES.items():
            assert voice  # not empty
            assert isinstance(voice, str)

    def test_alert_text_returns_alert_voice(self):
        assert mood_voice("the build failed") == MOOD_VOICES["alert"]

    def test_neutral_text_returns_default(self):
        assert mood_voice("status update") == DEFAULT_VOICE
        assert MOOD_VOICES["neutral"] == DEFAULT_VOICE


# ---------------------------------------------------------------------------
# Verbosity
# ---------------------------------------------------------------------------


class TestVerbosity:
    def test_get_verbosity_defaults_to_full(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "heyvox.constants.VERBOSITY_FILE", str(tmp_path / "missing"),
        )
        assert get_verbosity() == "full"

    def test_get_verbosity_reads_short(self, tmp_path, monkeypatch):
        path = tmp_path / "verbosity"
        path.write_text("short")
        monkeypatch.setattr("heyvox.constants.VERBOSITY_FILE", str(path))
        assert get_verbosity() == "short"

    def test_get_verbosity_unknown_value_falls_back_to_full(self, tmp_path, monkeypatch):
        path = tmp_path / "verbosity"
        path.write_text("loud")
        monkeypatch.setattr("heyvox.constants.VERBOSITY_FILE", str(path))
        assert get_verbosity() == "full"

    def test_apply_verbosity_skip_returns_none(self):
        assert apply_verbosity("anything", "skip") is None

    def test_apply_verbosity_short_takes_first_sentence(self):
        out = apply_verbosity("First. Second. Third.", "short")
        assert out == "First."

    def test_apply_verbosity_short_truncates_no_punct(self):
        out = apply_verbosity("x" * 200, "short")
        assert out == "x" * 100

    def test_apply_verbosity_full_unchanged(self):
        text = "Some long status update with details."
        assert apply_verbosity(text, "full") == text

    def test_apply_verbosity_summary_unchanged(self):
        """Legacy 'summary' value plays the full text (kept for back-compat)."""
        text = "Some text"
        assert apply_verbosity(text, "summary") == text


# ---------------------------------------------------------------------------
# extract_last_tts_block
# ---------------------------------------------------------------------------


class TestExtractLastTTSBlock:
    def test_extracts_block_at_end(self):
        text = "some preamble " * 30 + "<tts>hello world</tts>"
        assert extract_last_tts_block(text) == "hello world"

    def test_no_block_returns_none(self):
        assert extract_last_tts_block("no tags here") is None

    def test_empty_input_returns_none(self):
        assert extract_last_tts_block("") is None
        assert extract_last_tts_block(None) is None

    def test_skip_returns_none(self):
        text = "lots of stuff " * 20 + "<tts>SKIP</tts>"
        assert extract_last_tts_block(text) is None

    def test_too_short_returns_none(self):
        text = "lots of stuff " * 20 + "<tts>hi</tts>"
        assert extract_last_tts_block(text) is None

    def test_block_not_near_end_returns_none(self):
        """If <tts> sits in the first half, it's a stale block."""
        text = "<tts>hello world</tts>" + "x" * 1000
        assert extract_last_tts_block(text) is None

    def test_trailing_text_too_long_returns_none(self):
        text = "preamble " * 20 + "<tts>hello world</tts>" + "trailing " * 20
        assert extract_last_tts_block(text) is None

    def test_uses_last_block_when_multiple(self):
        text = (
            "preamble " * 20
            + "<tts>first one</tts> more stuff "
            + "<tts>second one</tts>"
        )
        assert extract_last_tts_block(text) == "second one"


# ---------------------------------------------------------------------------
# Constant integrity
# ---------------------------------------------------------------------------


class TestConstants:
    def test_verbosity_levels_complete(self):
        # If a new level is added, both producers see it via the shared tuple.
        assert set(VERBOSITY_LEVELS) >= {"full", "summary", "short", "skip"}

    def test_mood_voices_cover_all_detected_moods(self):
        """detect_mood's output domain must equal MOOD_VOICES' key set."""
        possible = {"alert", "cheerful", "thoughtful", "neutral"}
        assert set(MOOD_VOICES.keys()) == possible

    def test_default_voice_matches_neutral(self):
        assert DEFAULT_VOICE == MOOD_VOICES["neutral"]


# ---------------------------------------------------------------------------
# Drift guard: helpers must remain the only source of truth
# ---------------------------------------------------------------------------


class TestNoDuplicateHelperDefs:
    """If a producer reintroduces a local copy of detect_mood / verbosity /
    extract helpers, this guard fails. The P-producer-parity bug is exactly
    the case where the comment "must match worker.py" failed to enforce
    drift — only a test does.
    """

    @pytest.mark.parametrize(
        "module_path",
        [
            "heyvox/herald/worker.py",
            "heyvox/herald/daemon/watcher.py",
        ],
    )
    @pytest.mark.parametrize(
        "forbidden_def",
        [
            "def detect_mood_voice(",
            "def _apply_verbosity(",
            "def _get_verbosity(",
        ],
    )
    def test_producer_does_not_redefine_helper(self, module_path, forbidden_def):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        target = root / module_path
        assert target.exists(), f"{target} not found"
        src = target.read_text()
        assert forbidden_def not in src, (
            f"{module_path} reintroduced '{forbidden_def}'. "
            f"Use heyvox.herald.tts_helpers instead — "
            f"see DEFECT-LOG pattern P-producer-parity."
        )

    def test_worker_does_not_redefine_detect_mood(self):
        """detect_mood lives in tts_helpers; worker re-exports via import."""
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        src = (root / "heyvox/herald/worker.py").read_text()
        # The function MUST NOT have a fresh `def detect_mood(...)` body.
        # Import lines like "from ... import detect_mood" are fine.
        assert "def detect_mood(text:" not in src and "def detect_mood(text)" not in src, (
            "heyvox/herald/worker.py redefines detect_mood locally. "
            "Import from heyvox.herald.tts_helpers instead."
        )
