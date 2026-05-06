"""
Unit tests for `heyvox calibrate` CLI command.

Phase 13, Plan 04: heyvox calibrate command.

Tests cover:
- calibrate with default device: collects audio, runs calibration, saves to cache
- calibrate with --device filter: selects specific device
- calibrate --show: displays cached profiles without recording
- calibrate with no devices: exits with error
- calibrate subparser is registered in main()
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_args(**kwargs) -> SimpleNamespace:
    """Make a fake argparse Namespace for _cmd_calibrate."""
    defaults = {
        "device": None,
        "duration": 3,
        "show": False,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _make_audio_chunk(level: int = 100) -> np.ndarray:
    """Return a synthetic int16 audio chunk at the given peak level."""
    chunk = np.full(1280, level, dtype=np.int16)
    return chunk


# ---------------------------------------------------------------------------
# Task 1: calibrate command -- basic flow (no hardware)
# ---------------------------------------------------------------------------

class TestCalibrateFlow:
    """Tests for _cmd_calibrate function."""

    def test_calibrate_default_device_opens_stream_and_saves(self, tmp_path):
        """calibrate() with no --device finds the first input device, records, and saves."""
        from heyvox.cli import _cmd_calibrate

        # Build a fake PyAudio instance
        fake_device_info = {"name": "Built-in Microphone", "maxInputChannels": 1, "index": 0}
        mock_pa = MagicMock()
        mock_pa.get_device_count.return_value = 1
        mock_pa.get_device_info_by_index.return_value = fake_device_info
        mock_pa.get_default_input_device_info.return_value = fake_device_info

        # Build a fake audio stream that returns synthetic chunks
        fake_chunk_bytes = np.full(1280, 120, dtype=np.int16).tobytes()
        mock_stream = MagicMock()
        mock_stream.read.return_value = fake_chunk_bytes
        # pa.open() returns the stream directly (no context manager — PyAudio Stream
        # does not support the context manager protocol)
        mock_pa.open.return_value = mock_stream

        with patch("heyvox.cli._calibrate_open_pa", return_value=mock_pa), \
             patch("heyvox.cli._calibrate_get_cache_dir", return_value=tmp_path):
            args = _make_args()
            _cmd_calibrate(args)

        # Cache file should have been written
        cache_file = tmp_path / "mic-profiles.json"
        assert cache_file.exists(), "Cache file should be written after calibration"
        data = json.loads(cache_file.read_text())
        key = "built-in microphone"
        assert key in data, f"Expected cache key '{key}' in {list(data.keys())}"
        assert "noise_floor" in data[key]
        assert "silence_threshold" in data[key]
        assert "calibrated_at" in data[key]

    def test_calibrate_device_filter_selects_matching_device(self, tmp_path):
        """calibrate --device 'G435' selects a device matching the substring."""
        from heyvox.cli import _cmd_calibrate

        mock_pa = MagicMock()
        mock_pa.get_device_count.return_value = 2
        devices = [
            {"name": "Built-in Microphone", "maxInputChannels": 1, "index": 0},
            {"name": "G435 Wireless Gaming Headset", "maxInputChannels": 1, "index": 1},
        ]
        mock_pa.get_device_info_by_index.side_effect = devices.__getitem__

        fake_chunk_bytes = np.full(1280, 200, dtype=np.int16).tobytes()
        mock_stream = MagicMock()
        mock_stream.read.return_value = fake_chunk_bytes
        # pa.open() returns the stream directly (no context manager)
        mock_pa.open.return_value = mock_stream

        with patch("heyvox.cli._calibrate_open_pa", return_value=mock_pa), \
             patch("heyvox.cli._calibrate_get_cache_dir", return_value=tmp_path):
            args = _make_args(device="G435")
            _cmd_calibrate(args)

        cache_file = tmp_path / "mic-profiles.json"
        assert cache_file.exists()
        data = json.loads(cache_file.read_text())
        # Should have calibrated the G435, not built-in
        assert "g435 wireless gaming headset" in data
        assert "built-in microphone" not in data

    def test_calibrate_no_matching_device_exits_nonzero(self, tmp_path):
        """calibrate --device 'UnknownMic' exits with SystemExit when no match found."""
        from heyvox.cli import _cmd_calibrate

        mock_pa = MagicMock()
        mock_pa.get_device_count.return_value = 1
        mock_pa.get_device_info_by_index.return_value = {
            "name": "Built-in Microphone", "maxInputChannels": 1, "index": 0,
        }
        mock_pa.get_default_input_device_info.return_value = {
            "name": "Built-in Microphone", "maxInputChannels": 1, "index": 0,
        }

        with patch("heyvox.cli._calibrate_open_pa", return_value=mock_pa), \
             patch("heyvox.cli._calibrate_get_cache_dir", return_value=tmp_path):
            args = _make_args(device="UnknownMic")
            with pytest.raises(SystemExit) as exc_info:
                _cmd_calibrate(args)
            assert exc_info.value.code != 0

    def test_calibrate_no_input_devices_at_all_exits_nonzero(self, tmp_path):
        """calibrate exits nonzero when PyAudio finds no input devices."""
        from heyvox.cli import _cmd_calibrate

        mock_pa = MagicMock()
        mock_pa.get_device_count.return_value = 1
        mock_pa.get_device_info_by_index.return_value = {
            "name": "Output Device", "maxInputChannels": 0, "index": 0,
        }
        mock_pa.get_default_input_device_info.side_effect = OSError("no default input")

        with patch("heyvox.cli._calibrate_open_pa", return_value=mock_pa), \
             patch("heyvox.cli._calibrate_get_cache_dir", return_value=tmp_path):
            args = _make_args()
            with pytest.raises(SystemExit) as exc_info:
                _cmd_calibrate(args)
            assert exc_info.value.code != 0

    def test_calibrate_duration_controls_chunk_count(self, tmp_path):
        """calibrate --duration N reads approximately N*sample_rate/chunk_size chunks."""
        from heyvox.cli import _cmd_calibrate

        mock_pa = MagicMock()
        mock_pa.get_device_count.return_value = 1
        mock_pa.get_device_info_by_index.return_value = {
            "name": "Built-in Microphone", "maxInputChannels": 1, "index": 0,
        }
        mock_pa.get_default_input_device_info.return_value = {
            "name": "Built-in Microphone", "maxInputChannels": 1, "index": 0,
        }

        fake_chunk_bytes = np.full(1280, 50, dtype=np.int16).tobytes()
        mock_stream = MagicMock()
        mock_stream.read.return_value = fake_chunk_bytes
        # pa.open() returns the stream directly (no context manager)
        mock_pa.open.return_value = mock_stream

        with patch("heyvox.cli._calibrate_open_pa", return_value=mock_pa), \
             patch("heyvox.cli._calibrate_get_cache_dir", return_value=tmp_path):
            args = _make_args(duration=2)
            _cmd_calibrate(args)

        # With sample_rate=16000, chunk_size=1280, duration=2:
        # chunk_count = 2 * 16000 // 1280 = 25 chunks
        expected_chunks = 2 * 16000 // 1280
        # stream.read should have been called that many times
        assert mock_stream.read.call_count == expected_chunks, (
            f"Expected {expected_chunks} read calls, got {mock_stream.read.call_count}"
        )


# ---------------------------------------------------------------------------
# Task 2: calibrate --show
# ---------------------------------------------------------------------------

class TestCalibrateShow:
    """Tests for _cmd_calibrate with --show flag."""

    def test_show_displays_cached_entries(self, tmp_path, capsys):
        """calibrate --show prints cached calibration data to stdout."""
        from heyvox.cli import _cmd_calibrate

        # Pre-populate a cache file
        cache_data = {
            "built-in microphone": {
                "noise_floor": 45,
                "silence_threshold": 157,
                "calibrated_at": time.time() - 3600,
            },
            "g435 wireless gaming headset": {
                "noise_floor": 25,
                "silence_threshold": 87,
                "calibrated_at": time.time() - 7200,
            },
        }
        (tmp_path / "mic-profiles.json").write_text(json.dumps(cache_data))

        with patch("heyvox.cli._calibrate_get_cache_dir", return_value=tmp_path):
            args = _make_args(show=True)
            _cmd_calibrate(args)

        captured = capsys.readouterr()
        assert "built-in microphone" in captured.out.lower()
        assert "g435" in captured.out.lower()
        assert "45" in captured.out  # noise_floor
        assert "157" in captured.out  # silence_threshold

    def test_show_empty_cache_prints_message(self, tmp_path, capsys):
        """calibrate --show prints a helpful message when no cache exists."""
        from heyvox.cli import _cmd_calibrate

        with patch("heyvox.cli._calibrate_get_cache_dir", return_value=tmp_path):
            args = _make_args(show=True)
            _cmd_calibrate(args)

        captured = capsys.readouterr()
        # Should not crash, should print a "no cache" message
        assert captured.out.strip() != "", "Should print something when no cache exists"

    def test_show_does_not_open_pyaudio(self, tmp_path):
        """calibrate --show never opens a PyAudio stream."""
        from heyvox.cli import _cmd_calibrate

        with patch("heyvox.cli._calibrate_open_pa") as mock_pa_factory, \
             patch("heyvox.cli._calibrate_get_cache_dir", return_value=tmp_path):
            args = _make_args(show=True)
            _cmd_calibrate(args)

        mock_pa_factory.assert_not_called()


# ---------------------------------------------------------------------------
# Task 3: subparser registration
# ---------------------------------------------------------------------------

class TestCalibrateSubparser:
    """Tests that the calibrate subcommand is registered in main()."""

    def test_cmd_calibrate_is_module_level_function(self):
        """_cmd_calibrate must be a callable module-level function."""
        import heyvox.cli as cli
        assert hasattr(cli, "_cmd_calibrate"), "_cmd_calibrate must be a module-level function"
        assert callable(cli._cmd_calibrate), "_cmd_calibrate must be callable"

    def test_calibrate_in_main_parser(self):
        """main() parser includes 'calibrate' subcommand."""
        from unittest.mock import patch as _patch
        # Invoke main with 'calibrate --help', which should exit 0 (help) not with an error
        with _patch("sys.argv", ["heyvox", "calibrate", "--help"]):
            with pytest.raises(SystemExit) as exc:
                from heyvox.cli import main
                main()
            # --help exits 0
            assert exc.value.code == 0
