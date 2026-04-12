"""
Chrome companion WebSocket bridge for HeyVox.

Runs a local WebSocket server on 127.0.0.1:9285 that the Chrome extension
connects to. Provides per-tab media state tracking and control (pause/play).

Usage:
    bridge = ChromeBridge(on_state_change=my_callback)
    await bridge.start()        # starts WS server
    await bridge.pause_all()    # pause media in all tabs
    await bridge.play(tab_id)   # resume a specific tab
    tabs = bridge.get_tabs()    # get current tab states
    await bridge.stop()         # shut down

Requirement: CHROME-01
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger("heyvox.chrome.bridge")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9285


@dataclass
class TabState:
    """Media state of a single browser tab."""

    tab_id: int
    state: str  # "playing" | "paused"
    url: str = ""
    title: str = ""


@dataclass
class ChromeBridge:
    """Local WebSocket bridge between HeyVox and the Chrome companion extension."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    on_state_change: Callable[[dict[int, TabState]], None] | None = None

    _tabs: dict[int, TabState] = field(default_factory=dict, init=False, repr=False)
    _clients: set = field(default_factory=set, init=False, repr=False)
    _server: asyncio.Server | None = field(default=None, init=False, repr=False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the WebSocket server."""
        # Import here to keep the module importable without websockets installed
        try:
            from websockets.asyncio.server import serve as _ws_serve
        except ImportError:
            logger.error(
                "websockets package required: pip install websockets"
            )
            raise

        # websockets 13+ uses websockets.asyncio.server.serve (the legacy
        # websockets.server.serve API is deprecated and raises DeprecationWarning).
        self._server = await _ws_serve(
            self._handler,
            self.host,
            self.port,
        ).__aenter__()
        logger.info("Chrome bridge listening on ws://%s:%d", self.host, self.port)

    async def stop(self) -> None:
        """Stop the WebSocket server and disconnect all clients."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._clients.clear()
        self._tabs.clear()
        logger.info("Chrome bridge stopped")

    def get_tabs(self) -> dict[int, TabState]:
        """Return current tab states (snapshot)."""
        return dict(self._tabs)

    def get_playing_tabs(self) -> list[TabState]:
        """Return tabs currently playing media."""
        return [t for t in self._tabs.values() if t.state == "playing"]

    async def pause_all(self) -> None:
        """Send pause command to all connected extension instances."""
        await self._broadcast({"type": "pause"})

    async def pause_tab(self, tab_id: int) -> None:
        """Pause media in a specific tab."""
        await self._broadcast({"type": "pause", "tabId": tab_id})

    async def play_tab(self, tab_id: int) -> None:
        """Resume media in a specific tab."""
        await self._broadcast({"type": "play", "tabId": tab_id})

    async def query_tabs(self) -> None:
        """Request fresh tab state from all extension instances."""
        await self._broadcast({"type": "query"})

    # ------------------------------------------------------------------
    # WebSocket handler
    # ------------------------------------------------------------------

    async def _handler(self, websocket) -> None:
        """Handle a single WebSocket connection from the Chrome extension."""
        self._clients.add(websocket)
        remote = websocket.remote_address
        logger.info("Chrome extension connected from %s", remote)
        try:
            async for raw in websocket:
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                self._handle_message(msg)
        except Exception:
            pass  # Connection closed
        finally:
            self._clients.discard(websocket)
            logger.info("Chrome extension disconnected from %s", remote)

    def _handle_message(self, msg: dict) -> None:
        """Process an incoming message from the extension."""
        msg_type = msg.get("type")

        if msg_type == "tab_state":
            tab_id = msg.get("tabId")
            if tab_id is None:
                return
            state = msg.get("state", "none")
            if state == "none":
                self._tabs.pop(tab_id, None)
            else:
                self._tabs[tab_id] = TabState(
                    tab_id=tab_id,
                    state=state,
                    url=msg.get("url", ""),
                    title=msg.get("title", ""),
                )
            if self.on_state_change:
                self.on_state_change(self._tabs)

        elif msg_type == "tab_closed":
            tab_id = msg.get("tabId")
            if tab_id is not None:
                self._tabs.pop(tab_id, None)
                if self.on_state_change:
                    self.on_state_change(self._tabs)

        elif msg_type == "tab_states":
            # Bulk state update from query response
            tabs = msg.get("tabs", {})
            for tid_str, info in tabs.items():
                try:
                    tid = int(tid_str)
                except (ValueError, TypeError):
                    continue
                self._tabs[tid] = TabState(
                    tab_id=tid,
                    state=info.get("state", "paused"),
                    url=info.get("url", ""),
                    title=info.get("title", ""),
                )
            if self.on_state_change:
                self.on_state_change(self._tabs)

    async def _broadcast(self, msg: dict) -> None:
        """Send a JSON message to all connected extension instances."""
        if not self._clients:
            return
        data = json.dumps(msg)
        dead = set()
        for client in self._clients:
            try:
                await client.send(data)
            except Exception:
                dead.add(client)
        self._clients -= dead


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

async def _run_standalone(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Run the bridge as a standalone server (for heyvox chrome-bridge CLI)."""

    def on_change(tabs: dict[int, TabState]) -> None:
        playing = [t for t in tabs.values() if t.state == "playing"]
        if playing:
            titles = ", ".join(t.title or t.url for t in playing)
            print(f"[bridge] Playing: {titles}", file=sys.stderr)
        else:
            print("[bridge] No media playing", file=sys.stderr)

    bridge = ChromeBridge(host=host, port=port, on_state_change=on_change)
    await bridge.start()
    print(f"HeyVox Chrome bridge running on ws://{host}:{port}", file=sys.stderr)
    print("Install the chrome-extension/ folder in Chrome to connect.", file=sys.stderr)

    try:
        await asyncio.Future()  # Run forever
    except asyncio.CancelledError:
        pass
    finally:
        await bridge.stop()


def run_bridge(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Sync entry point for CLI."""
    try:
        asyncio.run(_run_standalone(host, port))
    except KeyboardInterrupt:
        pass
