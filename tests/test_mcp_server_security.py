"""Security guard tests for the MCP HTTP server bind (DEF-178).

The opt-in `streamable-http` MCP server has no per-user authentication. That is
an accepted risk for the single-user-Mac target *only because* it binds
loopback: FastMCP's Origin/DNS-rebinding protection engages for 127.0.0.1, and
a routable bind would expose the unauthenticated TTS-control tools to the local
network while silently disabling that protection. These guards keep the bind
loopback-only and keep the risk documented.

Net-free: exercises the pure `_is_loopback_host` helper and asserts on source
text; never binds a socket or starts the server.

References: .planning/DEFECT-LOG.md (DEF-178),
.context/release-audit/03-security.md §3b
"""

import sys
from pathlib import Path

import heyvox

# server.py redirects sys.stdout -> sys.stderr at import time (MCP stdio framing
# protection). Save/restore so importing it here cannot clobber the test
# runner's stdout.
_saved_stdout = sys.stdout
try:
    from heyvox.mcp.server import _is_loopback_host
finally:
    sys.stdout = _saved_stdout


def _server_source() -> str:
    path = Path(heyvox.__file__).parent / "mcp" / "server.py"
    assert path.is_file(), f"server.py not found at {path}"
    return path.read_text(encoding="utf-8")


def test_loopback_host_accepts_all_loopback_forms():
    for host in ("127.0.0.1", "localhost", "::1", "::ffff:127.0.0.1"):
        assert _is_loopback_host(host), f"{host} should count as loopback"
    # Case / surrounding whitespace must not defeat the check.
    assert _is_loopback_host("  LOCALHOST  ")
    assert _is_loopback_host("127.0.0.1\n")


def test_loopback_host_rejects_routable_addresses():
    for host in ("0.0.0.0", "192.168.1.5", "10.0.0.2", "203.0.113.7", "", "::"):
        assert not _is_loopback_host(host), f"{host} must NOT count as loopback"


def test_argparse_host_default_is_loopback():
    """The --host default must stay loopback so the port is never exposed."""
    src = _server_source()
    assert 'default="127.0.0.1"' in src, (
        "MCP --host default changed away from 127.0.0.1 — a routable default "
        "would expose the unauthenticated TTS tools to the network (DEF-178)."
    )
    # And the default must itself pass the loopback guard.
    assert _is_loopback_host("127.0.0.1")


def test_non_loopback_bind_is_guarded_and_warns():
    """A non-loopback --host override must hit the warning branch."""
    src = _server_source()
    assert "_is_loopback_host(args.host)" in src, (
        "the streamable-http bind no longer checks _is_loopback_host (DEF-178)"
    )
    assert "WARNING" in src, "non-loopback bind warning removed (DEF-178)"


def test_no_auth_risk_is_documented():
    """The accepted no-auth / loopback-only assumption stays disclosed in-code."""
    src = _server_source()
    assert "DEF-178" in src, "DEF-178 security note removed from server.py"
    lowered = src.lower()
    assert "no per-user authentication" in lowered
    assert "0.0.0.0" in src, "the never-bind-0.0.0.0 warning was removed"
