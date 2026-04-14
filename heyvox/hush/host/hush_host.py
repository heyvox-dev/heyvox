#!/usr/bin/env python3
"""Hush Native Messaging Host.

Bridges Chrome's Native Messaging protocol (stdin/stdout with 4-byte
little-endian length-prefixed JSON) and a local Unix domain socket server
(with an optional TCP fallback on localhost:9847).

Chrome launches this process; external clients (Herald, Vox, CLI tools)
connect to the socket.

Flow
----
1. On startup a Unix socket server is started in a background asyncio thread.
2. The main thread blocks on Chrome stdin, reading responses from the extension.
3. Socket clients send JSON commands (one per line).  The host stamps each
   command with a unique request ID, forwards it to Chrome via stdout, and
   waits up to CHROME_RESPONSE_TIMEOUT seconds for the matching response.
4. When Chrome replies (matching ID on stdin), the host strips the routing ID
   and writes the response back to the waiting socket client.

Wire format
-----------
Socket client  → host  : {"action": "pause"}\\n
Host           → Chrome: {"id": "abc123", "action": "pause"}   (4-byte prefixed)
Chrome         → host  : {"id": "abc123", "state": "paused", "tabs": [...]}
Host           → client: {"state": "paused", "tabs": [...]}\\n
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import struct
import sys
import threading
import uuid
from logging.handlers import RotatingFileHandler
from typing import Any

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# User-scoped temp dir — matches heyvox.constants._TMP (cannot import package here).
_TMP: str = os.environ.get("TMPDIR", "/tmp").rstrip("/")

SOCKET_PATH: str = f"{_TMP}/hush.sock"  # Must match heyvox.constants.HUSH_SOCK
TCP_HOST: str = "127.0.0.1"
TCP_PORT: int = 9847
LOG_PATH: str = f"{_TMP}/hush.log"  # Must match heyvox.constants.HUSH_LOG
LOG_MAX_BYTES: int = 1_048_576  # 1 MB
LOG_BACKUP_COUNT: int = 2
CHROME_RESPONSE_TIMEOUT: float = 3.0  # seconds

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _build_logger() -> logging.Logger:
    """Create a rotating-file logger for the host process.

    Returns:
        Configured logger instance.
    """
    logger = logging.getLogger("hush_host")
    logger.setLevel(logging.DEBUG)
    handler = RotatingFileHandler(
        LOG_PATH,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(message)s")
    )
    logger.addHandler(handler)
    return logger


log: logging.Logger = _build_logger()

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

# Maps request_id → Future[response_dict].  Only accessed from the asyncio
# event loop thread (via call_soon_threadsafe for resolution).
_pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

# Set once the asyncio loop is running inside the background thread.
_loop: asyncio.AbstractEventLoop | None = None

# Protects stdout writes so the main thread and (theoretically) any other
# writer cannot interleave bytes.
_stdout_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Chrome Native Messaging I/O
# Chrome uses a 4-byte little-endian unsigned int length prefix.
# ---------------------------------------------------------------------------


def _read_chrome_message() -> dict[str, Any] | None:
    """Read one message sent by Chrome on stdin (blocking).

    Returns:
        Parsed JSON dict, or None on EOF or read error.
    """
    raw_len = sys.stdin.buffer.read(4)
    if len(raw_len) < 4:
        return None
    (length,) = struct.unpack("<I", raw_len)
    raw_body = sys.stdin.buffer.read(length)
    if len(raw_body) < length:
        log.warning(
            "Short read from Chrome: expected %d bytes, got %d", length, len(raw_body)
        )
        return None
    try:
        return json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        log.warning("JSON decode error from Chrome: %s", exc)
        return None


def _write_chrome_message(message: dict[str, Any]) -> None:
    """Write one message to Chrome on stdout (thread-safe).

    Args:
        message: Dict to serialise and send.
    """
    body = json.dumps(message, separators=(",", ":")).encode("utf-8")
    header = struct.pack("<I", len(body))
    with _stdout_lock:
        sys.stdout.buffer.write(header + body)
        sys.stdout.buffer.flush()


# ---------------------------------------------------------------------------
# Response dispatch
# Called from the main thread; resolves futures in the asyncio loop thread.
# ---------------------------------------------------------------------------


def _dispatch_chrome_response(message: dict[str, Any]) -> None:
    """Route a message from Chrome to the waiting socket-client future.

    Args:
        message: Parsed JSON from Chrome; must contain an ``id`` field.
    """
    request_id: str | None = message.get("id")  # type: ignore[assignment]
    if not request_id:
        log.debug("Chrome message without id — ignoring: %s", message)
        return

    if _loop is None:
        log.warning("Event loop not ready; dropping response id=%s", request_id)
        return

    def _resolve() -> None:
        future = _pending.pop(request_id, None)
        if future is None:
            log.debug("No pending request for id=%s (already timed out?)", request_id)
            return
        if not future.done():
            payload = {k: v for k, v in message.items() if k != "id"}
            future.set_result(payload)

    _loop.call_soon_threadsafe(_resolve)


# ---------------------------------------------------------------------------
# Socket server (asyncio)
# ---------------------------------------------------------------------------


def _write_to_client(writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
    """Enqueue a newline-delimited JSON response to a socket client.

    Args:
        writer: asyncio StreamWriter for the connected client.
        message: Dict to serialise.
    """
    try:
        line = json.dumps(message, separators=(",", ":")) + "\n"
        writer.write(line.encode("utf-8"))
        asyncio.ensure_future(writer.drain())
    except Exception as exc:
        log.warning("Failed to enqueue response to client: %s", exc)


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    """Handle one connected socket client.

    Reads newline-delimited JSON commands, stamps each with a unique request
    ID, forwards to Chrome, and waits for the matching response.

    Args:
        reader: asyncio StreamReader for the client connection.
        writer: asyncio StreamWriter for the client connection.
    """
    peer = writer.get_extra_info("peername") or writer.get_extra_info("sockname")
    log.info("Client connected: %s", peer)

    try:
        while True:
            try:
                raw_line = await reader.readline()
            except (ConnectionResetError, asyncio.IncompleteReadError):
                break
            if not raw_line:
                break

            raw_line = raw_line.strip()
            if not raw_line:
                continue

            # Parse command from client
            try:
                command: dict[str, Any] = json.loads(raw_line.decode("utf-8"))
            except json.JSONDecodeError as exc:
                log.warning("Invalid JSON from client %s: %s", peer, exc)
                _write_to_client(writer, {"error": "invalid_json", "detail": str(exc)})
                continue

            if not isinstance(command, dict):
                log.warning("Non-dict command from client %s", peer)
                _write_to_client(writer, {"error": "expected_object"})
                continue

            # Stamp with a routing ID and register future before sending to
            # Chrome, to eliminate the race where Chrome responds before
            # _pending is populated.
            request_id = uuid.uuid4().hex
            command["id"] = request_id

            future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
            _pending[request_id] = future

            log.debug("→ Chrome [%s]: %s", request_id, command)

            try:
                _write_chrome_message(command)
            except Exception as exc:
                _pending.pop(request_id, None)
                log.error("Write to Chrome failed: %s", exc)
                _write_to_client(
                    writer, {"error": "chrome_write_error", "detail": str(exc)}
                )
                continue

            # Await Chrome response
            try:
                response = await asyncio.wait_for(
                    future, timeout=CHROME_RESPONSE_TIMEOUT
                )
                log.debug("← Chrome [%s]: %s", request_id, response)
                _write_to_client(writer, response)
            except asyncio.TimeoutError:
                _pending.pop(request_id, None)
                log.warning(
                    "Chrome response timeout for id=%s (action=%s)",
                    request_id,
                    command.get("action", "?"),
                )
                _write_to_client(
                    writer,
                    {"error": "timeout", "detail": "no response from Chrome extension"},
                )

    except Exception as exc:
        log.exception("Unhandled error handling client %s: %s", peer, exc)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        log.info("Client disconnected: %s", peer)


async def _run_servers() -> None:
    """Start the Unix socket server and optional TCP server, then serve forever."""
    global _loop
    _loop = asyncio.get_running_loop()

    servers: list[asyncio.AbstractServer] = []

    # --- Unix domain socket ---------------------------------------------------
    try:
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
        unix_server = await asyncio.start_unix_server(
            _handle_client, path=SOCKET_PATH
        )
        os.chmod(SOCKET_PATH, 0o600)
        servers.append(unix_server)
        log.info("Listening on Unix socket: %s", SOCKET_PATH)
    except Exception as exc:
        log.error("Could not start Unix socket server: %s", exc)

    # --- TCP fallback ---------------------------------------------------------
    try:
        tcp_server = await asyncio.start_server(
            _handle_client,
            host=TCP_HOST,
            port=TCP_PORT,
        )
        servers.append(tcp_server)
        log.info("Listening on TCP %s:%d", TCP_HOST, TCP_PORT)
    except Exception as exc:
        log.warning("Could not start TCP server (non-fatal): %s", exc)

    if not servers:
        log.critical("No servers could be started — aborting")
        # Signal the main thread by writing to stderr (Chrome will see it in
        # the process logs) and then let the main thread's stdin read
        # discover the dead process naturally.
        sys.exit(1)

    # asyncio.TaskGroup requires Python 3.11+; use gather for 3.9+ compat.
    await asyncio.gather(*[server.serve_forever() for server in servers])


def _asyncio_thread_entry() -> None:
    """Entry point for the background asyncio thread."""
    try:
        asyncio.run(_run_servers())
    except Exception as exc:
        log.exception("Asyncio thread crashed: %s", exc)


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def _cleanup() -> None:
    """Remove the Unix socket file when the process exits."""
    try:
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
            log.info("Cleaned up socket: %s", SOCKET_PATH)
    except OSError as exc:
        log.warning("Could not remove socket file %s: %s", SOCKET_PATH, exc)


atexit.register(_cleanup)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Start the socket server thread and enter the Chrome stdin read loop."""
    log.info("Hush native messaging host starting (pid=%d)", os.getpid())

    # Start the asyncio socket server in a daemon thread so the process exits
    # naturally when Chrome closes stdin (even if the thread is still running).
    worker = threading.Thread(
        target=_asyncio_thread_entry,
        daemon=True,
        name="hush-asyncio",
    )
    worker.start()
    log.info("Socket server thread started")

    # Main loop: block on Chrome's stdin; dispatch each response to waiting
    # socket-client futures.
    try:
        while True:
            message = _read_chrome_message()
            if message is None:
                log.info("Chrome closed stdin — shutting down")
                break
            log.debug("← stdin: %s", message)
            _dispatch_chrome_response(message)
    except KeyboardInterrupt:
        log.info("Received keyboard interrupt")
    except Exception as exc:
        log.exception("Fatal error in stdin read loop: %s", exc)
        sys.exit(1)

    log.info("Hush host exiting")


if __name__ == "__main__":
    main()
