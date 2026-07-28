"""
Bluetooth audio support for heyvox.

All Bluetooth-specific logic lives here so that the core mic pipeline
(mic.py, device_manager.py) stays clean for users on USB/wired/internal mics.

Public API:
  is_bluetooth_device(name)          — check if a named device is BT
  get_bluetooth_input_device_names() — set of lowercase BT mic names from CoreAudio
  mute_output_during_bt_switch(name) — context manager: mute during A2DP→HFP probe
  BtHfpMixin                         — mixin for DeviceManager (HFP state + methods)
"""
import time
from contextlib import contextmanager

import pyaudio

from heyvox.audio._coreaudio import (
    _enumerate_coreaudio_inputs,
    _kAudioDeviceTransportTypeBluetooth,
    _kAudioDeviceTransportTypeBluetoothLE,
)


def get_bluetooth_input_device_names() -> set[str]:
    """Return names (lowercase) of CoreAudio input devices on a Bluetooth
    transport (classic BT or BLE).

    DEF-147: the DEF-104 hotplug self-restart must NEVER fire for a Bluetooth
    mic. A BT-HFP device is chronically "live in CoreAudio but absent from
    PortAudio" as it flaps between A2DP (output-only) and HFP (bidirectional),
    so the DEF-104 detector misreads it as a fresh USB hotplug and restarts the
    daemon — and each restart tears the fragile SCO link apart.

    Returns an empty set if CoreAudio is unavailable (graceful degradation).
    """
    bt_types = {_kAudioDeviceTransportTypeBluetooth, _kAudioDeviceTransportTypeBluetoothLE}
    return {
        name.lower()
        for name, _alive, transport in _enumerate_coreaudio_inputs()
        if transport in bt_types
    }


def is_bluetooth_device(name: str) -> bool:
    """Return True if ``name`` matches a Bluetooth input device in CoreAudio.

    Used as the runtime gate in _try_switch_to_better_mic: BT HFP machinery
    only activates when the target device is actually Bluetooth.
    """
    name_lower = name.lower()
    return any(name_lower in bt_name or bt_name in name_lower
               for bt_name in get_bluetooth_input_device_names())


@contextmanager
def mute_output_during_bt_switch(device_name: str, settle_secs: float = 0.8):
    """Mute system output while opening a BT mic stream.

    A2DP → HFP profile switches emit a pop/static burst. Muting output during
    the stream open (and for settle_secs after) hides that artifact.

    Skipped for built-in mics (no profile switch) and for any device not
    confirmed as Bluetooth (DEF-243) — a USB/wired device never does an
    A2DP→HFP switch, so there is no pop to hide and muting is pure overhead.
    Silently no-op if volume helpers aren't importable (e.g. during CLI-only
    invocation).
    """
    from heyvox.audio.mic import is_builtin_mic
    if is_builtin_mic(device_name) or not is_bluetooth_device(device_name):
        yield
        return

    _was_muted = None
    try:
        from heyvox.herald.coreaudio import is_system_muted, set_system_muted
        _was_muted = is_system_muted()
        if not _was_muted:
            set_system_muted(True)
    except Exception:
        pass

    try:
        yield
    finally:
        if _was_muted is False:
            time.sleep(settle_secs)
            try:
                from heyvox.herald.coreaudio import set_system_muted
                set_system_muted(False)
            except Exception:
                pass


class BtHfpMixin:
    """Mixin that adds Bluetooth A2DP→HFP switching state and methods to
    DeviceManager.

    Requires the host class to provide:
      self._log(msg)
      self._do_mic_switch(name, priority, sample_rate, chunk_size) -> bool
      self._do_manual_pin(name, sample_rate, chunk_size) -> bool
      self.reinit(require_audio, expected) -> bool
      self.dev_name: str
    """

    _bt_hfp_target: str = ""
    _bt_hfp_trigger_time: float = 0.0
    _bt_hfp_attempts: int = 0
    _bt_hfp_pin_mode: bool = False

    _BT_HFP_RETRY_INTERVAL = 2.0
    _BT_HFP_MAX_ATTEMPTS = 5

    def _bt_trigger_hfp_switch(
        self, target_name: str, sample_rate: int, chunk_size: int,
    ) -> None:
        """Briefly open a mic stream on a BT device to trigger A2DP → HFP switch."""
        from heyvox.audio.mic import force_os_default_input
        try:
            _pa = pyaudio.PyAudio()
            try:
                found = False
                for _i in range(_pa.get_device_count()):
                    _d = _pa.get_device_info_by_index(_i)
                    if (target_name.lower() in _d['name'].lower()
                            and _d['maxInputChannels'] > 0):
                        found = True
                        with mute_output_during_bt_switch(target_name):
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
                    with mute_output_during_bt_switch(target_name):
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
        excluded_devices: list[str] | None = None,
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
            return False

        self._bt_hfp_attempts += 1

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
            return self._do_mic_switch(target, mic_priority, sample_rate, chunk_size, excluded_devices)

        if self._bt_hfp_attempts >= self._BT_HFP_MAX_ATTEMPTS:
            target = self._bt_hfp_target
            pin_mode = self._bt_hfp_pin_mode
            self._log(
                f"BT HFP attempt {self._bt_hfp_attempts}/{self._BT_HFP_MAX_ATTEMPTS} "
                f"exhausted after {elapsed:.1f}s — flushing PyAudio HAL cache via reinit"
            )
            self._bt_hfp_target = ""
            self._bt_hfp_pin_mode = False
            if self.reinit(require_audio=True, expected=True):
                if target.lower() in (self.dev_name or "").lower():
                    self._log(
                        f"BT HFP switch completed via post-reinit find_best_mic "
                        f"— now on '{self.dev_name}'"
                    )
                    return True
                if pin_mode:
                    if self._do_manual_pin(target, sample_rate, chunk_size):
                        return True
            self._log(
                f"BT HFP switch failed after {elapsed:.1f}s / "
                f"{self._BT_HFP_MAX_ATTEMPTS} attempts + cache flush "
                f"— keeping current mic"
            )
            return False

        self._log(
            f"BT HFP attempt {self._bt_hfp_attempts}/{self._BT_HFP_MAX_ATTEMPTS} "
            f"— still no input, re-triggering..."
        )
        self._bt_trigger_hfp_switch(self._bt_hfp_target, sample_rate, chunk_size)
        return False
