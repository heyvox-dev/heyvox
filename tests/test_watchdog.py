"""DEF-213: out-of-process liveness watchdog (heyvox/watchdog.py).

Decision-core unit tests plus one real-child E2E: a spawned watchdog must
SIGKILL a wedged fake parent (a plain `sleep` that never writes a heartbeat)
and then exit on its own once the parent is gone.
"""

import os
import signal
import subprocess
import sys
import time

import pytest

from heyvox.watchdog import (
    EXIT_PARENT_GONE,
    KILL_STALE,
    KILL_STARTUP,
    WAIT,
    check_once,
)

T0 = 1_000_000.0


def _decide(**overrides):
    kw = dict(
        now=T0 + 10,
        parent_alive=True,
        hb_mtime=None,
        started_at=T0,
        loop_reached=False,
        startup_deadline=600.0,
        stale_kill=420.0,
    )
    kw.update(overrides)
    return check_once(**kw)


# ---------------------------------------------------------------------------
# Decision core
# ---------------------------------------------------------------------------

def test_startup_waits_within_deadline():
    assert _decide(now=T0 + 599) == (WAIT, False)


def test_startup_kills_after_deadline():
    decision, _ = _decide(now=T0 + 601)
    assert decision == KILL_STARTUP


def test_stale_preexisting_heartbeat_does_not_count_as_loop_reached():
    """The previous instance's old heartbeat file must not flip us into
    runtime mode — only a write NEWER than our own start counts."""
    decision, loop_reached = _decide(now=T0 + 601, hb_mtime=T0 - 50)
    assert decision == KILL_STARTUP
    assert loop_reached is False


def test_fresh_heartbeat_latches_runtime_mode():
    decision, loop_reached = _decide(now=T0 + 30, hb_mtime=T0 + 20)
    assert (decision, loop_reached) == (WAIT, True)


def test_runtime_stale_kills():
    decision, _ = _decide(
        now=T0 + 1000, hb_mtime=T0 + 100, loop_reached=True,
    )
    assert decision == KILL_STALE


def test_runtime_fresh_waits():
    assert _decide(
        now=T0 + 1000, hb_mtime=T0 + 900, loop_reached=True,
    ) == (WAIT, True)


def test_runtime_missing_heartbeat_file_never_kills():
    """A deleted heartbeat file (constants cleanup on clean shutdown) is not
    evidence of a wedge — no data, no kill."""
    assert _decide(now=T0 + 99999, loop_reached=True) == (WAIT, True)


def test_parent_gone_exits_regardless_of_state():
    decision, _ = _decide(now=T0 + 99999, parent_alive=False)
    assert decision == EXIT_PARENT_GONE


# ---------------------------------------------------------------------------
# E2E: real child kills a wedged fake parent
# ---------------------------------------------------------------------------

@pytest.fixture
def _default_sigchld():
    """heyvox.audio.cues sets SIGCHLD=SIG_IGN at import (afplay zombies);
    under SIG_IGN the kernel auto-reaps children and Popen.poll() reports
    returncode 0 instead of -SIGKILL. Restore default handling so these
    tests can assert the real kill signal."""
    prev = signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    yield
    signal.signal(signal.SIGCHLD, prev)


def test_e2e_watchdog_kills_wedged_parent(tmp_path, _default_sigchld):
    hb_file = tmp_path / "heartbeat"  # never written → permanent "init wedge"
    lock_file = tmp_path / "watchdog.lock"
    env = dict(os.environ, HEYVOX_WATCHDOG_LOCK=str(lock_file))

    fake_parent = subprocess.Popen(["sleep", "60"])
    watchdog = None
    try:
        watchdog = subprocess.Popen(
            [
                sys.executable, "-m", "heyvox.watchdog",
                str(fake_parent.pid), str(hb_file),
                "0.5",   # startup deadline
                "420",
                "--poll-pid", "--interval=0.2",
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.time() + 10.0
        while fake_parent.poll() is None and time.time() < deadline:
            time.sleep(0.1)
        assert fake_parent.poll() is not None, "watchdog never killed the wedged parent"
        assert fake_parent.returncode == -signal.SIGKILL
        out, _ = watchdog.communicate(timeout=10.0)
        assert watchdog.returncode == 0, f"watchdog did not exit cleanly: {out}"
        assert "WEDGE" in out and "SIGKILL" in out
    finally:
        for proc in (watchdog, fake_parent):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5.0)


def test_e2e_second_watchdog_yields_to_lock_holder(tmp_path, _default_sigchld):
    """Post-execv respawn: a second watchdog for the same lock exits 0
    without touching the parent."""
    hb_file = tmp_path / "heartbeat"
    lock_file = tmp_path / "watchdog.lock"
    env = dict(os.environ, HEYVOX_WATCHDOG_LOCK=str(lock_file))

    fake_parent = subprocess.Popen(["sleep", "60"])
    first = None
    try:
        # First watchdog with huge thresholds — just holds the lock.
        first = subprocess.Popen(
            [
                sys.executable, "-m", "heyvox.watchdog",
                str(fake_parent.pid), str(hb_file),
                "9999", "9999", "--poll-pid", "--interval=0.2",
            ],
            env=env,
        )
        time.sleep(1.0)  # let it acquire the lock
        second = subprocess.run(
            [
                sys.executable, "-m", "heyvox.watchdog",
                str(fake_parent.pid), str(hb_file),
                "0.1", "0.1", "--poll-pid", "--interval=0.1",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=10.0,
        )
        assert second.returncode == 0
        assert "another watchdog" in second.stdout
        assert fake_parent.poll() is None, "yielding watchdog must not kill"
    finally:
        for proc in (first, fake_parent):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5.0)
