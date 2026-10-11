"""Guard tests for DEF-265/266/267 (slow dictation: MLX memory + Orca paste path).

1. DEF-266: the garbled-STT reverify borrowed mlx_whisper's single ModelHolder slot for
   large-v3 and never gave it back — the next dictation cold-reloaded the live
   model while the log said warm=True, and ~4 GB of freed buffers stayed cached.
2. DEF-265: the idle unload "freed" the model via importlib.reload(mlx_whisper), which
   leaves the transcribe submodule (and ModelHolder) untouched.
3. DEF-265: the memory watchdog measured RSS, which does not see Metal buffers.
4. DEF-267: Orca fell off the AX fast-path once its workspace provider set workspace_id.
"""
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from heyvox.audio import stt
from heyvox.input.injection import _ax_inject_text, type_text


@pytest.fixture
def fake_mlx_whisper(monkeypatch):
    """mlx_whisper stand-in whose transcribe() loads into a one-slot holder."""
    holder = type("ModelHolder", (), {"model": "live-model", "model_path": "live-path"})
    calls = []

    def transcribe(audio, **kwargs):
        calls.append(kwargs["path_or_hf_repo"])
        if kwargs.get("fail"):
            raise RuntimeError("decode failed")
        holder.model = f"model:{kwargs['path_or_hf_repo']}"
        holder.model_path = kwargs["path_or_hf_repo"]
        return {"text": "ok"}

    pkg = types.ModuleType("mlx_whisper")
    pkg.transcribe = transcribe
    sub = types.ModuleType("mlx_whisper.transcribe")
    sub.ModelHolder = holder
    monkeypatch.setitem(sys.modules, "mlx_whisper", pkg)
    monkeypatch.setitem(sys.modules, "mlx_whisper.transcribe", sub)
    released = []
    monkeypatch.setattr(stt, "_release_mlx_cache", lambda: released.append(True))
    return holder, calls, released


def test_reverify_puts_live_model_back(fake_mlx_whisper):
    holder, calls, released = fake_mlx_whisper
    result = stt.transcribe_with_other_model(
        [0.0], path_or_hf_repo="mlx-community/whisper-large-v3-mlx",
    )
    assert result == {"text": "ok"}
    assert calls == ["mlx-community/whisper-large-v3-mlx"]
    assert (holder.model, holder.model_path) == ("live-model", "live-path")
    assert released, "the borrowed model's buffers must leave MLX's cache"


def test_reverify_restores_on_failure(fake_mlx_whisper):
    holder, _calls, released = fake_mlx_whisper
    with pytest.raises(RuntimeError):
        stt.transcribe_with_other_model([0.0], path_or_hf_repo="big", fail=True)
    assert (holder.model, holder.model_path) == ("live-model", "live-path")
    assert released
    assert not stt._holder_lock.locked()


def test_idle_unload_drops_the_cached_model(fake_mlx_whisper, monkeypatch):
    holder, _calls, released = fake_mlx_whisper
    monkeypatch.setattr(stt, "_mlx_transcribing", False)
    monkeypatch.setattr(stt, "_mlx_last_use", 0.0)
    monkeypatch.setattr(stt, "_mlx_unload_secs", 1.0)
    stt._mlx_loaded.set()
    try:
        stt._unload_mlx_model()
        assert holder.model is None and holder.model_path is None
        assert not stt._mlx_loaded.is_set()
        assert released
    finally:
        stt._mlx_loaded.clear()


def test_process_footprint_is_measured():
    if sys.platform != "darwin":
        pytest.skip("proc_pid_rusage is macOS-only")
    from heyvox.ram_pressure import process_footprint_mb

    mb = process_footprint_mb()
    assert mb is not None and mb > 1


def _workspace_snap():
    snap = MagicMock()
    snap.leaf_role = "AXTextArea"
    snap.app_pid = 1234
    snap.app_name = "Orca"
    snap.workspace_id = "wt-1::/repo"
    snap.leaf_description = "Send a message…"
    snap.app_bundle_id = "com.stablyai.orca"
    return snap


def _ax_mock(store):
    focused = object()

    def _copy(el, attr, _):
        if attr == "AXFocusedUIElement":
            return (0, focused)
        if attr == "AXValue":
            return (0, store.get("value", ""))
        return (0, None)

    def _set(el, attr, val):
        if attr == "AXValue":
            store["value"] = val
        return 0

    mock = MagicMock()
    mock.AXUIElementCreateApplication.return_value = MagicMock()
    mock.AXUIElementCopyAttributeValue = MagicMock(side_effect=_copy)
    mock.AXUIElementSetAttributeValue = MagicMock(side_effect=_set)
    return mock


def test_workspace_managed_app_skips_ax_without_opt_in(monkeypatch):
    monkeypatch.delenv("HEYVOX_AX_CONDUCTOR", raising=False)
    store = {}
    with patch.dict("sys.modules", {"ApplicationServices": _ax_mock(store)}):
        assert _ax_inject_text(_workspace_snap(), "hallo") is False
    assert "value" not in store


def test_workspace_managed_app_uses_ax_with_profile_opt_in(monkeypatch):
    monkeypatch.delenv("HEYVOX_AX_CONDUCTOR", raising=False)
    store = {}
    with patch.dict("sys.modules", {"ApplicationServices": _ax_mock(store)}):
        assert _ax_inject_text(
            _workspace_snap(), "hallo", allow_workspace_managed=True,
        ) is True
    assert store["value"] == "hallo"


def test_type_text_ax_path_submits_via_cgevent_when_frontmost():
    snap = _workspace_snap()
    with patch("heyvox.input.injection._ax_inject_text", return_value=True) as ax, \
         patch("heyvox.input.injection.save_frontmost_pid", return_value=1234), \
         patch("heyvox.input.injection._post_return_key", return_value=True) as cg, \
         patch("heyvox.input.injection._osascript_press_enter") as osa:
        assert type_text(
            "hallo", app_name="Orca", snap=snap, enter_count=1,
            ax_workspace_managed=True,
        ) is True
    ax.assert_called_once_with(snap, "hallo", allow_workspace_managed=True)
    cg.assert_called_once_with(1)
    osa.assert_not_called()


def test_type_text_ax_path_falls_back_to_osascript_enter_when_not_frontmost():
    snap = _workspace_snap()
    with patch("heyvox.input.injection._ax_inject_text", return_value=True), \
         patch("heyvox.input.injection.save_frontmost_pid", return_value=999), \
         patch("heyvox.input.injection._post_return_key") as cg, \
         patch("heyvox.input.injection._osascript_press_enter") as osa:
        type_text("hallo", app_name="Orca", snap=snap, enter_count=1,
                  ax_workspace_managed=True)
    cg.assert_not_called()
    osa.assert_called_once_with(1, "Orca")


def test_orca_default_profile_opts_into_ax():
    from heyvox.config import _DEFAULT_APP_PROFILES

    orca = next(p for p in _DEFAULT_APP_PROFILES if p["name"] == "Orca")
    assert orca["ax_value_paste"] is True
