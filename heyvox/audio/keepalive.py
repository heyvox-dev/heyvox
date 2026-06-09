"""DEF-148: output-stream keep-alive for USB power-saving wireless headsets.

USB wireless headsets (e.g. the Logitech G535 over its Lightspeed/A00142
receiver) drop the *cold start* of a freshly opened output stream: after a
short silence the device parks the audio path in a low-power state, and the
next stream open loses its first ~0.5-0.7 s. Long sounds (TTS, YouTube) survive
because only their start is clipped; short cues (~0.5 s) are swallowed whole.

This is distinct from Lightspeed's ~1 ms *running* latency — that's the
in-stream figure; this is the wake-from-idle figure, which gaming never hits
(continuous stream) so it isn't optimized. The G535's aggressive power-saving
(33 h battery vs the G435's 18 h) is exactly what causes it; the G435 over its
A00150 receiver doesn't park as hard, so it never showed the problem.

Fix: hold a *silent* output stream open so the device never parks → cues play
immediately, no lead-in padding, no delay.

RELEVANT ONLY ON A USB-TRANSPORT OUTPUT DEVICE. On built-in speakers,
Bluetooth, or virtual devices the cold-start delay doesn't occur, so the
keep-alive stays OFF (no needless wake / battery drain) — it re-checks the
default output's transport periodically and starts/stops the silent stream
accordingly. A silent stream draws almost nothing at the amplifier; the cost is
that the device's idle deep-sleep is suppressed while a USB output is active.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import threading
import time


# ---------------------------------------------------------------------------
# CoreAudio: transport type of the current default OUTPUT device
# ---------------------------------------------------------------------------

def _fourcc(s: str) -> int:
    return (ord(s[0]) << 24) | (ord(s[1]) << 16) | (ord(s[2]) << 8) | ord(s[3])


class _AOPA(ctypes.Structure):
    _fields_ = [
        ("mSelector", ctypes.c_uint32),
        ("mScope", ctypes.c_uint32),
        ("mElement", ctypes.c_uint32),
    ]


_SYSOBJ = 1
_DEFAULT_OUTPUT = _fourcc("dOut")      # kAudioHardwarePropertyDefaultOutputDevice
_TRANSPORT = _fourcc("tran")           # kAudioDevicePropertyTransportType
_SCOPE_GLOBAL = _fourcc("glob")
_ELEM_MAIN = 0
_USB = _fourcc("usb ")                 # kAudioDeviceTransportTypeUSB


def default_output_transport() -> int:
    """Return the transport four-char-code (int) of the current default OUTPUT
    device, or 0 if it can't be determined (graceful degradation → no-op)."""
    try:
        ca_path = ctypes.util.find_library("CoreAudio")
        if not ca_path:
            return 0
        ca = ctypes.cdll.LoadLibrary(ca_path)

        # default output device id
        addr = _AOPA(_DEFAULT_OUTPUT, _SCOPE_GLOBAL, _ELEM_MAIN)
        dev = ctypes.c_uint32(0)
        size = ctypes.c_uint32(4)
        if ca.AudioObjectGetPropertyData(
            ctypes.c_uint32(_SYSOBJ), ctypes.byref(addr),
            ctypes.c_uint32(0), None, ctypes.byref(size), ctypes.byref(dev),
        ) != 0 or dev.value == 0:
            return 0

        # its transport type
        taddr = _AOPA(_TRANSPORT, _SCOPE_GLOBAL, _ELEM_MAIN)
        tval = ctypes.c_uint32(0)
        tsize = ctypes.c_uint32(4)
        if ca.AudioObjectGetPropertyData(
            ctypes.c_uint32(dev.value), ctypes.byref(taddr),
            ctypes.c_uint32(0), None, ctypes.byref(tsize), ctypes.byref(tval),
        ) != 0:
            return 0
        return tval.value
    except Exception:
        return 0


def default_output_is_usb() -> bool:
    """True only when the default output device is on a USB transport."""
    return default_output_transport() == _USB


# ---------------------------------------------------------------------------
# Keep-alive: silent output stream, gated on USB output
# ---------------------------------------------------------------------------

class OutputKeepAlive:
    """Holds a silent output stream open while the default output is USB.

    Runs a low-frequency monitor thread (no audio work on it — the silence is
    produced by a PortAudio callback). Safe to start/stop repeatedly; every
    failure path degrades to "keep-alive off" rather than raising.
    """

    def __init__(self, pa, log, *, check_interval: float = 5.0,
                 rate: int = 48000, chunk: int = 1024) -> None:
        self._pa = pa
        self._log = log
        self._interval = check_interval
        self._rate = rate
        self._chunk = chunk
        self._stream = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._silence = b"\x00" * (chunk * 2)  # int16 mono

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="output-keepalive", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._close_stream()

    # -- internal ----------------------------------------------------------

    def _cb(self, in_data, frame_count, time_info, status):
        import pyaudio
        return (b"\x00" * (frame_count * 2), pyaudio.paContinue)

    def _open_stream(self) -> None:
        if self._stream is not None:
            return
        try:
            import pyaudio
            self._stream = self._pa.open(
                format=pyaudio.paInt16, channels=1, rate=self._rate,
                output=True, frames_per_buffer=self._chunk,
                stream_callback=self._cb,
            )
            self._stream.start_stream()
            self._log("[keepalive] USB output detected — holding silent stream "
                      "open (DEF-148: prevents cold-start cue loss)")
        except Exception as e:
            self._log(f"[keepalive] could not open silent stream: {e}")
            self._stream = None

    def _close_stream(self) -> None:
        s, self._stream = self._stream, None
        if s is None:
            return
        try:
            s.stop_stream()
            s.close()
            self._log("[keepalive] output no longer USB — released silent stream")
        except Exception:
            pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if default_output_is_usb():
                    self._open_stream()
                else:
                    self._close_stream()
            except Exception as e:
                self._log(f"[keepalive] monitor error (continuing): {e}")
            self._stop.wait(self._interval)
        self._close_stream()
