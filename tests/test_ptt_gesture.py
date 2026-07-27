"""Tests for the PTT gesture recognizer (hold / double-tap / tap-to-stop).

The GestureRecognizer is the Quartz-free FSM behind push-to-talk. It decides
between three gestures on a single key:

  * hold              → push-to-talk (start on hold-confirm, stop on release)
  * double-tap        → hands-free recording (runs until external stop)
  * tap while active  → stop the current recording (toggle-off / DEF-116)

These tests drive it with a fake one-shot timer and a controllable clock so the
timing logic is deterministic and no real threads or Quartz are involved.
"""

import pytest

from heyvox.input.ptt import GestureRecognizer, _cancel_key_edge, _PTT_KEY_FLAGS


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class FakeTimer:
    """One-shot timer stand-in. Records start/cancel; fires only when asked."""

    def __init__(self, delay, cb):
        self.delay = delay
        self.cb = cb
        self.started = False
        self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


class TimerHub:
    """Collects FakeTimers so a test can fire the most recent one."""

    def __init__(self):
        self.timers = []

    def factory(self, delay, cb):
        t = FakeTimer(delay, cb)
        self.timers.append(t)
        return t

    @property
    def last(self):
        return self.timers[-1] if self.timers else None

    def fire_last(self):
        """Simulate the most recent timer firing (regardless of cancel state).

        We deliberately allow firing a cancelled timer so we can prove the
        recognizer's own _held guard — not just timer cancellation — prevents a
        spurious PTT start after release.
        """
        assert self.last is not None and self.last.started
        self.last.cb()


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def make_recognizer(double_tap=True, tap_max=0.2, dt_secs=0.35,
                    recording=False, busy=False):
    """Build a recognizer wired to an event log + simulated recording state.

    The action callbacks mutate ``state["recording"]`` the same way the real
    callbacks do (recording.start/stop flip ctx.is_recording), so is_recording
    queries stay consistent across a gesture sequence.
    """
    events = []
    state = {"recording": recording, "busy": busy}

    def _start_ptt():
        events.append("start_ptt")
        state["recording"] = True

    def _stop_ptt():
        events.append("stop_ptt")
        state["recording"] = False

    def _start_hf():
        events.append("start_handsfree")
        state["recording"] = True

    def _stop_other():
        events.append("stop_other")
        state["recording"] = False

    actions = {
        "start_ptt": _start_ptt,
        "stop_ptt": _stop_ptt,
        "start_handsfree": _start_hf,
        "stop_other": _stop_other,
    }
    queries = {
        "is_busy": lambda: state["busy"],
        "is_recording": lambda: state["recording"],
    }
    hub = TimerHub()
    clock = Clock()
    rec = GestureRecognizer(
        actions=actions,
        queries=queries,
        double_tap_enabled=double_tap,
        tap_max_secs=tap_max,
        double_tap_secs=dt_secs,
        timer_factory=hub.factory,
        clock=clock,
    )
    return rec, events, state, hub, clock


# ---------------------------------------------------------------------------
# Hold → push-to-talk
# ---------------------------------------------------------------------------

def test_hold_arms_timer_then_starts_ptt_on_confirm():
    rec, events, state, hub, clock = make_recognizer()
    rec.on_key_down()
    # Nothing yet — recording is deferred until the hold is confirmed.
    assert events == []
    assert hub.last is not None and hub.last.started
    assert hub.last.delay == pytest.approx(0.2)
    # Still held when the timer fires → commit to PTT.
    hub.fire_last()
    assert events == ["start_ptt"]
    assert state["recording"] is True


def test_hold_stops_on_release():
    rec, events, state, hub, clock = make_recognizer()
    rec.on_key_down()
    hub.fire_last()           # hold confirmed → start_ptt
    rec.on_key_up()
    assert events == ["start_ptt", "stop_ptt"]
    assert state["recording"] is False


def test_release_before_hold_timer_does_not_start_ptt():
    """A quick tap (released before tap_max_secs) must NOT become PTT, even if
    the OS timer thread fires late — the _held guard catches it."""
    rec, events, state, hub, clock = make_recognizer()
    rec.on_key_down()         # arm hold timer
    rec.on_key_up()           # released quickly → cancels timer
    assert hub.last.cancelled is True
    # Even if the cancelled timer somehow fires, no PTT start (not held).
    hub.fire_last()
    assert events == []
    assert state["recording"] is False


# ---------------------------------------------------------------------------
# Single tap → no-op
# ---------------------------------------------------------------------------

def test_single_tap_is_noop():
    rec, events, state, hub, clock = make_recognizer()
    rec.on_key_down()
    rec.on_key_up()
    assert events == []
    assert state["recording"] is False


# ---------------------------------------------------------------------------
# Double-tap → hands-free
# ---------------------------------------------------------------------------

def test_double_tap_starts_handsfree():
    rec, events, state, hub, clock = make_recognizer()
    # Tap 1
    rec.on_key_down()
    rec.on_key_up()
    # Tap 2 within the double-tap window
    clock.advance(0.1)
    rec.on_key_down()
    assert events == ["start_handsfree"]
    assert state["recording"] is True


