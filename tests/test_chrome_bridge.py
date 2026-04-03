"""Tests for Chrome companion WebSocket bridge.

Requirement: CHROME-01
"""

import asyncio
import json
import pytest

from heyvox.chrome.bridge import ChromeBridge, TabState, DEFAULT_PORT


# ---------------------------------------------------------------------------
# Unit tests (no WebSocket dependency needed)
# ---------------------------------------------------------------------------


class TestTabState:
    def test_create(self):
        ts = TabState(tab_id=1, state="playing", url="https://youtube.com", title="Video")
        assert ts.tab_id == 1
        assert ts.state == "playing"

    def test_defaults(self):
        ts = TabState(tab_id=2, state="paused")
        assert ts.url == ""
        assert ts.title == ""


class TestChromeBridgeState:
    """Test bridge state management without starting the server."""

    def test_initial_state(self):
        bridge = ChromeBridge()
        assert bridge.get_tabs() == {}
        assert bridge.get_playing_tabs() == []

    def test_handle_tab_state_playing(self):
        bridge = ChromeBridge()
        bridge._handle_message({
            "type": "tab_state",
            "tabId": 42,
            "state": "playing",
            "url": "https://youtube.com/watch?v=abc",
            "title": "Cool Video",
        })
        tabs = bridge.get_tabs()
        assert 42 in tabs
        assert tabs[42].state == "playing"
        assert tabs[42].title == "Cool Video"

    def test_handle_tab_state_none_removes(self):
        bridge = ChromeBridge()
        bridge._handle_message({
            "type": "tab_state",
            "tabId": 42,
            "state": "playing",
            "url": "https://youtube.com",
            "title": "Video",
        })
        assert 42 in bridge.get_tabs()

        bridge._handle_message({
            "type": "tab_state",
            "tabId": 42,
            "state": "none",
        })
        assert 42 not in bridge.get_tabs()

    def test_handle_tab_closed(self):
        bridge = ChromeBridge()
        bridge._handle_message({
            "type": "tab_state",
            "tabId": 10,
            "state": "paused",
            "url": "https://spotify.com",
            "title": "Song",
        })
        assert 10 in bridge.get_tabs()

        bridge._handle_message({"type": "tab_closed", "tabId": 10})
        assert 10 not in bridge.get_tabs()

    def test_handle_bulk_tab_states(self):
        bridge = ChromeBridge()
        bridge._handle_message({
            "type": "tab_states",
            "tabs": {
                "1": {"state": "playing", "url": "https://a.com", "title": "A"},
                "2": {"state": "paused", "url": "https://b.com", "title": "B"},
            },
        })
        tabs = bridge.get_tabs()
        assert len(tabs) == 2
        assert tabs[1].state == "playing"
        assert tabs[2].state == "paused"

    def test_get_playing_tabs(self):
        bridge = ChromeBridge()
        bridge._handle_message({
            "type": "tab_state", "tabId": 1, "state": "playing",
            "url": "https://a.com", "title": "A",
        })
        bridge._handle_message({
            "type": "tab_state", "tabId": 2, "state": "paused",
            "url": "https://b.com", "title": "B",
        })
        bridge._handle_message({
            "type": "tab_state", "tabId": 3, "state": "playing",
            "url": "https://c.com", "title": "C",
        })
        playing = bridge.get_playing_tabs()
        assert len(playing) == 2
        assert all(t.state == "playing" for t in playing)

    def test_state_change_callback(self):
        changes = []
        bridge = ChromeBridge(on_state_change=lambda tabs: changes.append(dict(tabs)))
        bridge._handle_message({
            "type": "tab_state", "tabId": 1, "state": "playing",
            "url": "", "title": "",
        })
        assert len(changes) == 1
        assert 1 in changes[0]

    def test_missing_tab_id_ignored(self):
        bridge = ChromeBridge()
        bridge._handle_message({"type": "tab_state", "state": "playing"})
        assert bridge.get_tabs() == {}

    def test_invalid_message_ignored(self):
        bridge = ChromeBridge()
        bridge._handle_message({"type": "unknown_type"})
        bridge._handle_message({})
        assert bridge.get_tabs() == {}

    def test_state_update_overwrites(self):
        bridge = ChromeBridge()
        bridge._handle_message({
            "type": "tab_state", "tabId": 5, "state": "playing",
            "url": "https://a.com", "title": "Playing",
        })
        bridge._handle_message({
            "type": "tab_state", "tabId": 5, "state": "paused",
            "url": "https://a.com", "title": "Paused now",
        })
        assert bridge.get_tabs()[5].state == "paused"
        assert bridge.get_tabs()[5].title == "Paused now"


class TestBridgeConfig:
    def test_default_host_port(self):
        bridge = ChromeBridge()
        assert bridge.host == "127.0.0.1"
        assert bridge.port == DEFAULT_PORT

    def test_custom_host_port(self):
        bridge = ChromeBridge(host="0.0.0.0", port=9999)
        assert bridge.host == "0.0.0.0"
        assert bridge.port == 9999


# ---------------------------------------------------------------------------
# Integration tests (require websockets)
# ---------------------------------------------------------------------------


def _find_free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.asyncio
async def test_ws_connect_and_send():
    """Extension connects and sends tab state via WebSocket."""
    try:
        import websockets
    except ImportError:
        pytest.skip("websockets not installed")

    port = _find_free_port()
    bridge = ChromeBridge(port=port)
    await bridge.start()
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(json.dumps({
                "type": "tab_state",
                "tabId": 99,
                "state": "playing",
                "url": "https://test.com",
                "title": "Test",
            }))
            await asyncio.sleep(0.1)
            assert 99 in bridge.get_tabs()
            assert bridge.get_tabs()[99].state == "playing"
    finally:
        await bridge.stop()


@pytest.mark.asyncio
async def test_ws_pause_broadcast():
    """Bridge broadcasts pause command to connected clients."""
    try:
        import websockets
    except ImportError:
        pytest.skip("websockets not installed")

    port = _find_free_port()
    bridge = ChromeBridge(port=port)
    await bridge.start()
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(json.dumps({
                "type": "tab_state", "tabId": 1, "state": "playing",
                "url": "", "title": "",
            }))
            await asyncio.sleep(0.05)

            await bridge.pause_all()

            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            assert msg["type"] == "pause"
    finally:
        await bridge.stop()


@pytest.mark.asyncio
async def test_ws_disconnect_cleanup():
    """Client disconnect removes it from broadcast set."""
    try:
        import websockets
    except ImportError:
        pytest.skip("websockets not installed")

    port = _find_free_port()
    bridge = ChromeBridge(port=port)
    await bridge.start()
    try:
        ws = await websockets.connect(f"ws://127.0.0.1:{port}")
        await asyncio.sleep(0.05)
        assert len(bridge._clients) == 1

        await ws.close()
        await asyncio.sleep(0.1)
        assert len(bridge._clients) == 0
    finally:
        await bridge.stop()
