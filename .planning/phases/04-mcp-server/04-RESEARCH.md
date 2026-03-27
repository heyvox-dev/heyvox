# Phase 4: MCP Server - Research

**Researched:** 2026-03-27
**Domain:** MCP Python SDK (FastMCP), stdio transport, async tool handlers, cross-process IPC with existing TTS engine
**Confidence:** HIGH (MCP SDK verified via Context7), HIGH (existing codebase fully read), MEDIUM (tool schema best practices)

---

## Summary

Phase 4 wires the existing Kokoro TTS engine (Phase 3) and main-loop state (Phase 2) to MCP tools so AI agents can call `voice_speak` and friends. The MCP Python SDK (v1.x) ships a high-level `FastMCP` class that reduces server setup to decorated functions — no protocol plumbing needed. The entire server runs as a standalone module (`python -m vox.mcp.server`) invoked via stdio by Claude Code or any MCP client.

The central integration challenge is that the MCP server is an async process while the TTS engine (`vox/audio/tts.py`) is thread-based and the main voice loop runs in a separate launchd process. The MCP server does NOT embed the main loop — it is a thin IPC layer. For `voice_speak` it calls `vox.audio.tts.speak()` directly (same process, the MCP server starts its own TTS worker). For `voice_status` and `voice_queue` control it reads/writes the same flag files and command files (`/tmp/vox-tts-playing`, `/tmp/vox-tts-cmd`, `/tmp/vox-recording`) already used by the CLI. `voice_config` reads and live-edits `~/.config/vox/config.yaml`.

The key prior decision (Phase 3) that shapes this phase: `vox setup` already writes the MCP server entry (`sys.executable -m vox.mcp.server`) to `~/.claude/settings.json`. The MCP server module at `vox/mcp/server.py` is currently a stub. Phase 4 fills it in.

**Primary recommendation:** Use `FastMCP` (not low-level `Server`) for all four tools. Run via `mcp.run(transport="stdio")`. Start the TTS worker in a lifespan context manager so it is running before the first tool call and shuts down cleanly. Keep all tool docstrings under 100 characters — they appear in the LLM context window.

---

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| mcp | >=1.0 (latest 1.12.4) | MCP SDK: FastMCP, stdio transport, tool decorators | Official Anthropic SDK; already declared in REQUIREMENTS.md (PROJ-02); only MCP implementation |
| vox.audio.tts | (internal) | TTS engine — speak, skip_current, stop_all, clear_queue, set_muted, is_muted, set_verbosity, get_verbosity | Already fully implemented in Phase 3; server imports and uses directly |
| vox.config | (internal) | load_config, VoxConfig, CONFIG_FILE | Already fully implemented in Phase 1 |
| vox.constants | (internal) | TTS_CMD_FILE, TTS_PLAYING_FLAG, RECORDING_FLAG | Flag file paths shared across all processes |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| asyncio | stdlib | Async runtime for FastMCP | FastMCP tools can be sync or async; sync preferred (TTS calls are non-blocking enqueue) |
| threading | stdlib | TTS worker is thread-based; MCP server bridges async→thread | Use asyncio.get_event_loop().run_in_executor() if a TTS call needs to block |
| os, pathlib | stdlib | Flag file reads, config file path | read TTS_PLAYING_FLAG, RECORDING_FLAG existence |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| FastMCP | Low-level mcp.server.Server | Low-level requires manual handler wiring (list_tools, call_tool decorators); FastMCP is functionally identical but 5x less boilerplate; only use low-level for custom protocol control not needed here |
| `mcp.run(transport="stdio")` | HTTP streamable transport | stdio is correct for Claude Code local tool calls; HTTP is for remote/multi-client (v2 scope per ADVV-05) |
| Sync tool functions | Async tool functions | Sync is cleaner when all operations are fire-and-forget enqueue; use async only if needing `await ctx.info(...)` for logging |
| Dedicated IPC socket to main loop | Flag file reads | A dedicated socket would give richer state (is the mic actually hot right now?); flag files are simpler, already exist, and are sufficient for v1 status |

**Installation (add to pyproject.toml):**
```bash
# mcp is already in REQUIREMENTS.md but NOT yet in pyproject.toml dependencies
# It must be added:
pip install "mcp>=1.0"
```

---

## Architecture Patterns

### How the MCP Server Fits in the System

