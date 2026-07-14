"""
Private CoreAudio ctypes bindings for heyvox.

Shared by mic.py (hotplug/alive detection) and bt.py (transport-type checks).
Not a public API — import from mic or bt instead.
"""
import ctypes
import ctypes.util
import time


def _log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


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
_kAudioHardwarePropertyDefaultInputDevice = _fourcc("dIn ")
_kAudioObjectPropertyScopeGlobal = _fourcc("glob")
_kAudioObjectPropertyScopeInput = _fourcc("inpt")
_kAudioObjectPropertyElementMain = 0
_kAudioObjectPropertyName = _fourcc("lnam")
_kAudioDevicePropertyDeviceIsAlive = _fourcc("livn")
_kAudioDevicePropertyStreams = _fourcc("stm#")
_kAudioDevicePropertyTransportType = _fourcc("tran")
_kAudioDeviceTransportTypeBluetooth = _fourcc("blue")
_kAudioDeviceTransportTypeBluetoothLE = _fourcc("blea")
_kAudioDeviceTransportTypeUSB = _fourcc("usb ")
_kAudioDeviceTransportTypeBuiltIn = _fourcc("bltn")
_kCFStringEncodingUTF8 = 0x08000100


def _enumerate_coreaudio_inputs() -> list[tuple[str, bool, int]]:
    """Return ``[(device_name, is_alive, transport)]`` for every CoreAudio
    device that has input streams. ``transport`` is the CoreAudio transport-type
    four-char-code (e.g. 'blue' = Bluetooth) used to exclude BT from DEF-104.

    Hits the **live** CoreAudio HAL directly via ctypes, bypassing PortAudio's
    per-process device cache. That cache is the root of DEF-104: a device
    hotplugged after the daemon's first PortAudio init is invisible to every
    PortAudio code path until the process restarts — but it shows up here
    immediately.

    Returns ``[]`` if CoreAudio is unavailable (graceful degradation).
    """
    try:
        ca_path = ctypes.util.find_library("CoreAudio")
        cf_path = ctypes.util.find_library("CoreFoundation")
        if not ca_path or not cf_path:
            return []

        ca = ctypes.cdll.LoadLibrary(ca_path)
        cf = ctypes.cdll.LoadLibrary(cf_path)

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
            return []

        buf = (ctypes.c_char * size.value)()
        io_size = ctypes.c_uint32(size.value)
        status = ca.AudioObjectGetPropertyData(
            ctypes.c_uint32(_kAudioObjectSystemObject), ctypes.byref(addr),
            ctypes.c_uint32(0), None, ctypes.byref(io_size), buf,
        )
        if status != 0:
            return []

        device_count = io_size.value // 4
        device_ids = [
            int.from_bytes(bytes(buf)[i * 4:(i + 1) * 4], byteorder="little")
            for i in range(device_count)
        ]

        results: list[tuple[str, bool, int]] = []
        for did in device_ids:
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
                continue

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
            is_alive = alive_val.value != 0

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
                    transport = 0
                    trans_addr = _AudioObjectPropertyAddress(
                        _kAudioDevicePropertyTransportType,
                        _kAudioObjectPropertyScopeGlobal,
                        _kAudioObjectPropertyElementMain,
                    )
                    trans_val = ctypes.c_uint32(0)
                    trans_size = ctypes.c_uint32(4)
                    if ca.AudioObjectGetPropertyData(
                        ctypes.c_uint32(did), ctypes.byref(trans_addr),
                        ctypes.c_uint32(0), None, ctypes.byref(trans_size),
                        ctypes.byref(trans_val),
                    ) == 0:
                        transport = trans_val.value
                    results.append((name, is_alive, transport))

        return results
    except Exception as e:
        _log(f"  CoreAudio enumeration failed: {e}")
        return []
