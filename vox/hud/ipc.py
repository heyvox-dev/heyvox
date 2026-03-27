"""
Unix socket IPC for HUD communication.

Provides the communication channel between vox.main and the HUD overlay process.
Protocol: newline-delimited JSON messages over a Unix domain socket.

Message types:
- {"type": "state", "state": "idle|listening|processing|speaking"}
- {"type": "audio_level", "level": 0.0-1.0}
- {"type": "transcript", "text": "..."}
- {"type": "tts_start", "text": "..."}
- {"type": "tts_end"}
- {"type": "queue_update", "count": N}
- {"type": "error", "message": "..."}

HUDServer: runs in the HUD overlay process, listens for incoming messages.
HUDClient: runs in vox.main / mcp/server.py, sends messages to the HUD.

Both sides silently degrade when the other is not running.

Requirement: HUD-08
"""

import json
import os
import socket
import threading
from typing import Callable

DEFAULT_SOCKET_PATH = "/tmp/vox-hud.sock"

# Backward-compatibility alias (some callers may use SOCKET_PATH)
SOCKET_PATH = DEFAULT_SOCKET_PATH


class HUDServer:
    """Unix domain socket server for receiving HUD state messages.

    Runs on a background daemon thread. The `on_message` callback is invoked
    on that background thread — callers must dispatch to the main thread
    before making AppKit calls.

    Requirement: HUD-08
    """

    def __init__(self, path: str = DEFAULT_SOCKET_PATH, on_message: Callable[[dict], None] = None):
        self._path = path
        self._on_message = on_message or (lambda msg: None)
        self._running = False

    def start(self) -> None:
        """Start the server on a daemon background thread."""
        self._running = True
        t = threading.Thread(target=self._serve, daemon=True)
        t.start()

    def _serve(self) -> None:
        """Main server loop: bind, listen, accept connections."""
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            pass

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as srv:
            srv.bind(self._path)
            srv.listen(1)
            while self._running:
                try:
                    conn, _ = srv.accept()
                    t = threading.Thread(target=self._handle, args=(conn,), daemon=True)
                    t.start()
                except OSError:
                    # Socket closed by shutdown()
                    break

    def _handle(self, conn: socket.socket) -> None:
        """Handle a single client connection; parse newline-delimited JSON."""
        buf = b""
        with conn:
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line)
                        self._on_message(msg)
                    except json.JSONDecodeError:
                        pass

    def shutdown(self) -> None:
        """Stop accepting connections and clean up the socket file."""
        self._running = False
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            pass


class HUDClient:
    """Unix domain socket client for sending HUD state messages.

    Silently degrades when the HUD overlay is not running — all send()
    calls are no-ops if the connection is unavailable.

    Requirement: HUD-08
    """

    def __init__(self, path: str = DEFAULT_SOCKET_PATH):
        self._path = path
        self._sock = None

    def connect(self) -> None:
        """Attempt to connect to the HUD server. Silent on failure."""
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(self._path)
            self._sock = s
        except (FileNotFoundError, ConnectionRefusedError):
            self._sock = None

    def send(self, msg: dict) -> None:
        """Send a JSON message. No-op if not connected."""
        if self._sock is None:
            return
        try:
            self._sock.sendall((json.dumps(msg) + "\n").encode())
        except (BrokenPipeError, OSError):
            self._sock = None

    def close(self) -> None:
        """Close the connection."""
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def reconnect(self) -> None:
        """Close and reconnect (for periodic retry)."""
        self.close()
        self.connect()
