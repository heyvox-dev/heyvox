"""
Microphone management for heyvox.

Handles device discovery, priority-based selection, and stream lifecycle.
Supports USB dongles and non-default audio devices (AUDIO-01).
Uses CoreAudio to filter out paired-but-disconnected Bluetooth devices.
"""

import ctypes
import ctypes.util
import hashlib
import threading
import time
from typing import Callable

import numpy as np
import pyaudio

from heyvox.constants import DEFAULT_SAMPLE_RATE, DEFAULT_CHUNK_SIZE, is_excluded_device_name
from heyvox.audio._coreaudio import (
    _AudioObjectPropertyAddress,
    _enumerate_coreaudio_inputs,
    _kAudioDevicePropertyStreams,
    _kAudioDeviceTransportTypeUSB,
    _kAudioHardwarePropertyDefaultInputDevice,
    _kAudioHardwarePropertyDevices,
    _kAudioObjectPropertyElementMain,
    _kAudioObjectPropertyName,
    _kAudioObjectPropertyScopeGlobal,
    _kAudioObjectPropertyScopeInput,
    _kAudioObjectSystemObject,
    _kCFStringEncodingUTF8,
)

__all__ = [
    "find_best_mic", "open_mic_stream", "detect_headset",
    "get_dead_input_device_names", "get_live_input_device_names",
    "get_default_input_device_name", "force_os_default_input",
    "clear_device_cooldowns", "clear_device_cooldown",
    "add_device_cooldown", "is_device_cooled_down", "is_builtin_mic",
    "start_pa_hotplug_watcher", "stop_pa_hotplug_watcher",
    "get_device_transport", "is_usb_transport", "probe_device_level",
    "record_pa_open_failure", "record_pa_open_success", "pa_storm_detected",
    "MIN_AUDIO_LEVEL",
]


def _log(msg: str) -> None:
    """Minimal log helper — avoids circular import with heyvox.main."""
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# Built-in mic name substrings — these devices are physically always present
# and should never be put in cooldown or rejected for low audio levels.
_BUILTIN_MIC_NAMES = ["macbook pro microphone", "macbook air microphone", "built-in microphone"]

def is_builtin_mic(device_name: str) -> bool:
    """Check if a device is a built-in microphone (always assumed working)."""
    name_lower = device_name.lower()
    return any(b in name_lower for b in _BUILTIN_MIC_NAMES)


# ---------------------------------------------------------------------------
# Transport classification (DEF-208)
# ---------------------------------------------------------------------------
#
# CoreAudio reports a transport type per device ('usb ', 'blue'/'blea', 'bltn',
# 'virt', ...). The escalating cooldown below was designed for genuinely-flaky
# Bluetooth links; applying it to a stable USB (Lightspeed dongle) transport
# demotes a fundamentally-healthy headset to the built-in mic for up to 30min
# on a transient blip. Selection policy therefore needs to know the transport.
#
# The map is cached briefly: cooldown decisions happen inside find_best_mic
# loops which may probe several devices per pass, and the HAL enumeration
# (~ms via ctypes) shouldn't run per device.

_transport_cache: dict[str, int] = {}
_transport_cache_ts: float = 0.0
_TRANSPORT_CACHE_TTL = 30.0


def get_device_transport(device_name: str) -> int:
    """Return the CoreAudio transport four-char-code for an input device.

    0 when the device is unknown to the live HAL (unplugged) or CoreAudio is
    unavailable — callers treat 0 as "not USB", i.e. the conservative BT-era
    policy applies.
    """
    global _transport_cache, _transport_cache_ts
    now = time.monotonic()
    if now - _transport_cache_ts > _TRANSPORT_CACHE_TTL:
        try:
            # DEF-217: MERGE instead of replace. A device that momentarily
            # drops out of the CoreAudio enumeration (a USB blip — the exact
            # moment cooldown decisions run) must keep its last-known
            # transport instead of degrading to "unknown" and landing in the
            # BT-era 30min tiers. Fresh values always win on conflict (a name
            # that re-appears on a different transport — same headset via BT
            # vs dongle — updates immediately); only ABSENCE is sticky.
            _transport_cache.update({
                name.lower(): transport
                for name, _alive, transport in _enumerate_coreaudio_inputs()
            })
        except Exception:
            pass  # DEF-217: keep the last-known map on enumeration failure
        _transport_cache_ts = now
    return _transport_cache.get(device_name.lower(), 0)


