"""German TTS via kikiri Kokoro weights: text prep, lexicon, routing, daemon path.

Net-free and model-free: the daemon's German pipeline is replaced by a fake,
the converter is tested on a synthetic state dict.
"""

import importlib.util
import os
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

import heyvox
from heyvox import model_pins
from heyvox.config import TTSConfig
from heyvox.herald import german_text, kikiri
from heyvox.herald.worker import HeraldWorker


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


class TestNormalize:
    @pytest.mark.parametrize("raw, spoken", [
        ("z. B. das", "zum Beispiel das"),
        ("z.B. das", "zum Beispiel das"),
        ("rot bzw. grün", "rot beziehungsweise grün"),
        ("ca. drei", "circa drei"),
        ("um 09:20 Uhr", "um 9 Uhr 20"),
        ("um 10:00", "um 10 Uhr"),
        ("0,6 s pro Satz", "0 Komma 6 Sekunden pro Satz"),
        ("16 GB RAM", "16 Gigabyte RAM"),
        ("in 250 ms", "in 250 Millisekunden"),
        ("zu 80 %", "zu 80 Prozent"),
    ])
    def test_cases(self, raw, spoken):
        assert german_text.normalize(raw) == spoken

    def test_unit_needs_number(self):
        assert german_text.normalize("5 sind fertig, s bleibt") == "5 sind fertig, s bleibt"

    def test_number_lists_not_decimals(self):
        assert german_text.normalize("1,2,3") == "1,2,3"

    def test_abbreviation_no_longer_splits_sentences(self):
        # The daemon splits on ". " — "z. B." must be gone before that.
        assert ". " not in german_text.normalize("Das ist z. B. gut")


# ---------------------------------------------------------------------------
# Lexicon
# ---------------------------------------------------------------------------


@pytest.fixture
def no_user_lexicon(monkeypatch, tmp_path):
    monkeypatch.setenv("HEYVOX_TTS_LEXICON_DE", str(tmp_path / "none.yaml"))


class TestLexicon:
    @pytest.mark.parametrize("raw, spoken", [
        ("Das Headset ist weg.", "Das Hedsett ist weg."),
        ("Drei Headsets.", "Drei Hedsetts."),
        ("Der Cache ist leer.", "Der Käsch ist leer."),
        ("Die Caches sind leer.", "Die Käsches sind leer."),
        ("im Kokoro-Daemon", "im Kokoro-Diemen"),
        ("den Pull Request", "den Pull-Rikwest"),
        ("den Pull-Request", "den Pull-Rikwest"),
        ("der Commit ist drin.", "der Commit ist drinn."),
        ("Termin 10 Uhr UK", "Termin 10 Uhr Ju-Kei"),
    ])
    def test_shipped_entries(self, no_user_lexicon, raw, spoken):
        assert german_text.apply_lexicon(raw) == spoken

    @pytest.mark.parametrize("text", ["Login", "Logbuch", "Bugfix", "Cachedatei", "drinnen"])
    def test_no_match_inside_words(self, no_user_lexicon, text):
        assert german_text.apply_lexicon(text) == text

    def test_acronyms_case_sensitive(self, no_user_lexicon):
        assert german_text.apply_lexicon("uk") == "uk"

    def test_user_file_adds_and_removes(self, tmp_path, monkeypatch):
        user = tmp_path / "tts-lexicon-de.yaml"
        user.write_text("Orca: Orka\nCache: ''\n")
        monkeypatch.setenv("HEYVOX_TTS_LEXICON_DE", str(user))
        assert german_text.apply_lexicon("Orca und Cache") == "Orka und Cache"

    def test_user_file_edit_applies_without_restart(self, tmp_path, monkeypatch):
        user = tmp_path / "tts-lexicon-de.yaml"
        user.write_text("Orca: Orka\n")
        monkeypatch.setenv("HEYVOX_TTS_LEXICON_DE", str(user))
        assert german_text.apply_lexicon("Orca") == "Orka"
        user.write_text("Orca: Orkah\n")
        os.utime(user, (user.stat().st_atime, user.stat().st_mtime + 5))
        assert german_text.apply_lexicon("Orca") == "Orkah"

    def test_broken_user_file_keeps_shipped(self, tmp_path, monkeypatch):
        user = tmp_path / "tts-lexicon-de.yaml"
        user.write_text("- not\n- a mapping\n")
        monkeypatch.setenv("HEYVOX_TTS_LEXICON_DE", str(user))
        assert german_text.apply_lexicon("Headset") == "Hedsett"

    def test_prepare_runs_both(self, no_user_lexicon):
        assert german_text.prepare("Cache z. B. um 09:30") == "Käsch zum Beispiel um 9 Uhr 30"


