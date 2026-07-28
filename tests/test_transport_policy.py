"""Transport-aware mic policy + PA-corruption recovery tests — DEF-208/209/210.

DEF-208: USB transports get short, capped cooldowns and a same-device fast
         retry; Bluetooth keeps the escalating BT-era tiers.
DEF-209: -9986 (paInternalError) storms across devices are detected so the
         daemon can self-restart (in-process PA recovery is proven dead).
DEF-210: a separate supervisor thread force-restarts when the main loop
         stops iterating (all other watchdogs live inside the loop).

References: .planning/DEFECT-LOG.md,
.planning/quick/260710-mic-selection-usb-rebuild/HANDOVER.md
"""

import threading
import time
import types
from unittest.mock import MagicMock

import pytest

import heyvox.audio.bt as bt
import heyvox.audio.mic as mic


@pytest.fixture(autouse=True)
def _reset_mic_state():
    """Clear cooldowns, storm tally, and transport cache before/after each test."""
    mic.clear_device_cooldowns()
    mic.record_pa_open_success()
    mic._transport_cache.clear()
    mic._transport_cache_ts = 0.0
    yield
    mic.clear_device_cooldowns()
    mic.record_pa_open_success()
    mic._transport_cache.clear()
    mic._transport_cache_ts = 0.0


# ---------------------------------------------------------------------------
# DEF-208: transport-aware cooldown tiers
# ---------------------------------------------------------------------------

def test_usb_cooldown_tiers_capped_at_60s(monkeypatch):
    """A USB device must never escalate into the 30min BT-era demotion."""
    monkeypatch.setattr(mic, "is_usb_transport", lambda name: True)
    key = "usb headset"
    for count, expected in [(1, 15), (2, 30), (3, 60), (99, 60)]:
        mic._device_failure_counts[key] = count
        assert mic._get_adaptive_cooldown(key) == expected, f"failure #{count}"


def test_bt_cooldown_tiers_keep_escalation(monkeypatch):
    """Bluetooth keeps the original escalating tiers (fail-and-fail-again)."""
    monkeypatch.setattr(mic, "is_usb_transport", lambda name: False)
    key = "bt headset"
    for count, expected in [(1, 120), (2, 300), (3, 600), (4, 1800), (99, 1800)]:
        mic._device_failure_counts[key] = count
        assert mic._get_adaptive_cooldown(key) == expected, f"failure #{count}"


def test_unknown_transport_uses_bt_tiers(monkeypatch):
    """A device absent from the live HAL (transport 0) gets the conservative tiers."""
    monkeypatch.setattr(mic, "get_device_transport", lambda name: 0)
    key = "mystery device"
    mic._device_failure_counts[key] = 4
    assert mic._get_adaptive_cooldown(key) == 1800


# ---------------------------------------------------------------------------
# DEF-217: sticky transport cache
# ---------------------------------------------------------------------------

_USB_FOURCC = mic._kAudioDeviceTransportTypeUSB
_BT_FOURCC = int.from_bytes(b"blue", "big")


def _force_cache_refresh():
    mic._transport_cache_ts = 0.0


def test_transport_sticky_when_device_momentarily_invisible(monkeypatch):
    """A USB device missing from one enumeration (blip) keeps its last-known
    transport — it must NOT degrade to unknown/BT-era tiers (DEF-217)."""
    monkeypatch.setattr(
        mic, "_enumerate_coreaudio_inputs",
        lambda: [("G535 Wireless Gaming Headset", True, _USB_FOURCC)],
    )
    _force_cache_refresh()
    assert mic.is_usb_transport("G535 Wireless Gaming Headset")

    monkeypatch.setattr(mic, "_enumerate_coreaudio_inputs", lambda: [])
    _force_cache_refresh()
    assert mic.is_usb_transport("G535 Wireless Gaming Headset"), (
        "momentary absence from the enumeration must not drop the transport"
    )


def test_transport_fresh_value_wins_over_sticky(monkeypatch):
    """Same device name re-appearing on a different transport updates
    immediately — only ABSENCE is sticky, never a conflicting live value."""
    monkeypatch.setattr(
        mic, "_enumerate_coreaudio_inputs",
        lambda: [("G435 Wireless Gaming Headset", True, _USB_FOURCC)],
    )
    _force_cache_refresh()
    assert mic.is_usb_transport("G435 Wireless Gaming Headset")

    monkeypatch.setattr(
        mic, "_enumerate_coreaudio_inputs",
        lambda: [("G435 Wireless Gaming Headset", True, _BT_FOURCC)],
    )
    _force_cache_refresh()
    assert not mic.is_usb_transport("G435 Wireless Gaming Headset")
    assert mic.get_device_transport("G435 Wireless Gaming Headset") == _BT_FOURCC