```
Claude Code (MCP client)
  │  stdio
  ▼
vox/mcp/server.py  ← Phase 4 target
  │ imports
  ├─► vox.audio.tts (speak, skip_current, stop_all, set_muted, set_verbosity...)
  │     └── TTS worker thread + Kokoro KPipeline (started in lifespan)
  ├─► vox.config (load_config, VoxConfig, CONFIG_FILE)
  └─► /tmp/vox-recording (flag file — read-only, written by main loop)
      /tmp/vox-tts-playing (flag file — read-only, written by TTS worker)
      /tmp/vox-tts-cmd (command file — written by server for cross-process control)

vox main loop (separate launchd process)
  └── reads /tmp/vox-tts-cmd to handle skip/mute from CLI
      writes /tmp/vox-recording during recording
      writes /tmp/vox-tts-playing via tts.py worker
```

**Important:** The MCP server process runs its OWN TTS worker thread (independent from the launchd main loop). `voice_speak` enqueues to this server's TTS worker. `vox skip`/`vox mute` CLI commands write to `TTS_CMD_FILE` which the server's worker also reads. This means CLI control commands work cross-process for both the launchd listener and any MCP server instance.

### Recommended Module Structure (Phase 4)

```
vox/mcp/
├── __init__.py    (exists, empty)
└── server.py      (FILL IN — currently a docstring-only stub)
```

### Pattern 1: FastMCP Server Entry Point with Lifespan

**What:** Create FastMCP instance, register tools, start TTS worker in lifespan, run on stdio.
**When to use:** Module-level entrypoint `python -m vox.mcp.server`

```python
# Source: Context7 /modelcontextprotocol/python-sdk (verified)
import sys
import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from mcp.server.fastmcp import FastMCP

# MCP-05: All logging to stderr
logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[None]:
    """Start TTS worker on server startup, shut down cleanly."""
    from vox.audio.tts import start_worker, shutdown as tts_shutdown
    from vox.config import load_config
    config = load_config()
    start_worker(config)
    try:
        yield
    finally:
        tts_shutdown()

mcp = FastMCP("vox", lifespan=lifespan)

# Tools registered below with @mcp.tool()

if __name__ == "__main__":
    mcp.run(transport="stdio")
```

### Pattern 2: Tool Registration — Sync Preferred

**What:** Register tools with `@mcp.tool()`. Sync functions are fine because TTS calls are non-blocking enqueue operations (speak() returns immediately).

```python
# Source: Context7 /modelcontextprotocol/python-sdk (verified)
from mcp.server.fastmcp import FastMCP

@mcp.tool()
def voice_speak(text: str, verbosity: str = "full") -> str:
    """Speak text via TTS. verbosity: full|summary|short|skip"""
    from vox.audio.tts import speak
    speak(text, verbosity=verbosity)
    return "queued"
```

Key rules for tool schemas (MCP-06 — lean schemas):
- Docstring becomes the tool description in the LLM context window — keep it under 100 chars
- Parameter descriptions come from `Annotated[str, Field(description="...")]` or just the name
- Return `str` (not dict) wherever possible — simpler context window footprint
- Avoid Optional parameters with complex defaults — agents get confused

### Pattern 3: `voice_status` — Read Flag Files

**What:** Read flag files to determine current state. No IPC to main loop needed — files are truth.

```python
@mcp.tool()
def voice_status() -> str:
    """Return current vox state: idle|recording|speaking|transcribing"""
    import os
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
```

**Note:** "transcribing" state would require additional IPC to the main loop's `busy` flag — not worth it for v1. Approximation: if not recording and not speaking → "idle" covers transcribing too.

### Pattern 4: `voice_queue` — Multi-Action Tool

**What:** Single tool handles list/skip/stop/clear/mute/unmute. Reduces tool count (MCP-06).

```python
@mcp.tool()
def voice_queue(action: str = "list") -> str:
    """Manage TTS queue. action: list|skip|stop|clear|mute|unmute"""
    from vox.audio.tts import (
        skip_current, stop_all, clear_queue,
        set_muted, is_muted, _tts_queue
    )
    if action == "list":
        size = _tts_queue.qsize()
        muted = is_muted()
        return f"queued={size} muted={muted}"
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
```

