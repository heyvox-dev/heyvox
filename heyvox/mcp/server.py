"""
MCP voice server for heyvox.

Exposes voice control tools to LLM agents via the Model Context Protocol.

Run as:
    python -m heyvox.mcp.server
        stdio transport — one sidecar process per agent session
    python -m heyvox.mcp.server --transport streamable-http [--host H] [--port P]
        shared HTTP server on localhost — ONE process serves all sessions
        (register with: claude mcp add -s user -t http vox http://127.0.0.1:8014/mcp)

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
from mcp.server.fastmcp import FastMCP  # noqa: E402

# stateless_http: tools keep no per-session server state (verbosity/style/mute
# live in shared files), so requests need no session pinning. Clients survive
# server restarts without a session re-handshake. Ignored by stdio transport.
mcp = FastMCP("heyvox", stateless_http=True)


def _init_tts() -> None:
    """Initialize TTS settings once per server process.

    Deliberately NOT in the FastMCP lifespan: the lowlevel server enters the
    lifespan once per MCP session (per request with stateless_http), and
    start_worker() resets the shared verbosity/style files to config defaults
    — that must happen once per process, not on every new agent session.
    """
    from heyvox.audio.tts import start_worker
    from heyvox.config import load_config
    try:
        start_worker(load_config())
    except Exception as exc:
        # Degraded start beats a launchd crash loop: tools still respond,
        # voice_speak fails gracefully at call time.
        print(f"vox MCP server: TTS init failed ({exc})", file=sys.stderr)


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
    from heyvox.constants import (
        RECORDING_FLAG, TTS_PLAYING_FLAG, HERALD_PLAYING_PID,
        HERALD_QUEUE_DIR, HERALD_HOLD_DIR,
    )
    from heyvox.audio.tts import is_muted, get_verbosity, get_tts_style, get_tts_style_prompt
    from heyvox.ipc import read_state
    _ipc_state = read_state()

    # Primary: atomic state file; fallback: legacy flag files
    recording = os.path.exists(RECORDING_FLAG) or _ipc_state.get("recording", False)
    speaking = (
        _ipc_state.get("tts_playing", False)
        or bool(_ipc_state.get("herald_playing_pid"))
        or os.path.exists(TTS_PLAYING_FLAG)
        or os.path.exists(HERALD_PLAYING_PID)
    )

    if recording:
        state = "recording"
    elif speaking:
        state = "speaking"
    else:
        state = "idle"

    import glob
    queue_count = len(glob.glob(HERALD_QUEUE_DIR + "/*.wav"))
    hold_count = len(glob.glob(HERALD_HOLD_DIR + "/*.wav"))

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
    import argparse

    parser = argparse.ArgumentParser(description="heyvox MCP voice server")
    parser.add_argument(
        "--transport", choices=["stdio", "streamable-http"], default="stdio",
        help="stdio: per-session sidecar (default). "
             "streamable-http: shared localhost server for all sessions.",
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind host for streamable-http (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8014,
                        help="bind port for streamable-http (default: 8014)")
    args = parser.parse_args()

    _init_tts()

    # Restore original stdout. stdio needs it for JSON-RPC framing; for
    # streamable-http it is just regular process output (launchd log).
    sys.stdout = _original_stdout

    if args.transport == "streamable-http":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
