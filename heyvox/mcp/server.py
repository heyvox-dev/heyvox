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

Security (DEF-178):
- The default `stdio` transport opens NO network port — it is a per-session
  sidecar over stdin/stdout, with no cross-process surface. This is what
  `heyvox setup` registers.
- The opt-in `streamable-http` server binds 127.0.0.1 only. FastMCP auto-enables
  DNS-rebinding / Origin+Host validation for loopback binds, so a malicious web
  page cannot reach it (a forged Origin is rejected with 403).
- It has NO per-user authentication. On a MULTI-user Mac, another local account
  (or any already-running local process) can call the tools. The blast radius is
  bounded to TTS control — `voice_speak` (say arbitrary text), `voice_queue`
  (skip/stop/mute), `voice_config` (verbosity/style/mute). No filesystem,
  subprocess, or credential-bearing tool is exposed. This is accepted for the
  single-user-Mac target; shared-Mac users should keep the default stdio
  transport (no port) or firewall port 8014.
- NEVER bind a non-loopback host (e.g. 0.0.0.0): it exposes the unauthenticated
  tools to the local network AND silently disables FastMCP's Origin protection.
  `_is_loopback_host()` guards `--host` and warns loudly on override.

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

# DEF-224: without this, HeyVox is plumbed but mute. Herald's Stop hook only
# speaks when the agent's response contains a <tts> block, and nothing else
# teaches the agent that convention — not the README, not `heyvox setup`, not
# the tool docstrings. FastMCP surfaces `instructions` to the client at
# initialize time, which is the one channel that reaches the agent before it
# writes its first response.
_INSTRUCTIONS = """\
HeyVox gives this session a voice on the user's Mac. Speech is generated
locally; nothing is sent to a cloud service.

How to speak
------------
End every response with a <tts>...</tts> block. A shell hook extracts that
block and speaks it aloud. Text outside the block is only read on screen.

    <tts>Tests pass, three files changed.</tts>

The user is often listening rather than reading, so put the key takeaway in
the FIRST sentence — it may be the only one played.

What belongs in a <tts> block
-----------------------------
Plain spoken prose. No markdown, code, URLs, or file paths — they are
unpleasant to listen to. Summarise instead: say "the config file" rather than
reading out a path.

Use <tts>SKIP</tts> only when a response carries genuinely no information for
a listener. When in doubt, speak.

Length and tone
---------------
Call voice_status() to read the user's configured verbosity and style. Its
'style_instruction' field states how long and in what register to speak;
follow it. Re-check it if the user asks you to be briefer or more detailed.

The tools
---------
- voice_speak(text)  — speak immediately, outside the normal response flow
                       (progress updates during long-running work).
- voice_status()     — current state, verbosity, and style instruction.
- voice_queue(...)   — list/skip/stop/clear/mute the playback queue.
- voice_config(...)  — read or change verbosity, mute, and style.

Prefer the <tts> block for ordinary replies; reserve voice_speak() for
speaking at a moment when you are not returning a response.
"""

# stateless_http: tools keep no per-session server state (verbosity/style/mute
# live in shared files), so requests need no session pinning. Clients survive
# server restarts without a session re-handshake. Ignored by stdio transport.
mcp = FastMCP("heyvox", stateless_http=True, instructions=_INSTRUCTIONS)


def _is_loopback_host(host: str) -> bool:
    """True if the bind host is loopback-only (safe for the shared-HTTP server).

    FastMCP's DNS-rebinding / Origin protection only engages for loopback binds,
    and binding a routable address would expose the unauthenticated TTS-control
    tools to the local network. Used to warn loudly if `--host` is overridden
    away from loopback (DEF-178).
    """
    return host.strip().lower() in {
        "127.0.0.1",
        "localhost",
        "::1",
        "::ffff:127.0.0.1",
    }


def _init_tts() -> None:
    """Initialize TTS settings once per server process.

    Deliberately NOT in the FastMCP lifespan: the lowlevel server enters the
    lifespan once per MCP session (per request with stateless_http), and
    start_worker() writes the shared verbosity/style files — that must happen
    once per process, not on every new agent session.

    DEF-228: seed_only, because under the stdio transport there is one server
    process per agent session. An unconditional reset to config defaults would
    wipe the user's runtime verbosity/style every time they open a new session.
    Only the listener daemon owns those files outright.
    """
    from heyvox.audio.tts import start_worker
    from heyvox.config import load_config
    try:
        start_worker(load_config(), seed_only=True)
    except Exception as exc:
        # Degraded start beats a launchd crash loop: tools still respond,
        # voice_speak fails gracefully at call time.
        print(f"vox MCP server: TTS init failed ({exc})", file=sys.stderr)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def voice_speak(text: str, verbosity: str = "full") -> str:
    """Speak text aloud via TTS. verbosity: full|short|skip"""
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
        HERALD_QUEUE_DIR,
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

    return (
        f"state={state} muted={is_muted()} verbosity={get_verbosity()} "
        f"style={get_tts_style()} queue={queue_count}\n"
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
        if not _is_loopback_host(args.host):
            print(
                f"vox MCP server: WARNING — binding non-loopback host "
                f"{args.host!r}. The HTTP server has no per-user authentication, "
                "so this exposes the TTS-control tools to anything that can "
                "reach this address, and FastMCP's browser-Origin protection "
                "only applies to loopback binds. Use 127.0.0.1 (default) on "
                "shared or networked machines.",
                file=sys.stderr,
            )
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