**Note:** `_tts_queue` is a module-level Queue in `vox.audio.tts`. Accessing it directly from the server is fine since they share the same process. The `.qsize()` method is best-effort (not atomic) but sufficient for status reporting.

### Pattern 5: `voice_config` — Get/Set Runtime Config

**What:** Read current config for GET, live-edit YAML file for SET. SET does NOT restart the TTS worker — verbosity and mute changes take effect immediately via the tts module's module-level state.

```python
@mcp.tool()
def voice_config(action: str = "get", key: str = "", value: str = "") -> str:
    """Get or set voice config. action: get|set. key: verbosity|voice|speed|muted"""
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
            set_verbosity(value)  # Takes effect immediately in TTS module
            return f"verbosity set to {value}"
        elif key == "muted":
            set_muted(value.lower() in ("true", "1", "yes"))
            return f"muted set to {value}"
        else:
            return f"unsupported key: {key}. Use: verbosity|muted"
    else:
        return f"unknown action: {action}. Use: get|set"
```

**Note:** Persisting changes to `config.yaml` is NOT required for v1. Live state changes (verbosity, muted) apply to the running session. YAML writes add complexity and are better deferred.

### Anti-Patterns to Avoid

- **Printing to stdout**: Any `print()` in the MCP server corrupts the stdio transport. Use `sys.stderr.write()` or Python `logging` with `stream=sys.stderr`.
- **Importing heavy deps at module top**: `from kokoro import KPipeline` at top level causes multi-second import delay every time Claude Code starts the server. Use lazy imports or the lifespan pattern.
- **Blocking tool functions**: If a tool call blocks (e.g., waits for TTS to finish), it hangs the MCP server for all other tools during that time. `speak()` must enqueue and return immediately.
- **Too many tools**: Each tool burns ~100-300 tokens in the LLM context window just for its schema. 4-5 tools max (MCP-06). Combine related operations (e.g., all queue actions into one `voice_queue` tool).
- **Complex return types**: Returning dicts/Pydantic models causes the SDK to serialize them as structured output — larger schema, more context burn. Return `str` for status tools.
- **TTS worker started at import time**: Starting `start_worker()` at module level means it starts even if the module is just imported for testing. Use the lifespan pattern.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| MCP protocol framing | Custom JSON-RPC | `mcp.server.fastmcp.FastMCP` | Protocol details (chunked reads, capability negotiation, error codes) are complex and version-sensitive |
| Tool schema validation | Manual type checks | FastMCP type annotations | FastMCP generates JSON Schema from Python type hints automatically |
| Stdio framing | Manual stdin/stdout | `mcp.run(transport="stdio")` | Handles newline framing, message delimiters, and stream flushing |
| Async event loop | `asyncio.run(main())` by hand | `mcp.run(transport="stdio")` | FastMCP manages the event loop, signal handlers, and clean shutdown |

**Key insight:** FastMCP turns the MCP server into ~50 lines of Python. The SDK handles all protocol complexity. The real work in Phase 4 is integrating with the existing TTS module, not protocol implementation.

---

## Common Pitfalls

### Pitfall 1: stdout Pollution Corrupts MCP Transport
**What goes wrong:** Any output to stdout (print, logging to stdout, third-party library writes) corrupts the stdio JSON-RPC framing. The MCP client receives malformed messages and disconnects silently or with cryptic errors.
**Why it happens:** MCP stdio uses stdout exclusively for protocol messages. Claude Code launches the server process and reads stdout as a byte stream.
**How to avoid:** Set `logging.basicConfig(stream=sys.stderr)` at module top before any imports. Patch `sys.stdout` as a guard: `sys.stdout = sys.stderr`. Check all third-party imports for stdout writes (kokoro uses loguru which writes to stdout by default — redirect it).
**Warning signs:** Claude Code shows "server disconnected" immediately after connecting, or tool calls return parse errors.

### Pitfall 2: Kokoro/loguru Writes to stdout
**What goes wrong:** `kokoro` (and its dependency `loguru`) write initialization messages to stdout by default. This corrupts the MCP stdio transport the first time TTS is invoked.
**Why it happens:** loguru's default sink is `sys.stdout`. kokoro calls `logger.info()` on pipeline init.
**How to avoid:** Redirect loguru before importing kokoro:
```python
import sys
from loguru import logger
logger.remove()                          # Remove default stdout sink
logger.add(sys.stderr, level="WARNING") # Add stderr sink instead
```
Or in the lifespan, before `start_worker()`:
```python
try:
    from loguru import logger
    logger.remove()
    logger.add(sys.stderr, level="WARNING")
except ImportError:
    pass
```
**Warning signs:** First `voice_speak` call causes MCP disconnect.

