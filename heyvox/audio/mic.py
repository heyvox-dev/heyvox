"""
Microphone management for heyvox.

Handles device discovery, priority-based selection, and stream lifecycle.
Supports USB dongles and non-default audio devices (AUDIO-01).
Uses CoreAudio to filter out paired-but-disconnected Bluetooth devices.
"""

import ctypes
import ctypes.util

import numpy as np
import pyaudio

from heyvox.constants import DEFAULT_SAMPLE_RATE, DEFAULT_CHUNK_SIZE

# Re-export for use by hotplug scan in main.py
__all__ = ["find_best_mic", "open_mic_stream", "detect_headset", "get_dead_input_device_names"]


# ---------------------------------------------------------------------------
# CoreAudio device-alive check (filters disconnected Bluetooth devices)
# ---------------------------------------------------------------------------

def _fourcc(s: str) -> int:
    return int.from_bytes(s.encode("ascii"), byteorder="big")


class _AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = [
        ("mSelector", ctypes.c_uint32),
        ("mScope", ctypes.c_uint32),
        ("mElement", ctypes.c_uint32),
    ]


_kAudioObjectSystemObject = 1
_kAudioHardwarePropertyDevices = _fourcc("dev#")
_kAudioObjectPropertyScopeGlobal = _fourcc("glob")
_kAudioObjectPropertyScopeInput = _fourcc("inpt")
_kAudioObjectPropertyElementMain = 0
_kAudioObjectPropertyName = _fourcc("lnam")
_kAudioDevicePropertyDeviceIsAlive = _fourcc("livn")
_kAudioDevicePropertyStreams = _fourcc("stm#")
_kCFStringEncodingUTF8 = 0x08000100


def _get_dead_input_device_names() -> set[str]:
    """Return names of CoreAudio input devices that are not alive.

    macOS keeps paired-but-disconnected Bluetooth devices in the audio device
    list. PyAudio still enumerates them and can even open streams that return
    low-level noise, causing find_best_mic to select a phantom device.

    This function queries CoreAudio's kAudioDevicePropertyDeviceIsAlive to
    identify dead devices so they can be skipped during mic selection.

    Returns an empty set if CoreAudio is unavailable (graceful degradation).
    """
    try:
        ca_path = ctypes.util.find_library("CoreAudio")
        cf_path = ctypes.util.find_library("CoreFoundation")
        if not ca_path or not cf_path:
            return set()

        ca = ctypes.cdll.LoadLibrary(ca_path)
        cf = ctypes.cdll.LoadLibrary(cf_path)

        # Setup CFString helpers
        cf.CFStringGetCStringPtr.restype = ctypes.c_char_p
        cf.CFStringGetCStringPtr.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        cf.CFStringGetLength.restype = ctypes.c_long
        cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringGetCString.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32,
        ]
        cf.CFRelease.argtypes = [ctypes.c_void_p]

        def cfstr_to_str(cfstr) -> str:
            if not cfstr:
                return ""
            ptr = cf.CFStringGetCStringPtr(cfstr, _kCFStringEncodingUTF8)
            if ptr:
                return ptr.decode("utf-8")
            length = cf.CFStringGetLength(cfstr) * 4 + 1
            buf = ctypes.create_string_buffer(length)
            if cf.CFStringGetCString(cfstr, buf, length, _kCFStringEncodingUTF8):
                return buf.value.decode("utf-8")
            return ""

        # Get all device IDs
        addr = _AudioObjectPropertyAddress(
            _kAudioHardwarePropertyDevices,
            _kAudioObjectPropertyScopeGlobal,
            _kAudioObjectPropertyElementMain,
        )
        size = ctypes.c_uint32(0)
        status = ca.AudioObjectGetPropertyDataSize(
            ctypes.c_uint32(_kAudioObjectSystemObject), ctypes.byref(addr),
            ctypes.c_uint32(0), None, ctypes.byref(size),
        )
        if status != 0 or size.value == 0:
            return set()

        buf = (ctypes.c_char * size.value)()
        io_size = ctypes.c_uint32(size.value)
        status = ca.AudioObjectGetPropertyData(
            ctypes.c_uint32(_kAudioObjectSystemObject), ctypes.byref(addr),
            ctypes.c_uint32(0), None, ctypes.byref(io_size), buf,
        )
        if status != 0:
            return set()

        device_count = io_size.value // 4
        device_ids = [
            int.from_bytes(bytes(buf)[i * 4:(i + 1) * 4], byteorder="little")
            for i in range(device_count)
        ]

        dead_names = set()
        for did in device_ids:
            # Check if device has input streams
            stream_addr = _AudioObjectPropertyAddress(
                _kAudioDevicePropertyStreams,
                _kAudioObjectPropertyScopeInput,
                _kAudioObjectPropertyElementMain,
            )
            stream_size = ctypes.c_uint32(0)
            status = ca.AudioObjectGetPropertyDataSize(
                ctypes.c_uint32(did), ctypes.byref(stream_addr),
                ctypes.c_uint32(0), None, ctypes.byref(stream_size),
            )
            if status != 0 or stream_size.value == 0:
                continue  # Not an input device

            # Check DeviceIsAlive
            alive_addr = _AudioObjectPropertyAddress(
                _kAudioDevicePropertyDeviceIsAlive,
                _kAudioObjectPropertyScopeGlobal,
                _kAudioObjectPropertyElementMain,
            )
            alive_val = ctypes.c_uint32(0)
            alive_size = ctypes.c_uint32(4)
            status = ca.AudioObjectGetPropertyData(
                ctypes.c_uint32(did), ctypes.byref(alive_addr),
                ctypes.c_uint32(0), None, ctypes.byref(alive_size),
                ctypes.byref(alive_val),
            )
            if status != 0:
                continue

            if alive_val.value == 0:
                # Device is dead — get its name
                name_addr = _AudioObjectPropertyAddress(
                    _kAudioObjectPropertyName,
                    _kAudioObjectPropertyScopeGlobal,
                    _kAudioObjectPropertyElementMain,
                )
                cfstr = ctypes.c_void_p(0)
                name_size = ctypes.c_uint32(ctypes.sizeof(cfstr))
                status = ca.AudioObjectGetPropertyData(
                    ctypes.c_uint32(did), ctypes.byref(name_addr),
                    ctypes.c_uint32(0), None, ctypes.byref(name_size),
                    ctypes.byref(cfstr),
                )
                if status == 0 and cfstr.value:
                    name = cfstr_to_str(cfstr.value)
                    cf.CFRelease(cfstr)
                    if name:
                        dead_names.add(name.lower())
                        _log(f"  CoreAudio: '{name}' is not alive (disconnected)")

        return dead_names
    except Exception as e:
        _log(f"  CoreAudio alive check failed: {e}")
        return set()