def is_usb_transport(device_name: str) -> bool:
    """True when the device sits on a USB transport (incl. Lightspeed dongles)."""
    return get_device_transport(device_name) == _kAudioDeviceTransportTypeUSB


# ---------------------------------------------------------------------------
# PortAudio-corruption storm detection (DEF-209)
# ---------------------------------------------------------------------------
#
# -9986 (paInternalError) on Pa_OpenStream can mean the process-wide PortAudio
# context is corrupted. Observed 2026-07-10: EVERY device failed with -9986 and
# in-process recovery (terminate + fresh PyAudio()) did NOT clear it — only a
# full process restart did. These hooks let every open path report its outcome;
# main.py restarts the process when a storm is detected.
#
# Storm = at least _PA_STORM_MIN_FAILURES -9986 failures across at least
# _PA_STORM_MIN_DEVICES distinct devices within _PA_STORM_WINDOW_SECS, with no
# successful open in between (any success clears the tally). The multi-device
# requirement separates context corruption from a single flaky device — the
# latter is what cooldowns are for, not a restart.

_PA_STORM_ERRNO = -9986
_PA_STORM_WINDOW_SECS = 120.0
_PA_STORM_MIN_FAILURES = 6
_PA_STORM_MIN_DEVICES = 2

_pa_storm_lock = threading.Lock()
_pa_storm_events: list[tuple[float, str]] = []  # (monotonic ts, device key)


def _is_pa_internal_error(exc: BaseException) -> bool:
    if getattr(exc, "errno", None) == _PA_STORM_ERRNO:
        return True
    return str(_PA_STORM_ERRNO) in str(exc)


def record_pa_open_failure(device_name: str, exc: BaseException) -> None:
    """Report a failed stream open. Only -9986 counts toward the storm."""
    if not _is_pa_internal_error(exc):
        return
    now = time.monotonic()
    with _pa_storm_lock:
        _pa_storm_events.append((now, device_name.lower()))
        cutoff = now - _PA_STORM_WINDOW_SECS
        while _pa_storm_events and _pa_storm_events[0][0] < cutoff:
            _pa_storm_events.pop(0)


def record_pa_open_success() -> None:
    """Report a successful stream open — clears the storm tally."""
    with _pa_storm_lock:
        _pa_storm_events.clear()


def pa_storm_detected() -> bool:
    """True when the -9986 failure pattern indicates PA context corruption."""
    cutoff = time.monotonic() - _PA_STORM_WINDOW_SECS
    with _pa_storm_lock:
        recent = [(t, d) for t, d in _pa_storm_events if t >= cutoff]
        if len(recent) < _PA_STORM_MIN_FAILURES:
            return False
        return len({d for _t, d in recent}) >= _PA_STORM_MIN_DEVICES


def clear_pa_storm() -> None:
    """Reset the storm tally (used after a suppressed restart to stop re-firing)."""
    record_pa_open_success()


# ---------------------------------------------------------------------------
# Device cooldown — prevents re-selecting a dead Bluetooth device every cycle
# ---------------------------------------------------------------------------

# Maps lowercase device name → timestamp of last failure (time.time()).
_device_cooldowns: dict[str, float] = {}

# Maps lowercase device name → consecutive failure count (for adaptive cooldown).
_device_failure_counts: dict[str, int] = {}

# Adaptive cooldown tiers (seconds) — indexed by failure count (0-based, capped).
# BT links legitimately fail-and-fail-again, so escalation up to 30min is the
# right call there. A USB transport is stable by construction: a failure is a
# transient blip or a hardware mute, both of which deserve a quick retry, never
# a 30min demotion to the built-in mic (DEF-208).
_COOLDOWN_TIERS = [120, 300, 600, 1800]  # BT-era: 2min, 5min, 10min, 30min cap
_COOLDOWN_TIERS_USB = [15, 30, 60]       # USB: gentle, capped at 60s


def _get_adaptive_cooldown(device_key: str) -> float:
    """Return the current cooldown duration for a device based on failure count.

    Tier table depends on the device's transport: USB gets short, capped
    cooldowns; everything else keeps the BT-era escalation (DEF-208).
    """
    count = _device_failure_counts.get(device_key, 0)
    tiers = _COOLDOWN_TIERS_USB if is_usb_transport(device_key) else _COOLDOWN_TIERS
    # count is 1-based (incremented before calling), so subtract 1 for 0-based tier index
    tier = min(max(0, count - 1), len(tiers) - 1)
    return tiers[tier]