### Pitfall 3: TTS Worker Not Started Before First Tool Call
**What goes wrong:** `voice_speak` calls `speak()` which enqueues to `_tts_queue`, but if `start_worker()` was never called, the queue fills up and nothing plays. No error is raised.
**Why it happens:** The TTS module is designed to be a no-op if the worker isn't started (graceful degradation for non-TTS environments).
**How to avoid:** Use the lifespan pattern to guarantee `start_worker(config)` runs before any tool handler. Don't call `start_worker()` lazily inside the tool function (risk of double-start race).
**Warning signs:** `voice_speak` returns "queued" but no audio plays.

### Pitfall 4: Module-Level State Shared With Launchd Main Process
**What goes wrong:** The developer assumes the MCP server shares state with the running `vox start --daemon` process (same TTS queue, same mute state). It does NOT — they are separate OS processes.
**Why it happens:** Both processes import `vox.audio.tts` and use its module-level globals, but these are per-process. The MCP server has its own TTS worker, its own `_muted` flag, etc.
**How to avoid:** Accept this as the design. Cross-process coordination uses flag files (`TTS_CMD_FILE`, `TTS_PLAYING_FLAG`, `RECORDING_FLAG`) which both processes read/write. The `voice_status` tool should read the flag files (shared state) not the module variables (process-local state) for the recording/speaking bits.
**Warning signs:** `vox mute` (CLI) and `voice_queue(action="mute")` (MCP) appear to work independently of each other — they do, by design.

### Pitfall 5: `__main__` Guard Missing
**What goes wrong:** `mcp.run()` is called at module import time (not inside `if __name__ == "__main__"`). Any `import vox.mcp.server` (e.g., in tests) starts the event loop.
**Why it happens:** Easy oversight when porting from script to module.
**How to avoid:** Always guard: `if __name__ == "__main__": mcp.run(transport="stdio")`
**Warning signs:** `import vox.mcp.server` in tests hangs forever.

---

## Code Examples

Verified patterns from Context7 (MCP Python SDK v1.12.4):

### Minimal FastMCP Server (stdio)
```python
# Source: Context7 /modelcontextprotocol/python-sdk
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("vox")

@mcp.tool()
def my_tool(param: str) -> str:
    """Tool description (appears in LLM context)"""
    return "result"

if __name__ == "__main__":
    mcp.run(transport="stdio")
```

### Lifespan Context Manager
```python
# Source: Context7 /modelcontextprotocol/python-sdk
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from mcp.server.fastmcp import FastMCP

@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[None]:
    # startup
    resource.initialize()
    try:
        yield
    finally:
        # shutdown
        resource.cleanup()

mcp = FastMCP("vox", lifespan=lifespan)
```

### Logging to stderr only (MCP-05)
```python
# Source: MCP-05 requirement + Python logging docs
import sys
import logging
logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

# If kokoro/loguru is present, redirect it too:
try:
    from loguru import logger
    logger.remove()
    logger.add(sys.stderr, level="WARNING")
except ImportError:
    pass
```

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `mcp.server.Server` (low-level) | `FastMCP` (high-level) | MCP SDK v1.0 | 80% less boilerplate for tool servers |
| `launchctl load/unload` (deprecated) | `launchctl bootstrap/bootout` | macOS 10.15+ | Already handled in Phase 3 |
| `stdio` transport only | `stdio` + `streamable-http` | MCP SDK v1.0 | stdio is still correct for local Claude Code integration; HTTP is multi-client |

**Deprecated/outdated:**
- `mcp.server.Server` direct usage: Still works but verbose. Use FastMCP unless you need custom capability negotiation.
- Tool return as `list[types.TextContent]`: Low-level pattern. FastMCP auto-wraps `str` returns.

---

## Open Questions

1. **Should `voice_speak` block until TTS finishes, or return immediately?**
   - What we know: `speak()` enqueues and returns immediately; agents can call `voice_status` to poll
   - What's unclear: Whether agents expect the tool call to block (synchronous) or fire-and-forget
   - Recommendation: Return immediately with "queued". Blocking would hang the MCP server for the duration of speech. Agents that need sync behavior can poll `voice_status`.

