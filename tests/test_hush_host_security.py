"""Security guard tests for the Hush native messaging host.

Guards DEF-176: the unauthenticated plain-TCP fallback (127.0.0.1:9847) was
removed from ``heyvox/hush/host/hush_host.py``. That listener bypassed the
per-user ``$TMPDIR`` 0700 isolation every other IPC channel relies on, letting
any local process (including a different OS user on a shared Mac) send
``type-text`` / ``press-enter`` into the focused browser tab. It was also dead
code: the only clients (``injection.py``, ``hush-cli.sh``) connect via the
PID-suffixed ``AF_UNIX`` socket (DEF-105), never TCP.

These tests are net-free: they assert against source text only, never binding a
socket or touching the network. Source text (not ``inspect.getsource``) is used
deliberately — ``tests/conftest.py`` autouse-monkeypatches
``injection._hush_send``, so introspecting the live object would read the stub.

References: .planning/DEFECT-LOG.md (DEF-176),
.context/release-audit/03-security.md §3c
"""

import re
from pathlib import Path

import heyvox


def _read_source(*parts: str) -> str:
    """Return the text of a source file under the installed heyvox package."""
    path = Path(heyvox.__file__).parent.joinpath(*parts)
    assert path.is_file(), f"expected source file not found: {path}"
    return path.read_text(encoding="utf-8")


def test_no_tcp_port_9847_literal():
    """The hardcoded TCP fallback port must not reappear in the host."""
    src = _read_source("hush", "host", "hush_host.py")
    assert "9847" not in src, (
        "TCP fallback port 9847 reintroduced into hush_host.py — this is the "
        "DEF-176 unauthenticated text-injection hole. Keep the Unix socket only."
    )


def test_no_tcp_constants_or_tcp_server():
    """No TCP host/port constants and no asyncio.start_server (TCP) call."""
    src = _read_source("hush", "host", "hush_host.py")
    for banned in ("TCP_HOST", "TCP_PORT"):
        assert banned not in src, f"{banned} reintroduced into hush_host.py (DEF-176)"
    # start_unix_server is fine; the bare TCP start_server is the banned one.
    assert not re.search(r"asyncio\.start_server\b", src), (
        "asyncio.start_server (TCP listener) reintroduced into hush_host.py — "
        "only asyncio.start_unix_server is allowed (DEF-176)."
    )


def test_unix_socket_still_bound_and_locked_to_0600():
    """The surviving IPC channel (Unix socket) stays present and 0600."""
    src = _read_source("hush", "host", "hush_host.py")
    assert "start_unix_server" in src, "Unix socket server unexpectedly removed"
    assert "0o600" in src, (
        "Unix socket chmod 0o600 removed from hush_host.py — IPC permission "
        "regression (the socket must not be world/group accessible)."
    )


def test_injection_client_uses_af_unix_and_not_the_removed_tcp_port():
    """The in-process Hush client connects via AF_UNIX only.

    Documents why dropping the TCP fallback was safe: no client depended on it.
    """
    src = _read_source("input", "injection.py")
    assert "AF_UNIX" in src, "injection.py no longer uses an AF_UNIX Hush socket"
    assert "9847" not in src, (
        "injection.py references the removed Hush TCP port 9847 — no client "
        "should reach for the deleted TCP fallback (DEF-176)."
    )