# ---------------------------------------------------------------------------
# CoreAudio device queries (hotplug detection, alive-check, default input)
# ---------------------------------------------------------------------------

def get_dead_input_device_names() -> set[str]:
    """Return names (lowercase) of CoreAudio input devices that are NOT alive.

    macOS keeps paired-but-disconnected Bluetooth devices in the audio device
    list. PyAudio still enumerates them and can even open streams that return
    low-level noise, causing find_best_mic to select a phantom device. Querying
    CoreAudio's kAudioDevicePropertyDeviceIsAlive lets find_best_mic skip them.

    Returns an empty set if CoreAudio is unavailable (graceful degradation).
    """
    dead: set[str] = set()
    for name, alive, _transport in _enumerate_coreaudio_inputs():
        if not alive:
            dead.add(name.lower())
            _log(f"  CoreAudio: '{name}' is not alive (disconnected)")
    return dead


def get_live_input_device_names() -> set[str]:
    """Return names (lowercase) of CoreAudio input devices that ARE alive.

    The live-HAL view used to detect DEF-104 hotplugs that PortAudio's cached
    enumeration misses: a device present here but absent from PortAudio's
    ``get_device_count()`` was plugged in after the daemon started and stays
    invisible to every PortAudio code path until the process restarts. Empty
    set if CoreAudio is unavailable (graceful degradation — detection simply
    no-ops rather than false-firing a restart).
    """
    return {name.lower() for name, alive, _t in _enumerate_coreaudio_inputs() if alive}


def get_default_input_device_name() -> str | None:
    """Return the lowercase name of the current macOS default input device.

    Reads ``kAudioHardwarePropertyDefaultInputDevice`` from the **live** HAL via
    ctypes, so it reflects the device macOS actually routes input to right now —
    including a USB headset just hotplugged that PortAudio's per-process cache
    hasn't picked up yet.

    DEF-104 (2026-07-05 extension) uses this so the hotplug self-restart can fire
    for the user's *active* mic even when it isn't in ``mic_priority``: macOS
    makes a freshly-plugged USB headset the default input, which is a strong
    "the user wants this mic" signal that the priority list alone misses (the
    exact G435 incident — the daemon stayed on the built-in fallback because the
    only self-heal candidates were priority-list devices).

    Returns None on any failure or if no default is set (graceful degradation —
    detection no-ops rather than false-firing a restart).
    """
    try:
        ca_path = ctypes.util.find_library("CoreAudio")
        cf_path = ctypes.util.find_library("CoreFoundation")
        if not ca_path or not cf_path:
            return None
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

        # Read the default-input AudioDeviceID from the live HAL.
        default_addr = _AudioObjectPropertyAddress(
            _kAudioHardwarePropertyDefaultInputDevice,
            _kAudioObjectPropertyScopeGlobal,
            _kAudioObjectPropertyElementMain,
        )
        dev_id = ctypes.c_uint32(0)
        io_size = ctypes.c_uint32(ctypes.sizeof(dev_id))
        status = ca.AudioObjectGetPropertyData(
            ctypes.c_uint32(_kAudioObjectSystemObject),
            ctypes.byref(default_addr),
            ctypes.c_uint32(0), None,
            ctypes.byref(io_size), ctypes.byref(dev_id),
        )
        if status != 0 or dev_id.value == 0:
            return None

        # Map the ID to its device name.
        name_addr = _AudioObjectPropertyAddress(
            _kAudioObjectPropertyName,
            _kAudioObjectPropertyScopeGlobal,
            _kAudioObjectPropertyElementMain,
        )
        cfstr = ctypes.c_void_p(0)
        name_size = ctypes.c_uint32(ctypes.sizeof(cfstr))
        status = ca.AudioObjectGetPropertyData(
            ctypes.c_uint32(dev_id.value), ctypes.byref(name_addr),
            ctypes.c_uint32(0), None, ctypes.byref(name_size),
            ctypes.byref(cfstr),
        )
        if status != 0 or not cfstr.value:
            return None
        ptr = cf.CFStringGetCStringPtr(cfstr, _kCFStringEncodingUTF8)
        if ptr:
            name = ptr.decode("utf-8")
        else:
            length = cf.CFStringGetLength(cfstr) * 4 + 1
            nbuf = ctypes.create_string_buffer(length)
            name = (
                nbuf.value.decode("utf-8")
                if cf.CFStringGetCString(
                    cfstr, nbuf, length, _kCFStringEncodingUTF8
                )
                else ""
            )
        cf.CFRelease(cfstr)
        return name.lower() or None
    except Exception as e:
        _log(f"  CoreAudio default-input read failed: {e}")
        return None


