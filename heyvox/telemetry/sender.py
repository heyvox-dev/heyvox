"""Telemetry sender — disk-queued batched HTTPS POST.

Operates as a single background thread when telemetry is enabled. Once
per ``telemetry.batch_secs``:

1. Build the event list via :func:`events.build_events`.
2. Write the batch to ``TELEMETRY_QUEUE_DIR/<ts>.json`` (durable across crashes).
3. POST every queued batch to the configured endpoint.
4. On 2xx, delete the queue file and commit the counter snapshot.
5. On error, keep the queue file for the next attempt.

The thread tolerates server outage indefinitely — the queue file is the
durable backstop. A hard cap (``MAX_QUEUE_FILES``) prevents unbounded
disk growth if the server stays down for years.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

from heyvox.constants import TELEMETRY_DIR, TELEMETRY_LAST_BATCH_FILE, TELEMETRY_QUEUE_DIR

log = logging.getLogger(__name__)


# Cap on disk growth if the server is unreachable forever. Oldest pruned first.
MAX_QUEUE_FILES = 200

# Per-attempt HTTP timeout in seconds. Short — we don't want to block the
# background thread on a slow endpoint.
HTTP_TIMEOUT = 5.0


_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


# ---------------------------------------------------------------------------
# Queue management
# ---------------------------------------------------------------------------

def _ensure_dirs() -> None:
    Path(TELEMETRY_DIR).mkdir(parents=True, exist_ok=True)
    Path(TELEMETRY_QUEUE_DIR).mkdir(parents=True, exist_ok=True)


def enqueue(events: list[dict]) -> Path:
    """Write a batch to the queue and return the queue file path."""
    _ensure_dirs()
    ts = int(time.time() * 1000)  # ms precision; avoid collisions
    qf = Path(TELEMETRY_QUEUE_DIR) / f"batch-{ts}.json"
    qf.write_text(json.dumps(events))
    _prune_queue()
    return qf


def _prune_queue() -> None:
    files = sorted(Path(TELEMETRY_QUEUE_DIR).glob("batch-*.json"))
    if len(files) <= MAX_QUEUE_FILES:
        return
    for old in files[: len(files) - MAX_QUEUE_FILES]:
        try:
            old.unlink()
        except OSError:
            pass


def _queue_files() -> list[Path]:
    try:
        return sorted(Path(TELEMETRY_QUEUE_DIR).glob("batch-*.json"))
    except FileNotFoundError:
        return []


# ---------------------------------------------------------------------------
# HTTP send
# ---------------------------------------------------------------------------

def _post_batch(endpoint: str, payload_bytes: bytes, anon_id: str, version: str) -> bool:
    """Single-shot POST. Returns True iff the server returned 2xx."""
    import urllib.request

    req = urllib.request.Request(
        endpoint,
        data=payload_bytes,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": f"heyvox/{version}",
            "X-HeyVox-AnonID": anon_id,
            "X-HeyVox-Version": version,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:
        log.debug("telemetry POST failed: %s", exc)
        return False


def _send_queued_once() -> tuple[int, int]:
    """One drain pass. Returns ``(sent, remaining)``."""
    from heyvox.config import load_config
    from heyvox.telemetry.consent import get_anon_id

    try:
        cfg = load_config().telemetry
    except Exception:
        return (0, 0)

    try:
        from heyvox import __version__ as version
    except Exception:
        version = "unknown"

    anon_id = get_anon_id()
    sent = 0

    for qf in _queue_files():
        try:
            data = qf.read_bytes()
        except OSError:
            continue
        # Respect the per-batch ceiling — over-large legacy files get dropped
        # rather than retried forever.
        if len(data) > cfg.max_batch_kb * 1024:
            log.warning("telemetry: dropping oversized queue file %s", qf.name)
            try:
                qf.unlink()
            except OSError:
                pass
            continue

        if not _post_batch(cfg.endpoint, data, anon_id, version):
            # Stop draining — server is down or unreachable. Try next cycle.
            break

        try:
            qf.unlink()
        except OSError:
            pass
        sent += 1

    remaining = len(_queue_files())
    return (sent, remaining)


# ---------------------------------------------------------------------------
# Background thread
# ---------------------------------------------------------------------------

def _last_batch_age_secs() -> float:
    try:
        st = Path(TELEMETRY_LAST_BATCH_FILE).stat()
        return max(0.0, time.time() - st.st_mtime)
    except FileNotFoundError:
        return float("inf")


def _touch_last_batch() -> None:
    try:
        _ensure_dirs()
        Path(TELEMETRY_LAST_BATCH_FILE).touch()
    except OSError:
        pass


def tick() -> dict:
    """One round of: build events → enqueue → drain. Used by the loop and tests.

    Returns a short status dict.
    """
    from heyvox.config import load_config
    from heyvox.telemetry import events as evmod

    cfg = load_config().telemetry
    if not cfg.enabled:
        return {"enabled": False}

    new_events = evmod.build_events(commit_snapshot=False)
    qf = enqueue(new_events) if new_events else None
    sent, remaining = _send_queued_once()
    if sent > 0:
        # All queued events through ``qf`` are confirmed delivered. Commit
        # the counter snapshot so we don't re-count the same window.
        evmod.commit_snapshot()
    _touch_last_batch()
    return {
        "enabled": True,
        "queued_now": str(qf) if qf else None,
        "sent": sent,
        "remaining": remaining,
    }


def _loop() -> None:
    from heyvox.config import load_config

    while not _stop_event.is_set():
        try:
            cfg = load_config().telemetry
            if cfg.enabled:
                tick()
            # Sleep in small increments so stop() returns quickly.
            slept = 0
            target = max(60, int(cfg.batch_secs))
            while slept < target and not _stop_event.is_set():
                _stop_event.wait(timeout=10)
                slept += 10
        except Exception as exc:
            log.warning("telemetry loop hiccup: %s", exc)
            _stop_event.wait(timeout=60)


def start_background() -> None:
    """Start the telemetry thread (no-op if already running)."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(
        target=_loop,
        name="heyvox-telemetry",
        daemon=True,
    )
    _thread.start()


def stop_background(timeout: float = 5.0) -> None:
    """Signal the telemetry thread to exit and wait briefly."""
    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=timeout)
