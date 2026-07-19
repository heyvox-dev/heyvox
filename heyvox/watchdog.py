"""Out-of-process liveness watchdog for the heyvox daemon (DEF-213).

Runs as a CHILD PROCESS with its own interpreter. Rationale: the observed
2026-07-14 incident wedged the daemon's audio init for ~17min in a C call
that HELD THE GIL — every in-process Python thread (including the DEF-210
wedge-supervisor thread and the heartbeat writer) stood still. A watchdog
can therefore only be trustworthy from OUTSIDE the wedged interpreter.

Contract:
- The daemon spawns ``python -m heyvox.watchdog <parent_pid> <heartbeat_file>
  <startup_deadline_secs> <stale_kill_secs>`` first thing in setup.
- STARTUP phase: if the heartbeat file is never touched (mtime >= our start
  time) within ``startup_deadline_secs``, the parent's init is wedged before
  the main loop ever ran → SIGKILL the parent.
- RUNTIME phase (after the first fresh heartbeat): if the heartbeat goes
  stale for more than ``stale_kill_secs``, every in-process recovery
  (DEF-210 supervisor at 300s, DEF-104/163/209 checks) has already failed or
  is starved → SIGKILL the parent.
- SIGKILL, never SIGTERM: launchd relaunches only on non-successful exit
  (``KeepAlive/SuccessfulExit=false``); the daemon's SIGTERM handler exits 0,
  which would leave it permanently DOWN. A wedged interpreter can't run a
  SIGTERM handler anyway (signal handlers need the GIL).
- Singleton via flock: execv-based self-restarts (DEF-104/209/210, memory
  watchdog) replace the process image but keep the PID, so the restarted
  daemon spawns a second watchdog. The incumbent holds the lock and keeps
  watching (same PID, same heartbeat file — still correct); the newcomer
  exits quietly.
- Parent-gone detection: we are a direct child, so a died parent reparents
  us (getppid() changes) → exit; launchd's fresh daemon brings a fresh
  watchdog which then acquires the free lock. ``--poll-pid`` switches to
  kill(pid, 0) liveness for tests, where the watched process is not our
  parent.

Known limitation (documented, accepted): on a true first run the macOS
microphone-permission dialog can legitimately block init; if the user leaves
it unanswered past the startup deadline, the daemon is killed and relaunched
(re-showing the dialog) roughly every deadline period. ``heyvox setup``
handles permissions before the daemon ever starts, so this only affects
manual no-wizard starts. Opt out entirely with ``HEYVOX_NO_WATCHDOG=1``.

No heyvox imports here — argv only, stdlib only, so the child stays tiny and
can never be wedged by the package's own audio stack.
"""

from __future__ import annotations

import fcntl
import os
import signal
import sys
import tempfile
import time

CHECK_INTERVAL_SECS = 15.0
# Env override exists for tests, which run a real child against a fake parent
# and must not collide with (or be blocked by) a live daemon's watchdog lock.
LOCK_FILE = os.environ.get("HEYVOX_WATCHDOG_LOCK") or os.path.join(
    tempfile.gettempdir(), "heyvox-watchdog.lock"
)

# check_once() decisions
WAIT = "wait"
KILL_STARTUP = "kill-startup"
KILL_STALE = "kill-stale"
EXIT_PARENT_GONE = "exit-parent-gone"


def _log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [watchdog] {msg}", flush=True)


def check_once(
    *,
    now: float,
    parent_alive: bool,
    hb_mtime: float | None,
    started_at: float,
    loop_reached: bool,
    startup_deadline: float,
    stale_kill: float,
) -> tuple[str, bool]:
    """Pure decision core — returns (decision, loop_reached').

    ``loop_reached`` latches True once a heartbeat newer than our own start
    time is seen; from then on staleness (not the startup deadline) governs.
    """
    if not parent_alive:
        return (EXIT_PARENT_GONE, loop_reached)
    if hb_mtime is not None and hb_mtime >= started_at:
        loop_reached = True
    if not loop_reached:
        if now - started_at > startup_deadline:
            return (KILL_STARTUP, loop_reached)
        return (WAIT, loop_reached)
    if hb_mtime is not None and now - hb_mtime > stale_kill:
        return (KILL_STALE, loop_reached)
    return (WAIT, loop_reached)