def force_os_default_input(name_substr: str) -> bool:
    """Set the macOS default input device by name via CoreAudio ctypes.

    This bypasses PyAudio entirely. PortAudio's CoreAudio HAL caches the
    device enumeration at process start, so secondary ``pyaudio.PyAudio()``
    instances spawned later (e.g. inside ``_bt_trigger_hfp_switch``) inherit
    the stale cache — they can't see a Bluetooth input entry that only
    appears after macOS activates HFP. Writing the default-input property
    via CoreAudio forces macOS to activate HFP for the target BT device at
    the OS level, regardless of what PyAudio thinks. See DEF-060.

    Args:
        name_substr: Case-insensitive substring of the target device name.

    Returns:
        True if a matching device was found and the default-input write
        succeeded. False on any failure (CoreAudio unavailable, no match,
        write rejected).
    """
    try:
        ca_path = ctypes.util.find_library("CoreAudio")
        cf_path = ctypes.util.find_library("CoreFoundation")
        if not ca_path or not cf_path:
            return False
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

        # Enumerate all CoreAudio devices (this call hits the live HAL, not
        # PyAudio's cache — so BT devices that just became HFP-available
        # show up here even when PyAudio can't see them).
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
            return False

        buf = (ctypes.c_char * size.value)()
        io_size = ctypes.c_uint32(size.value)
        status = ca.AudioObjectGetPropertyData(
            ctypes.c_uint32(_kAudioObjectSystemObject), ctypes.byref(addr),
            ctypes.c_uint32(0), None, ctypes.byref(io_size), buf,
        )
        if status != 0:
            return False

        device_count = io_size.value // 4
        device_ids = [
            int.from_bytes(bytes(buf)[i * 4:(i + 1) * 4], byteorder="little")
            for i in range(device_count)
        ]

        target_lower = name_substr.lower()
        for did in device_ids:
            # Must have input streams — skip output-only devices.
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

            # Fetch the name and substring-match against the target.
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
            if status != 0 or not cfstr.value:
                continue
            dev_name = cfstr_to_str(cfstr.value)
            cf.CFRelease(cfstr)

            if target_lower not in dev_name.lower():
                continue

            # Found it — write it as the default input device.
            default_addr = _AudioObjectPropertyAddress(
                _kAudioHardwarePropertyDefaultInputDevice,
                _kAudioObjectPropertyScopeGlobal,
                _kAudioObjectPropertyElementMain,
            )
            did_val = ctypes.c_uint32(did)
            status = ca.AudioObjectSetPropertyData(
                ctypes.c_uint32(_kAudioObjectSystemObject),
                ctypes.byref(default_addr),
                ctypes.c_uint32(0), None,
                ctypes.c_uint32(ctypes.sizeof(did_val)),
                ctypes.byref(did_val),
            )
            if status == 0:
                _log(
                    f"  CoreAudio: set default input to '{dev_name}' "
                    f"(AudioObjectID={did})"
                )
                return True
            _log(
                f"  CoreAudio: SetPropertyData(defaultInput) failed "
                f"status={status} for '{dev_name}'"
            )
            return False

        return False
    except Exception as e:
        _log(f"  CoreAudio force_os_default_input failed: {e}")
        return False


# Minimum audio level to consider a device producing real audio.
# Disconnected Bluetooth devices produce quantization noise at level 1-5.
# A real connected mic in a quiet room produces ambient noise above 10.
# Matches the silent-mic health check threshold in main.py.
MIN_AUDIO_LEVEL = 10


