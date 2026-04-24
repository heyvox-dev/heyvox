"""
Push-to-talk via Quartz CGEventTap for macOS.

Uses Quartz event tap instead of pynput because pynput misses the fn/Globe key.
Runs the CFRunLoop in a background daemon thread.

Supports: fn, right_cmd, right_alt, right_ctrl, right_shift modifier keys.
Also handles Escape key to cancel active recordings or pending transcriptions.
"""

import threading
import time
from collections.abc import Callable


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


def start_ptt_listener(ptt_key: str, callbacks: dict, log_fn: Callable[[str], None] | None = None) -> threading.Thread | None:
    """Start push-to-talk using Quartz CGEventTap.

    Creates an event tap that:
    - On PTT key press: calls callbacks["on_start"]()
    - On PTT key release: calls callbacks["on_stop"]()
    - On Escape (busy): calls callbacks["on_cancel_transcription"]()
    - On Escape (recording): calls callbacks["on_cancel_recording"]()
    - On Escape (speaking): calls callbacks["on_cancel_tts"]()

    Args:
        ptt_key: Key name from _PTT_KEY_FLAGS (e.g. "fn", "right_cmd").
        callbacks: Dict with keys:
            - "on_start": callable() — PTT key pressed, start recording
            - "on_stop": callable() — PTT key released, stop recording
            - "on_cancel_transcription": callable() — Escape during transcription
            - "on_cancel_recording": callable() — Escape during recording
            - "on_cancel_tts": callable() — Escape during TTS playback
            - "is_busy": callable() -> bool — is transcription in progress?
            - "is_recording": callable() -> bool — is recording active?
            - "is_speaking": callable() -> bool — is TTS playing?
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
    _stop_lock = threading.Lock()
    _stop_in_progress = False  # prevent duplicate stop calls

    # DEF-087: Track actual event flow so the watchdog can distinguish
    # "tap enabled but dead" from "tap working but user is idle". The old
    # watchdog only polled CGEventTapIsEnabled — which keeps returning True
    # even when macOS has silently stopped delivering events (Accessibility
    # permission hiccup, tccd restart, etc.). Now we count events received
    # and surface a WARNING if none arrive across a long observation window.
    _event_count = 0
    _first_event_logged = False
    _last_event_at = time.time()

    def callback(proxy, event_type, event, refcon):
        nonlocal ptt_held, _last_keydown_time, _stop_in_progress
        nonlocal _event_count, _first_event_logged, _last_event_at

        # DEF-087: touch the flow counters on every delivery. Cheap (two
        # integer writes + one time.time()) and runs in the Quartz C
        # thread, so we keep it outside the try/except to guarantee we
        # notice dead taps even if _callback_inner raises consistently.
        _event_count += 1
        _last_event_at = time.time()
        if not _first_event_logged:
            _first_event_logged = True
            _log(f"PTT event tap delivering events (first event: type={event_type})")

        # CRITICAL: Any unhandled exception in this Quartz C callback causes
        # macOS to permanently disable the event tap. All action callbacks
        # (recording.cancel, stop_tts, etc.) do heavy I/O that can throw.
        # Wrap everything so the tap survives.
        try:
            return _callback_inner(proxy, event_type, event, refcon)
        except Exception as e:
            _log(f"ERROR in event tap callback (tap preserved): {e}")
            return event  # pass event through on error

    def _callback_inner(proxy, event_type, event, refcon):
        nonlocal ptt_held, _last_keydown_time, _stop_in_progress

        # Handle Escape key — consume it (return None) when HeyVox acts on it,
        # so it doesn't propagate to the foreground app (e.g. exit fullscreen).
        if event_type == Quartz.kCGEventKeyDown:
            _last_keydown_time = time.time()
            keycode = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
            if keycode == ESCAPE_KEYCODE:
                # Diagnose source: pid==0 → real HID keypress; pid>0 → synthesized
                # by that process via CGEventPost. Flags show modifiers at keydown.
                try:
                    src_pid = Quartz.CGEventGetIntegerValueField(
                        event, Quartz.kCGEventSourceUnixProcessID
                    )
                except Exception:
                    src_pid = -1
                try:
                    src_flags = Quartz.CGEventGetFlags(event)
                except Exception:
                    src_flags = 0
                esc_src = f"src_pid={src_pid} flags=0x{src_flags:x}"
                if callbacks.get("is_busy", lambda: False)():
                    cancel_t = callbacks.get("on_cancel_transcription")
                    if cancel_t:
                        cancel_t()
                    _log(f"Escape: cancelling transcription ({esc_src})")
                    return None  # consume — don't pass to app
                elif callbacks.get("is_recording", lambda: False)():
                    cancel_r = callbacks.get("on_cancel_recording")
                    if cancel_r:
                        cancel_r()
                    _log(f"Escape: cancelling recording ({esc_src})")
                    return None  # consume — don't pass to app
                elif callbacks.get("is_speaking", lambda: False)():
                    cancel_tts = callbacks.get("on_cancel_tts")
                    if cancel_tts:
                        cancel_tts()
                    _log("Escape: stopping TTS")
                    return None  # consume — don't pass to app
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
            if is_busy:
                return event
            if is_rec:
                # Recording active (wake-word-triggered) — FN tap stops it
                _log("PTT key pressed during wake-word recording, stopping")
                on_stop = callbacks.get("on_stop")
                if on_stop:
                    on_stop()
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
            with _stop_lock:
                if _stop_in_progress:
                    return event
                if callbacks.get("is_recording", lambda: False)():
                    _stop_in_progress = True
            if _stop_in_progress:
                try:
                    _log("PTT key released, stopping recording")
                    on_stop = callbacks.get("on_stop")
                    if on_stop:
                        on_stop()
                finally:
                    with _stop_lock:
                        _stop_in_progress = False

        return event

    mask = (
        Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged) |
        Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
    )
    # kCGEventTapOptionDefault (not ListenOnly) so we can consume Escape
    # when HeyVox handles it — prevents it from reaching the foreground app
    # (e.g. Conductor exiting fullscreen).
    tap = Quartz.CGEventTapCreate(
        Quartz.kCGSessionEventTap,
        Quartz.kCGHeadInsertEventTap,
        Quartz.kCGEventTapOptionDefault,
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

    # Health monitor: macOS silently disables event taps when the system is
    # under load or after transient Accessibility permission changes.
    # Poll every 5s and re-enable if needed. Without this, ESC and fn stop
    # working with no visible error.
    # DEF-087: `CGEventTapIsEnabled` can keep returning True while the tap
    # is effectively dead (no events flowing). Cross-check the event-flow
    # counters from the callback closure and surface a WARNING + forced
    # re-enable if the tap stays silent for too long. Heartbeat cadence
    # (HEARTBEAT_SECS) keeps the log readable while still catching tap
    # death quickly enough to explain user reports like "fn doesn't work".
    SILENT_WARN_SECS = 120.0   # no events for this long → WARN + re-enable
    HEARTBEAT_SECS = 600.0     # log an alive-heartbeat every N seconds

    def _tap_watchdog():
        _consecutive_reenable = 0
        _last_heartbeat = time.time()
        _silence_warned = False
        while True:
            time.sleep(1.0)
            try:
                if not Quartz.CGEventTapIsEnabled(tap):
                    _consecutive_reenable += 1
                    Quartz.CGEventTapEnable(tap, True)
                    _log(f"WARNING: CGEventTap was disabled by macOS, re-enabled (#{_consecutive_reenable})")
                    _silence_warned = False  # re-enable may restore flow
                else:
                    _consecutive_reenable = 0

                now = time.time()
                silence = now - _last_event_at
                if silence > SILENT_WARN_SECS and not _silence_warned:
                    # Tap "enabled" per Quartz but we have not observed a
                    # single event in two minutes. Most common cause: the
                    # Accessibility permission was revoked or tccd is in
                    # a bad state. Attempt re-enable and tell the user.
                    Quartz.CGEventTapEnable(tap, True)
                    _log(
                        f"WARNING: PTT event tap enabled but silent for "
                        f"{silence:.0f}s (received {_event_count} events "
                        f"since start). Toggled re-enable; if fn still "
                        f"does nothing, re-grant Accessibility permission "
                        f"to the Python binary in System Settings."
                    )
                    _silence_warned = True
                elif silence < 5.0:
                    # Fresh events flowing — reset the warn latch so a
                    # later silent period produces another WARN.
                    _silence_warned = False

                if now - _last_heartbeat > HEARTBEAT_SECS:
                    _log(
                        f"[PTT] heartbeat: events={_event_count} "
                        f"last_event={silence:.0f}s ago enabled={bool(Quartz.CGEventTapIsEnabled(tap))}"
                    )
                    _last_heartbeat = now
            except Exception:
                break  # Tap object gone — thread exits

    wd = threading.Thread(target=_tap_watchdog, daemon=True)
    wd.start()

    return t