def get_dead_input_device_names() -> set[str]:
    """Public wrapper for _get_dead_input_device_names.

    Used by the hotplug scan in main.py to filter the device list before
    checking for higher-priority devices.
    """
    return _get_dead_input_device_names()


def find_best_mic(pa: pyaudio.PyAudio, mic_priority: list[str] | None = None, sample_rate: int = DEFAULT_SAMPLE_RATE, chunk_size: int = DEFAULT_CHUNK_SIZE, require_audio: bool = False) -> int | None:
    """Find the best working microphone based on priority list.

    Tests each candidate device by actually reading audio frames and checking for
    non-zero levels. Filters out disconnected Bluetooth devices via CoreAudio's
    DeviceIsAlive property. Falls back to system default if all devices fail.

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

    # Filter out disconnected Bluetooth devices (macOS keeps them in the list)
    dead_names = _get_dead_input_device_names()

    devices_by_priority = {name: [] for name in mic_priority}
    other_devices = []

    for i in range(pa.get_device_count()):
        d = pa.get_device_info_by_index(i)
        if d['maxInputChannels'] <= 0:
            continue
        # Skip devices that CoreAudio reports as not alive
        if d['name'].lower() in dead_names:
            _log(f"  [{i}] {d['name']}: skipped (not alive per CoreAudio)")
            continue
        matched = False
        for prio_name in mic_priority:
            if prio_name.lower() in d['name'].lower():
                devices_by_priority[prio_name].append((i, d['name']))
                matched = True
                break
        if not matched:
            other_devices.append((i, d['name']))

    # Minimum audio level to consider a device producing real audio.
    # Disconnected Bluetooth devices produce quantization noise at level 1-5.
    # A real connected mic in a quiet room produces ambient noise above 10.
    # Matches the silent-mic health check threshold in main.py.
    MIN_AUDIO_LEVEL = 10

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
            return max_level >= MIN_AUDIO_LEVEL
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
