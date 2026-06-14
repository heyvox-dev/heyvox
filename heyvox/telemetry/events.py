"""Telemetry event construction — counter deltas only, no log content.

Reads counter tags from the HeyVox main log and computes deltas against the
last snapshot stored at ``TELEMETRY_COUNTER_SNAPSHOT``. Each delta becomes
one event of type ``counter.delta`` ready for upload.

The snapshot is updated *after* a successful send, not before, so a failed
upload doesn't drop the delta — the next send retries with the same window.
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import time
from pathlib import Path

from heyvox.constants import TELEMETRY_COUNTER_SNAPSHOT, TELEMETRY_DIR


# Counter tags we track. Order matters only for the snapshot file readability.
TRACKED_TAGS = (
    "WAKE_VAD_DROP",
    "NEAR_MISS",
    "USER_EFFORT",
    "MIC_ZOMBIE",
    "KOKORO_RESTART",
)


def _ensure_dir() -> None:
    Path(TELEMETRY_DIR).mkdir(parents=True, exist_ok=True)


def _read_snapshot() -> dict[str, int]:
    try:
        return json.loads(Path(TELEMETRY_COUNTER_SNAPSHOT).read_text())
    except Exception:
        return {tag: 0 for tag in TRACKED_TAGS}


def _write_snapshot(counters: dict[str, int]) -> None:
    _ensure_dir()
    Path(TELEMETRY_COUNTER_SNAPSHOT).write_text(json.dumps(counters, indent=2))


def _count_current() -> dict[str, int]:
    """Grep tag occurrences in the current main log file."""
    try:
        from heyvox.config import load_config
        log_path = Path(load_config().log_file)
    except Exception:
        from heyvox.constants import LOG_FILE_DEFAULT
        log_path = Path(LOG_FILE_DEFAULT)

    counts = {tag: 0 for tag in TRACKED_TAGS}
    if not log_path.exists():
        return counts

    try:
        with log_path.open("r", errors="replace") as f:
            for line in f:
                for tag in TRACKED_TAGS:
                    if f"[{tag}]" in line:
                        counts[tag] += 1
    except Exception:
        pass
    return counts


def _safe(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
        return (out.stdout or "").strip()
    except Exception:
        return ""


def _system_info() -> dict:
    try:
        from heyvox import __version__ as hv_version
    except Exception:
        hv_version = "unknown"
    macos_prod = _safe(["sw_vers", "-productVersion"]) or "unknown"
    mac_model = _safe(["sysctl", "-n", "hw.model"]) or "unknown"

    # Hash the hostname so we have a stable but non-identifying machine key.
    host = platform.node() or "unknown"
    host_key = hashlib.sha256(host.encode()).hexdigest()[:16]

    return {
        "heyvox_version": hv_version,
        "macos_version": macos_prod,
        "mac_model": mac_model,
        "python": platform.python_version(),
        "machine_hash": host_key,
    }


def build_events(commit_snapshot: bool = False) -> list[dict]:
    """Build a list of telemetry events for the current window.

    Events emitted:
    * One ``counter.delta`` event per tag with a positive delta since the
      last successful send.
    * One ``heartbeat`` event with system info (always present so the server
      can track active installs even on quiet days).

    If ``commit_snapshot`` is True, the snapshot is updated immediately —
    use only after a successful send. The sender passes False on build and
    only flips to True after the POST returns 2xx.
    """
    current = _count_current()
    snapshot = _read_snapshot()

    sys_info = _system_info()
    ts = int(time.time())

    events: list[dict] = []

    # Heartbeat — minimal payload so server can track DAU even with no deltas.
    events.append({
        "type": "heartbeat",
        "ts": ts,
        "system": sys_info,
    })

    # Counter deltas
    for tag in TRACKED_TAGS:
        delta = max(0, current.get(tag, 0) - snapshot.get(tag, 0))
        if delta == 0:
            continue
        events.append({
            "type": "counter.delta",
            "ts": ts,
            "tag": tag,
            "delta": delta,
            "system": sys_info,
        })

    if commit_snapshot:
        _write_snapshot(current)

    return events


def commit_snapshot() -> None:
    """Persist the current counter values as the new baseline."""
    _write_snapshot(_count_current())


def preview() -> str:
    """Human-readable preview of what would be sent next.

    Used by ``heyvox telemetry preview`` and the "What's being sent…" menu
    item. Includes the anonymous ID at the top so the user can copy/save it
    before resetting.
    """
    from heyvox.telemetry.consent import get_anon_id, is_enabled

    events = build_events(commit_snapshot=False)
    aid = get_anon_id(create_if_missing=False) or "(not yet generated)"
    lines = [
        f"Telemetry enabled : {is_enabled()}",
        f"Anonymous ID      : {aid}",
        f"Events in window  : {len(events)}",
        "",
        "Payload preview:",
        json.dumps(events, indent=2),
    ]
    return "\n".join(lines)