def probe_device_level(
    pa: pyaudio.PyAudio,
    index: int,
    name: str,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    frames: int = 15,
) -> int:
    """Open a test stream and return the peak audio level (0 on error).

    Runs the pa.open + read loop on a watchdog thread because CoreAudio
    AUHAL err=-50 (paramErr) leaves PortAudio with a zombie stream: pa.open
    returns normally (error only printed to stderr) but the first
    stream.read() then spins forever in Pa_Sleep. Prior to the watchdog
    a single bad probe would freeze the entire main loop (DEF-057).

    Every open outcome is reported to the DEF-209 storm detector.
    """
    # 15 frames × chunk_size @ 16 kHz ≈ 1.2 s for a healthy probe, plus
    # the 0.8 s unmute settle. 4 s gives safe margin on the happy path
    # while still bounding the hang on AUHAL zombie streams.
    DEADLINE = 4.0

    result = {"level": 0, "err": None, "stream": None}
    done = threading.Event()

    # Mute output while probing to hide the A2DP→HFP pop. Use the OS mute
    # flag rather than setting volume=0 so Bluetooth headsets in HFP mode
    # do not receive a volume=0 HFP command (which the G435 and similar
    # headsets remember and later re-report, causing macOS to reset the
    # system volume to 0).
    _probe_was_muted = None
    if not is_builtin_mic(name):
        try:
            from heyvox.herald.coreaudio import is_system_muted, set_system_muted
            _probe_was_muted = is_system_muted()
            if not _probe_was_muted:
                _log(f"[VOL] probe_level mute: device='{name}' muting output")
                set_system_muted(True)
        except Exception:
            _probe_was_muted = None

    def _probe() -> None:
        try:
            s = pa.open(
                format=pyaudio.paInt16, channels=1,
                rate=sample_rate, input=True,
                input_device_index=index, frames_per_buffer=chunk_size,
            )
            result["stream"] = s
            record_pa_open_success()
            max_level = 0
            for _ in range(frames):
                data = np.frombuffer(
                    s.read(chunk_size, exception_on_overflow=False),
                    dtype=np.int16,
                )
                max_level = max(max_level, int(np.abs(data).max()))
            result["level"] = max_level
        except Exception as e:
            if result["stream"] is None:
                record_pa_open_failure(name, e)
            result["err"] = repr(e)
        finally:
            done.set()

    t = threading.Thread(target=_probe, daemon=True, name=f"probe-{name}")
    t.start()
    try:
        if not done.wait(timeout=DEADLINE):
            _log(f"  [{index}] {name}: PROBE TIMEOUT ({DEADLINE}s) — AUHAL zombie (err=-50?), abandoning thread")
            # Best-effort close; may no-op or raise on a broken stream.
            s = result.get("stream")
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
            return 0
        if result["err"]:
            _log(f"  [{index}] {name}: error - {result['err']}")
            return 0
        _log(f"  [{index}] {name}: max_level={result['level']}")
        return result["level"]
    finally:
        s = result.get("stream")
        if s is not None and not done.is_set():
            pass  # thread still owns the stream, don't double-close
        elif s is not None:
            try:
                s.close()
            except Exception:
                pass
        if _probe_was_muted is False:
            time.sleep(0.8)
            try:
                from heyvox.herald.coreaudio import set_system_muted
                set_system_muted(False)
                _log(f"[VOL] probe_level unmute: device='{name}'")
            except Exception:
                pass


