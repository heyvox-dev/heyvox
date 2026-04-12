"""
Conductor sidecar socket injection — send text directly to a Conductor
workspace session via JSON-RPC over Unix socket, bypassing clipboard + focus.

Protocol: The Conductor app runs a Node.js sidecar (index.bundled.js) that
listens on a Unix socket at $TMPDIR/conductor-sidecar-{PID}.sock. The sidecar
accepts JSON-RPC 2.0 messages. The "query" method sends a user message to an
active session.

Discovery chain:
  1. Find sidecar PID: ps aux | grep index.bundled.js
  2. Socket: $TMPDIR/conductor-sidecar-{PID}.sock
  3. Workspace ID: conductor.db → workspaces table (directory_name match)
  4. Session ID: conductor.db → sessions table (workspace_id + status)
"""

import json
import logging
import os
import socket
import sqlite3
import subprocess
import tempfile
import uuid

log = logging.getLogger(__name__)

_CONDUCTOR_DB = os.path.expanduser(
    "~/Library/Application Support/com.conductor.app/conductor.db"
)

# Cache sidecar socket path (survives across calls, invalidated on failure)
_cached_socket_path: str | None = None
_cached_sidecar_pid: int | None = None


def _find_sidecar_pid() -> int | None:
    """Find the Conductor sidecar node process PID.

    macOS pgrep truncates command lines so we use ps + grep instead.
    The sidecar is the node process running index.bundled.js.
    """
    try:
        result = subprocess.run(
            ["ps", "axo", "pid,command"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if "index.bundled.js" in line and "grep" not in line and "/bin/zsh" not in line:
                pid_str = line.strip().split()[0]
                return int(pid_str)
    except (subprocess.TimeoutExpired, ValueError, OSError, IndexError):
        pass
    return None


def _find_socket_path() -> str | None:
    """Find the Conductor sidecar Unix socket path."""
    global _cached_socket_path, _cached_sidecar_pid

    # Check cache first
    if _cached_socket_path and os.path.exists(_cached_socket_path):
        return _cached_socket_path

    pid = _find_sidecar_pid()
    if pid is None:
        _cached_socket_path = None
        _cached_sidecar_pid = None
        return None

    sock_path = os.path.join(tempfile.gettempdir(), f"conductor-sidecar-{pid}.sock")
    if os.path.exists(sock_path):
        _cached_socket_path = sock_path
        _cached_sidecar_pid = pid
        return sock_path

    _cached_socket_path = None
    _cached_sidecar_pid = None
    return None


def _db_query(sql: str, params: tuple = ()) -> list[tuple]:
    """Run a read-only query against conductor.db."""
    if not os.path.exists(_CONDUCTOR_DB):
        return []
    try:
        conn = sqlite3.connect(f"file:{_CONDUCTOR_DB}?mode=ro", uri=True)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        log.debug(f"conductor.db query failed: {e}")
        return []


def find_session_for_workspace(workspace_dir: str) -> tuple[str, str] | None:
    """Look up the active session ID and cwd for a Conductor workspace.

    Args:
        workspace_dir: The workspace directory basename (e.g. "philadelphia-v2").

    Returns:
        Tuple of (session_id, cwd), or None if not found.
    """
    rows = _db_query(
        "SELECT w.id, r.name FROM workspaces w "
        "JOIN repos r ON w.repository_id = r.id "
        "WHERE w.directory_name = ? OR w.secondary_directory_name = ? "
        "LIMIT 1",
        (workspace_dir, workspace_dir),
    )
    if not rows:
        return None
    workspace_id, repo_name = rows[0]

    rows = _db_query(
        "SELECT s.id FROM sessions s "
        "WHERE s.workspace_id = ? AND s.status = 'working' "
        "ORDER BY s.updated_at DESC LIMIT 1",
        (workspace_id,),
    )
    if not rows:
        # Fall back to any recent session (idle sessions can still receive messages)
        rows = _db_query(
            "SELECT s.id FROM sessions s "
            "WHERE s.workspace_id = ? "
            "ORDER BY s.updated_at DESC LIMIT 1",
            (workspace_id,),
        )
    if not rows:
        return None
    cwd = os.path.expanduser(f"~/conductor/workspaces/{repo_name}/{workspace_dir}")
    return rows[0][0], cwd


def find_active_session() -> tuple[str, str] | None:
    """Find the most recently active Conductor session and its cwd.

    Returns:
        Tuple of (session_id, cwd) or None if no session found.
    """
    rows = _db_query(
        "SELECT s.id, w.directory_name, r.name "
        "FROM sessions s "
        "JOIN workspaces w ON s.workspace_id = w.id "
        "JOIN repos r ON w.repository_id = r.id "
        "WHERE s.status = 'working' "
        "ORDER BY s.updated_at DESC LIMIT 1",
    )
    if not rows:
        return None
    session_id, workspace_dir, repo_name = rows[0]
    cwd = os.path.expanduser(f"~/conductor/workspaces/{repo_name}/{workspace_dir}")
    return session_id, cwd


def find_session_for_cwd(cwd: str) -> tuple[str, str] | None:
    """Look up the active session for a working directory path.

    Searches sessions by matching the cwd against workspace paths stored
    in conductor.db (both directory_name and secondary_directory_name).

    Args:
        cwd: Absolute path to the working directory.

    Returns:
        Tuple of (session_id, cwd), or None if not found.
    """
    basename = os.path.basename(cwd.rstrip("/"))
    return find_session_for_workspace(basename)


def _send_jsonrpc(sock_path: str, method: str, params: dict, timeout: float = 5.0) -> dict | None:
    """Send a JSON-RPC 2.0 request over a Unix socket and read the response.

    Returns the parsed response dict, or None on failure.
    """
    request_id = str(uuid.uuid4())[:8]
    message = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params,
    }
    payload = json.dumps(message) + "\n"

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(sock_path)
        sock.sendall(payload.encode("utf-8"))

        # Read response (may come as multiple chunks)
        buf = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            # JSON-RPC responses are newline-delimited
            if b"\n" in buf:
                break

        if buf:
            # Take the first complete line
            line = buf.split(b"\n", 1)[0]
            return json.loads(line)
        return None
    except (socket.error, json.JSONDecodeError, OSError) as e:
        log.debug(f"JSON-RPC send failed: {e}")
        return None
    finally:
        sock.close()


def inject_message(session_id: str, text: str, cwd: str) -> bool:
    """Send a user message to a Conductor session via the sidecar socket.

    This is the main entry point. It sends a "query" JSON-RPC call to the
    Conductor sidecar, which pushes the message into the session's input
    queue — exactly as if the user had typed it in the Conductor UI.

    Args:
        session_id: The Conductor session UUID.
        text: The message text to inject.
        cwd: Working directory for the session.

    Returns:
        True if the message was sent successfully, False otherwise.
    """
    global _cached_socket_path

    sock_path = _find_socket_path()
    if sock_path is None:
        log.warning("Conductor sidecar socket not found")
        return False

    turn_id = str(uuid.uuid4())
    params = {
        "type": "query",
        "id": session_id,
        "agentType": "claude",
        "prompt": text,
        "options": {
            "cwd": cwd,
            "turnId": turn_id,
        },
    }

    log.info(f"Injecting via Conductor socket: session={session_id[:8]}... "
             f"text={text[:60]}{'...' if len(text) > 60 else ''}")

    response = _send_jsonrpc(sock_path, "query", params)

    if response is None:
        log.warning("No response from Conductor sidecar")
        # Invalidate cache — socket may be stale
        _cached_socket_path = None
        return False

    if "error" in response:
        log.warning(f"Conductor sidecar error: {response['error']}")
        return False

    log.info("Conductor socket injection successful")
    return True


def is_available() -> bool:
    """Check if Conductor sidecar injection is available.

    Returns True if the sidecar socket exists and conductor.db is accessible.
    """
    return _find_socket_path() is not None and os.path.exists(_CONDUCTOR_DB)