def test_transport_kept_on_enumeration_failure(monkeypatch):
    """A CoreAudio failure keeps the last-known map instead of wiping it."""
    monkeypatch.setattr(
        mic, "_enumerate_coreaudio_inputs",
        lambda: [("G535 Wireless Gaming Headset", True, _USB_FOURCC)],
    )
    _force_cache_refresh()
    assert mic.is_usb_transport("G535 Wireless Gaming Headset")

    def _boom():
        raise RuntimeError("CoreAudio unavailable")

    monkeypatch.setattr(mic, "_enumerate_coreaudio_inputs", _boom)
    _force_cache_refresh()
    assert mic.is_usb_transport("G535 Wireless Gaming Headset")


# ---------------------------------------------------------------------------
# DEF-209: -9986 storm detection
# ---------------------------------------------------------------------------

def _err9986():
    return OSError(-9986, "Internal PortAudio error")


def test_storm_requires_min_failures():
    for i in range(5):
        mic.record_pa_open_failure(f"dev-{i % 2}", _err9986())
    assert not mic.pa_storm_detected()
    mic.record_pa_open_failure("dev-0", _err9986())
    assert mic.pa_storm_detected()


def test_storm_requires_multiple_devices():
    """One flaky device is a cooldown problem, not a context corruption."""
    for _ in range(10):
        mic.record_pa_open_failure("only-dev", _err9986())
    assert not mic.pa_storm_detected()


def test_storm_cleared_by_any_successful_open():
    for i in range(6):
        mic.record_pa_open_failure(f"dev-{i % 2}", _err9986())
    assert mic.pa_storm_detected()
    mic.record_pa_open_success()
    assert not mic.pa_storm_detected()


def test_storm_ignores_non_9986_errors():
    for i in range(10):
        mic.record_pa_open_failure(f"dev-{i % 3}", OSError(-9999, "device unavailable"))
        mic.record_pa_open_failure(f"dev-{i % 3}", ValueError("something else"))
    assert not mic.pa_storm_detected()


def test_storm_string_errno_fallback():
    """Exceptions without .errno still count when -9986 is in the message."""
    for i in range(6):
        mic.record_pa_open_failure(
            f"dev-{i % 2}", RuntimeError("[Errno -9986] Internal PortAudio error")
        )
    assert mic.pa_storm_detected()


def test_storm_window_expiry():
    """Failures older than the window must not keep the storm alive."""
    for i in range(6):
        mic.record_pa_open_failure(f"dev-{i % 2}", _err9986())
    assert mic.pa_storm_detected()
    with mic._pa_storm_lock:
        mic._pa_storm_events[:] = [
            (ts - mic._PA_STORM_WINDOW_SECS - 1.0, dev)
            for ts, dev in mic._pa_storm_events
        ]
    assert not mic.pa_storm_detected()


def test_open_mic_stream_records_9986_failure():
    """open_mic_stream must feed the storm detector on -9986."""
    pa = MagicMock()
    pa.open.side_effect = _err9986()
    pa.get_device_info_by_index.return_value = {"name": "Storm Device"}
    for i in range(6):
        pa.get_device_info_by_index.return_value = {"name": f"Storm Device {i % 2}"}
        with pytest.raises(OSError):
            mic.open_mic_stream(pa, i % 2)
    assert mic.pa_storm_detected()


def test_open_mic_stream_success_resets_storm():
    pa_bad = MagicMock()
    pa_bad.open.side_effect = _err9986()
    pa_bad.get_device_info_by_index.return_value = {"name": "Storm Device"}
    for i in range(6):
        pa_bad.get_device_info_by_index.return_value = {"name": f"d{i % 2}"}
        with pytest.raises(OSError):
            mic.open_mic_stream(pa_bad, 0)
    assert mic.pa_storm_detected()

    pa_good = MagicMock()
    pa_good.open.return_value = MagicMock()
    mic.open_mic_stream(pa_good, 0)
    assert not mic.pa_storm_detected()


# ---------------------------------------------------------------------------
# DEF-210: wedge supervisor
# ---------------------------------------------------------------------------

def test_wedge_supervisor_fires_on_stale_heartbeat(monkeypatch):
    import heyvox.main as main

    calls = []
    monkeypatch.setattr(main, "_WEDGE_RESTART_SECS", 0.1)
    monkeypatch.setattr(main, "_WEDGE_CHECK_INTERVAL", 0.05)
    monkeypatch.setattr(main, "_release_singleton", lambda: calls.append("release"))
    monkeypatch.setattr(main.os, "execv", lambda *a: calls.append("execv"))

    ctx = types.SimpleNamespace(shutdown=threading.Event())
    heartbeat = {"ts": time.time() - 999.0}
    main._start_wedge_supervisor(
        heartbeat, ctx, log=lambda m: None, hud_send=lambda m: None
    )
    deadline = time.time() + 2.0
    while "execv" not in calls and time.time() < deadline:
        time.sleep(0.02)
    ctx.shutdown.set()

    assert "execv" in calls
    assert "release" in calls