def find_best_mic(pa: pyaudio.PyAudio, mic_priority: list[str] | None = None, sample_rate: int = DEFAULT_SAMPLE_RATE, chunk_size: int = DEFAULT_CHUNK_SIZE, require_audio: bool = False, excluded_devices: list[str] | None = None) -> int | None:
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
        excluded_devices: Device name substrings that must never be selected
            (virtual/loopback devices like BlackHole or Microsoft Teams'
            virtual audio driver). Checked case-insensitively against every
            candidate, including the final system-default fallback.

    Returns:
        Device index (int) or None if no input device is available.
    """
    if mic_priority is None:
        mic_priority = ["MacBook Pro Microphone"]

    # Filter out disconnected Bluetooth devices (macOS keeps them in the list)
    dead_names = get_dead_input_device_names()

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
        if is_excluded_device_name(d['name'], excluded_devices):
            _log(f"  [{i}] {d['name']}: skipped (excluded device)")
            continue
        matched = False
        for prio_name in mic_priority:
            if prio_name.lower() in d['name'].lower():
                devices_by_priority[prio_name].append((i, d['name']))
                matched = True
                break
        if not matched:
            other_devices.append((i, d['name']))

    now = time.time()

    for rank, prio_name in enumerate(mic_priority):
        for index, dev_name in devices_by_priority[prio_name]:
            dev_key = dev_name.lower()
            _is_builtin = is_builtin_mic(dev_name)

            # Built-in mic is always assumed working — never cooldown or level-test it.
            # Just verify the stream opens successfully.
            if _is_builtin:
                try:
                    s = open_mic_stream(pa, index, sample_rate=sample_rate, chunk_size=chunk_size)
                    s.close()
                    _log(f"  [{index}] {dev_name}: built-in mic, accepting (always trusted)")
                    _device_cooldowns.pop(dev_key, None)
                    _device_failure_counts.pop(dev_key, None)
                    return index
                except Exception as e:
                    _log(f"  [{index}] {dev_name}: built-in mic stream failed: {e}")
                    continue

            # Skip devices that failed recently — prevents tight infinite loop
            # when a dead Bluetooth device (e.g. Jabra) stays highest-priority.
            cooldown_ts = _device_cooldowns.get(dev_key)
            adaptive_secs = _get_adaptive_cooldown(dev_key)
            if cooldown_ts is not None and now - cooldown_ts < adaptive_secs:
                remaining = int(adaptive_secs - (now - cooldown_ts))
                failures = _device_failure_counts.get(dev_key, 0)
                _log(f"  [{index}] {dev_name}: skipping (cooldown, {remaining}s remaining, failures={failures})")
                continue

            _log(f"Testing {dev_name}...")
            max_level = probe_device_level(pa, index, dev_name, sample_rate, chunk_size)

            if max_level >= MIN_AUDIO_LEVEL:
                # Device is producing real audio — clear any prior cooldown and
                # reset failure count (sustained audio proves it's working).
                _device_cooldowns.pop(dev_key, None)
                _device_failure_counts.pop(dev_key, None)
                return index

            if max_level == 0:
                # Silent device — increment failure count and put in adaptive cooldown.
                _device_failure_counts[dev_key] = _device_failure_counts.get(dev_key, 0) + 1
                _device_cooldowns[dev_key] = now
                adaptive_secs = _get_adaptive_cooldown(dev_key)
                _log(f"  [{index}] {dev_name}: zero audio, cooldown {adaptive_secs}s (failure #{_device_failure_counts[dev_key]})")

            # First-priority device: accept even at zero level if stream opened OK.
            # This supports virtual devices (BlackHole) that have no ambient audio.
            # Skip this fallback during dead-mic recovery (require_audio=True).
            if rank == 0 and not require_audio and max_level == 0:
                try:
                    from heyvox.audio.bt import mute_output_during_bt_switch as _mute_bt
                    with _mute_bt(dev_name):
                        s = open_mic_stream(pa, index, sample_rate=sample_rate, chunk_size=chunk_size)
                        s.close()
                    _log(f"  [{index}] {dev_name}: no audio but stream OK (first priority), accepting")
                    # Don't penalise virtual/first-priority devices with a cooldown.
                    _device_cooldowns.pop(dev_key, None)
                    return index
                except Exception:
                    pass

    for index, dev_name in other_devices:
        dev_key = dev_name.lower()
        _is_builtin = is_builtin_mic(dev_name)

        # Built-in mic: always accept if stream opens (no cooldown, no level test).
        if _is_builtin:
            try:
                s = open_mic_stream(pa, index, sample_rate=sample_rate, chunk_size=chunk_size)
                s.close()
                _log(f"  [{index}] {dev_name}: built-in mic, accepting (always trusted)")
                _device_cooldowns.pop(dev_key, None)
                _device_failure_counts.pop(dev_key, None)
                return index
            except Exception as e:
                _log(f"  [{index}] {dev_name}: built-in mic stream failed: {e}")
                continue

        cooldown_ts = _device_cooldowns.get(dev_key)
        adaptive_secs = _get_adaptive_cooldown(dev_key)
        if cooldown_ts is not None and now - cooldown_ts < adaptive_secs:
            remaining = int(adaptive_secs - (now - cooldown_ts))
            failures = _device_failure_counts.get(dev_key, 0)
            _log(f"  [{index}] {dev_name}: skipping (cooldown, {remaining}s remaining, failures={failures})")
            continue

        _log(f"Testing fallback {dev_name}...")
        max_level = probe_device_level(pa, index, dev_name, sample_rate, chunk_size)
        if max_level >= MIN_AUDIO_LEVEL:
            _device_cooldowns.pop(dev_key, None)
            _device_failure_counts.pop(dev_key, None)
            return index
        if max_level == 0:
            _device_failure_counts[dev_key] = _device_failure_counts.get(dev_key, 0) + 1
            _device_cooldowns[dev_key] = now
            adaptive_secs = _get_adaptive_cooldown(dev_key)
            _log(f"  [{index}] {dev_name}: zero audio, cooldown {adaptive_secs}s (failure #{_device_failure_counts[dev_key]})")

    try:
        default_info = pa.get_default_input_device_info()
        default_name = default_info.get('name', '')
        if default_name and is_excluded_device_name(default_name, excluded_devices):
            _log(f"System default '{default_name}' is an excluded device — refusing to use it")
            return None
        if default_name and require_audio and not is_builtin_mic(default_name):
            # When require_audio is set (recovery path), respect cooldown —
            # caller will retry or fall back to a second find_best_mic() without it.
            # Built-in mics are exempt: they're always assumed working.
            default_key = default_name.lower()
            cooldown_ts = _device_cooldowns.get(default_key)
            if cooldown_ts is not None and now - cooldown_ts < _get_adaptive_cooldown(default_key):
                remaining = int(_get_adaptive_cooldown(default_key) - (now - cooldown_ts))
                _log(f"System default '{default_name}' is in cooldown ({remaining}s remaining), skipping (require_audio=True)")
                return None
        _log(f"All mics failed level test, using system default as last resort: {default_name or 'unknown'}")
        return default_info['index']
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

    Reports the open outcome to the DEF-209 storm detector.
    """
    try:
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=sample_rate,
            input=True,
            input_device_index=dev_index,
            frames_per_buffer=chunk_size,
        )
    except Exception as e:
        try:
            _dev_name = pa.get_device_info_by_index(dev_index).get("name", "")
        except Exception:
            _dev_name = ""
        record_pa_open_failure(_dev_name or f"index-{dev_index}", e)
        raise
    record_pa_open_success()
    return stream


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


