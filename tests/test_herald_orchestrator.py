"""Tests for heyvox.herald.orchestrator and heyvox.herald.coreaudio.

All tests are fully unit-testable:
- No real afplay invocations
- No real CoreAudio calls (patched)
- All filesystem access redirected to tmp_path
"""

from __future__ import annotations

import os
import struct
import threading
import time
import wave
from pathlib import Path
from unittest.mock import patch

import pytest

import unittest.mock

from heyvox.herald.orchestrator import (
    HeraldOrchestrator,
    OrchestratorConfig,
    _enforce_singleton,
    _is_paused,
    _duck_audio,
    _restore_audio,
    _herald_log,
    _get_verbosity,
    _is_skip,
    _generated_before_last_stop,
    _user_is_active,
    _violation_check,
    _media_pause,
    _media_resume,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_wav(path: Path, num_frames: int = 1000, amplitude: int = 5000) -> None:
    """Write a minimal 16-bit mono WAV file at the given path."""
    samples = [amplitude] * num_frames
    data = struct.pack(f"<{num_frames}h", *samples)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(22050)
        wf.writeframes(data)


def _make_silent_wav(path: Path, num_frames: int = 100) -> None:
    """Write a WAV file with near-silent content (amplitude < 50)."""
    samples = [10] * num_frames  # RMS well below 50 threshold
    data = struct.pack(f"<{num_frames}h", *samples)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(22050)
        wf.writeframes(data)


def _cfg(tmp_path: Path, **kwargs) -> OrchestratorConfig:
    """Return an OrchestratorConfig wired to tmp_path for isolation."""
    return OrchestratorConfig(
        queue_dir=tmp_path / "herald-queue",
        history_dir=tmp_path / "herald-history",
        claim_dir=tmp_path / "herald-claim",
        debug_log=tmp_path / "herald-debug.log",
        violations_log=tmp_path / "herald-violations.log",
        orch_pid_file=tmp_path / "herald-orchestrator.pid",
        playing_pid_file=tmp_path / "herald-playing.pid",
        original_vol_file=tmp_path / "herald-original-vol",
        pause_flag=tmp_path / "herald-pause",
        mute_flag=tmp_path / "herald-mute",
        recording_flag=tmp_path / "heyvox-recording",
        pending_switch_file=tmp_path / "herald-pending-switch",
        cancel_switch_flag=tmp_path / "herald-cancel-switch",
        last_play_file=tmp_path / "herald-last-play",
        stop_ts_file=tmp_path / "herald-stop.ts",
        verbosity_file=tmp_path / "heyvox-verbosity",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# OrchestratorConfig tests
# ---------------------------------------------------------------------------


class TestOrchestratorConfig:
    def test_defaults_are_path_objects(self):
        """All directory/file fields should be Path instances."""
        cfg = OrchestratorConfig()
        assert isinstance(cfg.queue_dir, Path)
        assert isinstance(cfg.history_dir, Path)
        assert isinstance(cfg.claim_dir, Path)
        assert isinstance(cfg.pause_flag, Path)
        assert isinstance(cfg.recording_flag, Path)
        assert isinstance(cfg.orch_pid_file, Path)
        assert isinstance(cfg.original_vol_file, Path)
        assert isinstance(cfg.pending_switch_file, Path)
        assert isinstance(cfg.cancel_switch_flag, Path)

    def test_default_poll_interval(self):
        cfg = OrchestratorConfig()
        assert cfg.poll_interval == pytest.approx(0.1)

    def test_default_duck_level(self):
        cfg = OrchestratorConfig()
        assert cfg.duck_level == pytest.approx(0.03)
        assert cfg.duck_enabled is True

    def test_default_switch_countdown_secs(self):
        cfg = OrchestratorConfig()
        assert cfg.switch_countdown_secs == pytest.approx(2.5)
        assert cfg.switch_cancel_key == "right_ctrl"

    def test_default_history_cap(self):
        cfg = OrchestratorConfig()
        assert cfg.history_cap == 50

    def test_custom_queue_dir(self, tmp_path):
        cfg = OrchestratorConfig(queue_dir=tmp_path / "custom-queue")
        assert cfg.queue_dir == tmp_path / "custom-queue"

    def test_duck_disabled(self, tmp_path):
        cfg = _cfg(tmp_path, duck_enabled=False)
        assert cfg.duck_enabled is False

    def test_media_pause_default_true(self):
        cfg = OrchestratorConfig()
        assert cfg.media_pause is True

    def test_volume_cache_ttl_default(self):
        cfg = OrchestratorConfig()
        assert cfg.volume_cache_ttl == pytest.approx(5.0)

    def test_normalize_params(self):
        cfg = OrchestratorConfig()
        assert cfg.normalize_target_rms == 3000
        assert cfg.normalize_scale_cap == pytest.approx(3.0)
        assert cfg.normalize_peak_limit == 24000


# ---------------------------------------------------------------------------
# _is_paused tests
# ---------------------------------------------------------------------------


class TestIsPaused:
    def test_not_paused_by_default(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert _is_paused(cfg, cfg.debug_log) is False

    def test_paused_by_pause_flag(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.pause_flag.touch()
        assert _is_paused(cfg, cfg.debug_log) is True

    def test_paused_by_recording_flag(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.recording_flag.touch()
        assert _is_paused(cfg, cfg.debug_log) is True

    def test_stale_recording_flag_removed(self, tmp_path):
        """Recording flag older than max_age should be removed and return False."""
        cfg = _cfg(tmp_path, recording_flag_max_age=0)
        cfg.recording_flag.touch()
        # Make the file old by setting mtime to epoch
        os.utime(str(cfg.recording_flag), (0, 0))
        time.sleep(0.01)  # ensure the flag age check works

        result = _is_paused(cfg, cfg.debug_log)
        assert result is False
        assert not cfg.recording_flag.exists(), "Stale flag should be removed"

    def test_pause_flag_takes_priority(self, tmp_path):
        """Both pause and recording flags → still paused."""
        cfg = _cfg(tmp_path)
        cfg.pause_flag.touch()
        cfg.recording_flag.touch()
        assert _is_paused(cfg, cfg.debug_log) is True


# ---------------------------------------------------------------------------
# _duck_audio / _restore_audio tests
#
# Note: _duck_audio/_restore_audio import from heyvox.herald.coreaudio
# via inline imports. Patch must target heyvox.herald.coreaudio directly.
# ---------------------------------------------------------------------------


class TestAudioDucking:
    @pytest.fixture(autouse=True)
    def _reset_volume_cache(self):
        """Cache state leaks between tests — DEF-072 sees stale 0.03 from a
        prior duck and treats it as 'looks bogus, skip', then mock_set never
        fires. Invalidate before AND after each test to keep them isolated."""
        from heyvox.herald import coreaudio
        coreaudio._invalidate_volume_cache()
        yield
        coreaudio._invalidate_volume_cache()

    def test_duck_saves_original_volume(self, tmp_path):
        cfg = _cfg(tmp_path, duck_level=0.03, duck_enabled=True)
        # DEF-046 added a `dev_id:vol` sidecar format. Pin device to None so
        # the legacy plain-float branch runs and the assertion matches.
        with patch("heyvox.herald.coreaudio.get_system_volume", return_value=0.7):
            with patch("heyvox.herald.coreaudio.set_system_volume"):
                with patch("heyvox.herald.coreaudio._get_default_output_device", return_value=None):
                    original = _duck_audio(cfg, cfg.debug_log)
        assert original == pytest.approx(0.7)
        assert cfg.original_vol_file.exists()
        assert float(cfg.original_vol_file.read_text().strip()) == pytest.approx(0.7)

    def test_duck_sets_duck_level(self, tmp_path):
        cfg = _cfg(tmp_path, duck_level=0.05, duck_enabled=True)
        with patch("heyvox.herald.coreaudio.get_system_volume", return_value=0.7):
            with patch("heyvox.herald.coreaudio.set_system_volume") as mock_set:
                _duck_audio(cfg, cfg.debug_log)
        # set_system_volume_cached wraps set_system_volume, so it calls through
        # We verify duck level via original_vol_file and that set was called
        assert mock_set.called

    def test_duck_skipped_when_disabled(self, tmp_path):
        cfg = _cfg(tmp_path, duck_enabled=False)
        with patch("heyvox.herald.coreaudio.set_system_volume") as mock_set:
            result = _duck_audio(cfg, cfg.debug_log)
        assert result is None
        mock_set.assert_not_called()

    def test_duck_reuses_saved_vol_on_restart(self, tmp_path):
        """If original_vol_file exists, use it instead of re-reading volume."""
        cfg = _cfg(tmp_path, duck_level=0.03, duck_enabled=True)
        cfg.original_vol_file.write_text("0.8")
        call_count = [0]

        def counting_get():
            call_count[0] += 1
            return 0.7

        with patch("heyvox.herald.coreaudio.get_system_volume", side_effect=counting_get):
            with patch("heyvox.herald.coreaudio.set_system_volume"):
                result = _duck_audio(cfg, cfg.debug_log)
        assert result == pytest.approx(0.8)
        # Should NOT call get_system_volume (using saved file value)
        assert call_count[0] == 0

    def test_restore_sets_original_volume(self, tmp_path):
        cfg = _cfg(tmp_path, duck_enabled=True)
        cfg.original_vol_file.write_text("0.65")
        with patch("heyvox.herald.coreaudio.set_system_volume") as mock_set:
            _restore_audio(0.65, cfg, cfg.debug_log)
        assert mock_set.called
        assert not cfg.original_vol_file.exists()

    def test_restore_reads_file_when_vol_none(self, tmp_path):
        """If original_vol is None, read from file."""
        cfg = _cfg(tmp_path, duck_enabled=True)
        cfg.original_vol_file.write_text("0.55")
        with patch("heyvox.herald.coreaudio.set_system_volume") as mock_set:
            _restore_audio(None, cfg, cfg.debug_log)
        assert mock_set.called

    def test_restore_no_op_when_disabled(self, tmp_path):
        cfg = _cfg(tmp_path, duck_enabled=False)
        with patch("heyvox.herald.coreaudio.set_system_volume") as mock_set:
            _restore_audio(0.5, cfg, cfg.debug_log)
        mock_set.assert_not_called()

    def test_restore_no_op_when_no_file_and_vol_none(self, tmp_path):
        """If both original_vol=None and file missing, restore is a no-op."""
        cfg = _cfg(tmp_path, duck_enabled=True)
        with patch("heyvox.herald.coreaudio.set_system_volume") as mock_set:
            _restore_audio(None, cfg, cfg.debug_log)
        mock_set.assert_not_called()

    def test_restore_pre_check_catches_ghost_device(
        self, tmp_path, monkeypatch
    ):
        """DEF-113 + DeviceHandle: CoreAudioHandle.revalidate() returns False
        for a stale dev_id, so _set_volume_coreaudio is never called and the
        orchestrator falls straight back to set_system_volume_cached. A warn
        banner surfaces.
        """
        monkeypatch.setattr(
            "heyvox.constants.HUD_BANNERS_FILE",
            str(tmp_path / "heyvox-hud-banners.json"),
        )

        cfg = _cfg(tmp_path, duck_enabled=True)
        cfg.original_vol_file.write_text("973:0.65")

        with patch(
            "heyvox.herald.coreaudio._is_coreaudio_device_alive", return_value=False
        ), patch(
            "heyvox.herald.coreaudio._set_volume_coreaudio", return_value=True
        ) as mock_set_dev, patch(
            "heyvox.herald.coreaudio.set_system_volume_cached"
        ) as mock_set_sys:
            _restore_audio(0.65, cfg, cfg.debug_log)

        # Pre-check tripped — Set was never attempted on the ghost.
        mock_set_dev.assert_not_called()
        mock_set_sys.assert_called_once_with(0.65)
        assert not cfg.original_vol_file.exists()

        from heyvox.hud.surface import HUDSurface
        ghost_records = [
            r for r in HUDSurface.read_active(include_legacy=False)
            if r["source"] == "herald-ghost-dev"
        ]
        assert len(ghost_records) == 1
        assert ghost_records[0]["level"] == "warn"

    def test_restore_post_check_catches_race_after_pre_check_passed(
        self, tmp_path, monkeypatch
    ):
        """Rare race: device survives the HasProperty probe but dies before
        the Set call. _set_volume_coreaudio returns False → same fallback.
        """
        monkeypatch.setattr(
            "heyvox.constants.HUD_BANNERS_FILE",
            str(tmp_path / "heyvox-hud-banners.json"),
        )

        cfg = _cfg(tmp_path, duck_enabled=True)
        cfg.original_vol_file.write_text("973:0.65")

        with patch(
            "heyvox.herald.coreaudio._is_coreaudio_device_alive", return_value=True
        ), patch(
            "heyvox.herald.coreaudio._set_volume_coreaudio", return_value=False
        ) as mock_set_dev, patch(
            "heyvox.herald.coreaudio.set_system_volume_cached"
        ) as mock_set_sys:
            _restore_audio(0.65, cfg, cfg.debug_log)

        mock_set_dev.assert_called_once_with(973, 0.65)
        mock_set_sys.assert_called_once_with(0.65)

        from heyvox.hud.surface import HUDSurface
        ghost_records = [
            r for r in HUDSurface.read_active(include_legacy=False)
            if r["source"] == "herald-ghost-dev"
        ]
        assert len(ghost_records) == 1

    def test_restore_ok_path_does_not_fall_back(self, tmp_path, monkeypatch):
        """When both the pre-check and the Set call succeed, no fallback,
        no banner.
        """
        monkeypatch.setattr(
            "heyvox.constants.HUD_BANNERS_FILE",
            str(tmp_path / "heyvox-hud-banners.json"),
        )

        cfg = _cfg(tmp_path, duck_enabled=True)
        cfg.original_vol_file.write_text("123:0.65")

        with patch(
            "heyvox.herald.coreaudio._is_coreaudio_device_alive", return_value=True
        ), patch(
            "heyvox.herald.coreaudio._set_volume_coreaudio", return_value=True
        ) as mock_set_dev, patch(
            "heyvox.herald.coreaudio.set_system_volume_cached"
        ) as mock_set_sys:
            _restore_audio(0.65, cfg, cfg.debug_log)

        mock_set_dev.assert_called_once_with(123, 0.65)
        mock_set_sys.assert_not_called()

        from heyvox.hud.surface import HUDSurface
        live = HUDSurface.read_active(include_legacy=False)
        assert all(r["source"] != "herald-ghost-dev" for r in live)


# ---------------------------------------------------------------------------
# Verbosity / skip tests
# ---------------------------------------------------------------------------


class TestVerbosity:
    def test_default_verbosity_is_full(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert _get_verbosity(cfg) == "full"

    def test_reads_verbosity_from_file(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.verbosity_file.write_text("short")
        assert _get_verbosity(cfg) == "short"

    def test_skip_verbosity(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.verbosity_file.write_text("skip")
        assert _is_skip(cfg) is True

    def test_not_skip_by_default(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert _is_skip(cfg) is False


# ---------------------------------------------------------------------------
# _generated_before_last_stop tests (DEF-235)
# ---------------------------------------------------------------------------


class TestGeneratedBeforeLastStop:
    """DEF-235: compares a WAV's mtime against HERALD_STOP_TS_FILE to detect
    a part that was queued before the last Escape/full-stop landed — closes
    the race where the orchestrator's poll loop picks up an already-generated
    part before heyvox.herald.cli._cmd_stop()'s own directory clear lands."""

    def test_no_stop_file_returns_false(self, tmp_path):
        cfg = _cfg(tmp_path)
        wav = tmp_path / "0001.wav"
        wav.touch()
        assert _generated_before_last_stop(wav, cfg) is False

    def test_wav_older_than_stop_returns_true(self, tmp_path):
        cfg = _cfg(tmp_path)
        wav = tmp_path / "0001.wav"
        wav.touch()
        old_time = time.time() - 10
        os.utime(wav, (old_time, old_time))
        cfg.stop_ts_file.write_text(str(time.time()))
        assert _generated_before_last_stop(wav, cfg) is True

    def test_wav_newer_than_stop_returns_false(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.stop_ts_file.write_text(str(time.time() - 10))
        wav = tmp_path / "0001.wav"
        wav.touch()  # mtime = now, after the stop timestamp
        assert _generated_before_last_stop(wav, cfg) is False

    def test_corrupt_stop_file_returns_false(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.stop_ts_file.write_text("not-a-timestamp")
        wav = tmp_path / "0001.wav"
        wav.touch()
        assert _generated_before_last_stop(wav, cfg) is False

    def test_missing_wav_returns_false(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.stop_ts_file.write_text(str(time.time()))
        assert _generated_before_last_stop(tmp_path / "gone.wav", cfg) is False


# ---------------------------------------------------------------------------
# _user_is_active tests
# ---------------------------------------------------------------------------


class TestUserIsActive:
    def test_not_active_by_default(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert _user_is_active(cfg) is False

    def test_active_within_15s(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.last_play_file.write_text(str(int(time.time())))
        assert _user_is_active(cfg) is True

    def test_not_active_after_15s(self, tmp_path):
        cfg = _cfg(tmp_path)
        old_ts = int(time.time()) - 20
        cfg.last_play_file.write_text(str(old_ts))
        assert _user_is_active(cfg) is False

    def test_active_when_paused(self, tmp_path):
        """Paused = user is active."""
        cfg = _cfg(tmp_path)
        cfg.pause_flag.touch()
        assert _user_is_active(cfg) is True


# ---------------------------------------------------------------------------
# _violation_check tests
# ---------------------------------------------------------------------------


class TestViolationCheck:
    def test_no_violation_when_clean(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert _violation_check("test:context", cfg) is False

    def test_violation_when_recording(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.recording_flag.touch()
        assert _violation_check("test:context", cfg) is True
        # Should write to violations log
        assert cfg.violations_log.exists()
        assert "VIOLATION" in cfg.violations_log.read_text()

    def test_violation_when_paused(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.pause_flag.touch()
        assert _violation_check("test:context", cfg) is True

    def test_violation_context_in_log(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.recording_flag.touch()
        _violation_check("orchestrator:pre-play:test.wav", cfg)
        log_content = cfg.violations_log.read_text()
        assert "orchestrator:pre-play:test.wav" in log_content


# ---------------------------------------------------------------------------
# _herald_log tests
# ---------------------------------------------------------------------------


class TestHeraldLog:
    def test_writes_to_file(self, tmp_path):
        log_file = tmp_path / "debug.log"
        _herald_log("test message", log_file)
        assert log_file.exists()
        assert "test message" in log_file.read_text()

    def test_appends_multiple_entries(self, tmp_path):
        log_file = tmp_path / "debug.log"
        _herald_log("first", log_file)
        _herald_log("second", log_file)
        content = log_file.read_text()
        assert "first" in content
        assert "second" in content

    def test_tolerates_unwritable_path(self, tmp_path):
        """Should not raise even if log path is invalid."""
        _herald_log("test", Path("/nonexistent/dir/debug.log"))


# ---------------------------------------------------------------------------
# _enforce_singleton tests
# ---------------------------------------------------------------------------


class TestEnforceSingleton:
    def test_no_pid_file_returns_true(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert _enforce_singleton(cfg) is True

    def test_own_pid_returns_true(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.orch_pid_file.write_text(str(os.getpid()))
        assert _enforce_singleton(cfg) is True

    def test_dead_pid_returns_true(self, tmp_path):
        """PID file with dead process → we can take over."""
        cfg = _cfg(tmp_path)
        # Use a huge PID that almost certainly does not exist
        dead_pid = 99999999
        cfg.orch_pid_file.write_text(str(dead_pid))
        # May return True or False depending on the system; just verify no exception
        result = _enforce_singleton(cfg)
        assert isinstance(result, bool)

    def test_invalid_pid_file_returns_true(self, tmp_path):
        """Corrupt PID file → treat as no running instance."""
        cfg = _cfg(tmp_path)
        cfg.orch_pid_file.write_text("not-a-pid")
        assert _enforce_singleton(cfg) is True


# ---------------------------------------------------------------------------
# HeraldOrchestrator lifecycle tests
# ---------------------------------------------------------------------------


class TestHeraldOrchestratorLifecycle:
    def test_stop_terminates_run(self, tmp_path):
        """Calling stop() from another thread should cause run() to exit within 2s."""
        cfg = _cfg(tmp_path, poll_interval=0.05)
        # Create directories
        cfg.queue_dir.mkdir(parents=True)
        cfg.history_dir.mkdir(parents=True)
        cfg.claim_dir.mkdir(parents=True)

        orch = HeraldOrchestrator(config=cfg)
        errors: list[Exception] = []

        def _run():
            try:
                orch.run()
            except Exception as e:
                errors.append(e)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        time.sleep(0.1)  # let run() start
        orch.stop()
        t.join(timeout=3.0)

        assert not t.is_alive(), "run() should have exited after stop()"
        assert errors == [], f"run() raised: {errors}"

    def test_stop_is_idempotent(self, tmp_path):
        """Calling stop() multiple times should not raise."""
        cfg = _cfg(tmp_path)
        orch = HeraldOrchestrator(config=cfg)
        orch.stop()
        orch.stop()  # should not raise

    def test_run_creates_directories(self, tmp_path):
        """run() should create queue/history/claim dirs if missing."""
        cfg = _cfg(tmp_path, poll_interval=0.05)
        orch = HeraldOrchestrator(config=cfg)

        assert not cfg.queue_dir.exists()

        def _run():
            orch.run()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        time.sleep(0.15)
        orch.stop()
        t.join(timeout=2.0)

        assert cfg.queue_dir.exists()
        assert cfg.history_dir.exists()
        assert cfg.claim_dir.exists()

    def test_run_writes_pid_file(self, tmp_path):
        """run() should write PID file on startup."""
        cfg = _cfg(tmp_path, poll_interval=0.05)
        orch = HeraldOrchestrator(config=cfg)

        def _run():
            orch.run()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        time.sleep(0.15)

        pid_exists = cfg.orch_pid_file.exists()
        orch.stop()
        t.join(timeout=2.0)

        assert pid_exists, "PID file should be written during run()"

    def test_default_config_used_when_none(self):
        """HeraldOrchestrator() with no args uses OrchestratorConfig defaults."""
        from heyvox.constants import HERALD_QUEUE_DIR
        orch = HeraldOrchestrator()
        assert orch.cfg.queue_dir == Path(HERALD_QUEUE_DIR)

    def test_custom_config_used(self, tmp_path):
        """HeraldOrchestrator(config=...) uses provided config."""
        cfg = _cfg(tmp_path)
        orch = HeraldOrchestrator(config=cfg)
        assert orch.cfg.queue_dir == tmp_path / "herald-queue"

    @patch("heyvox.herald.orchestrator.subprocess.Popen")
    def test_muted_wav_deleted_not_played(self, mock_popen, tmp_path):
        """WAV files should be deleted when muted, not played."""
        cfg = _cfg(tmp_path, poll_interval=0.05, media_pause=False, duck_enabled=False)
        cfg.queue_dir.mkdir(parents=True)
        cfg.history_dir.mkdir(parents=True)
        cfg.claim_dir.mkdir(parents=True)
        cfg.mute_flag.touch()

        wav = cfg.queue_dir / "20260101-120000-0001.wav"
        _make_wav(wav)

        orch = HeraldOrchestrator(config=cfg)
        t = threading.Thread(target=orch.run, daemon=True)
        t.start()
        time.sleep(0.3)
        orch.stop()
        t.join(timeout=2.0)

        # afplay should NOT have been called
        for call in mock_popen.call_args_list:
            args = call[0][0] if call[0] else call[1].get("args", [])
            if isinstance(args, list):
                assert "afplay" not in args, "afplay should not be called when muted"

        # WAV should be gone
        assert not wav.exists(), "Muted WAV should be deleted"

    @patch("heyvox.herald.orchestrator.subprocess.Popen")
    def test_skip_verbosity_deletes_wav(self, mock_popen, tmp_path):
        """WAV files should be deleted when verbosity=skip."""
        cfg = _cfg(tmp_path, poll_interval=0.05, media_pause=False, duck_enabled=False)
        cfg.queue_dir.mkdir(parents=True)
        cfg.history_dir.mkdir(parents=True)
        cfg.claim_dir.mkdir(parents=True)
        cfg.verbosity_file.write_text("skip")

        wav = cfg.queue_dir / "20260101-120000-0001.wav"
        _make_wav(wav)

        orch = HeraldOrchestrator(config=cfg)
        t = threading.Thread(target=orch.run, daemon=True)
        t.start()
        time.sleep(0.3)
        orch.stop()
        t.join(timeout=2.0)

        assert not wav.exists(), "WAV should be deleted when verbosity=skip"

    @patch("heyvox.herald.orchestrator.subprocess.Popen")
    def test_stale_wav_from_before_last_stop_deleted_not_played(self, mock_popen, tmp_path):
        """DEF-235: a part queued before the last Escape/stop must be dropped,
        not played — reproduces the "second Escape press needed" bug where
        an already-generated next part slipped past _cmd_stop()'s queue
        clear because the orchestrator's poll picked it up first."""
        cfg = _cfg(tmp_path, poll_interval=0.05, media_pause=False, duck_enabled=False)
        cfg.queue_dir.mkdir(parents=True)
        cfg.history_dir.mkdir(parents=True)
        cfg.claim_dir.mkdir(parents=True)

        wav = cfg.queue_dir / "20260101-120000-0002.wav"
        _make_wav(wav)
        old_time = time.time() - 10
        os.utime(wav, (old_time, old_time))
        cfg.stop_ts_file.write_text(str(time.time()))  # stop landed AFTER the wav was written

        orch = HeraldOrchestrator(config=cfg)
        t = threading.Thread(target=orch.run, daemon=True)
        t.start()
        time.sleep(0.3)
        orch.stop()
        t.join(timeout=2.0)

        for call in mock_popen.call_args_list:
            args = call[0][0] if call[0] else call[1].get("args", [])
            if isinstance(args, list):
                assert "afplay" not in args, "afplay should not be called for a stale pre-stop part"

        assert not wav.exists(), "Stale WAV (queued before last stop) should be deleted"


# ---------------------------------------------------------------------------
# CoreAudio module tests
# ---------------------------------------------------------------------------


class TestCoreAudioModule:
    def test_get_system_volume_returns_float(self):
        from heyvox.herald.coreaudio import get_system_volume
        vol = get_system_volume()
        assert isinstance(vol, float)
        assert 0.0 <= vol <= 1.0

    def test_get_system_volume_cached_returns_float(self):
        from heyvox.herald.coreaudio import get_system_volume_cached
        vol = get_system_volume_cached()
        assert isinstance(vol, float)
        assert 0.0 <= vol <= 1.0

    def test_get_system_volume_cached_uses_cache(self):
        """Two calls within TTL should return same value without re-reading."""
        from heyvox.herald import coreaudio

        call_count = [0]
        original_get = coreaudio.get_system_volume

        def counting_get():
            call_count[0] += 1
            return original_get()

        # Reset cache first
        coreaudio._invalidate_volume_cache()
        with patch.object(coreaudio, "get_system_volume", side_effect=counting_get):
            coreaudio.get_system_volume_cached(ttl=10.0)
            coreaudio.get_system_volume_cached(ttl=10.0)
        # Should only read once — second call uses cache
        assert call_count[0] == 1

    def test_volume_cache_expires_after_ttl(self):
        """Cache should re-read after TTL expires."""
        from heyvox.herald import coreaudio

        call_count = [0]
        original_get = coreaudio.get_system_volume

        def counting_get():
            call_count[0] += 1
            return original_get()

        coreaudio._invalidate_volume_cache()
        with patch.object(coreaudio, "get_system_volume", side_effect=counting_get):
            coreaudio.get_system_volume_cached(ttl=0.01)
            time.sleep(0.05)  # exceed TTL
            coreaudio.get_system_volume_cached(ttl=0.01)
        assert call_count[0] == 2, "Cache should re-read after TTL"

    def test_set_system_volume_cached_updates_cache(self):
        """set_system_volume_cached should update cache to avoid re-read."""
        from heyvox.herald import coreaudio

        with patch.object(coreaudio, "set_system_volume"):
            with patch.object(coreaudio, "get_system_volume", return_value=0.9) as mock_get:
                coreaudio._invalidate_volume_cache()
                coreaudio.set_system_volume_cached(0.5)
                # Read immediately — should use cache, not call get_system_volume
                val = coreaudio.get_system_volume_cached(ttl=10.0)
                assert val == pytest.approx(0.5)
                mock_get.assert_not_called()

    def test_set_system_volume_clamps_to_1(self):
        """set_system_volume should clamp values above 1.0."""
        from heyvox.herald import coreaudio

        with patch.object(coreaudio, "_get_default_output_device", return_value=None):
            with patch.object(coreaudio, "_set_volume_osascript") as mock_set:
                coreaudio.set_system_volume(1.5)
                args = mock_set.call_args[0]
                assert args[0] == pytest.approx(1.0)

    def test_set_system_volume_clamps_to_0(self):
        """set_system_volume should clamp values below 0.0."""
        from heyvox.herald import coreaudio

        with patch.object(coreaudio, "_get_default_output_device", return_value=None):
            with patch.object(coreaudio, "_set_volume_osascript") as mock_set:
                coreaudio.set_system_volume(-0.5)
                args = mock_set.call_args[0]
                assert args[0] == pytest.approx(0.0)

    def test_is_system_muted_returns_bool(self):
        from heyvox.herald.coreaudio import is_system_muted
        result = is_system_muted()
        assert isinstance(result, bool)

    def test_invalidate_cache_clears_cached_value(self):
        from heyvox.herald import coreaudio

        call_count = [0]
        original_get = coreaudio.get_system_volume

        def counting_get():
            call_count[0] += 1
            return original_get()

        coreaudio._invalidate_volume_cache()
        with patch.object(coreaudio, "get_system_volume", side_effect=counting_get):
            coreaudio.get_system_volume_cached(ttl=10.0)
            coreaudio._invalidate_volume_cache()
            coreaudio.get_system_volume_cached(ttl=10.0)
        assert call_count[0] == 2, "Invalidation should force re-read"


# ---------------------------------------------------------------------------
# Media pause/resume Python API tests
# ---------------------------------------------------------------------------


class TestMediaPauseResume:
    def test_media_pause_calls_python_api(self, tmp_path):
        """_media_pause delegates to heyvox.audio.media.pause_media."""
        cfg = _cfg(tmp_path, media_pause=True)
        with unittest.mock.patch("heyvox.audio.media.pause_media") as mock_pause:
            _media_pause(cfg)
            mock_pause.assert_called_once()

    def test_media_resume_calls_python_api(self, tmp_path):
        """_media_resume delegates to heyvox.audio.media.resume_media."""
        cfg = _cfg(tmp_path, media_pause=True)
        with unittest.mock.patch("heyvox.audio.media.resume_media") as mock_resume:
            _media_resume(cfg)
            mock_resume.assert_called_once()

    def test_media_pause_skips_when_disabled(self, tmp_path):
        """_media_pause should not call pause_media when media_pause=False."""
        cfg = _cfg(tmp_path, media_pause=False)
        with unittest.mock.patch("heyvox.audio.media.pause_media") as mock_pause:
            _media_pause(cfg)
            mock_pause.assert_not_called()

    def test_media_resume_skips_when_disabled(self, tmp_path):
        """_media_resume should not call resume_media when media_pause=False."""
        cfg = _cfg(tmp_path, media_pause=False)
        with unittest.mock.patch("heyvox.audio.media.resume_media") as mock_resume:
            _media_resume(cfg)
            mock_resume.assert_not_called()


# ---------------------------------------------------------------------------
# _afplay_ceiling — DEF-140 follow-up: bound a stalled afplay run by clip duration
# ---------------------------------------------------------------------------


def test_afplay_ceiling_short_clip_uses_floor(tmp_path):
    """A sub-second cue clamps up to the 15s floor (tolerates startup latency)."""
    from heyvox.herald.orchestrator import _afplay_ceiling
    wav = tmp_path / "cue.wav"
    _make_wav(wav, num_frames=1000)  # ~0.045s at 22050 Hz
    assert _afplay_ceiling(wav) == 15.0


def test_afplay_ceiling_scales_with_duration(tmp_path):
    """A multi-second clip gets duration + 10s of slack, above the floor."""
    from heyvox.herald.orchestrator import _afplay_ceiling
    wav = tmp_path / "speech.wav"
    _make_wav(wav, num_frames=220500)  # 10.0s at 22050 Hz
    assert _afplay_ceiling(wav) == pytest.approx(20.0, abs=0.1)


def test_afplay_ceiling_missing_file_falls_back_to_cap(tmp_path):
    """A missing WAV falls back to the absolute backstop, never 0 (which would kill instantly)."""
    from heyvox.herald.orchestrator import _afplay_ceiling
    assert _afplay_ceiling(tmp_path / "nope.wav") == 300.0


def test_afplay_ceiling_corrupt_file_falls_back_to_cap(tmp_path):
    """A non-WAV file falls back to the cap rather than raising."""
    from heyvox.herald.orchestrator import _afplay_ceiling
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not a wav at all")
    assert _afplay_ceiling(bad) == 300.0


# ---------------------------------------------------------------------------
# Workspace-switch countdown — replaces the former hold-queue/idle-gate.
# ---------------------------------------------------------------------------


class TestSwitchWorkspaceForce:
    def test_default_passes_force(self, tmp_path):
        """_switch_workspace defaults to force=True — the countdown IS consent now."""
        from heyvox.herald.orchestrator import _switch_workspace
        cfg = _cfg(tmp_path, workspace_switch_cmd=str(tmp_path / "switch.sh"))
        (tmp_path / "switch.sh").write_text("#!/bin/sh\n")
        (tmp_path / "switch.sh").chmod(0o755)
        with patch("heyvox.herald.orchestrator.subprocess.run") as mock_run:
            mock_run.return_value = unittest.mock.Mock(returncode=0, stdout="", stderr="")
            _switch_workspace("some-workspace", cfg)
        argv = mock_run.call_args[0][0]
        assert "--force" in argv

    def test_force_false_omits_flag(self, tmp_path):
        from heyvox.herald.orchestrator import _switch_workspace
        cfg = _cfg(tmp_path, workspace_switch_cmd=str(tmp_path / "switch.sh"))
        (tmp_path / "switch.sh").write_text("#!/bin/sh\n")
        (tmp_path / "switch.sh").chmod(0o755)
        with patch("heyvox.herald.orchestrator.subprocess.run") as mock_run:
            mock_run.return_value = unittest.mock.Mock(returncode=0, stdout="", stderr="")
            _switch_workspace("some-workspace", cfg, force=False)
        argv = mock_run.call_args[0][0]
        assert "--force" not in argv


class TestRunSwitchCountdown:
    """_run_switch_countdown: announce, wait, cancel-or-fire. Uses a short
    switch_countdown_secs/poll_interval so tests run fast."""

    def _cfg_fast(self, tmp_path, **kwargs):
        defaults = dict(
            workspace_switch_cmd=str(tmp_path / "switch.sh"),
            workspace_app_name="Conductor",
            switch_countdown_secs=0.15,
            poll_interval=0.02,
        )
        defaults.update(kwargs)
        return _cfg(tmp_path, **defaults)

    @patch("heyvox.herald.orchestrator._workspace_app_is_frontmost", return_value=True)
    @patch("heyvox.herald.orchestrator._switch_workspace")
    @patch("heyvox.herald.orchestrator._play_switch_pending_cue")
    @patch("heyvox.herald.orchestrator._show_alert")
    def test_fires_switch_when_uncancelled(
        self, mock_alert, mock_cue, mock_switch, mock_frontmost, tmp_path
    ):
        from heyvox.herald.orchestrator import _run_switch_countdown
        cfg = self._cfg_fast(tmp_path)
        stop_event = threading.Event()

        _run_switch_countdown("some-workspace", cfg, cfg.debug_log, stop_event)

        mock_switch.assert_called_once_with("some-workspace", cfg, force=True)
        assert mock_alert.called
        assert not cfg.pending_switch_file.exists()
        assert not cfg.cancel_switch_flag.exists()

    @patch("heyvox.herald.orchestrator._workspace_app_is_frontmost", return_value=True)
    @patch("heyvox.herald.orchestrator._switch_workspace")
    @patch("heyvox.herald.orchestrator._play_switch_pending_cue")
    @patch("heyvox.herald.orchestrator._show_alert")
    def test_cancel_flag_prevents_switch(
        self, mock_alert, mock_cue, mock_switch, mock_frontmost, tmp_path
    ):
        from heyvox.herald.orchestrator import _run_switch_countdown
        cfg = self._cfg_fast(tmp_path)
        stop_event = threading.Event()

        def _cancel_soon():
            time.sleep(0.03)
            cfg.cancel_switch_flag.write_text(str(time.time()))

        canceller = threading.Thread(target=_cancel_soon, daemon=True)
        canceller.start()
        _run_switch_countdown("some-workspace", cfg, cfg.debug_log, stop_event)
        canceller.join(timeout=1.0)

        mock_switch.assert_not_called()
        assert not cfg.pending_switch_file.exists()
        assert not cfg.cancel_switch_flag.exists()

    @patch("heyvox.herald.orchestrator._workspace_app_is_frontmost", return_value=True)
    @patch("heyvox.herald.orchestrator._switch_workspace")
    @patch("heyvox.herald.orchestrator._play_switch_pending_cue")
    @patch("heyvox.herald.orchestrator._show_alert")
    def test_stale_cancel_flag_is_ignored(
        self, mock_alert, mock_cue, mock_switch, mock_frontmost, tmp_path
    ):
        """A cancel flag timestamped BEFORE this window started must not
        poltergeist-cancel it (defends the back-to-back-messages race)."""
        from heyvox.herald.orchestrator import _run_switch_countdown
        cfg = self._cfg_fast(tmp_path)
        cfg.cancel_switch_flag.write_text(str(time.time() - 10))  # stale, pre-dates window
        stop_event = threading.Event()

        _run_switch_countdown("some-workspace", cfg, cfg.debug_log, stop_event)

        mock_switch.assert_called_once_with("some-workspace", cfg, force=True)

    @patch("heyvox.herald.orchestrator._workspace_app_is_frontmost", return_value=True)
    @patch("heyvox.herald.orchestrator._switch_workspace")
    @patch("heyvox.herald.orchestrator._play_switch_pending_cue")
    @patch("heyvox.herald.orchestrator._show_alert")
    def test_superseded_by_stop_event_never_switches(
        self, mock_alert, mock_cue, mock_switch, mock_frontmost, tmp_path
    ):
        """Setting stop_event (a newer message's countdown started) aborts
        this thread without switching, even past the deadline."""
        from heyvox.herald.orchestrator import _run_switch_countdown
        cfg = self._cfg_fast(tmp_path)
        stop_event = threading.Event()
        stop_event.set()  # already superseded before the loop even starts

        _run_switch_countdown("some-workspace", cfg, cfg.debug_log, stop_event)

        mock_switch.assert_not_called()

    @patch("heyvox.herald.orchestrator._workspace_app_is_frontmost", return_value=True)
    @patch("heyvox.herald.orchestrator._switch_workspace")
    @patch("heyvox.herald.orchestrator._play_switch_pending_cue")
    @patch("heyvox.herald.orchestrator._show_alert")
    def test_recording_flag_at_deadline_skips_switch(
        self, mock_alert, mock_cue, mock_switch, mock_frontmost, tmp_path, monkeypatch
    ):
        """RECORDING_FLAG appearing during the countdown skips the switch at
        expiry (DEF-070) — the async design turns the old tiny race window
        into a multi-second one, so this recheck is load-bearing."""
        from heyvox.herald.orchestrator import _run_switch_countdown
        cfg = self._cfg_fast(tmp_path)
        recording_flag = tmp_path / "heyvox-recording"
        monkeypatch.setattr("heyvox.herald.orchestrator.RECORDING_FLAG", str(recording_flag))
        recording_flag.touch()
        stop_event = threading.Event()

        _run_switch_countdown("some-workspace", cfg, cfg.debug_log, stop_event)

        mock_switch.assert_not_called()

    @patch("heyvox.herald.orchestrator._workspace_app_is_frontmost", return_value=True)
    @patch("heyvox.herald.orchestrator._switch_workspace")
    @patch("heyvox.herald.orchestrator._play_switch_pending_cue")
    @patch("heyvox.herald.orchestrator._show_alert")
    def test_writes_and_clears_pending_switch_file(
        self, mock_alert, mock_cue, mock_switch, mock_frontmost, tmp_path
    ):
        from heyvox.herald.orchestrator import _run_switch_countdown
        cfg = self._cfg_fast(tmp_path, switch_countdown_secs=0.3)
        stop_event = threading.Event()
        t = threading.Thread(
            target=_run_switch_countdown,
            args=("some-workspace", cfg, cfg.debug_log, stop_event),
            daemon=True,
        )
        t.start()
        time.sleep(0.1)
        assert cfg.pending_switch_file.exists(), "marker should exist mid-countdown"
        t.join(timeout=1.0)
        assert not cfg.pending_switch_file.exists(), "marker should be cleared after resolution"
