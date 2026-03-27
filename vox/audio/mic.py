"""
Microphone management for vox.

Handles device discovery, priority-based selection, and stream lifecycle.
Supports USB dongles and non-default audio devices (AUDIO-01).
"""

import numpy as np
import pyaudio

from vox.constants import DEFAULT_SAMPLE_RATE, DEFAULT_CHUNK_SIZE


def find_best_mic(pa, mic_priority=None, sample_rate=DEFAULT_SAMPLE_RATE, chunk_size=DEFAULT_CHUNK_SIZE):
    """Find the best working microphone based on priority list.

    Tests each candidate device by actually reading audio frames and checking for
    non-zero levels. Falls back to system default if all devices fail.

    Args:
        pa: PyAudio instance
        mic_priority: List of device name substrings in priority order.
            First matching working device wins.
        sample_rate: Sample rate to test with (Hz).
        chunk_size: Frames per buffer for the test stream.

    Returns:
        Device index (int) or None if no input device is available.
    """
    if mic_priority is None:
        mic_priority = ["MacBook Pro Microphone"]

    devices_by_priority = {name: [] for name in mic_priority}
    other_devices = []

    for i in range(pa.get_device_count()):
        d = pa.get_device_info_by_index(i)
        if d['maxInputChannels'] <= 0:
            continue
        matched = False
        for prio_name in mic_priority:
            if prio_name.lower() in d['name'].lower():
                devices_by_priority[prio_name].append((i, d['name']))
                matched = True
                break
        if not matched:
            other_devices.append((i, d['name']))

    def test_mic(index, name, frames=15):
        try:
            test_stream = pa.open(
                format=pyaudio.paInt16, channels=1,
                rate=sample_rate, input=True,
                input_device_index=index, frames_per_buffer=chunk_size,
            )
            max_level = 0
            for _ in range(frames):
                data = np.frombuffer(
                    test_stream.read(chunk_size, exception_on_overflow=False),
                    dtype=np.int16,
                )
                max_level = max(max_level, np.abs(data).max())
            test_stream.close()
            _log(f"  [{index}] {name}: max_level={max_level}")
            return max_level > 0
        except Exception as e:
            _log(f"  [{index}] {name}: error - {e}")
            return False

    for prio_name in mic_priority:
        for index, dev_name in devices_by_priority[prio_name]:
            _log(f"Testing {dev_name}...")
            if test_mic(index, dev_name):
                return index

    for index, dev_name in other_devices:
        _log(f"Testing fallback {dev_name}...")
        if test_mic(index, dev_name):
            return index

    try:
        default = pa.get_default_input_device_info()['index']
        _log("All mics failed level test, using system default as last resort")
        return default
    except IOError:
        _log("ERROR: No input devices available")
        return None


def open_mic_stream(pa, dev_index, sample_rate=DEFAULT_SAMPLE_RATE, chunk_size=DEFAULT_CHUNK_SIZE):
    """Open a PyAudio input stream for the given device index.

    Args:
        pa: PyAudio instance
        dev_index: Device index to open.
        sample_rate: Sample rate in Hz.
        chunk_size: Frames per buffer.

    Returns:
        Open PyAudio stream.
    """
    return pa.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=sample_rate,
        input=True,
        input_device_index=dev_index,
        frames_per_buffer=chunk_size,
    )


def _log(msg):
    """Minimal log helper — avoids circular import with vox.main."""
    import time
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)