# ---------------------------------------------------------------------------
# kikiri converter helpers
# ---------------------------------------------------------------------------


class TestKikiri:
    def test_weight_norm_keys_renamed(self):
        sd = {"decoder": {
            "module.conv.parametrizations.weight.original0": 1,
            "module.conv.parametrizations.weight.original1": 2,
            "conv.bias": 3,
        }}
        assert kikiri.flatten_state_dict(sd) == {
            "decoder.conv.weight_g": 1,
            "decoder.conv.weight_v": 2,
            "decoder.conv.bias": 3,
        }

    def test_speakers_pinned(self):
        for speaker in kikiri.SPEAKERS:
            assert kikiri.repo_for(speaker) in model_pins.MODEL_REVISIONS

    def test_model_dir_carries_revision(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HEYVOX_KIKIRI_DIR", str(tmp_path))
        rev = model_pins.MODEL_REVISIONS[kikiri.repo_for("martin")]
        assert kikiri.model_dir("martin") == tmp_path / f"martin-{rev[:8]}"
        assert not kikiri.is_prepared("martin")

    def test_unknown_speaker_rejected(self):
        with pytest.raises(ValueError):
            kikiri.prepare("nobody")


# ---------------------------------------------------------------------------
# Config + agent language instruction
# ---------------------------------------------------------------------------


class TestConfig:
    def test_defaults_unchanged_behaviour(self):
        cfg = TTSConfig()
        assert (cfg.reply_language, cfg.german_engine, cfg.kikiri_voice) == ("en", "qwen", "martin")

    @pytest.mark.parametrize("field, value", [
        ("reply_language", "fr"), ("german_engine", "piper"), ("kikiri_voice", "bob"),
    ])
    def test_invalid_values_rejected(self, field, value):
        with pytest.raises(ValueError):
            TTSConfig(**{field: value})

    def test_style_prompt_carries_german_instruction(self):
        from heyvox.audio import tts

        with patch("heyvox.audio.tts.get_reply_language", return_value="de"):
            assert "German" in tts.get_tts_style_prompt()
        with patch("heyvox.audio.tts.get_reply_language", return_value="en"):
            assert "German" not in tts.get_tts_style_prompt()


# ---------------------------------------------------------------------------
# Worker routing
# ---------------------------------------------------------------------------


def _cfg(**kw):
    base = dict(reply_language="en", german_engine="qwen", kikiri_voice="martin",
                kikiri_speed=1.0, voice_override=None, qwen_voice_override=None)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def worker(tmp_path, monkeypatch):
    verbosity_file = str(tmp_path / "heyvox-verbosity")
    Path(verbosity_file).write_text("full")
    monkeypatch.setattr("heyvox.constants.VERBOSITY_FILE", verbosity_file)
    env = {"CONDUCTOR_WORKSPACE_NAME": "", "KOKORO_VOICE": "", "KIKIRI_VOICE": ""}
    with patch.dict(os.environ, env, clear=False), \
         patch("heyvox.herald.worker.HERALD_CLAIM_DIR", str(tmp_path / "claims")), \
         patch("heyvox.herald.worker._ensure_orchestrator"), \
         patch("heyvox.herald.workspace_label.detect_workspace_from_cwd", return_value=""):
        w = HeraldWorker()
        w._cwd = "/Users/test/project"
        w._maybe_prepend_workspace_label = lambda s: s
        calls = []
        w._generate = lambda text, voice, lang, speed: calls.append((text, voice, lang, speed)) or True
        w.calls = calls
        yield w


GERMAN = "Ich habe den Fehler gefunden und der Test ist jetzt grün."


class TestRouting:
    def test_reply_language_de_routes_german_to_kikiri(self, worker, monkeypatch):
        monkeypatch.setenv("HEYVOX_TTS_LANGS", "en-us")
        with patch("heyvox.herald.worker._tts_config",
                   return_value=_cfg(reply_language="de", german_engine="kikiri")):
            worker.process_response(f"<tts>{GERMAN}</tts>")
        assert worker.calls == [(GERMAN, "martin", "de", 1.0)]

    def test_english_still_english_in_german_mode(self, worker, monkeypatch):
        monkeypatch.setenv("HEYVOX_TTS_LANGS", "en-us")
        speech = "The build is green and the pull request is open now."
        with patch("heyvox.herald.worker._tts_config",
                   return_value=_cfg(reply_language="de", german_engine="kikiri")):
            worker.process_response(f"<tts>{speech}</tts>")
        (text, _voice, lang, speed), = worker.calls
        assert (text, lang, speed) == (speech, "en-us", 1.2)

    def test_reply_language_en_keeps_notice(self, worker, monkeypatch):
        from heyvox.herald.worker import GERMAN_SPEECH_NOTICE

        monkeypatch.setenv("HEYVOX_TTS_LANGS", "en-us")
        with patch("heyvox.herald.worker._tts_config",
                   return_value=_cfg(reply_language="en", german_engine="kikiri")):
            worker.process_response(f"<tts>{GERMAN}</tts>")
        assert worker.calls[0][0] == GERMAN_SPEECH_NOTICE

    def test_qwen_remains_default_german_engine(self, worker, monkeypatch):
        monkeypatch.setenv("HEYVOX_TTS_LANGS", "en-us,de")
        with patch("heyvox.herald.worker._tts_config", return_value=_cfg()):
            worker.process_response(f"<tts>{GERMAN}</tts>")
        (_text, voice, lang, _speed), = worker.calls
        assert lang == "de" and voice in {"Serena", "Vivian", "Aura", "Aria"}

    def test_kikiri_voice_from_config(self, worker):
        with patch("heyvox.herald.worker._tts_config",
                   return_value=_cfg(german_engine="kikiri", kikiri_voice="victoria")):
            assert worker._select_voice("alert", "de", None) == "victoria"


# ---------------------------------------------------------------------------
# Daemon German path
# ---------------------------------------------------------------------------


def _load_kokoro_daemon():
    path = Path(heyvox.__file__).parent / "herald" / "daemon" / "kokoro-daemon.py"
    spec = importlib.util.spec_from_file_location("heyvox_test_kokoro_daemon_de", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestDaemonGerman:
    def test_prepared_before_split_and_parts_written(self, tmp_path, no_user_lexicon):
        daemon = _load_kokoro_daemon()
        seen = []

        def fake_pipe(sentence, voice, speed):
            seen.append(sentence)
            yield SimpleNamespace(audio=np.full(2400, 0.1, dtype=np.float32))

        daemon.german_pipeline = lambda speaker: (fake_pipe, "/x/martin.safetensors")
        out = str(tmp_path / "out.wav")
        resp = daemon.generate_mlx(None, "Der Cache ist z. B. leer. Zweiter Satz.", "martin", "de", 1.0, out)

        assert seen == ["Der Käsch ist zum Beispiel leer.", "Zweiter Satz."]
        assert resp["ok"] and resp["parts"] == 2
        for p in (out, str(tmp_path / "out.part2.wav")):
            with wave.open(p) as w:
                assert w.getframerate() == 24000 and w.getnframes() == 2400

    def test_unknown_voice_falls_back_to_martin(self, tmp_path):
        daemon = _load_kokoro_daemon()
        asked = []

        def fake(speaker):
            asked.append(speaker)
            return (lambda s, voice, speed: iter([SimpleNamespace(audio=np.zeros(10))]), "v")

        daemon.german_pipeline = fake
        daemon.generate_mlx(None, "Hallo.", "af_sarah", "de", 1.0, str(tmp_path / "o.wav"))
        assert asked == ["martin"]


class TestDaemonChunks:
    def test_def279_all_chunks_of_a_sentence_kept(self, tmp_path):
        daemon = _load_kokoro_daemon()

        class FakeModel:
            def generate(self, text, voice, speed, lang_code):
                for _ in range(2):
                    yield SimpleNamespace(audio=np.full(1000, 0.1, dtype=np.float32), sample_rate=24000)

        out = str(tmp_path / "en.wav")
        resp = daemon.generate_mlx(FakeModel(), "One long sentence.", "af_sarah", "en-us", 1.2, out)
        assert resp["parts"] == 1
        with wave.open(out) as w:
            assert w.getnframes() == 2000
