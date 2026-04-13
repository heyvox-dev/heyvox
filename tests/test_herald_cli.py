"""
Tests for herald CLI stop and interrupt commands.

Covers:
- _cmd_stop: kills afplay PID, clears entire queue, clears TTS state
- _cmd_interrupt: kills afplay PID, does NOT clear queue
- dispatch routing for stop and interrupt
- tts.py wiring: stop_all uses "stop", interrupt uses "interrupt", clear_queue uses "skip"
"""

import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import heyvox.herald.cli as cli_mod
from heyvox.audio import tts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tmp_env(tmp: str):
    """Return (pid_file, queue_dir, tts_flag) paths inside tmp."""
    pid_file = os.path.join(tmp, "herald-playing.pid")
    queue_dir = os.path.join(tmp, "herald-queue")
    os.makedirs(queue_dir, exist_ok=True)
    tts_flag = os.path.join(tmp, "heyvox-tts-playing")
    return pid_file, queue_dir, tts_flag


def _patch_constants(pid_file, queue_dir, tts_flag):
    """Patch all three constants used by _cmd_stop/_cmd_interrupt/_cmd_skip."""
    return (
        patch("heyvox.constants.HERALD_PLAYING_PID", pid_file),
        patch("heyvox.constants.HERALD_QUEUE_DIR", queue_dir),
        patch("heyvox.constants.TTS_PLAYING_FLAG", tts_flag),
    )


# ---------------------------------------------------------------------------
# Tests for _cmd_stop
# ---------------------------------------------------------------------------

class TestCmdStopKillsAfplay(unittest.TestCase):
    """_cmd_stop reads HERALD_PLAYING_PID and sends SIGTERM."""

    def test_cmd_stop_kills_afplay(self):
        """_cmd_stop reads the PID file and sends SIGTERM to afplay."""
        with tempfile.TemporaryDirectory() as tmp:
            pid_file, queue_dir, tts_flag = _make_tmp_env(tmp)
            with open(pid_file, "w") as f:
                f.write("12345\n")

            p1, p2, p3 = _patch_constants(pid_file, queue_dir, tts_flag)
            with p1, p2, p3, patch("heyvox.herald.cli.os.kill") as mock_kill:
                result = cli_mod._cmd_stop()

        assert result == 0
        mock_kill.assert_called_once_with(12345, signal.SIGTERM)

    def test_cmd_stop_no_pid_file(self):
        """_cmd_stop returns 0 even if no PID file exists (afplay not running)."""
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = os.path.join(tmp, "nonexistent.pid")
            _, queue_dir, tts_flag = _make_tmp_env(tmp)

            p1, p2, p3 = _patch_constants(pid_file, queue_dir, tts_flag)
            with p1, p2, p3, patch("heyvox.herald.cli.os.kill") as mock_kill:
                result = cli_mod._cmd_stop()

        assert result == 0
        mock_kill.assert_not_called()

    def test_cmd_stop_invalid_pid_file(self):
        """_cmd_stop handles corrupt PID file gracefully."""
        with tempfile.TemporaryDirectory() as tmp:
            pid_file, queue_dir, tts_flag = _make_tmp_env(tmp)
            with open(pid_file, "w") as f:
                f.write("not-a-number\n")

            p1, p2, p3 = _patch_constants(pid_file, queue_dir, tts_flag)
            with p1, p2, p3, patch("heyvox.herald.cli.os.kill") as mock_kill:
                result = cli_mod._cmd_stop()

        assert result == 0
        mock_kill.assert_not_called()


class TestCmdStopClearsQueue(unittest.TestCase):
    """_cmd_stop clears all WAV files from queue directory."""

    def test_cmd_stop_clears_queue(self):
        """_cmd_stop removes all files from the queue directory."""
        with tempfile.TemporaryDirectory() as tmp:
            pid_file, queue_dir, tts_flag = _make_tmp_env(tmp)

            for name in ["msg1_part1.wav", "msg1_part2.wav", "msg2_part1.wav"]:
                Path(os.path.join(queue_dir, name)).touch()

            p1, p2, p3 = _patch_constants(pid_file, queue_dir, tts_flag)
            with p1, p2, p3, patch("heyvox.herald.cli.os.kill"):
                result = cli_mod._cmd_stop()

            assert result == 0
            remaining = os.listdir(queue_dir)
            assert remaining == [], f"Queue not cleared: {remaining}"

    def test_cmd_stop_empty_queue_ok(self):
        """_cmd_stop succeeds even if queue directory is empty."""
        with tempfile.TemporaryDirectory() as tmp:
            pid_file, queue_dir, tts_flag = _make_tmp_env(tmp)

            p1, p2, p3 = _patch_constants(pid_file, queue_dir, tts_flag)
            with p1, p2, p3, patch("heyvox.herald.cli.os.kill"):
                result = cli_mod._cmd_stop()

            assert result == 0