def test_wedge_supervisor_quiet_when_heartbeat_fresh(monkeypatch):
    import heyvox.main as main

    calls = []
    monkeypatch.setattr(main, "_WEDGE_RESTART_SECS", 10.0)
    monkeypatch.setattr(main, "_WEDGE_CHECK_INTERVAL", 0.05)
    monkeypatch.setattr(main, "_release_singleton", lambda: calls.append("release"))
    monkeypatch.setattr(main.os, "execv", lambda *a: calls.append("execv"))

    ctx = types.SimpleNamespace(shutdown=threading.Event())
    heartbeat = {"ts": time.time()}
    main._start_wedge_supervisor(
        heartbeat, ctx, log=lambda m: None, hud_send=lambda m: None
    )
    time.sleep(0.3)
    ctx.shutdown.set()

    assert calls == []


def test_wedge_supervisor_exits_on_shutdown(monkeypatch):
    """The supervisor must not fire once shutdown is set (clean exit path)."""
    import heyvox.main as main

    calls = []
    monkeypatch.setattr(main, "_WEDGE_RESTART_SECS", 0.01)
    monkeypatch.setattr(main, "_WEDGE_CHECK_INTERVAL", 0.05)
    monkeypatch.setattr(main, "_release_singleton", lambda: calls.append("release"))
    monkeypatch.setattr(main.os, "execv", lambda *a: calls.append("execv"))

    ctx = types.SimpleNamespace(shutdown=threading.Event())
    ctx.shutdown.set()  # already shutting down before the first check
    heartbeat = {"ts": time.time() - 999.0}
    main._start_wedge_supervisor(
        heartbeat, ctx, log=lambda m: None, hud_send=lambda m: None
    )
    time.sleep(0.2)

    assert calls == []


# ---------------------------------------------------------------------------
# DEF-208: DeviceManager USB same-device retry
# ---------------------------------------------------------------------------

_G535 = "G535 Wireless Gaming Headset"


def _make_dm(devices):
    from heyvox.device_manager import DeviceManager
    from heyvox.app_context import AppContext

    dm = DeviceManager(
        ctx=AppContext(), config=None, log_fn=lambda m: None,
        hud_send=lambda m: None,
    )
    pa = MagicMock()
    pa.get_device_count.return_value = len(devices)
    pa.get_device_info_by_index.side_effect = lambda i: devices[i]
    dm.pa = pa
    dm.stream = MagicMock()
    dm.dev_index = 0
    dm.dev_name = _G535
    return dm


def _patch_retry_deps(monkeypatch, *, usb=True, live=True, level=50):
    import heyvox.device_manager as dmod

    monkeypatch.setattr(dmod, "is_usb_transport", lambda n: usb)
    monkeypatch.setattr(
        dmod, "get_live_input_device_names",
        lambda: {_G535.lower()} if live else set(),
    )
    monkeypatch.setattr(dmod, "probe_device_level", lambda *a, **k: level)
    monkeypatch.setattr(dmod, "detect_headset", lambda pa, idx: True)
    monkeypatch.setattr(dmod, "open_mic_stream", lambda pa, idx, **k: MagicMock())
    return dmod


def test_usb_retry_reopens_same_device(monkeypatch):
    _patch_retry_deps(monkeypatch)
    dm = _make_dm([{"name": _G535, "maxInputChannels": 1}])
    assert dm._usb_same_device_retry(_G535, 16000, 1280) is True
    assert dm.dev_name == _G535
    assert dm.stream is not None
    assert not mic.is_device_cooled_down(_G535)


def test_usb_retry_declines_non_usb_transport(monkeypatch):
    _patch_retry_deps(monkeypatch, usb=False)
    dm = _make_dm([{"name": _G535, "maxInputChannels": 1}])
    assert dm._usb_same_device_retry(_G535, 16000, 1280) is False


def test_usb_retry_declines_when_device_gone_from_hal(monkeypatch):
    _patch_retry_deps(monkeypatch, live=False)
    dm = _make_dm([{"name": _G535, "maxInputChannels": 1}])
    assert dm._usb_same_device_retry(_G535, 16000, 1280) is False


def test_usb_retry_declines_when_still_silent(monkeypatch):
    _patch_retry_deps(monkeypatch, level=0)
    dm = _make_dm([{"name": _G535, "maxInputChannels": 1}])
    assert dm._usb_same_device_retry(_G535, 16000, 1280) is False


