"""Tests for heyvox.audio.device_handle (P-hotplug-cache).

Covers:
- CoreAudioHandle revalidate True / False / exception paths
- CoreAudioHandle.dropped sticks after a failure
- PortAudioHandle fast-path (index still maps to expected name)
- PortAudioHandle drift recovery (name found under a different index)
- PortAudioHandle drop (no device with the expected name)
- PortAudioHandle tolerates PyAudio OSError mid-scan
- PortAudioHandle missing expected_name is a no-op
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


from heyvox.audio.device_handle import CoreAudioHandle, PortAudioHandle


# ---------------------------------------------------------------------------
# CoreAudioHandle
# ---------------------------------------------------------------------------


class TestCoreAudioHandle:
    def test_revalidate_returns_true_for_live_device(self):
        with patch(
            "heyvox.herald.coreaudio._is_coreaudio_device_alive", return_value=True
        ):
            h = CoreAudioHandle(dev_id=123)
            assert h.revalidate() is True
            assert h.dropped is False
            assert h.id == 123

    def test_revalidate_returns_false_and_drops_for_ghost(self):
        with patch(
            "heyvox.herald.coreaudio._is_coreaudio_device_alive", return_value=False
        ):
            h = CoreAudioHandle(dev_id=973)
            assert h.revalidate() is False
            assert h.dropped is True

    def test_revalidate_short_circuits_after_drop(self):
        """Once dropped, revalidate must not call into CoreAudio again."""
        call_count = {"n": 0}

        def fake_check(_dev_id):
            call_count["n"] += 1
            return False

        with patch(
            "heyvox.herald.coreaudio._is_coreaudio_device_alive", side_effect=fake_check
        ):
            h = CoreAudioHandle(dev_id=42)
            assert h.revalidate() is False
            assert h.revalidate() is False
            assert h.revalidate() is False
        # First call set dropped=True; subsequent calls short-circuited.
        assert call_count["n"] == 1

    def test_revalidate_treats_exceptions_as_dead(self):
        with patch(
            "heyvox.herald.coreaudio._is_coreaudio_device_alive",
            side_effect=RuntimeError("ctypes blew up"),
        ):
            h = CoreAudioHandle(dev_id=1)
            assert h.revalidate() is False
            assert h.dropped is True


# ---------------------------------------------------------------------------
# PortAudioHandle
# ---------------------------------------------------------------------------


def _fake_pa(devices: list[str]):
    """Build a mock PyAudio instance backed by a list of device names.

    Index in the list = PortAudio device index. Out-of-range raises OSError.
    """
    pa = MagicMock()

    def info_by_index(i: int):
        if i < 0 or i >= len(devices):
            raise OSError(f"index {i} out of range")
        return {"name": devices[i], "maxInputChannels": 1}

    pa.get_device_info_by_index.side_effect = info_by_index
    pa.get_device_count.return_value = len(devices)
    return pa


class TestPortAudioHandle:
    def test_revalidate_fast_path_index_still_matches(self):
        pa = _fake_pa(["MacBook Pro Microphone", "G435", "BlackHole 2ch"])
        h = PortAudioHandle(pa=pa, idx=1, expected_name="G435")
        assert h.revalidate() is True
        assert h.idx == 1
        assert h.dropped is False
        # Drift scan must NOT have run — only the fast-path lookup.
        pa.get_device_info_by_index.assert_called_once_with(1)

    def test_revalidate_drift_recovery_finds_under_new_index(self):
        """Hotplug shuffles the list — same name appears at a new index."""
        pa = _fake_pa(["BlackHole 2ch", "MacBook Pro Microphone", "G435"])
        # Cached: G435 was at index 1; now lives at index 2.
        h = PortAudioHandle(pa=pa, idx=1, expected_name="G435")
        assert h.revalidate() is True
        assert h.idx == 2
        assert h.dropped is False

    def test_revalidate_drops_when_name_absent(self):
        pa = _fake_pa(["MacBook Pro Microphone", "BlackHole 2ch"])
        h = PortAudioHandle(pa=pa, idx=0, expected_name="G435")
        assert h.revalidate() is False
        assert h.dropped is True

    def test_revalidate_drops_when_get_device_count_raises(self):
        pa = MagicMock()
        pa.get_device_info_by_index.side_effect = OSError("dead")
        pa.get_device_count.side_effect = OSError("dead")
        h = PortAudioHandle(pa=pa, idx=2, expected_name="G435")
        assert h.revalidate() is False
        assert h.dropped is True

    def test_revalidate_tolerates_partial_failures_during_drift_scan(self):
        """If some indices error during the scan, surviving ones still count."""
        pa = MagicMock()
        pa.get_device_count.return_value = 3

        def info_by_index(i):
            if i == 0:
                raise OSError("transient")
            if i == 1:
                return {"name": "G435", "maxInputChannels": 1}
            if i == 2:
                return {"name": "BlackHole 2ch"}
            raise OSError("oob")

        pa.get_device_info_by_index.side_effect = info_by_index
        # Initial idx points at a stale slot.
        h = PortAudioHandle(pa=pa, idx=99, expected_name="G435")
        assert h.revalidate() is True
        assert h.idx == 1

    def test_revalidate_short_circuits_after_drop(self):
        pa = _fake_pa(["MacBook Pro Microphone"])
        h = PortAudioHandle(pa=pa, idx=0, expected_name="G435")
        assert h.revalidate() is False
        # Reset spy and ensure the next call doesn't query PyAudio at all.
        pa.get_device_count.reset_mock()
        pa.get_device_info_by_index.reset_mock()
        assert h.revalidate() is False
        pa.get_device_count.assert_not_called()
        pa.get_device_info_by_index.assert_not_called()

    def test_revalidate_no_expected_name_is_noop(self):
        pa = _fake_pa(["MacBook Pro Microphone"])
        h = PortAudioHandle(pa=pa, idx=0, expected_name="")
        assert h.revalidate() is True
        pa.get_device_info_by_index.assert_not_called()

    def test_idx_updated_in_place_on_drift(self):
        """Drift recovery must update the in-place attribute, not return a copy."""
        pa = _fake_pa(["BlackHole 2ch", "G435", "MacBook Pro Microphone"])
        h = PortAudioHandle(pa=pa, idx=0, expected_name="G435")
        h.revalidate()
        # Outside callers read .idx directly to wire it into a new stream open.
        assert h.idx == 1