2. **Should the MCP server also write to `TTS_CMD_FILE` for cross-process control?**
   - What we know: `voice_queue(action="skip")` calls `skip_current()` which only affects the MCP server's own TTS worker, not the launchd main process
   - What's unclear: Whether users expect `voice_queue` to also stop audio from the running daemon
   - Recommendation: For v1, only control the MCP server's own worker. Document this. Cross-process control can be added by also writing to `TTS_CMD_FILE` in v1.1.

3. **pyproject.toml: `mcp` not currently listed as a dependency**
   - What we know: REQUIREMENTS.md mentions MCP (PROJ-02), but `pyproject.toml` only has: openwakeword, pyaudio, numpy, PyYAML, pydantic, platformdirs, sherpa-onnx, pyobjc-*
   - Recommendation: Add `"mcp>=1.0"` to `[project.dependencies]` in pyproject.toml as part of Phase 4.

4. **`vox.mcp.server` as `__main__` module: does `python -m vox.mcp.server` work with the current package layout?**
   - What we know: `vox/mcp/__init__.py` exists (empty). `vox/mcp/server.py` exists (stub). `python -m vox.mcp.server` should work.
   - Recommendation: Verify this actually runs without `__main__.py` — it should since Python supports `python -m package.module`.

---

## Sources

### Primary (HIGH confidence)
- Context7 `/modelcontextprotocol/python-sdk` — FastMCP tool registration, lifespan, stdio transport, logging
- `/Users/work/conductor/workspaces/vox-v2/mogadishu/vox/audio/tts.py` — Full TTS API: speak(), skip_current(), stop_all(), clear_queue(), set_muted(), is_muted(), set_verbosity(), get_verbosity(), start_worker(), shutdown()
- `/Users/work/conductor/workspaces/vox-v2/mogadishu/vox/constants.py` — TTS_CMD_FILE, TTS_PLAYING_FLAG, RECORDING_FLAG paths
- `/Users/work/conductor/workspaces/vox-v2/mogadishu/vox/config.py` — VoxConfig, load_config(), CONFIG_FILE
- `/Users/work/conductor/workspaces/vox-v2/mogadishu/vox/setup/wizard.py` — Existing MCP settings.json writer (MCP-07 already done)
- `/Users/work/conductor/workspaces/vox-v2/mogadishu/pyproject.toml` — `mcp` not yet in dependencies

### Secondary (MEDIUM confidence)
- `.planning/phases/03-cli-tts-output/03-02-SUMMARY.md` — Prior decisions: sys.executable in settings.json, MCP auto-approve pattern
- Prior decision log in CLAUDE.md: "MCP auto-approve writes to ~/.claude/settings.json mcpServers key with sys.executable for portability"

---

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — MCP SDK verified via Context7; internal imports verified by reading source
- Architecture: HIGH — IPC boundaries clear from reading all three process types; flag file paths confirmed in constants.py
- Pitfalls: HIGH — stdout corruption is a well-known MCP pitfall; loguru issue is specific and verified by reading kokoro dependency chain
- Tool design: MEDIUM — tool schema best practices are somewhat subjective; lean toward simple string returns

**Research date:** 2026-03-27
**Valid until:** 2026-04-27 (MCP SDK is actively developed; re-check if >30 days pass)

---

## Pre-Planning Summary

Phase 4 is straightforward given the Phase 3 foundation:

1. **`vox/mcp/server.py`**: Fill the existing stub with ~80-100 lines. Four tools: `voice_speak`, `voice_status`, `voice_queue`, `voice_config`. FastMCP lifespan starts/stops the TTS worker. All logging to stderr.

2. **`pyproject.toml`**: Add `"mcp>=1.0"` to `[project.dependencies]`.

3. **MCP-07 (auto-approve)**: Already done in Phase 3 wizard (`vox/setup/wizard.py` Step 7). Phase 4 only needs to verify the module path `vox.mcp.server` matches what the wizard writes.

4. **No new files needed** beyond filling `vox/mcp/server.py`.

5. **One gotcha to resolve first**: loguru stdout pollution from kokoro must be suppressed before the lifespan starts the TTS worker.
