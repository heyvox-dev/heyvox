"""macOS audio output device management via CoreAudio ctypes.

Provides listing, default get/set for system output devices.
Used by the HUD overlay to let users select their speaker/headphone output.
Since Herald's afplay uses the system default output, switching the default
device here routes all TTS to the selected device.
"""

import ctypes
import ctypes.util
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CoreAudio framework via ctypes
# ---------------------------------------------------------------------------

_coreaudio = None
_cf = None


def _load_frameworks():
    global _coreaudio, _cf
    if _coreaudio is not None:
        return True
    ca_path = ctypes.util.find_library("CoreAudio")
    cf_path = ctypes.util.find_library("CoreFoundation")
    if not ca_path or not cf_path:
        log.warning("CoreAudio or CoreFoundation not found")
        return False
    _coreaudio = ctypes.cdll.LoadLibrary(ca_path)
    _cf = ctypes.cdll.LoadLibrary(cf_path)

    # CFString helpers
    _cf.CFStringGetLength.restype = ctypes.c_long
    _cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
    _cf.CFStringGetCStringPtr.restype = ctypes.c_char_p
    _cf.CFStringGetCStringPtr.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    _cf.CFStringGetCString.restype = ctypes.c_bool
    _cf.CFStringGetCString.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32,
    ]
    _cf.CFRelease.argtypes = [ctypes.c_void_p]
    return True


class _AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = [
        ("mSelector", ctypes.c_uint32),
        ("mScope", ctypes.c_uint32),
        ("mElement", ctypes.c_uint32),
    ]


# CoreAudio property selectors (FourCC → uint32)
def _fourcc(s: str) -> int:
    return int.from_bytes(s.encode("ascii"), byteorder="big")


_kAudioObjectSystemObject = 1
_kAudioHardwarePropertyDevices = _fourcc("dev#")
# macOS ≤15 uses 'dout', macOS 26+ uses 'dOut' — detected at runtime
_kAudioHardwarePropertyDefaultOutputDevice_legacy = _fourcc("dout")
_kAudioHardwarePropertyDefaultOutputDevice_new = _fourcc("dOut")
_kAudioObjectPropertyScopeGlobal = _fourcc("glob")
_kAudioObjectPropertyScopeOutput = _fourcc("outp")
_kAudioObjectPropertyElementMain = 0
_kAudioObjectPropertyName = _fourcc("lnam")
_kAudioDevicePropertyStreams = _fourcc("stm#")

_default_output_selector: int | None = None

_kCFStringEncodingUTF8 = 0x08000100


def _cfstring_to_str(cfstr) -> str:
    """Convert a CFStringRef to a Python string."""
    if not cfstr:
        return ""
    # Fast path: direct C string pointer
    ptr = _cf.CFStringGetCStringPtr(cfstr, _kCFStringEncodingUTF8)
    if ptr:
        return ptr.decode("utf-8")
    # Slow path: copy to buffer
    length = _cf.CFStringGetLength(cfstr) * 4 + 1
    buf = ctypes.create_string_buffer(length)
    if _cf.CFStringGetCString(cfstr, buf, length, _kCFStringEncodingUTF8):
        return buf.value.decode("utf-8")
    return ""


def _get_property_data(object_id: int, selector: int, scope: int, element: int = 0):
    """Get raw property data bytes from a CoreAudio object."""
    addr = _AudioObjectPropertyAddress(selector, scope, element)
    size = ctypes.c_uint32(0)
    status = _coreaudio.AudioObjectGetPropertyDataSize(
        ctypes.c_uint32(object_id), ctypes.byref(addr),
        ctypes.c_uint32(0), None, ctypes.byref(size),
    )
    if status != 0 or size.value == 0:
        return None
    buf = (ctypes.c_char * size.value)()
    io_size = ctypes.c_uint32(size.value)
    status = _coreaudio.AudioObjectGetPropertyData(
        ctypes.c_uint32(object_id), ctypes.byref(addr),
        ctypes.c_uint32(0), None, ctypes.byref(io_size), buf,
    )
    if status != 0:
        return None
    return bytes(buf)[:io_size.value]


def _get_device_name(device_id: int) -> str:
    """Get the name of a CoreAudio device. Returns CFString → Python str."""
    addr = _AudioObjectPropertyAddress(
        _kAudioObjectPropertyName,
        _kAudioObjectPropertyScopeGlobal,
        _kAudioObjectPropertyElementMain,
    )
    cfstr = ctypes.c_void_p(0)
    size = ctypes.c_uint32(ctypes.sizeof(cfstr))
    status = _coreaudio.AudioObjectGetPropertyData(
        ctypes.c_uint32(device_id), ctypes.byref(addr),
        ctypes.c_uint32(0), None, ctypes.byref(size), ctypes.byref(cfstr),
    )
    if status != 0 or not cfstr.value:
        return f"Device {device_id}"
    name = _cfstring_to_str(cfstr.value)
    _cf.CFRelease(cfstr)
    return name


