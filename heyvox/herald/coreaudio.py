"""CoreAudio volume control via ctypes for Herald.

Provides read/write access to macOS system output volume without spawning
osascript subprocesses. Uses the CoreAudio C API via ctypes.

Key APIs used:
  AudioObjectGetPropertyData / AudioObjectSetPropertyData
  kAudioHardwarePropertyDefaultOutputDevice (0x6465765f)
  kAudioDevicePropertyVolumeScalar (0x766f6c6d) per channel
  kAudioDevicePropertyMute (0x6d757465)

All functions fall back to osascript on CoreAudio failure to ensure
the Herald orchestrator never stops because of a volume API change.

Requirements: HERALD-03 (CoreAudio ctypes for system volume)
"""

import ctypes
import ctypes.util
import logging
import subprocess
import threading
import time

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CoreAudio constants
# ---------------------------------------------------------------------------

kAudioObjectSystemObject: int = 1
kAudioObjectPropertyScopeGlobal: int = 0x676C6F62   # 'glob'
kAudioObjectPropertyScopeOutput: int = 0x6F757470   # 'outp'
kAudioObjectPropertyElementMain: int = 0             # kAudioObjectPropertyElementMaster

# Property selectors
kAudioHardwarePropertyDefaultOutputDevice: int = 0x6465765F  # 'dev_' -> 'dOut' / 'dOut'
kAudioDevicePropertyVolumeScalar: int = 0x766F6C6D           # 'volm'
kAudioDevicePropertyMute: int = 0x6D757465                   # 'mute'

# ---------------------------------------------------------------------------
# CoreAudio structs
# ---------------------------------------------------------------------------


class AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = [
        ("mSelector", ctypes.c_uint32),
        ("mScope", ctypes.c_uint32),
        ("mElement", ctypes.c_uint32),
    ]


# ---------------------------------------------------------------------------
# Load CoreAudio
# ---------------------------------------------------------------------------

_ca: ctypes.CDLL | None = None
_ca_loaded: bool | None = None  # None = not tried yet
_ca_lock = threading.Lock()


def _load_coreaudio() -> ctypes.CDLL | None:
    """Load the CoreAudio framework (lazy, cached, thread-safe)."""
    global _ca, _ca_loaded
    with _ca_lock:
        if _ca_loaded is not None:
            return _ca
        try:
            _ca = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
            )
            # Declare signatures for the functions we need
            addr_type = ctypes.POINTER(AudioObjectPropertyAddress)
            _ca.AudioObjectGetPropertyData.argtypes = [
                ctypes.c_uint32,  # inObjectID
                addr_type,        # inAddress
                ctypes.c_uint32,  # inQualifierDataSize
                ctypes.c_void_p,  # inQualifierData
                ctypes.POINTER(ctypes.c_uint32),  # ioDataSize
                ctypes.c_void_p,  # outData
            ]
            _ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
            _ca.AudioObjectSetPropertyData.argtypes = [
                ctypes.c_uint32,
                addr_type,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_uint32,  # inDataSize
                ctypes.c_void_p,  # inData
            ]
            _ca.AudioObjectSetPropertyData.restype = ctypes.c_int32
            _ca_loaded = True
            log.debug("CoreAudio loaded")
        except Exception as e:
            log.warning("CoreAudio load failed: %s — will fall back to osascript", e)
            _ca = None
            _ca_loaded = False
        return _ca


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_default_output_device() -> int | None:
    """Return the AudioObjectID of the default output device, or None."""
    ca = _load_coreaudio()
    if ca is None:
        return None
    addr = AudioObjectPropertyAddress(
        kAudioHardwarePropertyDefaultOutputDevice,
        kAudioObjectPropertyScopeGlobal,
        kAudioObjectPropertyElementMain,
    )
    device_id = ctypes.c_uint32(0)
    data_size = ctypes.c_uint32(ctypes.sizeof(device_id))
    err = ca.AudioObjectGetPropertyData(
        kAudioObjectSystemObject,
        ctypes.byref(addr),
        0, None,
        ctypes.byref(data_size),
        ctypes.byref(device_id),
    )
    if err != 0:
        log.debug("AudioObjectGetPropertyData(default device) err=%d", err)
        return None
    return int(device_id.value)


def _get_volume_coreaudio(device_id: int) -> float | None:
    """Read master output volume scalar [0.0, 1.0] via CoreAudio."""
    ca = _load_coreaudio()
    if ca is None:
        return None

    # Try channel 0 (master), then channel 1 (left) as fallback
    for channel in (0, 1):
        addr = AudioObjectPropertyAddress(
            kAudioDevicePropertyVolumeScalar,
            kAudioObjectPropertyScopeOutput,
            channel,
        )
        vol = ctypes.c_float(0.0)
        data_size = ctypes.c_uint32(ctypes.sizeof(vol))
        err = ca.AudioObjectGetPropertyData(
            device_id,
            ctypes.byref(addr),
            0, None,
            ctypes.byref(data_size),
            ctypes.byref(vol),
        )
        if err == 0:
            return float(vol.value)
    return None


def _set_volume_coreaudio(device_id: int, volume: float) -> bool:
    """Set master output volume scalar [0.0, 1.0] via CoreAudio."""
    ca = _load_coreaudio()
    if ca is None:
        return False

    volume = max(0.0, min(1.0, volume))
    vol = ctypes.c_float(volume)
    data_size = ctypes.c_uint32(ctypes.sizeof(vol))

    # Set channel 0 (master), then channels 1 and 2 (stereo)
    success = False
    for channel in (0, 1, 2):
        addr = AudioObjectPropertyAddress(
            kAudioDevicePropertyVolumeScalar,
            kAudioObjectPropertyScopeOutput,
            channel,
        )
        err = ca.AudioObjectSetPropertyData(
            device_id,
            ctypes.byref(addr),
            0, None,
            data_size,
            ctypes.byref(vol),
        )
        if err == 0:
            success = True
    return success


