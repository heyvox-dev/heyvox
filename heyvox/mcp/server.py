"""
MCP voice server for heyvox.

Exposes voice control tools to LLM agents via the Model Context Protocol.
Run as: python -m heyvox.mcp.server  (stdio transport, registered by heyvox setup)

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

# Safety net: save original stdout then redirect rogue writes to stderr.
# This prevents any third-party library print() calls from corrupting the
# MCP stdio JSON-RPC framing during import and tool execution.
# Restored in the __main__ guard before mcp.run() so FastMCP can use it.
_original_stdout = sys.stdout
sys.stdout = sys.stderr

# ---------------------------------------------------------------------------
# MCP server setup
# ---------------------------------------------------------------------------
from contextlib import asynccontextmanager  # noqa: E402
from collections.abc import AsyncIterator  # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[None]:
    """Start TTS worker on server startup; shut down cleanly on exit."""
    from heyvox.audio.tts import start_worker, shutdown as tts_shutdown
    from heyvox.config import load_config
    config = load_config()
    try:
        start_worker(config)
    except ImportError as exc:
        # kokoro or sounddevice not installed — server still starts.
        # voice_speak calls will fail gracefully at call time with a clear error.
        print(f"vox MCP server: TTS worker not started ({exc}). "
              "Install kokoro + sounddevice for TTS support.", file=sys.stderr)
    try:
        yield
    finally:
        tts_shutdown()


mcp = FastMCP("heyvox", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def voice_speak(text: str, verbosity: str = "full") -> str:
    """Speak text aloud via TTS. verbosity: full|summary|short|skip"""
    from heyvox.audio.tts import speak
    speak(text, verbosity=verbosity)
    return "queued"


@mcp.tool()
def voice_status() -> str:
    """Return current vox state, TTS settings, and style instructions.

    The 'style_instruction' field tells you how to formulate <tts> blocks.
    Always follow the style instruction when writing TTS output.
    """
    from heyvox.constants import RECORDING_FLAG, TTS_PLAYING_FLAG
    from heyvox.audio.tts import is_muted, get_verbosity, get_tts_style, get_tts_style_prompt

    recording = os.path.exists(RECORDING_FLAG)
    speaking = os.path.exists(TTS_PLAYING_FLAG) or os.path.exists("/tmp/herald-playing.pid")

    if recording:
        state = "recording"
    elif speaking:
        state = "speaking"
    else:
        state = "idle"

    import glob
    queue_count = len(glob.glob("/tmp/herald-queue/*.wav"))
    hold_count = len(glob.glob("/tmp/herald-hold/*.wav"))

    return (
        f"state={state} muted={is_muted()} verbosity={get_verbosity()} "
        f"style={get_tts_style()} queue={queue_count} held={hold_count}\n"
        f"style_instruction: {get_tts_style_prompt()}"
    )


@mcp.tool()
def voice_queue(action: str = "list") -> str:
    """Manage TTS queue. action: list|skip|stop|clear|mute|unmute"""
    from heyvox.audio.tts import (
        skip_current, stop_all, clear_queue,
        set_muted, is_muted,
    )
    if action == "list":
        return f"muted={is_muted()}"
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
    """Get or set voice config. action: get|set, key: verbosity|muted|style"""
    from heyvox.audio.tts import (
        set_verbosity, get_verbosity, set_muted, is_muted,
        get_tts_style, set_tts_style, TTS_STYLE_PROMPTS,
    )
    from heyvox.config import load_config

    if action == "get":
        cfg = load_config()
        return (
            f"verbosity={get_verbosity()} "
            f"muted={is_muted()} "
            f"style={get_tts_style()} "
            f"voice={cfg.tts.voice} "
            f"speed={cfg.tts.speed}\n"
            f"available_styles: {', '.join(TTS_STYLE_PROMPTS.keys())}"
        )
    elif action == "set":
        if key == "verbosity":
            set_verbosity(value)
            return f"verbosity set to {value}"
        elif key == "muted":
            set_muted(value.lower() in ("true", "1", "yes"))
            return f"muted set to {value}"
        elif key == "style":
            set_tts_style(value)
            return f"style set to {value}"
        else:
            return f"unsupported key: {key}. Use: verbosity|muted|style"
    else:
        return f"unknown action: {action}. Use: get|set"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Restore original stdout for FastMCP's stdio transport framing.
    # During module import and tool execution, rogue prints went to stderr.
    # Now we hand stdout back to the MCP protocol layer.
    sys.stdout = _original_stdout
    mcp.run(transport="stdio")
