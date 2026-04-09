"""
Microphone management for heyvox.

Handles device discovery, priority-based selection, and stream lifecycle.
Supports USB dongles and non-default audio devices (AUDIO-01).
"""

import numpy as np
import pyaudio

from heyvox.constants import DEFAULT_SAMPLE_RATE, DEFAULT_CHUNK_SIZE


def find_best_mic(pa: pyaudio.PyAudio, mic_priority: list[str] | None = None, sample_rate: int = DEFAULT_SAMPLE_RATE, chunk_size: int = DEFAULT_CHUNK_SIZE, require_audio: bool = False) -> int | None:
    """Find the best working microphone based on priority list.

    Tests each candidate device by actually reading audio frames and checking for
    non-zero levels. Falls back to system default if all devices fail.

    Args:
        pa: PyAudio instance
        mic_priority: List of device name substrings in priority order.
            First matching working device wins.
        sample_rate: Sample rate to test with (Hz).
        chunk_size: Frames per buffer for the test stream.
        require_audio: If True, reject devices producing zero audio.
            Used during dead-mic recovery to avoid re-selecting a silent device.

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
        test_stream = None
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
            _log(f"  [{index}] {name}: max_level={max_level}")
            return max_level > 0
        except Exception as e:
            _log(f"  [{index}] {name}: error - {e}")
            return False
        finally:
            if test_stream is not None:
                try:
                    test_stream.close()
                except Exception:
                    pass

    for rank, prio_name in enumerate(mic_priority):
        for index, dev_name in devices_by_priority[prio_name]:
            _log(f"Testing {dev_name}...")
            if test_mic(index, dev_name):
                return index
            # First-priority device: accept even at zero level if stream opened OK.
            # This supports virtual devices (BlackHole) that have no ambient audio.
            # Skip this fallback during dead-mic recovery (require_audio=True).
            if rank == 0 and not require_audio:
                try:
                    s = pa.open(format=pyaudio.paInt16, channels=1, rate=sample_rate,
                                input=True, input_device_index=index, frames_per_buffer=chunk_size)
                    s.close()
                    _log(f"  [{index}] {dev_name}: no audio but stream OK (first priority), accepting")
                    return index
                except Exception:
                    pass

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


def open_mic_stream(pa: pyaudio.PyAudio, dev_index: int, sample_rate: int = DEFAULT_SAMPLE_RATE, chunk_size: int = DEFAULT_CHUNK_SIZE) -> pyaudio.Stream:
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


def detect_headset(pa, selected_input_index: int) -> bool:
    """Detect whether the selected microphone is part of a headset.

    Checks if there is an output device whose name partially overlaps with the
    selected input device's name. Uses case-insensitive substring matching in
    both directions to handle Bluetooth/USB name variations such as
    "G435 Wireless" (input) vs "G435 Bluetooth" (output).

    Returns True when a paired output is found — meaning we are in headset
    mode and echo suppression can be disabled (headphones prevent feedback).
    Returns False when only speaker-only output is available, meaning echo
    suppression should be active to avoid TTS being picked up by the mic.

    Requirement: AUDIO-10

    Args:
        pa: PyAudio instance.
        selected_input_index: Device index of the chosen microphone.

    Returns:
        True if a matching output device (headset) is found, False otherwise.
    """
    try:
        input_info = pa.get_device_info_by_index(selected_input_index)
        selected_name = input_info['name'].lower()
    except Exception:
        return False

    for i in range(pa.get_device_count()):
        try:
            d = pa.get_device_info_by_index(i)
        except Exception:
            continue
        if d['maxOutputChannels'] <= 0:
            continue
        out_name = d['name'].lower()
        if selected_name in out_name or out_name in selected_name:
            return True

    return False


def _log(msg: str) -> None:
    """Minimal log helper — avoids circular import with heyvox.main."""
    import time
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)