def _get_mute_coreaudio(device_id: int) -> bool | None:
    """Read mute state via CoreAudio. Returns True if muted, False if not, None on error."""
    ca = _load_coreaudio()
    if ca is None:
        return None
    for channel in (0, 1):
        addr = AudioObjectPropertyAddress(
            kAudioDevicePropertyMute,
            kAudioObjectPropertyScopeOutput,
            channel,
        )
        muted = ctypes.c_uint32(0)
        data_size = ctypes.c_uint32(ctypes.sizeof(muted))
        err = ca.AudioObjectGetPropertyData(
            device_id,
            ctypes.byref(addr),
            0, None,
            ctypes.byref(data_size),
            ctypes.byref(muted),
        )
        if err == 0:
            return bool(muted.value)
    return None


# ---------------------------------------------------------------------------
# osascript fallbacks
# ---------------------------------------------------------------------------


def _get_volume_osascript() -> float:
    """Read system volume via osascript (0-100 scale → 0.0–1.0)."""
    try:
        result = subprocess.run(
            ["osascript", "-e", "output volume of (get volume settings)"],
            capture_output=True, text=True, timeout=3.0,
        )
        val = result.stdout.strip()
        if val:
            return float(val) / 100.0
    except Exception as e:
        log.debug("osascript volume read failed: %s", e)
    return 0.5  # safe default


def _set_volume_osascript(volume: float) -> None:
    """Set system volume via osascript (0.0–1.0 → 0–100 scale)."""
    vol_int = int(round(volume * 100))
    try:
        subprocess.run(
            ["osascript", "-e", f"set volume output volume {vol_int}"],
            capture_output=True, timeout=3.0,
        )
    except Exception as e:
        log.debug("osascript volume set failed: %s", e)


def _is_muted_osascript() -> bool:
    """Check system mute via osascript."""
    try:
        result = subprocess.run(
            ["osascript", "-e", "output muted of (get volume settings)"],
            capture_output=True, text=True, timeout=3.0,
        )
        return result.stdout.strip() == "true"
    except Exception as e:
        log.debug("osascript mute check failed: %s", e)
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_system_volume() -> float:
    """Read macOS system output volume as a scalar in [0.0, 1.0].

    Uses CoreAudio ctypes for efficiency. Falls back to osascript on error.

    Returns:
        Volume in [0.0, 1.0]. Returns 0.5 as safe default on complete failure.

    Requirement: HERALD-03
    """
    device_id = _get_default_output_device()
    if device_id is not None:
        vol = _get_volume_coreaudio(device_id)
        if vol is not None:
            return vol
    # Fallback
    return _get_volume_osascript()


def set_system_volume(volume: float) -> None:
    """Set macOS system output volume from a scalar in [0.0, 1.0].

    Uses CoreAudio ctypes for efficiency. Falls back to osascript on error.

    Args:
        volume: Target volume in [0.0, 1.0]. Clamped if out of range.

    Requirement: HERALD-03
    """
    volume = max(0.0, min(1.0, volume))
    device_id = _get_default_output_device()
    if device_id is not None and _set_volume_coreaudio(device_id, volume):
        return
    # Fallback
    _set_volume_osascript(volume)


def is_system_muted() -> bool:
    """Return True if the system output is currently muted.

    Uses CoreAudio ctypes. Falls back to osascript on error.

    Requirement: HERALD-03
    """
    device_id = _get_default_output_device()
    if device_id is not None:
        muted = _get_mute_coreaudio(device_id)
        if muted is not None:
            return muted
    return _is_muted_osascript()


# ---------------------------------------------------------------------------
# Cached volume access (HERALD-04: read at most every 5 seconds)
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cached_volume: float | None = None
_cached_at: float = 0.0


def get_system_volume_cached(ttl: float = 5.0) -> float:
    """Return cached system volume, re-reading from CoreAudio only after TTL.

    This avoids polling CoreAudio on every 300ms orchestrator loop tick.
    The cache is automatically invalidated when set_system_volume() is called.

    Args:
        ttl: Cache lifetime in seconds. Default 5.0.

    Returns:
        Volume in [0.0, 1.0].

    Requirement: HERALD-04 (volume checked at most once per 5s)
    """
    global _cached_volume, _cached_at
    now = time.monotonic()
    with _cache_lock:
        if _cached_volume is not None and (now - _cached_at) < ttl:
            return _cached_volume
    # Read outside lock (slow operation)
    vol = get_system_volume()
    with _cache_lock:
        _cached_volume = vol
        _cached_at = now
    return vol


def _invalidate_volume_cache() -> None:
    """Invalidate the cached volume so the next read goes to CoreAudio."""
    global _cached_volume, _cached_at
    with _cache_lock:
        _cached_volume = None
        _cached_at = 0.0


def set_system_volume_cached(volume: float) -> None:
    """Set system volume and update the cache to the new value.

    Use this instead of set_system_volume() to keep the cache consistent.

    Args:
        volume: Target volume in [0.0, 1.0].
    """
    global _cached_volume, _cached_at
    volume = max(0.0, min(1.0, volume))
    set_system_volume(volume)
    with _cache_lock:
        _cached_volume = volume
        _cached_at = time.monotonic()
