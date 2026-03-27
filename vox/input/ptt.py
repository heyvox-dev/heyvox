"""
Push-to-talk via Quartz CGEventTap for macOS.

Uses Quartz event tap instead of pynput because pynput misses the fn/Globe key.
Runs the CFRunLoop in a background daemon thread.

Supports: fn, right_cmd, right_alt, right_ctrl, right_shift modifier keys.
Also handles Escape key to cancel active recordings or pending transcriptions.
"""

import threading
import time


# Modifier flag masks for PTT key detection (CGEventFlags values)
_PTT_KEY_FLAGS = {
    "fn":            0x800000,   # NSEventModifierFlagFunction (fn/Globe)
    "right_cmd":     0x100010,   # Right Command
    "right_command": 0x100010,
    "right_alt":     0x100040,   # Right Option
    "right_option":  0x100040,
    "right_ctrl":    0x102000,   # Right Control
    "right_shift":   0x100004,   # Right Shift
}

ESCAPE_KEYCODE = 53


def start_ptt_listener(ptt_key: str, callbacks: dict, log_fn=None) -> threading.Thread | None:
    """Start push-to-talk using Quartz CGEventTap.

    Creates an event tap that:
    - On PTT key press: calls callbacks["on_start"]()
    - On PTT key release: calls callbacks["on_stop"]()
    - On Escape (busy): calls callbacks["on_cancel_transcription"]()
    - On Escape (recording): calls callbacks["on_cancel_recording"]()

    Args:
        ptt_key: Key name from _PTT_KEY_FLAGS (e.g. "fn", "right_cmd").
        callbacks: Dict with keys:
            - "on_start": callable() — PTT key pressed, start recording
            - "on_stop": callable() — PTT key released, stop recording
            - "on_cancel_transcription": callable() — Escape during transcription
            - "on_cancel_recording": callable() — Escape during recording
            - "is_busy": callable() -> bool — is transcription in progress?
            - "is_recording": callable() -> bool — is recording active?
        log_fn: Optional callable(str) for log output.

    Returns:
        Background thread running the CFRunLoop, or None if setup failed.
    """
    import Quartz  # lazy: requires pyobjc-framework-Quartz

    def _log(msg):
        if log_fn:
            log_fn(msg)
        else:
            print(msg, flush=True)

    flag_mask = _PTT_KEY_FLAGS.get(ptt_key.lower())
    if flag_mask is None:
        _log(f"WARNING: PTT key '{ptt_key}' not supported for Quartz mode, disabling PTT")
        return None

    ptt_held = False
    _last_keydown_time = 0.0  # suppress false fn-release after keyDown events

    def callback(proxy, event_type, event, refcon):
        nonlocal ptt_held, _last_keydown_time

        # Handle Escape key
        if event_type == Quartz.kCGEventKeyDown:
            _last_keydown_time = time.time()
            keycode = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
            if keycode == ESCAPE_KEYCODE:
                if callbacks.get("is_busy", lambda: False)():
                    cancel_t = callbacks.get("on_cancel_transcription")
                    if cancel_t:
                        cancel_t()
                    _log("Escape: cancelling transcription")
                elif callbacks.get("is_recording", lambda: False)():
                    cancel_r = callbacks.get("on_cancel_recording")
                    if cancel_r:
                        cancel_r()
            return event

        # Only process modifier flag changes for PTT
        if event_type != Quartz.kCGEventFlagsChanged:
            return event

        flags = Quartz.CGEventGetFlags(event)
        fn_down = bool(flags & flag_mask)

        if fn_down and not ptt_held:
            ptt_held = True
            is_busy = callbacks.get("is_busy", lambda: False)()
            is_rec = callbacks.get("is_recording", lambda: False)()
            if is_busy or is_rec:
                return event
            _log("PTT key pressed, starting recording")
            on_start = callbacks.get("on_start")
            if on_start:
                on_start()

        elif not fn_down and ptt_held:
            # Ignore false releases caused by other key events (within 50ms)
            if time.time() - _last_keydown_time < 0.05:
                return event
            ptt_held = False
            if callbacks.get("is_recording", lambda: False)():
                _log("PTT key released, stopping recording")
                on_stop = callbacks.get("on_stop")
                if on_stop:
                    on_stop()

        return event

    mask = (
        Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged) |
        Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
    )
    tap = Quartz.CGEventTapCreate(
        Quartz.kCGSessionEventTap,
        Quartz.kCGHeadInsertEventTap,
        Quartz.kCGEventTapOptionListenOnly,
        mask,
        callback,
        None,
    )

    if tap is None:
        _log("WARNING: Failed to create CGEventTap for PTT. Check Accessibility permissions.")
        return None

    source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)

    def run_loop():
        loop = Quartz.CFRunLoopGetCurrent()
        Quartz.CFRunLoopAddSource(loop, source, Quartz.kCFRunLoopDefaultMode)
        Quartz.CGEventTapEnable(tap, True)
        _log(f"Push-to-talk enabled (key: {ptt_key}, Quartz event tap)")
        Quartz.CFRunLoopRun()

    t = threading.Thread(target=run_loop, daemon=True)
    t.start()
    return t