def _parent_alive(parent_pid: int, poll_pid: bool) -> bool:
    if poll_pid:
        try:
            os.kill(parent_pid, 0)
            return True
        except OSError:
            return False
    return os.getppid() == parent_pid


def _hb_mtime(path: str) -> float | None:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def run(
    parent_pid: int,
    hb_file: str,
    startup_deadline: float,
    stale_kill: float,
    *,
    poll_pid: bool = False,
    check_interval: float = CHECK_INTERVAL_SECS,
) -> int:
    lock = open(LOCK_FILE, "w")  # noqa: SIM115 — held for the process lifetime
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _log(f"another watchdog already holds {LOCK_FILE} — exiting (post-execv respawn)")
        return 0

    started_at = time.time()
    loop_reached = False
    last_wake = time.time()
    _log(
        f"armed for pid={parent_pid} (DEF-213): startup deadline "
        f"{startup_deadline:.0f}s, runtime stale-kill {stale_kill:.0f}s"
    )

    while True:
        time.sleep(check_interval)
        now = time.time()
        # System-suspend guard: after a Mac sleep the heartbeat mtime is
        # hours old through no fault of the daemon's. If far more wall-clock
        # passed than our own sleep interval, we just woke — skip one round
        # so the parent's loop gets to write a fresh heartbeat first. A
        # parent that was ALREADY wedged before the suspend still dies one
        # check later.
        if now - last_wake > check_interval * 3 + 60.0:
            _log(
                f"suspend/resume detected ({now - last_wake:.0f}s gap) — "
                f"granting one grace round before judging heartbeat age"
            )
            last_wake = now
            continue
        last_wake = now
        decision, loop_reached = check_once(
            now=now,
            parent_alive=_parent_alive(parent_pid, poll_pid),
            hb_mtime=_hb_mtime(hb_file),
            started_at=started_at,
            loop_reached=loop_reached,
            startup_deadline=startup_deadline,
            stale_kill=stale_kill,
        )
        if decision == EXIT_PARENT_GONE:
            _log(f"parent pid={parent_pid} gone — exiting")
            return 0
        if decision in (KILL_STARTUP, KILL_STALE):
            reason = (
                f"init never reached the main loop within {startup_deadline:.0f}s"
                if decision == KILL_STARTUP
                else f"heartbeat stale beyond {stale_kill:.0f}s with parent alive"
            )
            _log(
                f"WEDGE: {reason} — SIGKILL pid={parent_pid} so launchd "
                f"relaunches it (DEF-213; SIGTERM would exit 0 = no relaunch, "
                f"and a GIL-wedged interpreter can't run handlers anyway)"
            )
            try:
                os.kill(parent_pid, signal.SIGKILL)
            except OSError as e:
                _log(f"SIGKILL failed ({e}) — will re-check")
            # next iteration observes the dead parent and exits


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        _log("usage: heyvox.watchdog <parent_pid> <hb_file> <startup_deadline> <stale_kill> [--poll-pid]")
        return 2
    parent_pid = int(argv[0])
    hb_file = argv[1]
    startup_deadline = float(argv[2])
    stale_kill = float(argv[3])
    extra = argv[4:]
    poll_pid = "--poll-pid" in extra
    check_interval = CHECK_INTERVAL_SECS
    for arg in extra:
        if arg.startswith("--interval="):
            check_interval = float(arg.split("=", 1)[1])
    return run(
        parent_pid, hb_file, startup_deadline, stale_kill,
        poll_pid=poll_pid, check_interval=check_interval,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