def add_device_cooldown(device_name: str) -> None:
    """Add a device to cooldown after it was detected as silent/dead.

    Built-in microphones are never put in cooldown — they are physically
    always present and assumed working.

    Increments the failure count for adaptive cooldown escalation.
    Call this from the silent mic recovery path so that the hotplug scanner
    doesn't immediately re-select the dead device.
    """
    if is_builtin_mic(device_name):
        _log(f"Device '{device_name}' is built-in, skipping cooldown")
        return
    key = device_name.lower()
    _device_failure_counts[key] = _device_failure_counts.get(key, 0) + 1
    _device_cooldowns[key] = time.time()
    cooldown_secs = _get_adaptive_cooldown(key)
    _transport_tag = "usb" if is_usb_transport(key) else "bt-era"
    _log(
        f"Device '{device_name}' added to cooldown for {cooldown_secs}s "
        f"(failure #{_device_failure_counts[key]}, {_transport_tag} tiers)"
    )


def is_device_cooled_down(device_name: str) -> bool:
    """Check if a device is currently in cooldown (recently failed).

    Uses adaptive cooldown duration based on how many times the device
    has failed consecutively.
    """
    key = device_name.lower()
    ts = _device_cooldowns.get(key)
    if ts is None:
        return False
    return (time.time() - ts) < _get_adaptive_cooldown(key)


def clear_device_cooldown(device_name: str) -> None:
    """Clear cooldown for a single device after it produced real audio.

    Call this when a device is confirmed working (e.g. successful PTT recording)
    to allow hotplug scan to select it again immediately.
    """
    key = device_name.lower()
    had_cooldown = key in _device_cooldowns
    _device_cooldowns.pop(key, None)
    _device_failure_counts.pop(key, None)
    if had_cooldown:
        _log(f"Device '{device_name}' cooldown cleared (confirmed working)")


def clear_device_cooldowns() -> None:
    """Clear all device cooldowns and failure counts.

    Call this when Bluetooth state changes (connect/disconnect event) so that
    newly-connected devices are tested immediately rather than waiting for the
    cooldown window to expire.

    Example usage in main.py Bluetooth event handler::

        from heyvox.audio.mic import clear_device_cooldowns
        clear_device_cooldowns()
    """
    count = len(_device_cooldowns)
    _device_cooldowns.clear()
    _device_failure_counts.clear()
    if count:
        _log(f"Device cooldowns cleared ({count} device(s) released)")


