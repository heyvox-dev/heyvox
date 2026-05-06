"""Tests for heyvox.config — configuration loading and validation."""

import pytest

from heyvox.config import (
    AppProfileConfig,
    HeyvoxConfig,
    TTSConfig,
    WakeWordConfig,
    load_config,
)


class TestHeyvoxConfigDefaults:
    """All fields should have sensible defaults without any config file."""

    def test_default_config_creates_successfully(self):
        cfg = HeyvoxConfig()
        assert cfg.threshold == 0.5
        assert cfg.cooldown_secs == 2.0

    def test_default_wake_words(self):
        cfg = HeyvoxConfig()
        assert cfg.wake_words.start == "hey_vox"
        assert cfg.wake_words.stop == "hey_vox"

    def test_default_stt(self):
        cfg = HeyvoxConfig()
        assert cfg.stt.backend == "local"
        assert cfg.stt.local.engine == "mlx"
        assert cfg.stt.local.mlx_model == "mlx-community/whisper-small-mlx"

    def test_default_tts(self):
        cfg = HeyvoxConfig()
        assert cfg.tts.enabled is True
        assert cfg.tts.voice == "af_heart"
        assert cfg.tts.speed == 1.0
        assert cfg.tts.verbosity == "full"
        assert cfg.tts.pause_media is False

    def test_default_ptt(self):
        cfg = HeyvoxConfig()
        assert cfg.push_to_talk.enabled is True
        assert cfg.push_to_talk.key == "fn"

    def test_default_audio(self):
        cfg = HeyvoxConfig()
        assert cfg.audio.sample_rate == 16000
        assert cfg.audio.chunk_size == 1280

    def test_default_target_mode(self):
        cfg = HeyvoxConfig()
        assert cfg.target_mode == "always-focused"
        assert cfg.target_app == ""

    def test_default_agents_list(self):
        cfg = HeyvoxConfig()
        assert "Claude" in cfg.agents
        assert "Cursor" in cfg.agents


class TestWakeWordConfig:
    """Wake word stop defaults to start when empty."""

    def test_stop_defaults_to_start(self):
        ww = WakeWordConfig(start="my_wake_word")
        assert ww.stop == "my_wake_word"

    def test_explicit_stop_preserved(self):
        ww = WakeWordConfig(start="start_word", stop="stop_word")
        assert ww.stop == "stop_word"

    def test_empty_stop_becomes_start(self):
        ww = WakeWordConfig(start="hello", stop="")
        assert ww.stop == "hello"


class TestTTSConfig:
    """TTS config validation — verbosity, ducking, script_path."""

    def test_valid_verbosity_values(self):
        for v in ("full", "summary", "short", "skip"):
            cfg = TTSConfig(verbosity=v)
            assert cfg.verbosity == v

    def test_invalid_verbosity_raises(self):
        with pytest.raises(Exception):
            TTSConfig(verbosity="loud")

    def test_ducking_percent_clamped_high(self):
        cfg = TTSConfig(ducking_percent=150)
        assert cfg.ducking_percent == 100

    def test_ducking_percent_clamped_low(self):
        cfg = TTSConfig(ducking_percent=-10)
        assert cfg.ducking_percent == 0

    def test_ducking_percent_normal(self):
        cfg = TTSConfig(ducking_percent=60)
        assert cfg.ducking_percent == 60

    def test_script_path_none_is_ok(self):
        cfg = TTSConfig(script_path=None)
        assert cfg.script_path is None

    def test_script_path_nonexistent_raises(self):
        with pytest.raises(Exception):
            TTSConfig(script_path="/nonexistent/path/to/script.sh")


class TestHeyvoxConfigValidation:
    """Validation of root config fields."""

    def test_valid_target_modes(self):
        for mode in ("always-focused", "pinned-app", "last-agent"):
            cfg = HeyvoxConfig(target_mode=mode)
            assert cfg.target_mode == mode

    def test_invalid_target_mode_raises(self):
        with pytest.raises(Exception):
            HeyvoxConfig(target_mode="auto-magic")

    def test_extra_fields_ignored(self):
        cfg = HeyvoxConfig(unknown_field="hello", another=123)
        assert not hasattr(cfg, "unknown_field")


