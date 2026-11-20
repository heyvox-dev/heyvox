"""DEF-277: Escape must stop TTS reliably and leave a trace when it can't.

Covers:
- is_playback_active(): playing flag, afplay PID file, fresh queued part
  (gap between parts / before the first part), stale orphan ignored.
- Escape handler in the event-tap callback: consumes + cancels while TTS is
  active, logs a pass-through line otherwise (previously silent).
- _stop_tts_from_escape(): returns immediately, the Herald stop runs on a
  thread (a blocking tap callback gets the tap disabled by macOS).
- secure_input_owner(): parses kCGSSessionSecureInputPID from ioreg.
"""

import os
import sys
import threading
import time
import types

import pytest


# ---------------------------------------------------------------------------
# is_playback_active
# ---------------------------------------------------------------------------

@pytest.fixture
def herald_paths(tmp_path, monkeypatch):
    import heyvox.constants as c
    flag = tmp_path / "heyvox-tts-playing"
    pid = tmp_path / "herald-playing.pid"
    queue = tmp_path / "herald-queue"
    queue.mkdir()
    monkeypatch.setattr(c, "TTS_PLAYING_FLAG", str(flag))
    monkeypatch.setattr(c, "HERALD_PLAYING_PID", str(pid))
    monkeypatch.setattr(c, "HERALD_QUEUE_DIR", str(queue))
    return flag, pid, queue


def test_idle_is_not_active(herald_paths):
    from heyvox.audio.tts import is_playback_active
    assert is_playback_active() is False


def test_playing_flag_is_active(herald_paths):
    from heyvox.audio.tts import is_playback_active
    flag, _, _ = herald_paths
    flag.touch()
    assert is_playback_active() is True


def test_playing_pid_file_is_active(herald_paths):
    from heyvox.audio.tts import is_playback_active
    _, pid, _ = herald_paths
    pid.write_text("123")
    assert is_playback_active() is True


def test_fresh_queued_part_counts_as_active(herald_paths):
    """Gap between two parts: flags gone, next part already queued."""
    from heyvox.audio.tts import is_playback_active
    _, _, queue = herald_paths
    (queue / "1795168318884-02.wav").write_bytes(b"x")
    assert is_playback_active() is True


def test_stale_orphan_in_queue_is_ignored(herald_paths):
    from heyvox.audio.tts import is_playback_active, _QUEUED_PART_MAX_AGE_SECS
    _, _, queue = herald_paths
    wav = queue / "1795000000000-01.wav"
    wav.write_bytes(b"x")
    old = time.time() - _QUEUED_PART_MAX_AGE_SECS - 10
    os.utime(wav, (old, old))
    assert is_playback_active() is False


def test_non_wav_sidecars_are_ignored(herald_paths):
    from heyvox.audio.tts import is_playback_active
    _, _, queue = herald_paths
    (queue / "1795168318884-01.workspace").write_text("ws")
    assert is_playback_active() is False


# ---------------------------------------------------------------------------
# Escape handling inside the event-tap callback (fake Quartz)
# ---------------------------------------------------------------------------

def _fake_quartz(captured):
    q = types.SimpleNamespace()
    q.kCGEventKeyDown = 10
    q.kCGEventFlagsChanged = 12
    q.kCGEventTapDisabledByTimeout = 0xFFFFFFFE
    q.kCGEventTapDisabledByUserInput = 0xFFFFFFFF
    q.kCGKeyboardEventKeycode = 9
    q.kCGEventSourceUnixProcessID = 41
    q.kCGSessionEventTap = 1
    q.kCGHeadInsertEventTap = 0
    q.kCGEventTapOptionDefault = 0
    q.kCFRunLoopDefaultMode = "default"
    q.CGEventMaskBit = lambda t: 1 << t

    def tap_create(_loc, _place, _opts, _mask, cb, _ref):
        captured["cb"] = cb
        return object()

    q.CGEventTapCreate = tap_create
    q.CFMachPortCreateRunLoopSource = lambda *a: object()
    q.CFRunLoopGetCurrent = lambda: object()
    q.CFRunLoopAddSource = lambda *a: None
    q.CGEventTapEnable = lambda *a: None
    q.CGEventTapIsEnabled = lambda tap: True
    q.CFRunLoopRun = lambda: None
    q.CGEventGetIntegerValueField = lambda ev, field: (
        ev["keycode"] if field == q.kCGKeyboardEventKeycode else 0
    )
    q.CGEventGetFlags = lambda ev: 0
    return q


@pytest.fixture
def esc_callback(monkeypatch):
    import heyvox.input.ptt as ptt
    captured = {}
    monkeypatch.setitem(sys.modules, "Quartz", _fake_quartz(captured))
    monkeypatch.setattr(ptt, "secure_input_enabled", lambda: False)
    logs = []
    state = {"speaking": False, "cancelled": 0}

    def cancel_tts():
        state["cancelled"] += 1

    callbacks = {
        "is_busy": lambda: False,
        "is_recording": lambda: False,
        "is_speaking": lambda: state["speaking"],
        "on_cancel_tts": cancel_tts,
    }
    ptt.start_ptt_listener("fn", callbacks, log_fn=logs.append)
    return captured["cb"], state, logs


def test_escape_while_speaking_is_consumed_and_stops(esc_callback):
    cb, state, logs = esc_callback
    state["speaking"] = True
    ev = {"keycode": 53}
    assert cb(None, 10, ev, None) is None
    assert state["cancelled"] == 1
    assert any(line.startswith("Escape: stopping TTS") for line in logs)


def test_escape_while_idle_passes_through_and_is_logged(esc_callback):
    cb, state, logs = esc_callback
    ev = {"keycode": 53}
    assert cb(None, 10, ev, None) is ev
    assert state["cancelled"] == 0
    assert any("Escape: passed through" in line for line in logs)


def test_other_keys_are_not_logged(esc_callback):
    cb, state, logs = esc_callback
    ev = {"keycode": 0}
    assert cb(None, 10, ev, None) is ev
    assert not any("Escape" in line for line in logs)


# ---------------------------------------------------------------------------
# _stop_tts_from_escape runs the stop off the tap thread
# ---------------------------------------------------------------------------

def test_stop_tts_from_escape_returns_before_stop_finishes(monkeypatch):
    import heyvox.audio.tts as tts
    import heyvox.main as main
    done = threading.Event()

    def slow_stop():
        time.sleep(0.5)
        done.set()

    monkeypatch.setattr(tts, "stop_all", slow_stop)
    monkeypatch.setattr(main, "log", lambda msg: None)
    sent = []
    t0 = time.monotonic()
    main._stop_tts_from_escape(sent.append)
    assert time.monotonic() - t0 < 0.2
    assert done.wait(3.0)
    deadline = time.monotonic() + 1.0
    while not sent and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sent == [{"type": "state", "state": "idle"}]


# ---------------------------------------------------------------------------
# secure_input_owner parsing
# ---------------------------------------------------------------------------

def test_secure_input_owner_parses_pid(monkeypatch):
    import subprocess
    import heyvox.input.ptt as ptt

    def fake_run(argv, **kw):
        if argv[0] == "ioreg":
            out = '"IOConsoleUsers" = ({"kCGSSessionSecureInputPID"=76296,"x"=1})'
        else:
            out = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome\n"
        return subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert ptt.secure_input_owner() == "Google Chrome (pid 76296)"


def test_secure_input_owner_none_when_absent(monkeypatch):
    import subprocess
    import heyvox.input.ptt as ptt
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, '"IOConsoleUsers" = ({})', ""),
    )
    assert ptt.secure_input_owner() is None
