"""System RAM-pressure detector → menu-bar banner.

Surfaces machine-wide memory pressure as a HUD banner via ``HUDSurface`` —
the same primitive the mic-zombie / too-quiet detectors use, so it renders
with the established ⚠️/❌ menu-bar symbol + hover tooltip and needs no new
overlay code.

WHY this exists (and why it is distinct from the self-RSS watchdog in
``main.py``): that watchdog watches heyvox's OWN process and *restarts* it
when it balloons. This watches the WHOLE machine and only *shows a banner*,
because system RAM pressure is what forces MLX Whisper to cold-reload
(``recording.py`` force-unloads the model at RSS>1500MB under swap pressure,
and the 10-min idle unloader also frees it). A cold reload adds ~1.7s to the
next dictation — previously the user had no visible signal for why STT got
slow. Closes DEFECT-LOG pattern P-new (silent state change → visible signal).

Signal: macOS ``kern.memorystatus_vm_pressure_level`` is canonical — it maps
1:1 to Activity Monitor's green/yellow/red memory graph (1=normal, 2=warn,
4=critical). ``psutil.virtual_memory().available`` gives the human-readable
"GB free" number and a configurable floor. A banner fires on EITHER signal,
whichever is worse, so a low-free-RAM floor still trips even when the kernel
hasn't escalated its pressure flag yet.

No PyObjC imports — the decision core (``evaluate``) is pure and unit-tested
in isolation (mirrors ``hud/menu_bar_title.py``).
"""

from __future__ import annotations

import ctypes
import ctypes.util

# HUDSurface source key — one banner record is kept/overwritten under this name.
BANNER_SOURCE = "ram-pressure"

# macOS pressure levels from <sys/kern_memorystatus.h> (bit values 1/2/4).
PRESSURE_NORMAL = 1
PRESSURE_WARN = 2
PRESSURE_CRITICAL = 4


def macos_pressure_level() -> int | None:
    """Return ``kern.memorystatus_vm_pressure_level`` (1/2/4) or ``None``.

    Read via ``sysctlbyname`` through libc — no subprocess spawn per poll.
    Returns ``None`` on any failure (non-macOS, missing sysctl) so callers
    fall back to the available-RAM floor alone.
    """
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        libc.sysctlbyname.argtypes = [
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p,
            ctypes.c_size_t,
        ]
        libc.sysctlbyname.restype = ctypes.c_int
        val = ctypes.c_int(0)
        size = ctypes.c_size_t(ctypes.sizeof(val))
        rc = libc.sysctlbyname(
            b"kern.memorystatus_vm_pressure_level",
            ctypes.byref(val),
            ctypes.byref(size),
            None,
            0,
        )
        if rc != 0:
            return None
        return int(val.value)
    except Exception:
        return None


def evaluate(
    available_mb: float,
    warn_mb: float,
    crit_mb: float,
    pressure_level: int | None,
) -> tuple[str | None, str]:
    """Pure decision core: map RAM state → ``(level, banner_text)``.

    ``level`` is ``"error"``, ``"warn"`` or ``None`` (clear the banner).
    A banner fires on EITHER the macOS pressure level OR the free-RAM floor,
    whichever is worse. Pure function — no I/O, fully unit-testable.
    """
    crit_hit = (
        pressure_level is not None and pressure_level >= PRESSURE_CRITICAL
    ) or available_mb < crit_mb
    warn_hit = (
        pressure_level is not None and pressure_level >= PRESSURE_WARN
    ) or available_mb < warn_mb

    if crit_hit:
        level = "error"
    elif warn_hit:
        level = "warn"
    else:
        return (None, "")

    gb = available_mb / 1024.0
    if level == "error":
        text = (
            f"System RAM critical: {gb:.1f} GB free — STT cold-reloads + swap "
            "thrash. Close apps / fewer parallel sessions."
        )
    else:
        text = (
            f"System RAM low: {gb:.1f} GB free — STT may cold-reload (slower). "
            "Close apps or reduce parallel sessions."
        )
    return (level, text)


def check_and_surface(
    *,
    warn_mb: float = 2048.0,
    crit_mb: float = 1024.0,
    ttl_secs: float = 120.0,
    log=None,
) -> str | None:
    """Poll system RAM and write/clear the ``ram-pressure`` HUD banner.

    Best-effort and never raises — a monitoring path must not break the main
    loop. ``ttl_secs`` defaults to 120s so the banner survives one missed
    60s poll while pressure persists, and is cleared explicitly on recovery.

    Returns the active level (``"warn"`` / ``"error"`` / ``None``) for tests
    and logging.
    """
    try:
        import psutil

        available_mb = psutil.virtual_memory().available / 1024 / 1024
    except Exception:
        return None

    level, text = evaluate(available_mb, warn_mb, crit_mb, macos_pressure_level())

    try:
        from heyvox.hud.surface import HUDSurface

        if level is None:
            HUDSurface.clear(BANNER_SOURCE)
        else:
            HUDSurface.banner(level, BANNER_SOURCE, text, ttl_secs=ttl_secs)
    except Exception:
        pass

    if log is not None:
        try:
            # [RAM_PRESSURE] tag — greppable / log-health-friendly, like the
            # other observability tags (WAKE_VAD_DROP, USER_EFFORT, ...).
            if level is not None:
                log(
                    f"[RAM_PRESSURE] level={level} available={available_mb:.0f}MB "
                    f"(warn<{warn_mb:.0f} crit<{crit_mb:.0f})"
                )
        except Exception:
            pass

    return level
