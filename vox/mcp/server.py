"""
MCP voice server for vox.

Exposes voice control tools to LLM agents via the Model Context Protocol.
Run as: python -m vox.mcp.server  (stdio transport, registered by vox setup)

MCP tools:
- voice_speak(text, verbosity)    -- speak text via Kokoro TTS
- voice_status()                  -- return current vox state
- voice_queue(action)             -- manage TTS queue
- voice_config(action, key, value)-- get or set voice config

Requirements: MCP-01 through MCP-06
"""

# ---------------------------------------------------------------------------
# Stdout protection — MUST be first, before any vox or third-party imports
# (MCP-05: stdout is reserved for stdio transport framing)
# ---------------------------------------------------------------------------
import sys
import os
import logging

logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

# Suppress loguru stdout pollution from kokoro before TTS worker starts.
# loguru defaults to sys.stdout; kokoro calls logger.info() on pipeline init,
# which would corrupt the MCP stdio JSON-RPC framing.
try:
    from loguru import logger as _loguru_logger
    _loguru_logger.remove()
    _loguru_logger.add(sys.stderr, level="WARNING")
except ImportError:
    pass

# ---------------------------------------------------------------------------
# MCP server setup
# ---------------------------------------------------------------------------
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from mcp.server.fastmcp import FastMCP


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[None]:
    """Start TTS worker on server startup; shut down cleanly on exit."""
    from vox.audio.tts import start_worker, shutdown as tts_shutdown
    from vox.config import load_config
    config = load_config()
    start_worker(config)
    try:
        yield
    finally:
        tts_shutdown()


mcp = FastMCP("vox", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def voice_speak(text: str, verbosity: str = "full") -> str:
    """Speak text aloud via TTS. verbosity: full|summary|short|skip"""
    from vox.audio.tts import speak
    speak(text, verbosity=verbosity)
    return "queued"


@mcp.tool()
def voice_status() -> str:
    """Return current vox state and TTS settings"""
    from vox.constants import RECORDING_FLAG, TTS_PLAYING_FLAG
    from vox.audio.tts import is_muted, get_verbosity

    recording = os.path.exists(RECORDING_FLAG)
    speaking = os.path.exists(TTS_PLAYING_FLAG)

    if recording:
        state = "recording"
    elif speaking:
        state = "speaking"
    else:
        state = "idle"

    return f"state={state} muted={is_muted()} verbosity={get_verbosity()}"


@mcp.tool()
def voice_queue(action: str = "list") -> str:
    """Manage TTS queue. action: list|skip|stop|clear|mute|unmute"""
    from vox.audio.tts import (
        skip_current, stop_all, clear_queue,
        set_muted, is_muted, _tts_queue,
    )
    if action == "list":
        return f"queued={_tts_queue.qsize()} muted={is_muted()}"
    elif action == "skip":
        skip_current()
        return "skipped"
    elif action == "stop":
        stop_all()
        return "stopped"
    elif action == "clear":
        clear_queue()
        return "cleared"
    elif action == "mute":
        set_muted(True)
        return "muted"
    elif action == "unmute":
        set_muted(False)
        return "unmuted"
    else:
        return f"unknown action: {action}. Use: list|skip|stop|clear|mute|unmute"


@mcp.tool()
def voice_config(action: str = "get", key: str = "", value: str = "") -> str:
    """Get or set voice config. action: get|set, key: verbosity|muted"""
    from vox.audio.tts import set_verbosity, get_verbosity, set_muted, is_muted
    from vox.config import load_config

    if action == "get":
        cfg = load_config()
        return (
            f"verbosity={get_verbosity()} "
            f"muted={is_muted()} "
            f"voice={cfg.tts.voice} "
            f"speed={cfg.tts.speed}"
        )
    elif action == "set":
        if key == "verbosity":
            set_verbosity(value)
            return f"verbosity set to {value}"
        elif key == "muted":
            set_muted(value.lower() in ("true", "1", "yes"))
            return f"muted set to {value}"
        else:
            return f"unsupported key: {key}. Use: verbosity|muted"
    else:
        return f"unknown action: {action}. Use: get|set"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
