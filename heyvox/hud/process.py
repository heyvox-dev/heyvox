"""
HUD overlay process lifecycle management for heyvox.

Handles launching, killing, and health-checking the HUD overlay subprocess.
Extracted from main.py as part of Phase 06 decomposition.

Requirements: DECOMP-01
"""
import os
import sys
import subprocess
import time
from heyvox.constants import HUD_STDERR_LOG


# Module-level state for the overlay subprocess
_indicator_proc = None
_hud_log_fh = None  # stderr log file handle for HUD subprocess


def _kill_overlay_pids(pids: list[int], log_fn=None) -> None:
    """Kill overlay processes by PID. Uses SIGKILL -- SIGTERM is unreliable on orphaned AppKit processes."""
    for pid in pids:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
    # Wait for macOS to reclaim window server resources (ghost icon cleanup)
    if pids:
        time.sleep(0.5)


def kill_orphan_indicators(log_fn=None) -> None:
    """Kill any leftover overlay processes from previous sessions."""
    my_pid = os.getpid()
    try:
        result = subprocess.run(
            ["pgrep", "-f", "heyvox.hud.overlay"],
            capture_output=True, text=True, timeout=3,
        )
        pids = []
        for pid_str in result.stdout.strip().split('\n'):
            if pid_str.strip():
                pid = int(pid_str.strip())
                if pid != my_pid:
                    pids.append(pid)
        _kill_overlay_pids(pids, log_fn=log_fn)
    except Exception:
        pass


def kill_duplicate_overlays(keep_pid: int | None = None, log_fn=None) -> None:
    """Ensure only one overlay process is running. Kill extras."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "heyvox.hud.overlay"],
            capture_output=True, text=True, timeout=3,
        )
        pids = [int(p.strip()) for p in result.stdout.strip().split('\n') if p.strip()]
        if len(pids) <= 1:
            return
        to_kill = [pid for pid in pids if pid != keep_pid]
        _kill_overlay_pids(to_kill, log_fn=log_fn)
        if log_fn:
            for pid in to_kill:
                log_fn(f"Killed duplicate overlay (pid={pid})")
    except Exception:
        pass


def launch_hud_overlay(menu_bar_only: bool = False, log_fn=None) -> None:
    """Launch the HUD overlay process once. It stays alive for the entire session.

    Kills any orphan/duplicate overlays first to guarantee exactly one instance.
    """
    global _indicator_proc, _hud_log_fh
    if _indicator_proc is not None and _indicator_proc.poll() is None:
        # Already running -- just ensure no duplicates
        kill_duplicate_overlays(keep_pid=_indicator_proc.pid, log_fn=log_fn)
        return
    # Kill anything leftover before launching
    kill_orphan_indicators(log_fn=log_fn)
    try:
        cmd = [sys.executable, "-m", "heyvox.hud.overlay"]
        if menu_bar_only:
            cmd.append("--menu-bar-only")
        # Close previous log handle if restarting overlay
        if _hud_log_fh is not None:
            try:
                _hud_log_fh.close()
            except OSError:
                pass
        _hud_log_fh = open(HUD_STDERR_LOG, "a")
        _indicator_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=_hud_log_fh,
        )
        if log_fn:
            log_fn(f"HUD overlay launched (pid={_indicator_proc.pid})")
    except Exception as e:
        if log_fn:
            log_fn(f"WARNING: Could not launch HUD overlay: {e}")


def stop_hud_overlay() -> None:
    """Terminate the HUD overlay process on shutdown."""
    global _indicator_proc, _hud_log_fh
    if _indicator_proc:
        try:
            _indicator_proc.terminate()
            _indicator_proc.wait(timeout=3)
        except Exception:
            try:
                _indicator_proc.kill()
            except Exception:
                pass
        _indicator_proc = None
    if _hud_log_fh is not None:
        try:
            _hud_log_fh.close()
        except OSError:
            pass
        _hud_log_fh = None


def get_indicator_proc():
    """Return current overlay subprocess handle (or None)."""
    return _indicator_proc