class TestCmdStopClearsTtsState(unittest.TestCase):
    """_cmd_stop removes TTS_PLAYING_FLAG immediately."""

    def test_cmd_stop_clears_tts_state(self):
        """_cmd_stop removes TTS flag file."""
        with tempfile.TemporaryDirectory() as tmp:
            pid_file, queue_dir, tts_flag = _make_tmp_env(tmp)
            Path(tts_flag).touch()

            p1, p2, p3 = _patch_constants(pid_file, queue_dir, tts_flag)
            with p1, p2, p3, patch("heyvox.herald.cli.os.kill"):
                result = cli_mod._cmd_stop()

            assert result == 0
            assert not os.path.exists(tts_flag), "TTS flag should be removed"

    def test_cmd_stop_no_tts_flag_ok(self):
        """_cmd_stop succeeds even if TTS flag doesn't exist."""
        with tempfile.TemporaryDirectory() as tmp:
            pid_file, queue_dir, tts_flag = _make_tmp_env(tmp)

            p1, p2, p3 = _patch_constants(pid_file, queue_dir, tts_flag)
            with p1, p2, p3, patch("heyvox.herald.cli.os.kill"):
                result = cli_mod._cmd_stop()

            assert result == 0


# ---------------------------------------------------------------------------
# Tests for _cmd_interrupt
# ---------------------------------------------------------------------------

class TestCmdInterruptKillsAfplay(unittest.TestCase):
    """_cmd_interrupt kills afplay but does NOT clear queue."""

    def test_cmd_interrupt_kills_afplay(self):
        """_cmd_interrupt sends SIGTERM to the afplay process."""
        with tempfile.TemporaryDirectory() as tmp:
            pid_file, queue_dir, tts_flag = _make_tmp_env(tmp)
            with open(pid_file, "w") as f:
                f.write("99999\n")

            p1, p2, p3 = _patch_constants(pid_file, queue_dir, tts_flag)
            with p1, p2, p3, patch("heyvox.herald.cli.os.kill") as mock_kill:
                result = cli_mod._cmd_interrupt()

        assert result == 0
        mock_kill.assert_called_once_with(99999, signal.SIGTERM)

    def test_cmd_interrupt_does_not_clear_queue(self):
        """_cmd_interrupt preserves unrelated queued messages (D-06)."""
        with tempfile.TemporaryDirectory() as tmp:
            pid_file, queue_dir, tts_flag = _make_tmp_env(tmp)

            queue_files = ["msg2_part1.wav", "msg2_part2.wav", "msg3_part1.wav"]
            for name in queue_files:
                Path(os.path.join(queue_dir, name)).touch()

            p1, p2, p3 = _patch_constants(pid_file, queue_dir, tts_flag)
            with p1, p2, p3, patch("heyvox.herald.cli.os.kill"):
                result = cli_mod._cmd_interrupt()

            assert result == 0
            remaining = sorted(os.listdir(queue_dir))
            assert remaining == sorted(queue_files), \
                f"Queue should be intact: expected {sorted(queue_files)}, got {remaining}"


# ---------------------------------------------------------------------------
# Tests for dispatch routing
# ---------------------------------------------------------------------------

class TestDispatchRouting(unittest.TestCase):
    """dispatch() correctly routes stop and interrupt commands."""

    def test_dispatch_routes_stop(self):
        """dispatch(['stop']) routes to _cmd_stop (returns 0, not 'Unknown command')."""
        with tempfile.TemporaryDirectory() as tmp:
            pid_file, queue_dir, tts_flag = _make_tmp_env(tmp)

            p1, p2, p3 = _patch_constants(pid_file, queue_dir, tts_flag)
            with p1, p2, p3, patch("heyvox.herald.cli.os.kill"):
                result = cli_mod.dispatch(["stop"])

        assert result == 0, f"stop command should return 0, got {result}"

    def test_dispatch_routes_interrupt(self):
        """dispatch(['interrupt']) routes to _cmd_interrupt (returns 0)."""
        with tempfile.TemporaryDirectory() as tmp:
            pid_file, queue_dir, tts_flag = _make_tmp_env(tmp)

            p1, p2, p3 = _patch_constants(pid_file, queue_dir, tts_flag)
            with p1, p2, p3, patch("heyvox.herald.cli.os.kill"):
                result = cli_mod.dispatch(["interrupt"])

        assert result == 0, f"interrupt command should return 0, got {result}"

    def test_dispatch_unknown_still_returns_1(self):
        """Genuinely unknown commands still return exit code 1."""
        result = cli_mod.dispatch(["nonexistent_command"])
        assert result == 1


# ---------------------------------------------------------------------------
# Tests for tts.py wiring
# ---------------------------------------------------------------------------

class TestTtsWiring(unittest.TestCase):
    """tts.py functions call the correct herald commands."""

    @patch("heyvox.audio.tts._herald")
    def test_tts_stop_all_calls_herald_stop(self, mock_herald):
        """stop_all() must call herald stop (not skip or unknown)."""
        mock_herald.return_value = MagicMock(returncode=0)
        tts.stop_all()
        mock_herald.assert_called_once_with("stop")

    @patch("heyvox.audio.tts._herald")
    def test_tts_interrupt_calls_herald_interrupt(self, mock_herald):
        """interrupt() must call herald interrupt (not skip — D-06 selective purge)."""
        mock_herald.return_value = MagicMock(returncode=0)
        tts.interrupt()
        mock_herald.assert_called_once_with("interrupt")

    @patch("heyvox.audio.tts._herald")
    def test_tts_clear_queue_calls_herald_skip(self, mock_herald):
        """clear_queue() must call herald skip (just clear files, no PID kill)."""
        mock_herald.return_value = MagicMock(returncode=0)
        tts.clear_queue()
        mock_herald.assert_called_once_with("skip")


if __name__ == "__main__":
    unittest.main()