def _resolve_default_output_selector() -> int | None:
    """Detect which property selector works for default output device.

    macOS 26 (Tahoe) renamed 'dout' → 'dOut'. We probe both and cache the result.
    """
    global _default_output_selector
    if _default_output_selector is not None:
        return _default_output_selector

    _coreaudio.AudioObjectHasProperty.restype = ctypes.c_bool
    _coreaudio.AudioObjectHasProperty.argtypes = [
        ctypes.c_uint32, ctypes.POINTER(_AudioObjectPropertyAddress),
    ]
    for sel in (_kAudioHardwarePropertyDefaultOutputDevice_new,
                _kAudioHardwarePropertyDefaultOutputDevice_legacy):
        addr = _AudioObjectPropertyAddress(sel, _kAudioObjectPropertyScopeGlobal, 0)
        if _coreaudio.AudioObjectHasProperty(
            ctypes.c_uint32(_kAudioObjectSystemObject), ctypes.byref(addr),
        ):
            _default_output_selector = sel
            return sel
    return None


def _has_output_streams(device_id: int) -> bool:
    """Check if a device has any output streams."""
    data = _get_property_data(
        device_id, _kAudioDevicePropertyStreams,
        _kAudioObjectPropertyScopeOutput,
    )
    return data is not None and len(data) > 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass
class OutputDevice:
    device_id: int  # CoreAudio AudioDeviceID
    name: str
    is_default: bool = False


def list_output_devices() -> list[OutputDevice]:
    """List all available audio output devices with their CoreAudio IDs."""
    if not _load_frameworks():
        return []

    data = _get_property_data(
        _kAudioObjectSystemObject, _kAudioHardwarePropertyDevices,
        _kAudioObjectPropertyScopeGlobal,
    )
    if not data:
        return []

    # Parse device IDs (array of UInt32)
    count = len(data) // 4
    device_ids = [
        int.from_bytes(data[i * 4 : (i + 1) * 4], byteorder="little")
        for i in range(count)
    ]

    default_id = get_default_output_id()

    devices = []
    for did in device_ids:
        if _has_output_streams(did):
            name = _get_device_name(did)
            devices.append(OutputDevice(
                device_id=did,
                name=name,
                is_default=(did == default_id),
            ))
    return devices


def get_default_output_id() -> int | None:
    """Get the CoreAudio device ID of the current default output."""
    if not _load_frameworks():
        return None

    selector = _resolve_default_output_selector()
    if selector is None:
        return None

    addr = _AudioObjectPropertyAddress(
        selector, _kAudioObjectPropertyScopeGlobal, _kAudioObjectPropertyElementMain,
    )
    device_id = ctypes.c_uint32(0)
    size = ctypes.c_uint32(ctypes.sizeof(device_id))
    status = _coreaudio.AudioObjectGetPropertyData(
        ctypes.c_uint32(_kAudioObjectSystemObject), ctypes.byref(addr),
        ctypes.c_uint32(0), None, ctypes.byref(size), ctypes.byref(device_id),
    )
    if status != 0:
        return None
    return device_id.value


def set_default_output_device(device_id: int) -> bool:
    """Set the system default output device. Returns True on success."""
    if not _load_frameworks():
        return False

    selector = _resolve_default_output_selector()
    if selector is None:
        log.warning("No default output device property available")
        return False

    addr = _AudioObjectPropertyAddress(
        selector, _kAudioObjectPropertyScopeGlobal, _kAudioObjectPropertyElementMain,
    )
    did = ctypes.c_uint32(device_id)
    status = _coreaudio.AudioObjectSetPropertyData(
        ctypes.c_uint32(_kAudioObjectSystemObject), ctypes.byref(addr),
        ctypes.c_uint32(0), None,
        ctypes.c_uint32(ctypes.sizeof(did)), ctypes.byref(did),
    )
    if status != 0:
        log.warning(f"Failed to set output device {device_id}: status={status}")
        return False
    return True


def friendly_output_name(name: str) -> str:
    """Shorten device name for menu display."""
    if not name:
        return "Unknown"
    n = name
    if "macbook" in n.lower() and "speakers" in n.lower():
        return "Built-in Speakers"
    for suffix in [" Gaming Headset", " Wireless Gaming Headset",
                   " USB Audio", " Audio Device", " Output"]:
        if n.endswith(suffix):
            n = n[: -len(suffix)]
            break
    return n.strip()
