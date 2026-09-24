"""DEF-256: explicit BT pin that exhausts its HFP wait while CoreAudio shows a
live input (PortAudio cache blind) may bypass the DEF-147 restart guard once."""
import threading
import types

import pytest

pytest.importorskip("pyaudio")

from heyvox import main as hmain


@pytest.fixture
def env(tmp_path, monkeypatch):
    marker = tmp_path / "marker"
    req = tmp_path / "req"
    monkeypatch.setattr(hmain, "HOTPLUG_RESTART_MARKER", str(marker))
    monkeypatch.setattr(hmain, "MIC_SWITCH_REQUEST_FILE", str(req))
    monkeypatch.setattr(hmain.time, "sleep", lambda *_: None)
    monkeypatch.setattr(hmain, "_release_singleton", lambda: None)
    execs = []
    monkeypatch.setattr(hmain.os, "execv", lambda *a: execs.append(a))
    monkeypatch.setattr(
        "heyvox.audio.bt.get_bluetooth_input_device_names",
        lambda: {"jabra elite 7 pro"},
    )
    return types.SimpleNamespace(marker=marker, req=req, execs=execs)


def _call(**kw):
    ctx = types.SimpleNamespace(shutdown=threading.Event())
    logs = []
    hmain._restart_for_hotplug_candidate(
        "Jabra Elite 7 Pro", logs.append, lambda m: None, ctx, 300.0, [], **kw
    )
    return logs


def test_bt_blocked_by_default(env):
    logs = _call()
    assert env.execs == []
    assert any("DEF-147" in line for line in logs)
    assert not env.req.exists()


def test_bt_allowed_for_explicit_pin_and_repins_after_restart(env):
    _call(allow_bluetooth=True)
    assert len(env.execs) == 1
    assert env.req.read_text() == "Jabra Elite 7 Pro"


def test_bt_pin_restart_still_bound_by_cooldown(env):
    _call(allow_bluetooth=True)
    logs = _call(allow_bluetooth=True)
    assert len(env.execs) == 1
    assert any("not looping" in line for line in logs)


def test_hfp_exhaustion_requests_bt_restart_only_for_pin(monkeypatch):
    from heyvox.device_manager import DeviceManager
    dm = DeviceManager.__new__(DeviceManager)
    dm._hotplug_restart_request = None
    dm._hotplug_restart_allow_bt = False
    dm._request_hotplug_restart("Jabra Elite 7 Pro", allow_bluetooth=True)
    assert dm.pop_hotplug_restart_request() == "Jabra Elite 7 Pro"
    assert dm.hotplug_restart_allow_bt is True
    dm._request_hotplug_restart("G435")
    assert dm.hotplug_restart_allow_bt is False
