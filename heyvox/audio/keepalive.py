"""DEF-148/DEF-150: output keep-alive + cue playback for USB power-saving headsets.

USB wireless headsets (e.g. the Logitech G535 over its Lightspeed/A00142
receiver) park the output path after a short silence; opening a FRESH output
stream then loses its cold start (~0.5-0.7 s). Long sounds (TTS, YouTube)
survive because only their start clips; short cues (~0.5 s) and — crucially —
any separate ``afplay`` process (which opens its own fresh stream every time)
are swallowed whole. Verified: with the keep-alive holding one silent stream
open, a cue written INTO that already-running stream is audible, while a
separate ``afplay`` of the same file is silent.

So this module does two things on a USB-transport output:
 1. Holds a silent output stream open so the device never parks.
 2. Plays HeyVox's cues (listening/ok/...) by writing them INTO that same
    already-warm stream — no fresh stream, no cold start, no delay.

On built-in speakers / Bluetooth / virtual outputs the cold-start delay doesn't
occur, so the keep-alive stays OFF and ``play_cue_via_stream`` returns False —
the caller (cues.py) falls back to ``afplay``.

DEF-153: the keep-alive owns its own PortAudio context instead of borrowing the
DeviceManager's. A context created before a USB device flap (e.g. the G535
power-cycling) holds a stale CoreAudio device ID — every reopen then fails with
-9986/-10851 forever, cues silently fall back to afplay, and afplay is exactly
what this device class swallows. On open failure the context is dropped and
recreated fresh on the next tick (a fresh context resolves the current ID).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import subprocess
import tempfile
import threading
import wave


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

        addr = _AOPA(_DEFAULT_OUTPUT, _SCOPE_GLOBAL, _ELEM_MAIN)
        dev = ctypes.c_uint32(0)
        size = ctypes.c_uint32(4)
        if ca.AudioObjectGetPropertyData(
            ctypes.c_uint32(_SYSOBJ), ctypes.byref(addr),
            ctypes.c_uint32(0), None, ctypes.byref(size), ctypes.byref(dev),
        ) != 0 or dev.value == 0:
            return 0

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
# Module singleton: the running keep-alive with an OPEN usb stream (else None).
# cues.py calls play_cue_via_stream() — returns False when there's no warm
# stream so the caller falls back to afplay.
# ---------------------------------------------------------------------------

_ACTIVE: "OutputKeepAlive | None" = None


def play_cue_via_stream(name: str, path: str) -> bool:
    ka = _ACTIVE
    if ka is None:
        return False
    return ka.play_cue(name, path)


class OutputKeepAlive:
    """Holds a silent output stream open while the default output is USB, and
    plays cues into that same warm stream (no cold start).

    Runs a low-frequency monitor thread (the silence + cue audio is produced by
    a PortAudio callback). Every failure path degrades to "off" / "fall back to
    afplay" rather than raising.

    Owns its own PortAudio context (DEF-153): created lazily on first open,
    dropped + recreated after an open failure so a stale device ID from a USB
    flap can't wedge the keep-alive permanently. Never touch the
    DeviceManager's shared context here — its lifecycle belongs to the mic.
    """

    def __init__(self, log, *, check_interval: float = 5.0,
                 rate: int = 48000, chunk: int = 1024) -> None:
        self._log = log
        self._pa = None          # own PortAudio context (DEF-153), lazy
        self._open_fails = 0     # consecutive open failures, for log throttle
        self._interval = check_interval
        self._rate = rate
        self._chunk = chunk
        self._stream = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Cue playback over the warm stream
        self._cues: dict[str, object] = {}   # name -> int16 ndarray @ rate
        self._cue_buf = None                 # currently-playing samples
        self._cue_pos = 0
        self._cue_lock = threading.Lock()

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
        self._drop_pa()

    # -- cue playback over the warm stream --------------------------------

    def _load_cue(self, path: str):
        """afconvert the cue to the stream's rate/mono/int16 → numpy array, cached."""
        try:
            import numpy as np
        except Exception:
            return None
        tmp = tempfile.mktemp(suffix=".wav")
        try:
            r = subprocess.run(
                ["afconvert", "-f", "WAVE", "-d", f"LEI16@{self._rate}",
                 "-c", "1", path, tmp],
                capture_output=True,
            )
            if r.returncode != 0 or not os.path.exists(tmp):
                return None
            wf = wave.open(tmp)
            data = wf.readframes(wf.getnframes())
            wf.close()
            return np.frombuffer(data, dtype=np.int16).copy()
        except Exception as e:
            self._log(f"[keepalive] cue load failed for {path}: {e}")
            return None
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def play_cue(self, name: str, path: str) -> bool:
        """Queue a cue to play over the warm stream. Returns False (→ caller
        falls back to afplay) if no stream is currently open."""
        if self._stream is None:
            return False
        buf = self._cues.get(name)
        if buf is None:
            buf = self._load_cue(path)
            if buf is None:
                return False
            self._cues[name] = buf
        with self._cue_lock:
            self._cue_buf = buf
            self._cue_pos = 0
        return True

    # -- internal ----------------------------------------------------------

    def _cb(self, in_data, frame_count, time_info, status):
        import pyaudio
        with self._cue_lock:
            buf = self._cue_buf
            if buf is not None:
                import numpy as np
                chunk = buf[self._cue_pos:self._cue_pos + frame_count]
                self._cue_pos += len(chunk)
                if self._cue_pos >= len(buf):
                    self._cue_buf = None  # finished
                if len(chunk) < frame_count:
                    chunk = np.concatenate(
                        [chunk, np.zeros(frame_count - len(chunk), dtype=np.int16)]
                    )
                return (chunk.tobytes(), pyaudio.paContinue)
        return (b"\x00" * (frame_count * 2), pyaudio.paContinue)

    def _open_stream(self) -> None:
        global _ACTIVE
        if self._stream is not None:
            return
        try:
            import pyaudio
            if self._pa is None:
                self._pa = pyaudio.PyAudio()
            self._stream = self._pa.open(
                format=pyaudio.paInt16, channels=1, rate=self._rate,
                output=True, frames_per_buffer=self._chunk,
                stream_callback=self._cb,
            )
            self._stream.start_stream()
            _ACTIVE = self
            if self._open_fails:
                self._log(f"[keepalive] stream recovered after "
                          f"{self._open_fails} failed attempt(s) — fresh PA context (DEF-153)")
            self._open_fails = 0
            self._log("[keepalive] USB output detected — holding silent stream "
                      "open + routing cues through it (DEF-148/150)")
        except Exception as e:
            self._stream = None
            self._open_fails += 1
            # DEF-153: a context created before a USB flap holds a stale
            # CoreAudio device ID — reopen fails with -9986/-10851 forever.
            # Drop it so the next tick recreates a fresh one (which resolves
            # the device's CURRENT ID). Log first failure, then 1/min; call
            # out the DEF-104 escalation if fresh contexts keep failing too.
            self._drop_pa()
            if self._open_fails == 1 or self._open_fails % 12 == 0:
                self._log(f"[keepalive] could not open silent stream "
                          f"(attempt {self._open_fails}, PA context dropped for fresh retry): {e}")
            if self._open_fails == 24:
                self._log("[keepalive] fresh PA contexts keep failing for 2min — "
                          "process-level PA staleness (DEF-104 class), daemon restart required")

    def _close_stream(self) -> None:
        global _ACTIVE
        if _ACTIVE is self:
            _ACTIVE = None
        s, self._stream = self._stream, None
        if s is None:
            return
        try:
            s.stop_stream()
            s.close()
            self._log("[keepalive] output no longer USB — released silent stream")
        except Exception:
            pass

    def _drop_pa(self) -> None:
        """Terminate and forget our own PA context (DEF-153). Safe to call
        with no context; never called while a stream is open on it."""
        pa, self._pa = self._pa, None
        if pa is None:
            return
        try:
            pa.terminate()
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