def test_demotion_banner_surfaces_and_clears(monkeypatch):
    """DEF-208 (ported from the lost DEF-202 build): landing on the built-in
    mic despite a configured non-built-in priority device must surface a
    menu-bar banner; a non-built-in device clears it."""
    from heyvox.hud.surface import HUDSurface

    calls = []
    monkeypatch.setattr(HUDSurface, "banner", classmethod(
        lambda cls, **kw: calls.append(("banner", kw.get("source")))))
    monkeypatch.setattr(HUDSurface, "clear", classmethod(
        lambda cls, source: calls.append(("clear", source))))

    dm = _make_dm([{"name": "MacBook Pro Microphone", "maxInputChannels": 1}])
    dm.dev_name = "MacBook Pro Microphone"
    dm._maybe_surface_demotion([_G535, "MacBook Pro Microphone"])
    assert ("banner", "mic-demoted") in calls

    calls.clear()
    dm.dev_name = _G535
    dm._maybe_surface_demotion([_G535, "MacBook Pro Microphone"])
    assert ("clear", "mic-demoted") in calls

    calls.clear()
    dm.dev_name = "MacBook Pro Microphone"
    dm._maybe_surface_demotion(["MacBook Pro Microphone"])  # nothing to demote from
    assert calls == []


def test_reinit_usb_retry_skips_cooldown_and_demotion(monkeypatch):
    """reinit() on a healthy USB device must re-adopt it — no cooldown, no
    fallback to the built-in mic (the DEF-208 core behavior)."""

    dmod_patched = _patch_retry_deps(monkeypatch)
    dm = _make_dm([{"name": _G535, "maxInputChannels": 1}])
    fresh_pa = dm.pa  # reuse the same mock as the "fresh" post-terminate PA
    monkeypatch.setattr(dmod_patched.pyaudio, "PyAudio", lambda: fresh_pa)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    assert dm.reinit(require_audio=True) is True
    assert dm.dev_name == _G535
    assert not mic.is_device_cooled_down(_G535)


# ---------------------------------------------------------------------------
# DEF-243: A2DP→HFP pop-suppression mute must not fire for non-Bluetooth
# transports — it has no pop to hide and was muting system output on every
# USB probe retry, indefinitely, for a persistently-silent USB mic.
# ---------------------------------------------------------------------------

def _mock_probe_stream():
    stream = MagicMock()
    stream.read.return_value = b"\x00\x00" * 1280
    return stream


def test_probe_device_level_skips_mute_for_usb(monkeypatch):
    monkeypatch.setattr(mic, "is_usb_transport", lambda name: True)
    mute_calls = []
    monkeypatch.setattr("heyvox.herald.coreaudio.is_system_muted", lambda: False)
    monkeypatch.setattr("heyvox.herald.coreaudio.set_system_muted", lambda v: mute_calls.append(v))

    pa = MagicMock()
    pa.open.return_value = _mock_probe_stream()
    mic.probe_device_level(pa, 0, "G435 Wireless Gaming Headset")

    assert mute_calls == [], "confirmed-USB probe must never touch system mute"


def test_probe_device_level_still_mutes_for_non_usb(monkeypatch):
    """Bluetooth (and unknown-transport, per DEF-217's conservative default)
    devices keep the pop-suppression mute."""
    monkeypatch.setattr(mic, "is_usb_transport", lambda name: False)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    mute_calls = []
    monkeypatch.setattr("heyvox.herald.coreaudio.is_system_muted", lambda: False)
    monkeypatch.setattr("heyvox.herald.coreaudio.set_system_muted", lambda v: mute_calls.append(v))

    pa = MagicMock()
    pa.open.return_value = _mock_probe_stream()
    mic.probe_device_level(pa, 0, "G435 Bluetooth")

    assert mute_calls == [True, False]


def test_mute_during_bt_switch_skips_for_non_bluetooth(monkeypatch):
    monkeypatch.setattr(bt, "is_bluetooth_device", lambda name: False)
    mute_calls = []
    monkeypatch.setattr("heyvox.herald.coreaudio.is_system_muted", lambda: False)
    monkeypatch.setattr("heyvox.herald.coreaudio.set_system_muted", lambda v: mute_calls.append(v))

    with bt.mute_output_during_bt_switch("G435 Wireless Gaming Headset"):
        pass

    assert mute_calls == [], "non-Bluetooth device must not trigger the A2DP pop mute"


def test_mute_during_bt_switch_still_mutes_for_bluetooth(monkeypatch):
    monkeypatch.setattr(bt, "is_bluetooth_device", lambda name: True)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    mute_calls = []
    monkeypatch.setattr("heyvox.herald.coreaudio.is_system_muted", lambda: False)
    monkeypatch.setattr("heyvox.herald.coreaudio.set_system_muted", lambda v: mute_calls.append(v))

    with bt.mute_output_during_bt_switch("G435 Bluetooth"):
        pass

    assert mute_calls == [True, False]