# ---------------------------------------------------------------------------
# USB hotplug watcher — DEF-104 diagnostic
# ---------------------------------------------------------------------------
#
# Background thread that polls the PortAudio device list and logs a
# [USB_HOTPLUG] tag whenever the (index, name, max_input_channels) tuple-set
# changes. Catches the moment of USB re-enumeration that strands HeyVox on a
# stale PA index (G435 0→1 drift in DEF-104). Without this watcher, the drift
# is only logged as a side-effect during the next AUDIO-13 reinit — minutes
# after the actual hotplug event.
#
# Cost: one short PA enumeration every interval (~5–20 ms). Safe to run while
# the DeviceManager owns its own PA instance — we use a getter callback rather
# than a captured reference so the watcher always queries the *current* PA
# (DeviceManager rebuilds its PA on reinit; a captured ref would go stale).

_pa_hotplug_thread: threading.Thread | None = None
_pa_hotplug_stop = threading.Event()


def _enumerate_pa_signature(pa: pyaudio.PyAudio) -> list[tuple[int, str, int]]:
    """Return [(index, name, max_input_channels), ...] for all PA devices.

    Used by the hotplug watcher and only logs structure, not levels, so it's
    safe to call as often as needed.
    """
    out: list[tuple[int, str, int]] = []
    try:
        n = pa.get_device_count()
    except Exception:
        return out
    for i in range(n):
        try:
            d = pa.get_device_info_by_index(i)
            out.append((
                i,
                str(d.get("name", "")),
                int(d.get("maxInputChannels", 0)),
            ))
        except Exception:
            continue
    return out


def _hash_pa_signature(sig: list[tuple[int, str, int]]) -> str:
    """Short stable hash of the device signature for change detection."""
    s = "|".join(f"{i}:{n}:{c}" for i, n, c in sig)
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


def _pa_hotplug_loop(get_pa: Callable[[], pyaudio.PyAudio | None], interval_secs: float) -> None:
    """Poll PA device list; log [USB_HOTPLUG] on every change."""
    last_sig: list[tuple[int, str, int]] = []
    last_hash = ""
    started = False
    while not _pa_hotplug_stop.is_set():
        try:
            pa = get_pa()
            if pa is None:
                # DeviceManager rebuilding — skip this tick, try again later
                if _pa_hotplug_stop.wait(interval_secs):
                    break
                continue
            cur_sig = _enumerate_pa_signature(pa)
            cur_hash = _hash_pa_signature(cur_sig)
            if not started:
                _log(
                    f"[USB_HOTPLUG] watcher started: {len(cur_sig)} devices, "
                    f"hash={cur_hash}"
                )
                started = True
                last_sig, last_hash = cur_sig, cur_hash
            elif cur_hash != last_hash:
                old_set = {(i, n, c) for i, n, c in last_sig}
                new_set = {(i, n, c) for i, n, c in cur_sig}
                added = sorted(new_set - old_set)
                removed = sorted(old_set - new_set)
                _log(
                    f"[USB_HOTPLUG] device list changed: "
                    f"{last_hash} -> {cur_hash} "
                    f"(+{len(added)}/-{len(removed)}) "
                    f"added={added!r} removed={removed!r}"
                )
                last_sig, last_hash = cur_sig, cur_hash
        except Exception as e:
            _log(f"[USB_HOTPLUG] watcher error (continuing): {e}")
        if _pa_hotplug_stop.wait(interval_secs):
            break


def start_pa_hotplug_watcher(
    get_pa: Callable[[], pyaudio.PyAudio | None],
    interval_secs: float = 10.0,
) -> None:
    """Start the USB hotplug watcher thread (idempotent).

    Args:
        get_pa: Callable returning the current PortAudio instance (or None
            during a transient rebuild). Use a closure over DeviceManager
            rather than capturing a reference — DeviceManager rebuilds its
            PA on reinit and stale refs would silently report old state.
        interval_secs: Poll interval. 10s is a reasonable default — fine-grained
            enough to bracket a slow speaker, cheap enough at ~5–20 ms per poll.

    Requirement: DEF-104 diagnostic instrumentation.
    """
    global _pa_hotplug_thread
    if _pa_hotplug_thread is not None and _pa_hotplug_thread.is_alive():
        return
    _pa_hotplug_stop.clear()
    _pa_hotplug_thread = threading.Thread(
        target=_pa_hotplug_loop,
        args=(get_pa, interval_secs),
        name="pa-hotplug-watcher",
        daemon=True,
    )
    _pa_hotplug_thread.start()


def stop_pa_hotplug_watcher() -> None:
    """Signal the watcher to exit. No-op if not started."""
    _pa_hotplug_stop.set()
