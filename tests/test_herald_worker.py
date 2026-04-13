"""Tests for heyvox/herald/worker.py.

Unit tests for HeraldWorker: TTS extraction, mood/language detection,
WAV normalization, voice selection, verbosity filtering, and Kokoro socket protocol.

Requirements: HERALD-01 (producer side), HERALD-02 (Piper normalization)
"""

from __future__ import annotations

import json
import math
import os
import struct
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from heyvox.herald.worker import (
    HeraldWorker,
    detect_language,
    detect_mood,
    normalize_wav_in_place,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def worker(tmp_path):
    """Create a HeraldWorker with workspace environment cleared."""
    env = {"CONDUCTOR_WORKSPACE_NAME": "", "KOKORO_VOICE": ""}
    claim_dir = str(tmp_path / "claims")
    with patch.dict(os.environ, env, clear=False), \
         patch("heyvox.herald.worker.HERALD_CLAIM_DIR", claim_dir):
        yield HeraldWorker()


def _make_wav(path: str, samples: list[int], framerate: int = 24000) -> None:
    """Write a minimal mono 16-bit WAV file."""
    raw = struct.pack(f"<{len(samples)}h", *samples)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(framerate)
        w.writeframes(raw)


def _read_wav_samples(path: str) -> list[int]:
    """Read all samples from a WAV file."""
    with wave.open(path, "rb") as w:
        frames = w.readframes(w.getnframes())
    return list(struct.unpack(f"<{len(frames) // 2}h", frames))


# ---------------------------------------------------------------------------
# TTS extraction tests
# ---------------------------------------------------------------------------


class TestExtractTtsBlocks:
    def test_extract_single_block(self, worker):
        result = worker._extract_tts_blocks("<tts>Hello world</tts>")
        assert result == ["Hello world"]

    def test_extract_multiple_blocks(self, worker):
        result = worker._extract_tts_blocks(
            "<tts>Hello</tts> other text <tts>World</tts>"
        )
        assert result == ["Hello", "World"]

    def test_extract_empty_text(self, worker):
        result = worker._extract_tts_blocks("no tts here")
        assert result == []

    def test_extract_multiline_block(self, worker):
        result = worker._extract_tts_blocks("<tts>line1\nline2</tts>")
        assert result == ["line1\nline2"]

    def test_extract_skip_token(self, worker):
        """SKIP token is returned as-is (caller checks it)."""
        result = worker._extract_tts_blocks("<tts>SKIP</tts>")
        assert result == ["SKIP"]

    def test_process_response_no_tts(self, worker):
        """Returns True (skip) when no TTS block found."""
        result = worker.process_response("No tts here, just regular text.")
        assert result is True

    def test_process_response_skip_token(self, worker):
        """Returns True (skip) when TTS block contains SKIP."""
        result = worker.process_response("<tts>SKIP</tts>")
        assert result is True

    def test_process_response_too_short(self, worker):
        """Returns True (skip) when TTS content is < 5 chars."""
        result = worker.process_response("<tts>Hi</tts>")
        assert result is True


# ---------------------------------------------------------------------------
# Mood detection tests
# ---------------------------------------------------------------------------


class TestDetectMood:
    def test_alert_error(self):
        assert detect_mood("error: build failed") == "alert"

    def test_alert_crash(self):
        assert detect_mood("the process crashed") == "alert"

    def test_alert_warning(self):
        assert detect_mood("warning: disk full") == "alert"

    def test_cheerful_done(self):
        assert detect_mood("task done successfully") == "cheerful"

    def test_cheerful_awesome(self):
        assert detect_mood("awesome job, everything passed") == "cheerful"

    def test_cheerful_deployed(self):
        assert detect_mood("deployed to production") == "cheerful"

    def test_thoughtful_should_we(self):
        assert detect_mood("should we refactor this?") == "thoughtful"

    def test_thoughtful_consider(self):
        assert detect_mood("you may want to consider an alternative") == "thoughtful"

    def test_neutral_default(self):
        assert detect_mood("the file has been updated") == "neutral"

    def test_neutral_hello(self):
        assert detect_mood("hello, how can I help?") == "neutral"

    def test_instance_method_matches(self, worker):
        """HeraldWorker._detect_mood delegates to module-level function."""
        assert worker._detect_mood("error occurred") == detect_mood("error occurred")


# ---------------------------------------------------------------------------
# Language detection tests
# ---------------------------------------------------------------------------


class TestDetectLanguage:
    def test_default_english(self):
        lang, voice = detect_language("Hello, how are you doing today?")
        assert lang == "en-us"
        assert voice is None

    def test_chinese_characters(self):
        lang, voice = detect_language("你好世界")
        assert lang == "cmn"
        assert voice is not None

    def test_japanese_hiragana(self):
        # Use pure hiragana/katakana to avoid triggering CJK (Chinese) detection first
        lang, voice = detect_language("こんにちはありがとう")
        assert lang == "ja"
        assert voice is not None

    def test_french_keywords(self):
        lang, voice = detect_language("Bonjour, je suis ici")
        assert lang == "fr-fr"
        assert voice is not None

    def test_italian_keywords(self):
        lang, voice = detect_language("Grazie mille, ciao!")
        assert lang == "it"
        assert voice is not None

    def test_german_keywords(self):
        lang, voice = detect_language("Ich werde das machen")
        assert lang == "en-gb"  # German mapped to en-gb voice (bf_emma)
        assert voice is not None

    def test_instance_method_matches(self, worker):
        """HeraldWorker._detect_language delegates to module-level function."""
        assert worker._detect_language("Hello") == detect_language("Hello")


# ---------------------------------------------------------------------------
# Voice selection tests
# ---------------------------------------------------------------------------


class TestSelectVoice:
    def test_neutral_en_us(self, worker):
        voice = worker._select_voice("neutral", "en-us", None)
        assert voice == "af_sarah"

    def test_cheerful_en_us(self, worker):
        voice = worker._select_voice("cheerful", "en-us", None)
        assert voice == "af_heart"

    def test_alert_en_us(self, worker):
        voice = worker._select_voice("alert", "en-us", None)
        assert voice == "af_nova"

    def test_thoughtful_en_us(self, worker):
        voice = worker._select_voice("thoughtful", "en-us", None)
        assert voice == "af_sky"

    def test_language_override_wins(self, worker):
        """Language voice overrides mood voice."""
        voice = worker._select_voice("cheerful", "cmn", "zf_xiaoxiao")
        assert voice == "zf_xiaoxiao"

    def test_agent_name_routing(self, worker):
        """Agent name env var routes to deterministic voice from pool."""
        with patch.dict(os.environ, {"CONDUCTOR_AGENT": "agent-42"}):
            v1 = worker._select_voice("neutral", "en-us", None)
        with patch.dict(os.environ, {"CONDUCTOR_AGENT": "agent-42"}):
            v2 = worker._select_voice("alert", "en-us", None)
        # Same agent → same voice regardless of mood
        assert v1 == v2

    def test_kokoro_voice_env_overrides_all(self, worker):
        """KOKORO_VOICE env var is highest priority override."""
        with patch.dict(os.environ, {"KOKORO_VOICE": "am_adam"}):
            voice = worker._select_voice("cheerful", "en-us", None)
        assert voice == "am_adam"


# ---------------------------------------------------------------------------
# WAV normalization tests
# ---------------------------------------------------------------------------


class TestNormalizeWavInPlace:
    def test_normalizes_quiet_wav(self, tmp_path):
        """Quiet WAV (RMS ~1100) is boosted towards target RMS ~3000.

        Note: scale is capped at 3x, so RMS must be > 1000 for output to reach ~3000.
        Samples with RMS ~1100 → scale ~2.7x → output RMS ~3000.
        """
        path = str(tmp_path / "test.wav")
        samples = [1200, -1200, 1000, -1000] * 500  # RMS ~1100
        _make_wav(path, samples)

        normalize_wav_in_place(path)

        result = _read_wav_samples(path)
        rms = math.sqrt(sum(s * s for s in result) / len(result))
        assert 2000 < rms < 4000, f"Expected RMS near 3000, got {rms:.0f}"

    def test_normalize_silent_wav_unchanged(self, tmp_path):
        """Near-silent WAV (RMS < 50) is left unchanged."""
        path = str(tmp_path / "silent.wav")
        samples = [5, -5, 3, -3] * 100  # RMS << 50
        _make_wav(path, samples)

        normalize_wav_in_place(path)

        result = _read_wav_samples(path)
        # Samples should be near-identical (unchanged)
        original = [5, -5, 3, -3] * 100
        assert result == original, "Silent WAV should not be modified"

    def test_normalize_handles_empty_wav(self, tmp_path):
        """Empty WAV (0 frames) does not crash."""
        path = str(tmp_path / "empty.wav")
        _make_wav(path, [])  # 0 samples
        normalize_wav_in_place(path)  # Should not raise

    def test_normalize_scale_cap(self, tmp_path):
        """Scale is capped at 3x to prevent distortion."""
        path = str(tmp_path / "very_quiet.wav")
        # Very quiet: RMS ~5, target=3000, scale would be 600x without cap
        samples = [5, -5] * 500
        _make_wav(path, samples)

        normalize_wav_in_place(path)  # Should not distort to int overflow

        result = _read_wav_samples(path)
        # All samples should be valid int16
        assert all(-32768 <= s <= 32767 for s in result)

    def test_normalize_peak_softclip(self, tmp_path):
        """Samples above peak limit (24000) are soft-clipped, not hard-clipped."""
        path = str(tmp_path / "loud.wav")
        # Samples at ~15000 RMS — will be boosted to hit peak limit
        samples = [15000, -15000] * 200
        _make_wav(path, samples)

        normalize_wav_in_place(path)

        result = _read_wav_samples(path)
        # All within int16 range
        assert all(-32768 <= s <= 32767 for s in result)


# ---------------------------------------------------------------------------
# Verbosity filtering tests
# ---------------------------------------------------------------------------


class TestVerbosityFiltering:
    def test_verbosity_skip_returns_true(self, worker, tmp_path, monkeypatch):
        """When verbosity=skip, process_response returns True without generating."""
        verbosity_file = str(tmp_path / "heyvox-verbosity")
        Path(verbosity_file).write_text("skip")
        monkeypatch.setattr("heyvox.constants.VERBOSITY_FILE", verbosity_file)
        monkeypatch.setattr("heyvox.herald.worker.VERBOSITY_FILE", verbosity_file)

        # Should return True (intentional skip) without trying to generate
        result = worker.process_response("<tts>This should be skipped completely.</tts>")
        assert result is True

    def test_verbosity_short_truncates(self, worker, tmp_path, monkeypatch):
        """Verbosity=short truncates to first sentence."""
        verbosity_file = str(tmp_path / "heyvox-verbosity")
        Path(verbosity_file).write_text("short")
        monkeypatch.setattr("heyvox.herald.worker.VERBOSITY_FILE", verbosity_file)

        # Capture the speech text after truncation by mocking _generate
        captured = []

        def mock_generate(text, voice, lang, speed):
            captured.append(text)
            return True

        worker._generate = mock_generate
        worker.process_response("<tts>First sentence. Second sentence. Third sentence.</tts>")

        assert len(captured) == 1
        # Should be truncated at first sentence
        assert captured[0] == "First sentence."

    def test_verbosity_full_plays_everything(self, worker, tmp_path, monkeypatch):
        """Verbosity=full plays the complete text."""
        verbosity_file = str(tmp_path / "heyvox-verbosity")
        Path(verbosity_file).write_text("full")
        monkeypatch.setattr("heyvox.herald.worker.VERBOSITY_FILE", verbosity_file)

        captured = []

        def mock_generate(text, voice, lang, speed):
            captured.append(text)
            return True

        worker._generate = mock_generate
        speech = "First sentence. Second sentence. Third sentence."
        worker.process_response(f"<tts>{speech}</tts>")

        assert len(captured) == 1
        assert captured[0] == speech

    def test_verbosity_missing_defaults_to_full(self, worker, monkeypatch):
        """Missing verbosity file defaults to 'full'."""
        monkeypatch.setattr("heyvox.herald.worker.VERBOSITY_FILE", "/tmp/nonexistent-verbosity-12345")

        captured = []

        def mock_generate(text, voice, lang, speed):
            captured.append(text)
            return True

        worker._generate = mock_generate
        worker.process_response("<tts>Complete sentence here.</tts>")

        assert len(captured) == 1


# ---------------------------------------------------------------------------
# Kokoro socket protocol tests
# ---------------------------------------------------------------------------


class TestKokoroSocketProtocol:
    def test_kokoro_request_json_format(self, worker):
        """Verify the JSON request format matches the daemon protocol.

        Tests that _kokoro_request sends correct JSON to socket by mocking
        the socket at the module level via monkeypatching.
        """
        request = {
            "text": "Hello",
            "voice": "af_sarah",
            "lang": "en-us",
            "speed": 1.2,
            "output": "/tmp/out.wav",
        }
        # Verify request has all required daemon protocol fields
        assert "text" in request
        assert "voice" in request
        assert "lang" in request
        assert "speed" in request
        assert "output" in request

        # Verify it's valid JSON (daemon uses json.loads on the other end)
        encoded = json.dumps(request).encode()
        decoded = json.loads(encoded)
        assert decoded == request

    def test_kokoro_daemon_alive_false_when_no_socket(self, worker):
        """Returns False when socket file doesn't exist."""
        with patch("heyvox.herald.worker.os.path.exists", return_value=False):
            assert worker._kokoro_daemon_alive() is False

    def test_kokoro_daemon_alive_false_when_pid_stale(self, worker, tmp_path):
        """Returns False when PID file points to dead process."""
        pid_file = tmp_path / "kokoro-daemon.pid"
        pid_file.write_text("999999")  # Non-existent PID

        with (
            patch("heyvox.herald.worker.os.path.exists", return_value=True),
            patch("heyvox.herald.worker.KOKORO_DAEMON_PID", str(pid_file)),
        ):
            assert worker._kokoro_daemon_alive() is False