def test_handsfree_keeps_running_after_release():
    rec, events, state, hub, clock = make_recognizer()
    rec.on_key_down()
    rec.on_key_up()
    clock.advance(0.1)
    rec.on_key_down()         # → handsfree
    rec.on_key_up()           # release of 2nd tap must NOT stop it
    assert events == ["start_handsfree"]
    assert state["recording"] is True


def test_two_slow_taps_are_not_a_double_tap():
    rec, events, state, hub, clock = make_recognizer(dt_secs=0.35)
    rec.on_key_down()
    rec.on_key_up()
    clock.advance(0.5)        # gap exceeds double_tap_secs
    rec.on_key_down()         # treated as a fresh first press (arms hold timer)
    assert events == []
    assert hub.last.started   # a new hold timer was armed, not handsfree


def test_double_tap_disabled_for_first_tap_when_recording_absent():
    """The opening tap of a double-tap must not start a throwaway recording."""
    rec, events, state, hub, clock = make_recognizer()
    rec.on_key_down()
    assert events == []       # no start_ptt on the bare first down
    rec.on_key_up()
    assert events == []


# ---------------------------------------------------------------------------
# Tap while recording → stop (toggle-off + legacy DEF-116)
# ---------------------------------------------------------------------------

def test_tap_during_wakeword_recording_stops_it():
    """A tap while a wake-word recording is active stops it (DEF-116 path)."""
    rec, events, state, hub, clock = make_recognizer(recording=True)
    rec.on_key_down()
    assert events == ["stop_other"]
    assert state["recording"] is False


def test_tap_toggles_off_handsfree():
    rec, events, state, hub, clock = make_recognizer()
    rec.on_key_down()
    rec.on_key_up()
    clock.advance(0.1)
    rec.on_key_down()         # handsfree on
    rec.on_key_up()
    clock.advance(1.0)
    rec.on_key_down()         # tap → toggle off
    assert events == ["start_handsfree", "stop_other"]
    assert state["recording"] is False


# ---------------------------------------------------------------------------
# Self-heal: recording ended outside the recognizer (silence / stop-word)
# ---------------------------------------------------------------------------

def test_handsfree_ended_externally_then_hold_works():
    """Silence-timeout / stop-word stop the recording directly (recording.stop)
    — the recognizer must re-sync so the next gesture isn't swallowed."""
    rec, events, state, hub, clock = make_recognizer()
    rec.on_key_down()
    rec.on_key_up()
    clock.advance(0.1)
    rec.on_key_down()         # handsfree on
    rec.on_key_up()
    # Main loop stops it on silence (external to the recognizer):
    state["recording"] = False
    clock.advance(2.0)
    # A fresh hold should now work normally.
    rec.on_key_down()
    hub.fire_last()
    assert events == ["start_handsfree", "start_ptt"]


# ---------------------------------------------------------------------------
# Busy gate
# ---------------------------------------------------------------------------

def test_presses_ignored_while_busy():
    rec, events, state, hub, clock = make_recognizer(busy=True)
    rec.on_key_down()
    assert events == []
    assert hub.last is None   # no hold timer armed while busy
    rec.on_key_up()
    assert events == []


# ---------------------------------------------------------------------------
# Double-tap disabled → classic instant push-to-talk
# ---------------------------------------------------------------------------

def test_double_tap_disabled_is_instant_ptt():
    rec, events, state, hub, clock = make_recognizer(double_tap=False)
    rec.on_key_down()
    assert events == ["start_ptt"]   # immediate, no timer
    assert hub.last is None
    rec.on_key_up()
    assert events == ["start_ptt", "stop_ptt"]


def test_duplicate_down_while_holding_is_ignored():
    rec, events, state, hub, clock = make_recognizer(double_tap=False)
    rec.on_key_down()         # start_ptt
    rec.on_key_down()         # spurious duplicate edge — must not re-fire
    assert events == ["start_ptt"]


# ---------------------------------------------------------------------------
# Workspace-switch cancel key — _cancel_key_edge (pure, Quartz-free)
# ---------------------------------------------------------------------------

_RCTRL = _PTT_KEY_FLAGS["right_ctrl"]
_FN = _PTT_KEY_FLAGS["fn"]


def test_cancel_key_edge_rises_on_key_down():
    edge, now_down = _cancel_key_edge(_RCTRL, _RCTRL, was_down=False)
    assert edge is True
    assert now_down is True


def test_cancel_key_edge_no_rise_while_already_held():
    edge, now_down = _cancel_key_edge(_RCTRL, _RCTRL, was_down=True)
    assert edge is False
    assert now_down is True


def test_cancel_key_edge_release_is_not_a_rising_edge():
    edge, now_down = _cancel_key_edge(0, _RCTRL, was_down=True)
    assert edge is False
    assert now_down is False


def test_cancel_key_edge_ignores_other_bits():
    """A chord where an unrelated modifier flips must not look like our key."""
    edge, now_down = _cancel_key_edge(_FN, _RCTRL, was_down=False)
    assert edge is False
    assert now_down is False


def test_cancel_key_edge_distinguishes_from_fn():
    """fn and right_ctrl are disjoint bits — holding fn never rises the
    cancel-key edge even if the cancel key happens to be configured as fn
    (a misconfiguration ptt.py guards against elsewhere, but the pure edge
    function itself must still only look at its own bit)."""
    edge, now_down = _cancel_key_edge(_FN | _RCTRL, _RCTRL, was_down=False)
    assert edge is True
    assert now_down is True
