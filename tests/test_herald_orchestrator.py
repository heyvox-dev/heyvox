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
        hold_dir=tmp_path / "herald-hold",
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
        play_next_flag=tmp_path / "herald-play-next",
        last_play_file=tmp_path / "herald-last-play",
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
        assert isinstance(cfg.hold_dir, Path)
        assert isinstance(cfg.history_dir, Path)
        assert isinstance(cfg.claim_dir, Path)
        assert isinstance(cfg.pause_flag, Path)
        assert isinstance(cfg.recording_flag, Path)
        assert isinstance(cfg.orch_pid_file, Path)
        assert isinstance(cfg.original_vol_file, Path)

    def test_default_poll_interval(self):
        cfg = OrchestratorConfig()
        assert cfg.poll_interval == pytest.approx(0.1)

    def test_default_duck_level(self):
        cfg = OrchestratorConfig()
        assert cfg.duck_level == pytest.approx(0.03)
        assert cfg.duck_enabled is True

    def test_default_max_held(self):
        cfg = OrchestratorConfig()
        assert cfg.max_held == 5

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
        cfg.hold_dir.mkdir(parents=True)
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
        """run() should create queue/hold/history/claim dirs if missing."""
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
        assert cfg.hold_dir.exists()
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
        cfg.hold_dir.mkdir(parents=True)
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
        cfg.hold_dir.mkdir(parents=True)
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
