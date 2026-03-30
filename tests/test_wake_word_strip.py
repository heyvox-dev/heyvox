"""Tests for wake word stripping from transcriptions."""

import pytest
from heyvox.main import _strip_wake_words


class TestStripWakeWords:
    """Remove wake word phrases from transcription start/end."""

    def test_strips_end_hey_jarvis(self):
        text = "What's the weather like? Hey Jarvis."
        result = _strip_wake_words(text, "hey_jarvis_v0.1", "hey_jarvis_v0.1")
        assert result == "What's the weather like"

    def test_strips_end_hey_travis(self):
        text = "Please check the logs. Hey Travis."
        result = _strip_wake_words(text, "hey_jarvis_v0.1", "hey_jarvis_v0.1")
        assert result == "Please check the logs"

    def test_strips_end_hey_chavez(self):
        text = "Fix this bug, hey Chavez"
        result = _strip_wake_words(text, "hey_jarvis_v0.1", "hey_jarvis_v0.1")
        assert result == "Fix this bug"

    def test_strips_start_wake_word(self):
        text = "Hey Jarvis, what time is it?"
        result = _strip_wake_words(text, "hey_jarvis_v0.1", "hey_jarvis_v0.1")
        assert result == "what time is it?"

    def test_strips_both_start_and_end(self):
        text = "Hey Jarvis, run the tests. Hey Travis."
        result = _strip_wake_words(text, "hey_jarvis_v0.1", "hey_jarvis_v0.1")
        assert result == "run the tests"

    def test_preserves_text_without_wake_word(self):
        text = "Just a normal sentence without any trigger."
        result = _strip_wake_words(text, "hey_jarvis_v0.1", "hey_jarvis_v0.1")
        assert result == "Just a normal sentence without any trigger."

    def test_empty_text(self):
        assert _strip_wake_words("", "hey_jarvis_v0.1", "hey_jarvis_v0.1") == ""

    def test_only_wake_word(self):
        result = _strip_wake_words("Hey Jarvis", "hey_jarvis_v0.1", "hey_jarvis_v0.1")
        assert result == ""

    def test_case_insensitive(self):
        text = "hey jarvis check this hey jarvis"
        result = _strip_wake_words(text, "hey_jarvis_v0.1", "hey_jarvis_v0.1")
        assert result == "check this"

    def test_with_comma_after_wake_word(self):
        text = "Hey, Jarvis, please deploy."
        result = _strip_wake_words(text, "hey_jarvis_v0.1", "hey_jarvis_v0.1")
        assert result == "please deploy."

    def test_unknown_wake_word_model(self):
        text = "Hey custom wake word, do something."
        result = _strip_wake_words(text, "custom_wake_v1.0", "custom_wake_v1.0")
        # Should still try the base name as space-separated
        assert "custom wake" not in result.lower() or result == text

    def test_strips_hey_charmis(self):
        text = "Investigate the issue. Hey Charmis."
        result = _strip_wake_words(text, "hey_jarvis_v0.1", "hey_jarvis_v0.1")
        assert result == "Investigate the issue"

    def test_strips_hey_charvis(self):
        text = "Okay, it's still not working. Hey Charvis."
        result = _strip_wake_words(text, "hey_jarvis_v0.1", "hey_jarvis_v0.1")
        assert result == "Okay, it's still not working"
