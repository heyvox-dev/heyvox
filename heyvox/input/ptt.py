"""
Push-to-talk via Quartz CGEventTap for macOS.

Uses Quartz event tap instead of pynput because pynput misses the fn/Globe key.
Runs the CFRunLoop in a background daemon thread.

Supports: fn, right_cmd, right_alt, right_ctrl, right_shift modifier keys.
Also handles Escape key to cancel active recordings or pending transcriptions,
and an independent cancel key (default right_ctrl) to cancel a pending Herald
workspace switch without affecting TTS playback.
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


def _cancel_key_edge(flags: int, mask: int, was_down: bool) -> tuple[bool, bool]:
    """Rising-edge detector for the workspace-switch cancel key.

    Pure and Quartz-free (no timers, no hold/tap/double-tap ambiguity — a
    switch is either pending or it isn't) so it's independently unit-testable,
    mirroring GestureRecognizer's own testability contract.

    Returns (is_rising_edge, now_down).
    """
    now_down = bool(flags & mask)
    return (now_down and not was_down), now_down


class GestureRecognizer:
    """Turns raw PTT-key down/up edges into recording actions.

    Quartz-free and deterministic so it can be unit-tested without an event
    tap. ``start_ptt_listener`` builds one of these and feeds it ``on_key_down``
    / ``on_key_up`` from the CGEventTap callback.

    Three gestures on a single key:

    * **Hold** → push-to-talk. The key going down arms a one-shot timer of
      ``tap_max_secs``; if the key is still held when it fires, we commit to PTT
      (``start_ptt``) and stop on release (``stop_ptt``). Recording therefore
      starts ~``tap_max_secs`` after the press — the caller hides this latency
      by passing the idle pre-roll buffer to ``start_ptt`` so no speech is lost.
    * **Double-tap** → hands-free. Two quick taps (each shorter than
      ``tap_max_secs``, gap ≤ ``double_tap_secs``) fire ``start_handsfree``. The
      recording then runs until the main loop stops it on silence / stop wake
      word, or the user taps once more (toggle-off).
    * **Tap while recording** → ``stop_other``. A single tap during an active
      wake-word OR hands-free recording stops it (the legacy DEF-116 behavior,
      now also the hands-free toggle-off).

    A single isolated tap is a no-op — strictly better than the old behavior
    where a quick tap started+discarded a sub-``min_recording_secs`` recording.

    When ``double_tap_enabled`` is False the key reverts to classic instant
    push-to-talk: down starts immediately, up stops, no timer, no latency.

    Args:
        actions: callables ``start_ptt``, ``stop_ptt``, ``start_handsfree``,
            ``stop_other``. Missing keys are treated as no-ops.
        queries: callables ``is_busy`` / ``is_recording`` returning bool.
        double_tap_enabled: enable the double-tap → hands-free gesture.
        tap_max_secs: max press duration counted as a "tap"; also the hold
            promotion delay.
        double_tap_secs: max gap between the two taps.
        timer_factory: ``f(delay, callback) -> obj`` with ``.start()`` /
            ``.cancel()``. Defaults to ``threading.Timer``; tests inject a fake.
        clock: monotonic-ish time source (defaults to ``time.time``).
        log: optional ``callable(str)``.
    """

    def __init__(
        self,
        *,
        actions: dict,
        queries: dict,
        double_tap_enabled: bool = True,
        tap_max_secs: float = 0.2,
        double_tap_secs: float = 0.35,
        timer_factory=None,
        clock=time.time,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._actions = actions
        self._queries = queries
        self._double_tap_enabled = double_tap_enabled
        self._tap_max_secs = tap_max_secs
        self._double_tap_secs = double_tap_secs

        def _default_timer(delay, cb):
            return threading.Timer(delay, cb)

        self._timer_factory = timer_factory or _default_timer
        self._clock = clock
        self._log = log

        self._lock = threading.Lock()
        self._held = False
        # "idle" | "ptt" | "handsfree" — the mode of the recording WE started.
        # A wake-word recording shows up via queries.is_recording() while _mode
        # stays "idle"; that's intentional (a tap then toggles it off).
        self._mode = "idle"
        self._last_tap_up = 0.0
        self._hold_timer = None
        self._awaiting_hf_release = False

    # -- helpers ---------------------------------------------------------
    def _q(self, name: str) -> bool:
        fn = self._queries.get(name)
        try:
            return bool(fn()) if fn else False
        except Exception:
            return False

    def _act(self, name: str) -> None:
        fn = self._actions.get(name)
        if fn:
            fn()

    def _log_msg(self, msg: str) -> None:
        if self._log:
            self._log(msg)

    def _cancel_hold_timer(self) -> None:
        if self._hold_timer is not None:
            try:
                self._hold_timer.cancel()
            except Exception:
                pass
            self._hold_timer = None

    def _arm_hold_timer(self) -> None:
        self._cancel_hold_timer()
        self._hold_timer = self._timer_factory(self._tap_max_secs, self._on_hold_timeout)
        self._hold_timer.start()

    def _sync_mode(self) -> None:
        """Reconcile _mode with reality.

        Silence-timeout, stop wake word, and Escape-cancel all end the
        recording by calling recording.stop()/cancel() directly — the
        recognizer never sees it. Without this, a stale _mode ("ptt" or
        "handsfree") would swallow the next gesture.
        """
        if self._mode in ("ptt", "handsfree") and not self._q("is_recording"):
            self._mode = "idle"
            self._awaiting_hf_release = False

    # -- event entry points ---------------------------------------------
    def on_key_down(self) -> None:
        with self._lock:
            self._held = True
            if self._q("is_busy"):
                return  # mid-transcription: ignore presses entirely
            self._sync_mode()

            # Tap during an active recording (wake-word or hands-free) → stop.
            # PTT recordings are NOT stopped here (they stop on release).
            if self._mode != "ptt" and self._q("is_recording"):
                self._cancel_hold_timer()
                self._mode = "idle"
                self._awaiting_hf_release = False
                self._last_tap_up = 0.0
                self._log_msg("PTT key tapped during recording, stopping")
                self._act("stop_other")
                return

            if self._mode == "ptt":
                return  # already holding; ignore duplicate down

            # Idle, nothing recording.
            if not self._double_tap_enabled:
                self._mode = "ptt"
                self._log_msg("PTT key pressed, starting recording")
                self._act("start_ptt")
                return

            now = self._clock()
            if self._last_tap_up > 0.0 and (now - self._last_tap_up) <= self._double_tap_secs:
                # Second tap of a double-tap → hands-free recording.
                self._cancel_hold_timer()
                self._last_tap_up = 0.0
                self._mode = "handsfree"
                self._awaiting_hf_release = True
                self._log_msg("Double-tap detected, starting hands-free recording")
                self._act("start_handsfree")
                return

            # First press: arm the hold timer. Commit to PTT only if still held
            # when it fires; a quick release before then makes this a tap.
            self._arm_hold_timer()

    def on_key_up(self) -> None:
        with self._lock:
            if not self._held:
                return
            self._held = False
            self._sync_mode()

            if self._mode == "ptt":
                self._mode = "idle"
                self._log_msg("PTT key released, stopping recording")
                self._act("stop_ptt")
                return

            if self._mode == "handsfree":
                # Release of the second tap that armed hands-free — keep
                # recording. Later releases (key never re-pressed) won't reach
                # here because _held is already False.
                self._awaiting_hf_release = False
                return

            # Idle: this was a short tap. Cancel the pending hold timer (it was
            # not a hold) and open the double-tap window.
            self._cancel_hold_timer()
            if self._double_tap_enabled:
                self._last_tap_up = self._clock()

    def _on_hold_timeout(self) -> None:
        with self._lock:
            self._hold_timer = None
            if (
                self._held
                and self._mode == "idle"
                and not self._q("is_busy")
                and not self._q("is_recording")
            ):
                self._mode = "ptt"
                self._log_msg("PTT key held, starting recording")
                self._act("start_ptt")


def start_ptt_listener(
    ptt_key: str,
    callbacks: dict,
    log_fn: Callable[[str], None] | None = None,
    *,
    double_tap: bool = True,
    tap_max_secs: float = 0.2,
    double_tap_secs: float = 0.35,
    cancel_key: str | None = None,
) -> threading.Thread | None:
    """Start push-to-talk using Quartz CGEventTap.

    Key-edge events are translated into a `GestureRecognizer`, which decides
    between hold (push-to-talk), double-tap (hands-free), and tap-to-stop.

    Creates an event tap that:
    - On PTT key hold: calls callbacks["on_start"]() / ["on_stop"]()
    - On PTT key double-tap: calls callbacks["on_start_handsfree"]()
    - On PTT key tap during a recording: calls callbacks["on_stop_wake_via_ptt"]()
    - On Escape (busy): calls callbacks["on_cancel_transcription"]()
    - On Escape (recording): calls callbacks["on_cancel_recording"]()
    - On Escape (speaking): calls callbacks["on_cancel_tts"]()
    - On cancel_key down, while callbacks["is_switch_pending"]() is true:
      calls callbacks["on_cancel_switch"]()

    Args:
        ptt_key: Key name from _PTT_KEY_FLAGS (e.g. "fn", "right_cmd").
        callbacks: Dict with keys:
            - "on_start": callable() — hold confirmed, start PTT recording
            - "on_stop": callable() — PTT key released, stop recording
            - "on_start_handsfree": callable() — double-tap, start hands-free
              recording (falls back to "on_start" if not provided)
            - "on_stop_wake_via_ptt": callable() — tap during an active
              recording, stop it (falls back to "on_stop" if not provided)
            - "on_cancel_transcription": callable() — Escape during transcription
            - "on_cancel_recording": callable() — Escape during recording
            - "on_cancel_tts": callable() — Escape during TTS playback
            - "on_cancel_switch": callable() — cancel_key pressed while a
              Herald workspace switch is pending
            - "is_busy": callable() -> bool — is transcription in progress?
            - "is_recording": callable() -> bool — is recording active?
            - "is_speaking": callable() -> bool — is TTS playing?
            - "is_switch_pending": callable() -> bool — is a Herald
              workspace-switch countdown currently running?
        log_fn: Optional callable(str) for log output.
        double_tap: enable the double-tap → hands-free gesture.
        tap_max_secs: max press duration counted as a tap (also hold delay).
        double_tap_secs: max gap between the two taps.
        cancel_key: Key name from _PTT_KEY_FLAGS for cancelling a pending
            workspace switch (e.g. "right_ctrl"). None/unrecognized/same as
            ptt_key disables this feature (graceful degradation, same
            contract as an unrecognized ptt_key).

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

    cancel_flag_mask = None
    if cancel_key:
        if cancel_key.lower() == ptt_key.lower():
            _log(
                f"WARNING: workspace_switch.cancel_key '{cancel_key}' is the same as "
                f"push_to_talk.key — disabling the switch-cancel binding to avoid "
                f"double-booking the key."
            )
        else:
            cancel_flag_mask = _PTT_KEY_FLAGS.get(cancel_key.lower())
            if cancel_flag_mask is None:
                _log(
                    f"WARNING: cancel_key '{cancel_key}' not supported for Quartz "
                    f"mode, disabling switch-cancel"
                )

    # Gesture FSM — all recording-mode logic lives here; the Quartz callback
    # below only does edge detection and forwards on_key_down / on_key_up.
    recognizer = GestureRecognizer(
        actions={
            "start_ptt": callbacks.get("on_start", lambda: None),
            "stop_ptt": callbacks.get("on_stop", lambda: None),
            "start_handsfree": callbacks.get(
                "on_start_handsfree", callbacks.get("on_start", lambda: None)
            ),
            "stop_other": callbacks.get(
                "on_stop_wake_via_ptt", callbacks.get("on_stop", lambda: None)
            ),
        },
        queries={
            "is_busy": callbacks.get("is_busy", lambda: False),
            "is_recording": callbacks.get("is_recording", lambda: False),
        },
        double_tap_enabled=double_tap,
        tap_max_secs=tap_max_secs,
        double_tap_secs=double_tap_secs,
        log=_log,
    )

    ptt_held = False  # Quartz-level edge tracking (debounce duplicate flags)
    cancel_key_held = False  # same, for the workspace-switch cancel key
    _last_keydown_time = 0.0  # suppress false fn-release after keyDown events

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
        nonlocal ptt_held, _last_keydown_time
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
        nonlocal ptt_held, cancel_key_held, _last_keydown_time

        # macOS disables the tap after a slow callback response (timeout) or
        # a secure-input prompt (user input), and notifies via this special
        # event type instead of queuing it. React immediately here instead of
        # relying solely on _tap_watchdog's 1s poll below — under sustained
        # system load the tap can be re-disabled faster than the watchdog
        # polls, leaving Escape/PTT effectively dead until the next tick.
        if event_type in (
            Quartz.kCGEventTapDisabledByTimeout,
            Quartz.kCGEventTapDisabledByUserInput,
        ):
            Quartz.CGEventTapEnable(tap, True)
            _log("PTT event tap disabled by macOS, re-enabled immediately (in-callback)")
            return event

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

        # Edge detection only — all gesture/recording logic lives in the
        # recognizer. We still debounce duplicate flag events (ptt_held) and
        # suppress false releases that fire within 50 ms of a real keyDown
        # (chord typing flips the modifier briefly).
        if fn_down and not ptt_held:
            ptt_held = True
            recognizer.on_key_down()
        elif not fn_down and ptt_held:
            if time.time() - _last_keydown_time < 0.05:
                return event
            ptt_held = False
            recognizer.on_key_up()

        # Workspace-switch cancel key — independent of the PTT gesture above.
        # Only acts while a switch is actually pending, so it's a no-op for
        # every other state and never intercepts the key's normal behavior
        # otherwise.
        if cancel_flag_mask is not None:
            edge, cancel_key_held = _cancel_key_edge(flags, cancel_flag_mask, cancel_key_held)
            if edge and callbacks.get("is_switch_pending", lambda: False)():
                on_cancel_switch = callbacks.get("on_cancel_switch")
                if on_cancel_switch:
                    on_cancel_switch()
                _log("Cancel key: cancelling pending workspace switch")

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
    # DEF-087 follow-up (DEF-088): 120s was too aggressive — a normal user
    # reading code or in a meeting easily generates zero events for 2 min,
    # spamming the log with false-positive WARNs every cycle. 600s (10 min)
    # is rare under genuine activity and still catches the dead-tap case
    # the original DEF-087 was after.
    SILENT_WARN_SECS = 600.0   # no events for this long → WARN + re-enable
    HEARTBEAT_SECS = 1800.0    # log an alive-heartbeat every N seconds

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
