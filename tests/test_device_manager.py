"""Tests for heyvox.device_manager — DeviceManager class."""
import pytest

def test_device_manager_import():
    from heyvox.device_manager import DeviceManager


def test_device_manager_constructor_accepts_ctx_config_log_hud():
    from heyvox.device_manager import DeviceManager
    from heyvox.app_context import AppContext
    ctx = AppContext()
    dm = DeviceManager(ctx=ctx, config=None, log_fn=print, hud_send=lambda msg: None)
    assert dm.ctx is ctx


def test_device_manager_has_required_methods():
    from heyvox.device_manager import DeviceManager
    assert callable(getattr(DeviceManager, 'init', None))
    assert callable(getattr(DeviceManager, 'scan', None))
    assert callable(getattr(DeviceManager, 'reinit', None))
    assert callable(getattr(DeviceManager, 'health_check', None))
    assert callable(getattr(DeviceManager, 'cleanup', None))


def test_device_manager_initial_state():
    from heyvox.device_manager import DeviceManager
    from heyvox.app_context import AppContext
    ctx = AppContext()
    dm = DeviceManager(ctx=ctx, config=None, log_fn=print, hud_send=lambda msg: None)
    assert dm.pa is None
    assert dm.stream is None
    assert dm.dev_index is None
    assert dm.headset_mode is False
