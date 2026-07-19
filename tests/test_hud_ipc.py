"""Tests for heyvox.hud.ipc — Unix socket IPC for HUD communication."""

import json
import os
import time
import pytest

from heyvox.hud.ipc import HUDServer, HUDClient


@pytest.fixture
def socket_path():
    """Short socket path (Unix has 104-char limit on macOS)."""
    path = f"/tmp/heyvox-test-{os.getpid()}.sock"
    yield path
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


class TestHUDClient:
    """HUDClient — sending messages to HUD."""

    def test_send_noop_when_not_connected(self):
        client = HUDClient(path="/tmp/nonexistent-test.sock")
        # Should not raise
        client.send({"type": "state", "state": "idle"})

    def test_connect_fails_silently_when_no_server(self):
        client = HUDClient(path="/tmp/nonexistent-test.sock")
        client.connect()
        assert client._sock is None

    def test_close_is_idempotent(self):
        client = HUDClient(path="/tmp/nonexistent-test.sock")
        client.close()
        client.close()  # Should not raise

    def test_reconnect_when_not_connected(self):
        client = HUDClient(path="/tmp/nonexistent-test.sock")
        client.reconnect()  # Should not raise
        assert client._sock is None


class TestHUDServerClient:
    """Integration: HUDServer + HUDClient communicating via socket."""

    def test_send_receive_message(self, socket_path):
        received = []
        server = HUDServer(path=socket_path, on_message=lambda msg: received.append(msg))
        server.start()
        time.sleep(0.1)  # Let server bind

        client = HUDClient(path=socket_path)
        client.connect()
        assert client._sock is not None

        client.send({"type": "state", "state": "listening"})
        time.sleep(0.1)  # Let message arrive

        assert len(received) == 1
        assert received[0] == {"type": "state", "state": "listening"}

        client.close()
        server.shutdown()

    def test_multiple_messages(self, socket_path):
        received = []
        server = HUDServer(path=socket_path, on_message=lambda msg: received.append(msg))
        server.start()
        time.sleep(0.1)

        client = HUDClient(path=socket_path)
        client.connect()

        messages = [
            {"type": "state", "state": "listening"},
            {"type": "audio_level", "level": 0.75},
            {"type": "transcript", "text": "hello world"},
        ]
        for msg in messages:
            client.send(msg)

        time.sleep(0.2)

        assert len(received) == 3
        assert received[0]["type"] == "state"
        assert received[1]["level"] == 0.75
        assert received[2]["text"] == "hello world"

        client.close()
        server.shutdown()

    def test_client_send_after_server_shutdown(self, socket_path):
        server = HUDServer(path=socket_path)
        server.start()
        time.sleep(0.1)

        client = HUDClient(path=socket_path)
        client.connect()
        server.shutdown()
        time.sleep(0.1)

        # Should not raise, just silently fail
        client.send({"type": "state", "state": "idle"})
        client.close()

    def test_server_cleanup_removes_socket(self, socket_path):
        server = HUDServer(path=socket_path)
        server.start()
        time.sleep(0.1)
        assert os.path.exists(socket_path)
        server.shutdown()
        assert not os.path.exists(socket_path)


    def test_client_reconnects_after_server_restart(self, socket_path):
        received = []
        server1 = HUDServer(path=socket_path, on_message=received.append)
        server1.start()
        time.sleep(0.1)

        client = HUDClient(path=socket_path)
        client.connect()

        # Confirm initial connection works
        client.send({"type": "state", "state": "listening"})
        time.sleep(0.1)

        # Shutdown server1 and close client (simulates disconnect)
        server1.shutdown()
        client.close()
        time.sleep(0.1)

        # Send while disconnected — should be a silent no-op (_sock is None)
        client.send({"type": "state", "state": "orphaned"})

        # Start server2 on same path
        server2 = HUDServer(path=socket_path, on_message=received.append)
        server2.start()
        time.sleep(0.1)

        # Reconnect client and send post-reconnect message
        client.reconnect()
        client.send({"type": "state", "state": "reconnected"})
        time.sleep(0.1)

        server2.shutdown()
        client.close()

        assert any(m.get("state") == "listening" for m in received), "First message should have arrived"
        assert any(m.get("state") == "reconnected" for m in received), "Post-reconnect message should have arrived"
        assert not any(m.get("state") == "orphaned" for m in received), "Message while disconnected should be lost"

    def test_send_returns_false_and_recovers_after_stale_socket(self, socket_path):
        """DEF-215: a peer that vanished without the client being told (no
        client.close()) must make send() report failure via its return
        value — this is the actual production case (a HUD overlay process
        restart): `_sock` still looks set right up until a real write
        attempt against it fails, unlike the scenario above which always
        closes the client first and never exercises this path. A
        subsequent reconnect + resend (what hud_send()'s retry-once path
        does immediately on this signal) must succeed.
        """
        received = []
        server = HUDServer(path=socket_path, on_message=received.append)
        server.start()
        time.sleep(0.1)

        client = HUDClient(path=socket_path)
        client.connect()
        assert client._sock is not None

        # Simulate the peer being gone without the client's bookkeeping
        # knowing yet: swap in a socket stand-in whose write always fails,
        # exactly what a live socket to an already-dead peer does on its
        # next write attempt (real-socket teardown timing within a single
        # test process is too racy to rely on here).
        class _DeadSocket:
            def sendall(self, data):
                raise BrokenPipeError()
        client._sock = _DeadSocket()

        assert client.send({"type": "state", "state": "listening"}) is False, (
            "a write against a dead peer must be reported as a failure, "
            "not silently swallowed"
        )
        assert client._sock is None, "a failed send must clear _sock so callers know to reconnect"

        client.reconnect()
        assert client.send({"type": "state", "state": "listening"}) is True
        time.sleep(0.1)
        assert any(m.get("state") == "listening" for m in received)

        client.close()
        server.shutdown()

    def test_send_silently_drops_when_server_gone(self, socket_path):
        server = HUDServer(path=socket_path)
        server.start()
        time.sleep(0.1)

        client = HUDClient(path=socket_path)
        client.connect()

        server.shutdown()
        time.sleep(0.1)

        # All sends should be silent no-ops — no exception raised
        for i in range(10):
            client.send({"type": "state", "state": "idle", "seq": i})

        client.close()
        # Test passes if no exception was raised


class TestMessageSerialization:
    """Verify JSON message format."""

    def test_state_message_format(self):
        msg = {"type": "state", "state": "idle"}
        serialized = json.dumps(msg) + "\n"
        parsed = json.loads(serialized.strip())
        assert parsed["type"] == "state"
        assert parsed["state"] == "idle"

    def test_audio_level_range(self):
        for level in (0.0, 0.5, 1.0):
            msg = {"type": "audio_level", "level": level}
            parsed = json.loads(json.dumps(msg))
            assert 0.0 <= parsed["level"] <= 1.0

    def test_all_message_types_serialize(self):
        messages = [
            {"type": "state", "state": "idle"},
            {"type": "state", "state": "listening"},
            {"type": "state", "state": "processing"},
            {"type": "state", "state": "speaking"},
            {"type": "audio_level", "level": 0.42},
            {"type": "transcript", "text": "hello world"},
            {"type": "tts_start", "text": "speaking now"},
            {"type": "tts_end"},
            {"type": "queue_update", "count": 3},
            {"type": "error", "message": "something broke"},
        ]
        for msg in messages:
            serialized = json.dumps(msg) + "\n"
            parsed = json.loads(serialized.strip())
            assert parsed["type"] == msg["type"]
