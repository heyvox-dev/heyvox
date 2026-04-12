"""
Device management for heyvox.

Handles microphone initialization, hotplug scanning, zombie stream detection,
health checks, and manual mic switching.

Requirements: DECOMP-02
"""
import os
import sys
import time
import logging
from collections import deque
from typing import TYPE_CHECKING

import numpy as np
import pyaudio

from heyvox.audio.mic import (
    find_best_mic,
    open_mic_stream,
    detect_headset,
    get_dead_input_device_names,
    add_device_cooldown,
    is_device_cooled_down,
)
from heyvox.audio.cues import device_change_cue
from heyvox.constants import ACTIVE_MIC_FILE, MIC_SWITCH_REQUEST_FILE

if TYPE_CHECKING:
    from heyvox.app_context import AppContext
    from heyvox.config import HeyvoxConfig

log = logging.getLogger(__name__)


class DeviceManager:
    """Manages microphone lifecycle for the heyvox main event loop.

    Encapsulates all device management that was previously inline in main.py:
    - Initialization (find best mic, open stream, detect headset)
    - Hotplug scanning (periodically check for better devices)
    - Zombie stream detection (variance-based AUDIO-12)
    - Time-based dead mic detection (AUDIO-13)
    - IOError recovery
    - Manual mic switch handling (HUD menu)
    - Output device change detection (AUDIO-11)
    - Cleanup on shutdown

    All device-private state lives here. Recording state (is_recording, busy)
    lives on AppContext and is read via self.ctx.

    Args:
        ctx: AppContext instance — used to read is_recording/busy and set
            zombie_mic_reinit/last_good_audio_time.
        config: HeyvoxConfig instance.
        log_fn: Callable[[str], None] — the main.py log() function that writes
            to the log file.  Used for user-visible messages that match the
            existing log format.  New debug-only messages use the module-level
            logging.getLogger() instead.
        hud_send: Callable[[dict], None] — sends a message to the HUD overlay.
    """

    # -------------------------------------------------------------------------
    # Constants
    # -------------------------------------------------------------------------

    _DEVICE_SCAN_INTERVAL = 3.0      # seconds — fast detection for USB/BT hotplug
    _HEALTH_CHECK_INTERVAL = 5.0     # seconds — dead-mic health check cadence
    _ZOMBIE_FAIL_THRESHOLD = 2       # force reinit after N consecutive bad recordings
    _DEAD_MIC_TIMEOUT = 30.0         # force reinit after 30s of silence (AUDIO-13)

    # -------------------------------------------------------------------------
    # Construction
    # -------------------------------------------------------------------------

    def __init__(self, ctx: "AppContext", config: "HeyvoxConfig", log_fn, hud_send) -> None:
        self.ctx = ctx
        self.config = config
        self._log = log_fn
        self._hud_send = hud_send

        # Device state — private to DeviceManager
        self.pa: pyaudio.PyAudio | None = None
        self.stream = None
        self.dev_index: int | None = None
        self.dev_name: str = ""
        self.headset_mode: bool = False

        # Hotplug / pin state
        self._mic_pinned: bool = False
        self._last_device_scan: float = time.time()
        self._last_output_device: str = ""

        # Health check state
        self._last_health_check: float = time.time()
        self._health_cv_history: deque = deque(maxlen=6)  # Last 30s of CV values
        self._zero_streak: int = 0
        self._silent_recover_count: int = 0  # For exponential backoff

        # Cooldown tracking (per-DeviceManager, separate from module-level in mic.py)
        self._cooldown: dict[str, float] = {}

    # -------------------------------------------------------------------------
    # Init
    # -------------------------------------------------------------------------

    def init(self) -> None:
        """Initialize PyAudio, find best mic, open stream, detect headset.

        Calls sys.exit(1) if no microphone is available — matching the existing
        behavior in main.py.
        """
        mic_priority = self.config.mic_priority if self.config else None
        sample_rate = self.config.audio.sample_rate if self.config else 16000
        chunk_size = self.config.audio.chunk_size if self.config else 1280

        self._log("Opening audio stream...")
        self.pa = pyaudio.PyAudio()
        self.dev_index = find_best_mic(
            self.pa,
            mic_priority=mic_priority,
            sample_rate=sample_rate,
            chunk_size=chunk_size,
        )
        if self.dev_index is None:
            self._log("FATAL: No microphone available, exiting")
            sys.exit(1)

        self.dev_name = self.pa.get_device_info_by_index(self.dev_index)['name']
        self._log(f"Using input: [{self.dev_index}] {self.dev_name}")
        self.stream = open_mic_stream(
            self.pa, self.dev_index,
            sample_rate=sample_rate,
            chunk_size=chunk_size,
        )
        self._write_active_mic(self.dev_name)
        self.headset_mode = detect_headset(self.pa, self.dev_index)
        self._log(
            f"Headset detected: {self.headset_mode} "
            f"(echo suppression {'inactive' if self.headset_mode else 'active'})"
        )

        # Initialise AUDIO-13 timer
        self.ctx.last_good_audio_time = time.time()

        # Capture initial output device for change detection (AUDIO-11)
        try:
            _default_out = self.pa.get_default_output_device_info()
            self._last_output_device = _default_out['name']
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Active-mic file
    # -------------------------------------------------------------------------

    def _write_active_mic(self, name: str) -> None:
        """Write current mic name to ACTIVE_MIC_FILE so HUD menu can display it."""
        try:
            with open(ACTIVE_MIC_FILE, "w") as f:
                f.write(name)
        except OSError:
            pass

    # -------------------------------------------------------------------------
    # Reinit
    # -------------------------------------------------------------------------

    def reinit(self, require_audio: bool = False) -> bool:
        """Reinitialize the audio stack after a zombie stream is detected.

        Closes the existing stream and PyAudio instance, sleeps briefly, then
        opens a fresh instance.  Tries with require_audio=True first, falling
        back to any available device.

        IMPORTANT: The caller must ensure ``not ctx.is_recording and not ctx.busy``
        before calling this method.

        Returns:
            True if recovery succeeded, False if no mic could be found.
        """
        mic_priority = self.config.mic_priority if self.config else None
        sample_rate = self.config.audio.sample_rate if self.config else 16000
        chunk_size = self.config.audio.chunk_size if self.config else 1280

        self._log("Zombie stream reinit: forcing mic recovery after consecutive empty recordings")
        print("[mic] Zombie stream detected, forcing reinit...", file=sys.stderr, flush=True)
        self._hud_send({"type": "error", "text": "Mic zombie: reinitializing"})
        self._mic_pinned = False

        try:
            self.stream.stop_stream()
            self.stream.close()
        except Exception:
            pass
        self.pa.terminate()
        time.sleep(0.5)

        self.pa = pyaudio.PyAudio()
        dev_index = find_best_mic(
            self.pa,
            mic_priority=mic_priority,
            sample_rate=sample_rate,
            chunk_size=chunk_size,
            require_audio=True,
        )
        if dev_index is None:
            dev_index = find_best_mic(
                self.pa,
                mic_priority=mic_priority,
                sample_rate=sample_rate,
                chunk_size=chunk_size,
            )
        if dev_index is None:
            self._log("No mic found after zombie reinit, retrying in 2s...")
            time.sleep(2)
            return False

        self.dev_index = dev_index
        self.dev_name = self.pa.get_device_info_by_index(dev_index)['name']
        self._log(f"Zombie reinit recovered: [{dev_index}] {self.dev_name}")
        print(f"[mic] Zombie reinit recovered: [{dev_index}] {self.dev_name}", file=sys.stderr, flush=True)
        self.stream = open_mic_stream(
            self.pa, dev_index,
            sample_rate=sample_rate,
            chunk_size=chunk_size,
        )
        self.headset_mode = detect_headset(self.pa, dev_index)
        self._write_active_mic(self.dev_name)
        device_change_cue(self.dev_name, "input")
        self._hud_send({"type": "state", "text": f"Mic: {self.dev_name}"})
        self._health_cv_history.clear()
        self.ctx.last_good_audio_time = time.time()  # AUDIO-13: reset timeout
        return True

    # -------------------------------------------------------------------------
    # Health check (called each main loop iteration when idle)
    # -------------------------------------------------------------------------

    def health_check(self, audio: np.ndarray) -> None:
        """Check audio for silent-mic or zombie-stream conditions.

        Called from the main loop when not recording and not busy.  Uses two
        detection strategies:

        - **Zero-streak** (AUDIO-08): consecutive health-check intervals with
          audio level below 10 → flag for reinit.
        - **Variance / CV** (AUDIO-12): audio level above 10 but coefficient of
          variation suspiciously stable → likely a zombie stream.

        When either condition is detected, sets ``ctx.zombie_mic_reinit = True``
        so the main loop handles the actual reinit on the next iteration.

        Args:
            audio: Raw audio chunk as int16 numpy array.
        """
        now = time.time()
        if now - self._last_health_check < self._HEALTH_CHECK_INTERVAL:
            return
        self._last_health_check = now

        level = int(np.abs(audio).max())

        # Variance-based zombie stream detection (AUDIO-12)
        if level >= 10:
            abs_audio = np.abs(audio.astype(np.float32))
            _mean = float(abs_audio.mean())
            if _mean > 1e-6:
                _cv = float(abs_audio.std() / _mean)
                self._health_cv_history.append(_cv)
                if len(self._health_cv_history) >= 4:
                    _cv_values = list(self._health_cv_history)
                    _cv_std = float(np.std(_cv_values))
                    _cv_mean = float(np.mean(_cv_values))
                    if _cv_std < 0.05 and 0.45 < _cv_mean < 0.70:
                        self._log(
                            f"WARNING: Zombie stream detected "
                            f"(CV={_cv_mean:.3f}±{_cv_std:.3f}), forcing recovery"
                        )
                        print(
                            f"[mic] Zombie stream (CV={_cv_mean:.3f}±{_cv_std:.3f})",
                            file=sys.stderr, flush=True,
                        )
                        self._zero_streak = 2  # Force recovery path below

        if level < 10 or self._zero_streak >= 2:  # Dead mic OR zombie stream
            if level < 10:
                self._zero_streak += 1
            if self._zero_streak >= 2:  # 2 × 5s = 10s to detect (or forced by zombie check)
                self._silent_recover_count += 1
                _dead_mic_name = self.pa.get_device_info_by_index(self.dev_index)['name']
                # Exponential backoff: 1s, 5s, 15s, 30s, 60s, ... (cap 60s)
                _backoff = min(60, max(1, 5 * (2 ** (self._silent_recover_count - 2))))
                self._log(
                    f"WARNING: Silent mic detected ({_dead_mic_name}), re-scanning devices... "
                    f"(attempt {self._silent_recover_count}, backoff {_backoff}s)"
                )
                print(
                    f"[mic] Silent mic detected ({_dead_mic_name}), re-scanning...",
                    file=sys.stderr, flush=True,
                )
                self._hud_send({"type": "error", "text": f"Mic silent: {_dead_mic_name}"})
                self._zero_streak = 0
                self._mic_pinned = False  # Allow priority-based re-selection
                self._recover_silent_mic(_dead_mic_name, _backoff)
        else:
            self._zero_streak = 0

    def _recover_silent_mic(self, dead_mic_name: str, backoff: float) -> None:
        """Internal: close stream, sleep backoff, try to re-open a working mic.

        Called only from health_check when a silent/zombie mic is confirmed.
        Updates self.pa, self.stream, self.dev_index, self.dev_name.
        Sets ctx.zombie_mic_reinit = True if no mic is found, so the main loop
        can handle the retry.
        """
        mic_priority = self.config.mic_priority if self.config else None
        sample_rate = self.config.audio.sample_rate if self.config else 16000
        chunk_size = self.config.audio.chunk_size if self.config else 1280

        try:
            self.stream.stop_stream()
            self.stream.close()
        except Exception:
            pass
        self.pa.terminate()
        time.sleep(backoff)
        self.pa = pyaudio.PyAudio()

        # Mark the dead device in cooldown so hotplug scan won't re-select it
        add_device_cooldown(dead_mic_name)
        # Exclude the dead device to try alternatives first
        _exclude_prio = [
            p for p in (mic_priority or [])
            if p.lower() not in dead_mic_name.lower()
        ]
        dev_index = find_best_mic(
            self.pa,
            mic_priority=_exclude_prio or mic_priority,
            sample_rate=sample_rate,
            chunk_size=chunk_size,
            require_audio=True,
        )
        if dev_index is None:
            dev_index = find_best_mic(
                self.pa,
                mic_priority=mic_priority,
                sample_rate=sample_rate,
                chunk_size=chunk_size,
                require_audio=True,
            )
        if dev_index is None:
            self._log(f"No mic after reinit, retrying in {backoff}s...")
            time.sleep(backoff)
            self.ctx.zombie_mic_reinit = True
            return

        self.dev_index = dev_index
        self.dev_name = self.pa.get_device_info_by_index(dev_index)['name']
        self._log(f"Mic recovered: [{dev_index}] {self.dev_name}")
        print(f"[mic] Recovered: [{dev_index}] {self.dev_name}", file=sys.stderr, flush=True)
        if self.dev_name != dead_mic_name:
            self._silent_recover_count = 0  # Reset backoff on successful switch
        self.stream = open_mic_stream(
            self.pa, dev_index,
            sample_rate=sample_rate,
            chunk_size=chunk_size,
        )
        self.headset_mode = detect_headset(self.pa, dev_index)
        self._write_active_mic(self.dev_name)
        device_change_cue(self.dev_name, "input")
        self._hud_send({"type": "state", "text": f"Mic: {self.dev_name}"})
        self._health_cv_history.clear()

    # -------------------------------------------------------------------------
    # Dead mic timeout (AUDIO-13)
    # -------------------------------------------------------------------------

    def check_dead_mic_timeout(self) -> None:
        """Set zombie_mic_reinit if no real audio has been seen for DEAD_MIC_TIMEOUT seconds.

        Called on every main loop iteration (including during recording).  Does
        nothing if ctx.last_good_audio_time is zero (not yet initialised) or if
        the reinit flag is already set.
        """
        if not self.ctx.last_good_audio_time:
            return
        if self.ctx.zombie_mic_reinit:
            return
        dead_secs = time.time() - self.ctx.last_good_audio_time
        if dead_secs > self._DEAD_MIC_TIMEOUT:
            self._log(
                f"WARNING: No good audio for {dead_secs:.0f}s, "
                f"forcing mic reinit (AUDIO-13)"
            )
            print(
                f"[mic] Dead mic timeout ({dead_secs:.0f}s silence), forcing reinit...",
                file=sys.stderr, flush=True,
            )
            self.ctx.zombie_mic_reinit = True

    # -------------------------------------------------------------------------
    # IOError recovery
    # -------------------------------------------------------------------------

    def handle_io_error(self) -> bool:
        """Recover from a stream IOError (e.g. USB mic disconnected).

        Closes the broken stream, recreates PyAudio, and finds a new mic.
        Updates self.pa, self.stream, self.dev_index, self.dev_name.

        Returns:
            True if recovery succeeded, False if no mic could be found.
        """
        mic_priority = self.config.mic_priority if self.config else None
        sample_rate = self.config.audio.sample_rate if self.config else 16000
        chunk_size = self.config.audio.chunk_size if self.config else 1280

        self._log("Mic appears disconnected, searching for new mic...")
        self._mic_pinned = False  # Pinned device gone — allow priority-based selection
        try:
            self.stream.stop_stream()
            self.stream.close()
        except Exception:
            pass
        self.pa.terminate()
        time.sleep(0.5)

        self.pa = pyaudio.PyAudio()
        dev_index = find_best_mic(
            self.pa,
            mic_priority=mic_priority,
            sample_rate=sample_rate,
            chunk_size=chunk_size,
        )
        if dev_index is None:
            self._log("No mic found, retrying in 2s...")
            time.sleep(2)
            return False

        self.dev_index = dev_index
        self.dev_name = self.pa.get_device_info_by_index(dev_index)['name']
        self._log(f"Switched to: [{dev_index}] {self.dev_name}")
        self.stream = open_mic_stream(
            self.pa, dev_index,
            sample_rate=sample_rate,
            chunk_size=chunk_size,
        )
        self._write_active_mic(self.dev_name)
        device_change_cue(self.dev_name, "input")
        self._hud_send({"type": "state", "text": f"Mic: {self.dev_name}"})
        return True

    # -------------------------------------------------------------------------
    # Hotplug scan
    # -------------------------------------------------------------------------

    def scan(self) -> None:
        """Check for device hotplug events (only when idle — not recording/busy).

        Runs at most once per _DEVICE_SCAN_INTERVAL seconds.  Checks:
        - Whether a higher-priority mic has become available.
        - Whether the user requested a manual mic switch via HUD menu.
        - Whether the default output device changed (AUDIO-11).
        """
        if self.ctx.is_recording or self.ctx.busy:
            return
        now = time.time()
        if now - self._last_device_scan < self._DEVICE_SCAN_INTERVAL:
            return
        self._last_device_scan = now

        mic_priority = self.config.mic_priority if self.config else None
        sample_rate = self.config.audio.sample_rate if self.config else 16000
        chunk_size = self.config.audio.chunk_size if self.config else 1280

        try:
            # PortAudio caches the device list — create a temporary instance
            # to discover newly connected devices (e.g. USB/Bluetooth hotplug).
            _dead_names = get_dead_input_device_names()
            _scan_pa = pyaudio.PyAudio()
            try:
                current_count = _scan_pa.get_device_count()
                current_names: set[str] = set()
                for _di in range(current_count):
                    try:
                        _info = _scan_pa.get_device_info_by_index(_di)
                        if (
                            _info['maxInputChannels'] > 0
                            and _info['name'].lower() not in _dead_names
                        ):
                            current_names.add(_info['name'])
                    except Exception:
                        pass
            finally:
                _scan_pa.terminate()

            # If mic is pinned but the pinned device disappeared, unpin
            if self._mic_pinned and not any(
                self.dev_name.lower() in n.lower() or n.lower() in self.dev_name.lower()
                for n in current_names
            ):
                self._log(
                    f"Pinned mic '{self.dev_name}' disappeared — unpinning, "
                    f"reverting to priority list"
                )
                self._mic_pinned = False

            # Check if a higher-priority device is available but not selected.
            better_available = False
            for prio_name in (mic_priority if not self._mic_pinned else []):
                matching = [
                    n for n in current_names
                    if prio_name.lower() in n.lower()
                    and not is_device_cooled_down(n)
                ]
                if matching:
                    if prio_name.lower() in self.dev_name.lower():
                        break  # Already using this priority level or higher
                    better_available = True
                    self._log(
                        f"Higher-priority mic detected: {matching[0]} "
                        f"(current: {self.dev_name})"
                    )
                    break

            if better_available:
                try:
                    self.stream.stop_stream()
                    self.stream.close()
                except Exception:
                    pass
                self.pa.terminate()
                self.pa = pyaudio.PyAudio()
                dev_index = find_best_mic(
                    self.pa,
                    mic_priority=mic_priority,
                    sample_rate=sample_rate,
                    chunk_size=chunk_size,
                    require_audio=True,
                )
                if dev_index is not None:
                    self.dev_index = dev_index
                    self.dev_name = self.pa.get_device_info_by_index(dev_index)['name']
                    self._log(f"Switched to: [{dev_index}] {self.dev_name}")
                    self.stream = open_mic_stream(
                        self.pa, dev_index,
                        sample_rate=sample_rate,
                        chunk_size=chunk_size,
                    )
                    self.headset_mode = detect_headset(self.pa, dev_index)
                    self._log(f"Headset mode: {self.headset_mode}")
                    self._write_active_mic(self.dev_name)
                    device_change_cue(self.dev_name, "input")
                    self._hud_send({"type": "state", "text": f"Mic: {self.dev_name}"})

            # Check for manual mic switch request from HUD menu
            if os.path.exists(MIC_SWITCH_REQUEST_FILE):
                try:
                    with open(MIC_SWITCH_REQUEST_FILE) as f:
                        requested_name = f.read().strip()
                    os.unlink(MIC_SWITCH_REQUEST_FILE)
                    if requested_name:
                        self._log(f"Mic switch requested from menu: {requested_name}")
                        target_index = None
                        for n in current_names:
                            if requested_name.lower() in n.lower():
                                _scan2 = pyaudio.PyAudio()
                                try:
                                    for _di2 in range(_scan2.get_device_count()):
                                        try:
                                            _d2 = _scan2.get_device_info_by_index(_di2)
                                            if _d2['name'] == n and _d2['maxInputChannels'] > 0:
                                                target_index = _di2
                                                break
                                        except Exception:
                                            pass
                                finally:
                                    _scan2.terminate()
                                break
                        if target_index is not None:
                            try:
                                self.stream.stop_stream()
                                self.stream.close()
                            except Exception:
                                pass
                            self.pa.terminate()
                            self.pa = pyaudio.PyAudio()
                            self.dev_index = target_index
                            self.dev_name = self.pa.get_device_info_by_index(target_index)['name']
                            self._log(
                                f"Switched to: [{target_index}] {self.dev_name} (pinned)"
                            )
                            self.stream = open_mic_stream(
                                self.pa, target_index,
                                sample_rate=sample_rate,
                                chunk_size=chunk_size,
                            )
                            self.headset_mode = detect_headset(self.pa, target_index)
                            self._mic_pinned = True  # Suppress auto-switch back to priority mic
                            self._write_active_mic(self.dev_name)
                            device_change_cue(self.dev_name, "input")
                            self._hud_send({"type": "state", "text": f"Mic: {self.dev_name}"})
                        else:
                            self._log(f"Requested mic not found: {requested_name}")
                except Exception as e:
                    self._log(f"Mic switch request error: {e}")

            # Check if the default output device changed (AUDIO-11)
            try:
                _cur_out = self.pa.get_default_output_device_info()
                _cur_out_name = _cur_out['name']
                if _cur_out_name != self._last_output_device and self._last_output_device:
                    self._log(
                        f"Output device changed: {self._last_output_device} → {_cur_out_name}"
                    )
                    device_change_cue(_cur_out_name, "output")
                    self._hud_send({"type": "state", "text": f"Speaker: {_cur_out_name}"})
                self._last_output_device = _cur_out_name
            except Exception:
                pass

        except Exception:
            pass  # Don't crash the main loop on scan errors

    # -------------------------------------------------------------------------
    # Cleanup
    # -------------------------------------------------------------------------

    def cleanup(self) -> None:
        """Stop and close the audio stream, terminate PyAudio.

        Called on shutdown. Safe to call even if init() was never completed.
        """
        try:
            if self.stream is not None:
                self.stream.stop_stream()
                self.stream.close()
        except Exception:
            pass
        try:
            if self.pa is not None:
                self.pa.terminate()
        except Exception:
            pass
        self.stream = None
        self.pa = None
