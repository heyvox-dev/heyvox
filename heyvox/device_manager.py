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
    clear_device_cooldown,
    is_device_cooled_down,
    mute_output_during_bt_switch as _mute_during_bt_switch,
    force_os_default_input,
)
from heyvox.audio.cues import device_change_cue
from heyvox.audio.profile import MicProfileManager, MicProfileEntry
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

    def __init__(
        self,
        ctx: "AppContext",
        config: "HeyvoxConfig",
        log_fn,
        hud_send,
        profile_manager: "MicProfileManager | None" = None,
    ) -> None:
        self.ctx = ctx
        self.config = config
        self._log = log_fn
        self._hud_send = hud_send
        self.profile_manager = profile_manager

        # Device state — private to DeviceManager
        self.pa: pyaudio.PyAudio | None = None
        self.stream = None
        self.dev_index: int | None = None
        self.dev_name: str = ""
        self.headset_mode: bool = False
        self.active_profile: MicProfileEntry | None = None

        # Hotplug / pin state
        self._mic_pinned: bool = False
        self._last_device_scan: float = time.time()
        self._last_output_device: str = ""

        # Health check state
        self._last_health_check: float = time.time()
        self._health_cv_history: deque = deque(maxlen=6)  # Last 30s of CV values
        self._zero_streak: int = 0
        self._silent_recover_count: int = 0  # For exponential backoff
        self._last_resort_until: float = 0.0  # Suppress health checks until this time

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
        if self.profile_manager:
            self.active_profile = self.profile_manager.get_profile(self.dev_name)
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
        try:
            print("[mic] Zombie stream detected, forcing reinit...", file=sys.stderr, flush=True)
        except (BrokenPipeError, OSError):
            pass
        self._hud_send({"type": "error", "text": "Mic zombie: reinitializing"})
        self._mic_pinned = False

        # Remember the failing device so we can detect "same device re-selected"
        # after reinit — find_best_mic clears built-in-mic cooldowns, so the
        # existing last-resort grace period would miss that loop. See DEF-037.
        _prev_dev_name = self.dev_name

        # Cooldown the zombie device so find_best_mic and hotplug scan skip it
        add_device_cooldown(self.dev_name)

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
        _was_last_resort = False
        if dev_index is None:
            dev_index = find_best_mic(
                self.pa,
                mic_priority=mic_priority,
                sample_rate=sample_rate,
                chunk_size=chunk_size,
            )
            _was_last_resort = dev_index is not None  # Got it from non-require_audio fallback
        if dev_index is None:
            self._log("No mic found after zombie reinit, retrying in 2s...")
            time.sleep(2)
            return False

        self.dev_index = dev_index
        self.dev_name = self.pa.get_device_info_by_index(dev_index)['name']

        # If this is a last-resort pick (all others in cooldown), suppress
        # health check recovery for 60s to prevent flap loops.
        if _was_last_resort and is_device_cooled_down(self.dev_name):
            grace = 60
            self._last_resort_until = time.time() + grace
            self._log(
                f"Zombie reinit: last-resort device [{dev_index}] {self.dev_name} "
                f"(in cooldown), suppressing recovery for {grace}s"
            )
            try:
                print(f"[mic] Last-resort fallback: {self.dev_name}, grace {grace}s", file=sys.stderr, flush=True)
            except (BrokenPipeError, OSError):
                pass
        elif _prev_dev_name and self.dev_name.lower() == _prev_dev_name.lower():
            # Same device we just flagged as zombie got re-selected — usually
            # happens when find_best_mic special-cases a built-in mic and
            # clears its cooldown. Reinit won't help if the mic is genuinely
            # silent (input level 0, muted, broken), so back off to avoid a
            # 30-second reinit loop. See DEF-037.
            grace = 120
            self._last_resort_until = time.time() + grace
            self._log(
                f"Zombie reinit: same device [{dev_index}] {self.dev_name} "
                f"re-selected, suppressing recovery for {grace}s"
            )
            try:
                print(
                    f"[mic] Same mic after reinit ({self.dev_name}), grace {grace}s",
                    file=sys.stderr, flush=True,
                )
            except (BrokenPipeError, OSError):
                pass
        else:
            self._log(f"Zombie reinit recovered: [{dev_index}] {self.dev_name}")
            try:
                print(f"[mic] Zombie reinit recovered: [{dev_index}] {self.dev_name}", file=sys.stderr, flush=True)
            except (BrokenPipeError, OSError):
                pass

        self.stream = open_mic_stream(
            self.pa, dev_index,
            sample_rate=sample_rate,
            chunk_size=chunk_size,
        )
        self.headset_mode = detect_headset(self.pa, dev_index)
        if self.profile_manager:
            self.active_profile = self.profile_manager.get_profile(self.dev_name)
        self._write_active_mic(self.dev_name)
        device_change_cue(self.dev_name, "input")
        self._hud_send({"type": "state", "text": f"Mic: {self.dev_name}"})
        self._health_cv_history.clear()
        self.ctx.last_good_audio_time = time.time()  # AUDIO-13: reset timeout
        self.ctx.dead_mic_zero_chunks = 0
        self.ctx.dead_mic_low_chunks = 0
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

        # Last-resort grace period: when we're stuck on the only available
        # device (all others in cooldown), suppress recovery to prevent flapping.
        if now < self._last_resort_until:
            if level >= 10:
                # Real audio detected — device woke up, cancel grace period
                self._last_resort_until = 0.0
                self._zero_streak = 0
                self._silent_recover_count = 0
            return

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
                        try:
                            print(
                                f"[mic] Zombie stream (CV={_cv_mean:.3f}±{_cv_std:.3f})",
                                file=sys.stderr, flush=True,
                            )
                        except BrokenPipeError:
                            pass
                        self._zero_streak = 2  # Force recovery path below

        # Silence (level < 10) means a quiet room, not a dead mic.
        # Only trigger recovery on zombie stream detection (CV check above
        # sets _zero_streak = 2) or actual IOErrors (handled separately).
        if self._zero_streak >= 2:  # Zombie stream forced by CV check
            self._silent_recover_count += 1
            _dead_mic_name = self.pa.get_device_info_by_index(self.dev_index)['name']
            _backoff = min(5, max(1, 5 * (2 ** (self._silent_recover_count - 2))))
            self._log(
                f"WARNING: Zombie stream detected ({_dead_mic_name}), re-scanning devices... "
                f"(attempt {self._silent_recover_count}, backoff {_backoff}s)"
            )
            try:
                print(
                    f"[mic] Zombie stream detected ({_dead_mic_name}), re-scanning...",
                    file=sys.stderr, flush=True,
                )
            except BrokenPipeError:
                pass
            self._hud_send({"type": "error", "text": f"Mic zombie: {_dead_mic_name}"})
            self._zero_streak = 0
            self._mic_pinned = False
            self._recover_silent_mic(_dead_mic_name, _backoff)
        else:
            # Reset recovery backoff when we see real audio.
            if self._silent_recover_count > 0:
                self._silent_recover_count = 0
            # Clear this device's cooldown — it's producing audio, so
            # hotplug scan shouldn't skip it on the next cycle.
            if level >= 10:
                clear_device_cooldown(self.dev_name)

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
            self._log(f"No mic after reinit, retrying in 2s...")
            time.sleep(2)
            self.ctx.zombie_mic_reinit = True
            return

        self.dev_index = dev_index
        new_name = self.pa.get_device_info_by_index(dev_index)['name']

        # If we recovered to the SAME device that was just dead, don't reset
        # backoff — this isn't a real recovery, it's the system default fallback
        # returning the only available device.
        if new_name.lower() == dead_mic_name.lower():
            self._log(f"Mic recovered to SAME device: [{dev_index}] {new_name} (not resetting backoff)")
            try:
                print(f"[mic] Recovered to same device: [{dev_index}] {new_name}", file=sys.stderr, flush=True)
            except (BrokenPipeError, OSError):
                pass
        else:
            self._log(f"Mic recovered: [{dev_index}] {new_name}")
            try:
                print(f"[mic] Recovered: [{dev_index}] {new_name}", file=sys.stderr, flush=True)
            except (BrokenPipeError, OSError):
                pass
            self._silent_recover_count = 0  # Reset backoff only on genuine device switch

        self.dev_name = new_name
        self.stream = open_mic_stream(
            self.pa, dev_index,
            sample_rate=sample_rate,
            chunk_size=chunk_size,
        )
        self.headset_mode = detect_headset(self.pa, dev_index)
        if self.profile_manager:
            self.active_profile = self.profile_manager.get_profile(self.dev_name)
        self._write_active_mic(self.dev_name)
        device_change_cue(self.dev_name, "input")
        self._hud_send({"type": "state", "text": f"Mic: {self.dev_name}"})
        self._health_cv_history.clear()
        self.ctx.last_good_audio_time = time.time()  # AUDIO-13: reset timeout
        self.ctx.dead_mic_zero_chunks = 0
        self.ctx.dead_mic_low_chunks = 0

    # -------------------------------------------------------------------------
    # Dead mic timeout (AUDIO-13)
    # -------------------------------------------------------------------------

    def check_dead_mic_timeout(self) -> None:
        """Set zombie_mic_reinit if no real audio has been seen for DEAD_MIC_TIMEOUT seconds.

        Called on every main loop iteration (including during recording).  Does
        nothing if ctx.last_good_audio_time is zero (not yet initialised) or if
        the reinit flag is already set.

        DEF-051: if the user has explicitly pinned a mic from the HUD menu
        (`_mic_pinned`), AUDIO-13 does not force a reinit. A user-pinned mic
        is a deliberate choice — kicking them off after 30 s of idle silence
        (normal when not speaking) was causing the wireless mic to fall back
        to built-in constantly.
        """
        if not self.ctx.last_good_audio_time:
            return
        if self.ctx.zombie_mic_reinit:
            return
        if time.time() < self._last_resort_until:
            return
        if self._mic_pinned:
            return  # DEF-051: pinned mics are not subject to AUDIO-13
        dead_secs = time.time() - self.ctx.last_good_audio_time
        if dead_secs > self._DEAD_MIC_TIMEOUT:
            zero_n = self.ctx.dead_mic_zero_chunks
            low_n = self.ctx.dead_mic_low_chunks
            total = zero_n + low_n
            diag = (
                f"stream diag: {zero_n}/{total} all-zero chunks, "
                f"{low_n}/{total} low (1-9)"
            ) if total else "stream diag: no chunks observed"
            self._log(
                f"WARNING: No good audio for {dead_secs:.0f}s, "
                f"forcing mic reinit (AUDIO-13) — {diag}"
            )
            try:
                print(
                    f"[mic] Dead mic timeout ({dead_secs:.0f}s silence), forcing reinit... {diag}",
                    file=sys.stderr, flush=True,
                )
            except BrokenPipeError:
                pass
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

        _dead_name = self.dev_name
        self._log(f"Mic appears disconnected ({_dead_name}), searching for new mic...")
        self._mic_pinned = False  # Pinned device gone — allow priority-based selection

        # Cooldown the stalled/dead device so find_best_mic skips it
        add_device_cooldown(_dead_name)

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
            self._log("No mic found, retrying in 2s...")
            time.sleep(2)
            return False

        self.dev_index = dev_index
        self.dev_name = self.pa.get_device_info_by_index(dev_index)['name']
        self._log(f"Switched to: [{dev_index}] {self.dev_name}")

        # If the only available device is in cooldown (last resort), suppress
        # health check recovery for 60s to prevent flap loops.
        if is_device_cooled_down(self.dev_name):
            grace = 60
            self._last_resort_until = time.time() + grace
            self._log(f"IO recovery: last-resort device (in cooldown), suppressing recovery for {grace}s")

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
    # Bluetooth HFP wait + mic switch
    # -------------------------------------------------------------------------

    # Non-blocking BT HFP retry state (checked each scan cycle instead of blocking)
    _bt_hfp_target: str = ""        # device name we're waiting for
    _bt_hfp_trigger_time: float = 0 # when we first triggered the HFP switch
    _bt_hfp_attempts: int = 0       # how many re-checks so far
    _bt_hfp_pin_mode: bool = False  # True = user-requested pin (HUD menu);
                                    # False = priority auto-switch

    _BT_HFP_RETRY_INTERVAL = 2.0   # seconds between re-checks
    _BT_HFP_MAX_ATTEMPTS = 5       # give up after this many (Jabras need more time than G435)

    def _try_switch_to_better_mic(
        self,
        target_name: str,
        current_input_names: set[str],
        mic_priority: list[str] | None,
        sample_rate: int,
        chunk_size: int,
    ) -> bool:
        """Try to switch to a higher-priority mic. Handles Bluetooth A2DP→HFP.

        For Bluetooth devices that only show an output device (A2DP mode), we
        briefly open a mic stream to trigger the profile switch, then return
        False. On subsequent scan() calls, _continue_bt_hfp_wait() re-checks
        whether the input device has appeared (non-blocking).

        Returns True if the switch succeeded, False otherwise.
        """
        has_input = any(
            target_name.lower() in n.lower()
            for n in current_input_names
        )

        if not has_input:
            # A2DP mode — trigger HFP switch and schedule non-blocking retries
            self._log(f"BT device '{target_name}' has no input yet (A2DP), triggering HFP switch...")
            self._bt_trigger_hfp_switch(target_name, sample_rate, chunk_size)
            self._bt_hfp_target = target_name
            self._bt_hfp_trigger_time = time.time()
            self._bt_hfp_attempts = 0
            return False

        # Input device exists — proceed with actual switch
        return self._do_mic_switch(target_name, mic_priority, sample_rate, chunk_size)

    def _bt_trigger_hfp_switch(
        self, target_name: str, sample_rate: int, chunk_size: int,
    ) -> None:
        """Briefly open a mic stream on a BT device to trigger A2DP → HFP switch."""
        try:
            _pa = pyaudio.PyAudio()
            try:
                found = False
                for _i in range(_pa.get_device_count()):
                    _d = _pa.get_device_info_by_index(_i)
                    if (target_name.lower() in _d['name'].lower()
                            and _d['maxInputChannels'] > 0):
                        found = True
                        with _mute_during_bt_switch(target_name):
                            try:
                                _s = _pa.open(
                                    format=pyaudio.paInt16, channels=1,
                                    rate=sample_rate, input=True,
                                    input_device_index=_i,
                                    frames_per_buffer=chunk_size,
                                )
                                _s.close()
                            except Exception as probe_err:
                                self._log(
                                    f"BT HFP probe open failed for '{_d['name']}' "
                                    f"(idx={_i}, rate={sample_rate}, chunk={chunk_size}): "
                                    f"{type(probe_err).__name__}: {probe_err}"
                                )
                        break
                if not found:
                    self._log(
                        f"BT HFP probe: no input device matching '{target_name}' in "
                        f"current enumeration (device likely still in A2DP-only mode)"
                    )
                    # Fallback: bypass PyAudio's per-process HAL cache and ask
                    # CoreAudio directly to switch the default input. This is
                    # what nudges macOS to actually engage HFP for the headset
                    # when PortAudio's cached enumeration doesn't yet list an
                    # input entry for it (DEF-060).
                    with _mute_during_bt_switch(target_name):
                        if force_os_default_input(target_name):
                            self._log(
                                f"BT HFP probe: CoreAudio default-input write "
                                f"succeeded for '{target_name}' — HFP negotiation "
                                f"kicked off at the OS layer"
                            )
            finally:
                _pa.terminate()
        except Exception as e:
            self._log(f"BT HFP trigger failed: {e}")

    def _continue_bt_hfp_wait(
        self, mic_priority: list[str] | None, sample_rate: int, chunk_size: int,
    ) -> bool:
        """Non-blocking check: has the BT device switched to HFP yet?

        Called from scan() on each cycle. Returns True if the switch completed
        and mic was switched, False if still waiting or gave up.
        """
        if not self._bt_hfp_target:
            return False

        elapsed = time.time() - self._bt_hfp_trigger_time
        next_check_at = (self._bt_hfp_attempts + 1) * self._BT_HFP_RETRY_INTERVAL

        if elapsed < next_check_at:
            return False  # Not time to check yet

        self._bt_hfp_attempts += 1

        # Re-enumerate and look for input device
        try:
            _pa = pyaudio.PyAudio()
            try:
                has_input = False
                for _i in range(_pa.get_device_count()):
                    _d = _pa.get_device_info_by_index(_i)
                    if (self._bt_hfp_target.lower() in _d['name'].lower()
                            and _d['maxInputChannels'] > 0):
                        has_input = True
                        break
            finally:
                _pa.terminate()
        except Exception as e:
            self._log(f"BT HFP re-check failed: {e}")
            has_input = False

        if has_input:
            self._log(f"BT HFP switch completed after {elapsed:.1f}s — switching mic")
            target = self._bt_hfp_target
            pin_mode = self._bt_hfp_pin_mode
            self._bt_hfp_target = ""
            self._bt_hfp_pin_mode = False
            if pin_mode:
                return self._do_manual_pin(target, sample_rate, chunk_size)
            return self._do_mic_switch(
                target, mic_priority, sample_rate, chunk_size,
            )

        if self._bt_hfp_attempts >= self._BT_HFP_MAX_ATTEMPTS:
            # Last resort: PortAudio caches device enumeration per-process and
            # throwaway pyaudio.PyAudio() instances inherit the stale cache
            # (DEF-060). Flush the long-lived self.pa instance by calling
            # reinit() before giving up — find_best_mic then runs against a
            # fresh HAL snapshot and may pick up the BT input that just
            # became HFP-available.
            target = self._bt_hfp_target
            pin_mode = self._bt_hfp_pin_mode
            self._log(
                f"BT HFP attempt {self._bt_hfp_attempts}/{self._BT_HFP_MAX_ATTEMPTS} "
                f"exhausted after {elapsed:.1f}s — flushing PyAudio HAL cache via reinit"
            )
            self._bt_hfp_target = ""
            self._bt_hfp_pin_mode = False
            # scan() guards on (not is_recording and not busy), so reinit()
            # is safe here without additional checks.
            if self.reinit(require_audio=True):
                # After reinit the device list is fresh; if the BT input
                # landed, self.dev_name will now be the target and we're
                # done. Otherwise emit the original give-up log.
                if target.lower() in (self.dev_name or "").lower():
                    self._log(
                        f"BT HFP switch completed via post-reinit find_best_mic "
                        f"— now on '{self.dev_name}'"
                    )
                    return True
                if pin_mode:
                    # Honour the user pin by trying the explicit switch path.
                    if self._do_manual_pin(target, sample_rate, chunk_size):
                        return True
            self._log(
                f"BT HFP switch failed after {elapsed:.1f}s / "
                f"{self._BT_HFP_MAX_ATTEMPTS} attempts + cache flush "
                f"— keeping current mic, preserving headset_mode={self.headset_mode}"
            )
            return False

        # Re-trigger in case the first attempt didn't stick
        self._log(f"BT HFP attempt {self._bt_hfp_attempts}/{self._BT_HFP_MAX_ATTEMPTS} — still no input, re-triggering...")
        self._bt_trigger_hfp_switch(self._bt_hfp_target, sample_rate, chunk_size)
        return False

    def _do_mic_switch(
        self,
        target_name: str,
        mic_priority: list[str] | None,
        sample_rate: int,
        chunk_size: int,
    ) -> bool:
        """Actually switch to a new mic via find_best_mic. Returns True on success."""
        old_headset_mode = self.headset_mode
        # Probe first with a fresh PyAudio instance — don't close the live stream
        # unless we're actually going to switch. This prevents a periodic click/toc
        # cycle when find_best_mic keeps returning the current device (e.g. because
        # the "better" candidate is deferred).
        _probe_pa = pyaudio.PyAudio()
        try:
            candidate_index = find_best_mic(
                _probe_pa,
                mic_priority=mic_priority,
                sample_rate=sample_rate,
                chunk_size=chunk_size,
                require_audio=True,
            )
            candidate_name = (
                _probe_pa.get_device_info_by_index(candidate_index)['name']
                if candidate_index is not None else None
            )
        finally:
            _probe_pa.terminate()

        if candidate_name and candidate_name == self.dev_name:
            # No actual change — don't close/reopen/cue, stay on current stream
            return False

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
            with _mute_during_bt_switch(self.dev_name):
                self.stream = open_mic_stream(
                    self.pa, dev_index,
                    sample_rate=sample_rate,
                    chunk_size=chunk_size,
                )
            self.headset_mode = detect_headset(self.pa, dev_index)
            if self.profile_manager:
                self.active_profile = self.profile_manager.get_profile(self.dev_name)
            self._log(f"Headset mode: {self.headset_mode}")
            self._write_active_mic(self.dev_name)
            device_change_cue(self.dev_name, "input")
            self._hud_send({"type": "state", "text": f"Mic: {self.dev_name}"})
            return True

        # find_best_mic failed — reopen current device, preserve headset_mode
        self._log(f"Mic switch failed — preserving headset_mode={old_headset_mode}")
        self.headset_mode = old_headset_mode
        try:
            self.stream = open_mic_stream(
                self.pa, self.dev_index,
                sample_rate=sample_rate,
                chunk_size=chunk_size,
            )
        except Exception as e:
            self._log(f"Failed to reopen current mic: {e}")
        return False

    def _do_manual_pin(
        self, requested_name: str, sample_rate: int, chunk_size: int,
    ) -> bool:
        """Open the user-requested mic by name and pin it.

        Used by the HUD-menu manual-switch flow (direct pick) and by
        _continue_bt_hfp_wait when the pending target is a user pin that had
        to wait for BT A2DP→HFP. Re-enumerates devices to get a fresh index
        (indices shift after profile switches).

        Returns True on success, False if the device still isn't available.
        """
        _pa = pyaudio.PyAudio()
        target_index = None
        try:
            for _di in range(_pa.get_device_count()):
                try:
                    _d = _pa.get_device_info_by_index(_di)
                    if (requested_name.lower() in _d['name'].lower()
                            and _d['maxInputChannels'] > 0):
                        target_index = _di
                        break
                except Exception:
                    pass
        finally:
            _pa.terminate()

        if target_index is None:
            self._log(f"Manual pin failed: '{requested_name}' still has no input")
            return False

        try:
            self.stream.stop_stream()
            self.stream.close()
        except Exception:
            pass
        self.pa.terminate()
        self.pa = pyaudio.PyAudio()
        self.dev_index = target_index
        self.dev_name = self.pa.get_device_info_by_index(target_index)['name']
        self._log(f"Switched to: [{target_index}] {self.dev_name} (pinned)")
        with _mute_during_bt_switch(self.dev_name):
            self.stream = open_mic_stream(
                self.pa, target_index,
                sample_rate=sample_rate,
                chunk_size=chunk_size,
            )
        self.headset_mode = detect_headset(self.pa, target_index)
        if self.profile_manager:
            self.active_profile = self.profile_manager.get_profile(self.dev_name)
        self._mic_pinned = True  # Suppress auto-switch back to priority mic
        self._write_active_mic(self.dev_name)
        device_change_cue(self.dev_name, "input")
        self._hud_send({"type": "state", "text": f"Mic: {self.dev_name}"})
        # DEF-051: reset AUDIO-13 timer + diagnostic counters so a just-pinned
        # mic isn't immediately kicked out by a stale dead-mic countdown.
        self.ctx.last_good_audio_time = time.time()
        self.ctx.dead_mic_zero_chunks = 0
        self.ctx.dead_mic_low_chunks = 0
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

        # Check pending BT HFP wait (non-blocking, returns early if nothing pending)
        if self._continue_bt_hfp_wait(mic_priority, sample_rate, chunk_size):
            return  # Switch completed this cycle

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
                better_available = self._try_switch_to_better_mic(
                    matching[0], current_names, mic_priority, sample_rate, chunk_size,
                )

            # Check for manual mic switch request from HUD menu
            if os.path.exists(MIC_SWITCH_REQUEST_FILE):
                requested_name = ""
                try:
                    with open(MIC_SWITCH_REQUEST_FILE) as f:
                        requested_name = f.read().strip()
                    os.unlink(MIC_SWITCH_REQUEST_FILE)
                except Exception as e:
                    self._log(f"Mic switch request error: {e}")

                if requested_name:
                    self._log(f"Mic switch requested from menu: {requested_name}")
                    # If target currently has input channels, switch immediately.
                    # Otherwise it's a BT device in A2DP mode — trigger the
                    # A2DP→HFP profile switch and schedule non-blocking retries
                    # (same flow the priority auto-switch uses).
                    has_input = any(
                        requested_name.lower() in n.lower()
                        for n in current_names
                    )
                    if has_input:
                        self._do_manual_pin(requested_name, sample_rate, chunk_size)
                    else:
                        self._log(
                            f"Requested mic '{requested_name}' has no input yet "
                            f"(likely BT A2DP), triggering HFP switch..."
                        )
                        self._bt_trigger_hfp_switch(requested_name, sample_rate, chunk_size)
                        self._bt_hfp_target = requested_name
                        self._bt_hfp_trigger_time = time.time()
                        self._bt_hfp_attempts = 0
                        self._bt_hfp_pin_mode = True

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
