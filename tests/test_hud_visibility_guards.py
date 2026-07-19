"""Structural regression guards for the 2026-06-02 HUD menu-bar visibility
cluster (DEF-134/135/136).

The overlay is a PyObjC/AppKit GUI module that can't be exercised headlessly,
so these are source-level assertions: they pin the specific code shapes whose
absence caused each bug. If a refactor drops one, the guard fails loudly.
"""
import os

_OVERLAY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "heyvox", "hud", "overlay.py",
)


def _overlay_src() -> str:
    with open(_OVERLAY, encoding="utf-8") as f:
        return f.read()


def test_def134_status_item_sets_autosave_name():
    """DEF-134: the menu bar status item must set an autosaveName so macOS
    persists its position across launches. Without it the item lands in a random
    spot each start — sometimes hidden behind the notch — which read to the user
    as 'the recording indicator doesn't show up'."""
    src = _overlay_src()
    assert "setAutosaveName_(" in src, (
        "DEF-134: NSStatusItem.setAutosaveName_ missing — menu bar position "
        "won't persist and the icon will intermittently hide"
    )


def test_def135_toggle_reapplies_state_not_blank_orderfront():
    """DEF-135: toggling the floating pill at runtime must re-apply the current
    state (so the window is painted, or properly hidden), not orderFront a
    blank, un-painted window (the empty frosted-box bug)."""
    src = _overlay_src()
    assert "_REAPPLY_STATE" in src, "DEF-135: re-apply hook missing"
    assert "_LAST_STATE = state_str" in src, (
        "DEF-135: _apply_state must record the last state so the toggle can "
        "re-apply it"
    )
    start = src.index("def toggleOverlay_(")
    end = src.index("\n        def ", start + 1)
    body = src[start:end]
    assert "_REAPPLY_STATE()" in body, (
        "DEF-135: toggleOverlay_ must re-apply state via the hook"
    )
    assert "orderFrontRegardless" not in body, (
        "DEF-135: toggleOverlay_ must not orderFront a blank window directly — "
        "that surfaced the empty frosted box"
    )


def test_def136_error_branch_reads_text_field():
    """DEF-136: the HUD 'error' message handler must read the 'text' field that
    senders actually populate (device_manager/recording), not only 'message'
    (which logged 46 blank '[HUD] Error from client:' lines)."""
    src = _overlay_src()
    assert 'msg_dict.get("text")' in src or "msg_dict.get('text')" in src, (
        "DEF-136: error handler must read the 'text' field senders use"
    )


def test_mic_level_meter_reserves_width_before_image():
    """The listening-state mic-level bars must not repeat the DEF-134 bug:
    setLength_ has to run before setImage_ so macOS doesn't hide the status
    item mid-resize while it grows to fit the bars image."""
    src = _overlay_src()
    assert "_MIC_METER_IMG_W" in src, "mic-level meter constants missing"
    start = src.index("def _apply_state(")
    end = src.index("\ndef _make_dispatcher_class(", start)
    body = src[start:end]
    set_length_idx = body.index("status_item.setLength_(_w)")
    set_image_idx = body.index("btn.setImage_(_mic_level_bars_image(0.0))")
    assert set_length_idx < set_image_idx, (
        "setLength_ must run before the listening-state bars image is set, "
        "or macOS can hide the status item mid-resize (DEF-134 pattern)"
    )


def test_audio_level_handler_feeds_menubar_meter():
    """The dispatcher's audio_level branch must refresh the menu bar meter
    (not just the pill's WaveformView), gated on _LAST_STATE == 'listening'
    so it can't fire the AppKit call outside the recording window."""
    src = _overlay_src()
    start = src.index('elif msg_type == "audio_level":')
    end = src.index('elif msg_type ==', start + 1)
    body = src[start:end]
    assert "_update_menubar_meter(status_item, level)" in body, (
        "audio_level handler must feed the menu bar meter"
    )
    assert '_LAST_STATE == "listening"' in body, (
        "menu bar meter update must be gated on the listening state"
    )