class TestLoadConfig:
    """Test load_config() with real YAML files."""

    def test_missing_file_returns_defaults(self, tmp_path):
        cfg = load_config(tmp_path / "nonexistent.yaml")
        assert cfg.threshold == 0.5

    def test_empty_file_returns_defaults(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text("")
        cfg = load_config(f)
        assert cfg.threshold == 0.5

    def test_partial_yaml_merges_with_defaults(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text("threshold: 0.8\ncooldown_secs: 3.0\n")
        cfg = load_config(f)
        assert cfg.threshold == 0.8
        assert cfg.cooldown_secs == 3.0
        assert cfg.wake_words.start == "hey_vox"  # default preserved

    def test_nested_yaml_override(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text("tts:\n  voice: bf_emma\n  speed: 1.5\n")
        cfg = load_config(f)
        assert cfg.tts.voice == "bf_emma"
        assert cfg.tts.speed == 1.5
        assert cfg.tts.enabled is True  # default preserved

    def test_full_yaml_config(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text(
            "wake_words:\n"
            "  start: hey_vox\n"
            "  stop: stop_vox\n"
            "threshold: 0.6\n"
            "target_mode: last-agent\n"
            "tts:\n"
            "  pause_media: true\n"
            "push_to_talk:\n"
            "  key: right_cmd\n"
        )
        cfg = load_config(f)
        assert cfg.wake_words.start == "hey_vox"
        assert cfg.wake_words.stop == "stop_vox"
        assert cfg.threshold == 0.6
        assert cfg.target_mode == "last-agent"
        assert cfg.tts.pause_media is True
        assert cfg.push_to_talk.key == "right_cmd"

    def test_invalid_yaml_exits(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text("tts:\n  verbosity: screaming\n")
        with pytest.raises(SystemExit):
            load_config(f)

    def test_extra_yaml_fields_ignored(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text("future_feature: true\nthreshold: 0.7\n")
        cfg = load_config(f)
        assert cfg.threshold == 0.7


class TestAppProfileNewFields:
    """Phase 15 Plan 15-03: supports_ax_verify, has_session_detection,
    ax_settle_before_verify — defaults + profile-specific overrides (D-22).
    """

    def test_app_profile_default_supports_ax_verify_true(self):
        p = AppProfileConfig(name="Any")
        assert p.supports_ax_verify is True

    def test_app_profile_default_has_session_detection_false(self):
        p = AppProfileConfig(name="Any")
        assert p.has_session_detection is False

    def test_app_profile_default_ax_settle_before_verify_is_0_1(self):
        p = AppProfileConfig(name="Any")
        assert abs(p.ax_settle_before_verify - 0.1) < 1e-6

    def test_conductor_profile_has_session_detection_true(self):
        cfg = HeyvoxConfig()
        cond = cfg.get_app_profile("Conductor")
        assert cond is not None
        assert cond.has_session_detection is True

    def test_conductor_profile_ax_settle_before_verify_0_15(self):
        cfg = HeyvoxConfig()
        cond = cfg.get_app_profile("Conductor")
        assert cond is not None
        assert abs(cond.ax_settle_before_verify - 0.15) < 1e-6

    def test_conductor_profile_supports_ax_verify_true(self):
        cfg = HeyvoxConfig()
        cond = cfg.get_app_profile("Conductor")
        assert cond is not None
        assert cond.supports_ax_verify is True

    def test_terminal_profile_supports_ax_verify_false(self):
        cfg = HeyvoxConfig()
        term = cfg.get_app_profile("Terminal")
        assert term is not None
        assert term.supports_ax_verify is False

    def test_iterm2_profile_supports_ax_verify_false(self):
        cfg = HeyvoxConfig()
        it = cfg.get_app_profile("iTerm2")
        assert it is not None
        assert it.supports_ax_verify is False

    def test_user_profile_can_override_has_session_detection(self):
        p = AppProfileConfig(name="Custom", has_session_detection=True)
        assert p.has_session_detection is True

    def test_user_profile_omitting_new_fields_gets_defaults(self):
        # Simulates config.yaml where the user wrote only name + focus_shortcut.
        p = AppProfileConfig(name="Legacy", focus_shortcut="l")
        assert p.supports_ax_verify is True
        assert p.has_session_detection is False
        assert abs(p.ax_settle_before_verify - 0.1) < 1e-6
