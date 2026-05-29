"""Plain-text bug report generator.

Builds a single Markdown-formatted string suitable for pasting into a GitHub
Issue body or copying to the clipboard. Replaces the missing
``heyvox.doctor.run_bugreport`` function that the CLI used to call.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from heyvox.reporting.redact import redact_text


def _safe(cmd: list[str], timeout: float = 3.0) -> str:
    """Run ``cmd`` and return stripped stdout, or empty string on error."""
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
        return (out.stdout or "").strip()
    except Exception:
        return ""


def _heyvox_version() -> str:
    try:
        from heyvox import __version__
        return __version__
    except Exception:
        return "unknown"


def _macos_version() -> str:
    prod = _safe(["sw_vers", "-productVersion"])
    build = _safe(["sw_vers", "-buildVersion"])
    return f"{prod} ({build})" if prod else "unknown"


def _mac_model() -> str:
    return _safe(["sysctl", "-n", "hw.model"]) or "unknown"


def _ram_gb() -> str:
    mem = _safe(["sysctl", "-n", "hw.memsize"])
    try:
        return f"{int(mem) // (1024 ** 3)} GB"
    except (ValueError, TypeError):
        return "unknown"


def _service_status() -> str:
    try:
        from heyvox.setup.launchd import get_status, PLIST_PATH
        if not PLIST_PATH.exists():
            return "not installed"
        st = get_status()
        if st.get("running"):
            return f"running (PID {st['pid']})"
        if st.get("loaded"):
            return f"stopped (exit code {st.get('exit_code')})"
        return "not loaded"
    except Exception as exc:
        return f"status query failed: {exc}"


def _active_mic() -> str:
    try:
        from heyvox.constants import ACTIVE_MIC_FILE
        return Path(ACTIVE_MIC_FILE).read_text().strip() or "unknown"
    except Exception:
        return "unknown"


def _counter_summary() -> dict:
    """Quick grep-based counter pull from the main log."""
    try:
        from heyvox.config import load_config
        log_path = Path(load_config().log_file)
    except Exception:
        from heyvox.constants import LOG_FILE_DEFAULT
        log_path = Path(LOG_FILE_DEFAULT)

    counts = {
        "WAKE_VAD_DROP": 0,
        "NEAR_MISS": 0,
        "USER_EFFORT": 0,
        "MIC_ZOMBIE": 0,
        "KOKORO_RESTART": 0,
    }
    if not log_path.exists():
        return counts

    try:
        with log_path.open("r", errors="replace") as f:
            for line in f:
                for tag in counts:
                    if f"[{tag}]" in line:
                        counts[tag] += 1
    except Exception:
        pass
    return counts


def _last_log_lines(n: int = 80) -> list[str]:
    try:
        from heyvox.config import load_config
        log_path = Path(load_config().log_file)
    except Exception:
        from heyvox.constants import LOG_FILE_DEFAULT
        log_path = Path(LOG_FILE_DEFAULT)
    if not log_path.exists():
        return []
    try:
        with log_path.open("r", errors="replace") as f:
            return f.readlines()[-n:]
    except Exception:
        return []


def collect_system_info() -> dict:
    """Structured system info — also used by bundle.py for system_info.json."""
    return {
        "heyvox_version": _heyvox_version(),
        "macos_version": _macos_version(),
        "mac_model": _mac_model(),
        "ram": _ram_gb(),
        "service_status": _service_status(),
        "active_mic": _active_mic(),
    }


def run_bugreport(comment: str = "") -> str:
    """Build a Markdown bug-report string for clipboard / inline paste.

    No paths or usernames leak — all output runs through ``redact_text``.
    """
    sys_info = collect_system_info()
    counters = _counter_summary()
    tail = _last_log_lines(60)

    lines: list[str] = []
    lines.append("# HeyVox bug report")
    lines.append("")
    if comment.strip():
        lines.append("## What happened")
        lines.append("")
        lines.append(comment.strip())
        lines.append("")

    lines.append("## Environment")
    lines.append("")
    lines.append(f"- HeyVox: `{sys_info['heyvox_version']}`")
    lines.append(f"- macOS: `{sys_info['macos_version']}`")
    lines.append(f"- Mac: `{sys_info['mac_model']}` ({sys_info['ram']})")
    lines.append(f"- Service: {sys_info['service_status']}")
    lines.append(f"- Active mic: `{sys_info['active_mic']}`")
    lines.append("")

    lines.append("## Signal counters (current log file)")
    lines.append("")
    for tag, n in counters.items():
        lines.append(f"- `[{tag}]` × {n}")
    lines.append("")

    if tail:
        lines.append("## Last 60 log lines")
        lines.append("")
        lines.append("```")
        lines.extend(ln.rstrip("\n") for ln in tail)
        lines.append("```")

    return redact_text("\n".join(lines))
