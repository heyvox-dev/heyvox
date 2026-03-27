# Research: CLI vs MCP for Voice Integration with AI Coding Agents

**Date:** 2026-03-27
**Sources:** GitHub issues, blog posts, project READMEs, Spokenly docs, community articles

---

## TL;DR

The community is converging on a **hybrid consensus**: MCP for voice OUTPUT (agent-initiated speech), OS-level/CLI for voice INPUT (user-initiated dictation). Pure MCP voice has real friction — tool approval prompts, stdio single-client lock, context window bloat. But MCP remains the only standardized way for agents to *initiate* voice. The best voice tools use MCP sparingly and bypass it for the hot path.

---

## 1. The Landscape (March 2026)

### Three competing approaches exist:

| Approach | Examples | Voice IN | Voice OUT |
|----------|----------|----------|-----------|
| **MCP-only** | Spokenly, VoiceMode MCP | Agent calls `ask_user_dictation` tool | Agent calls `voice_speak` tool |
| **Hooks/CLI-only** | claude-code-hooks (shanraisshan), TTS hooks (cg/claude-code-tts) | Hooks on events, shell scripts | Hook triggers TTS on assistant response |
| **Native** | Claude Code `/voice` (March 2026) | Push-to-talk (spacebar), built-in | Not yet — output is text only |
| **Hybrid (Vox model)** | Vox (this project) | OS-level (wake word + STT + paste) | MCP tool (`voice_speak`) |

### Claude Code now has native voice (March 2026)
- Activated via `/voice` command
- Push-to-talk: hold spacebar, release to send
- Rolling out to ~5% of users initially
- Optimized for technical terms and repo names
- 20 languages supported
- **No wake word, no TTS output, no HUD** — input only

---

## 2. Why People Recommend CLI/Hooks Over MCP for Voice

### 2.1 Tool Approval Friction (THE #1 complaint)

This is the most documented pain point. From GitHub issue #10801 (28 upvotes, closed NOT_PLANNED):

> "Every MCP tool call triggers an approval dialog... no 'Always Allow' or 'Remember this choice'"

> "Cannot run automation overnight. Defeats the entire purpose of MCP servers."

> "The current model provides security theater without actual usability."

From issue #25966, a user reported **47 MCP approval prompts in a single research session** for read-only tools.

**Workaround exists but is clunky:** Add `mcp__servername` to `permissions.allow` in settings.json. No wildcards supported — must list each server explicitly.

**Impact on voice:** Every `voice_speak` or `voice_listen` call would require approval unless pre-configured. This breaks conversational flow entirely. Spokenly handles this by having the user approve once at the start, but the UX is still a speed bump.

### 2.2 Context Window Bloat

From the "Why I Switched from MCP to CLI" article (dev.to/allentcm):

> "MCP servers consumed 40-50% of the context budget before accomplishing anything useful."
> "An Atlassian MCP server alone dumped 73 tools with full schemas into memory."

Anthropic's own engineering blog acknowledged: "Tool results and definitions can sometimes consume 50,000+ tokens before an agent reads a request."

**Impact on voice:** A voice MCP server with 5-6 tools (listen, speak, status, queue, config, cancel) adds schema overhead every session. Not catastrophic, but unnecessary for the hot path (voice input).

### 2.3 stdio = Single Client Lock

From MCP architecture docs:

> "Local MCP servers that use the STDIO transport typically serve a single MCP client."

**Impact on voice:** If Vox runs as an stdio MCP server for Claude Code, Cursor cannot connect simultaneously. Streamable HTTP solves this but adds complexity and isn't universally supported yet.

Spokenly works around this by running an HTTP server on localhost:51089 for Cursor while using a stdio bridge for Claude Code (to avoid Claude Code's 60-second HTTP timeout).

### 2.4 Training Data Mismatch

From the dev.to article:

> "LLMs were trained extensively on CLI interactions from Stack Overflow, GitHub, and documentation. MCP schemas? Zero training data. The model is improvising every time."

**Impact on voice:** Less relevant for voice specifically, but means the LLM is less reliable at calling MCP voice tools correctly vs. a simple shell command.

### 2.5 Latency

No hard benchmarks found, but the architecture difference is clear:
- **CLI/OS-level:** Wake word detected -> STT -> paste text (direct, ~1-2 hops)
- **MCP:** Agent decides to listen -> MCP tool call -> approval -> server processes -> result returned -> agent processes (4-5 hops minimum)

For voice INPUT, the OS-level path is fundamentally faster because it doesn't require the LLM to be in the loop.

---

## 3. Why MCP Still Matters for Voice

### 3.1 Agent-Initiated Voice (Voice OUT)

There is no CLI/hook equivalent for "the agent decides to speak." Hooks fire on fixed events (session start, tool use, response complete), but they can't express "speak this specific text with this priority." MCP's `voice_speak` tool is the only clean way for the LLM to initiate speech.

### 3.2 Structured Context

Spokenly's `ask_user_dictation` returns structured data the agent can reason about. CLI paste is just raw text appearing in the input field — the agent doesn't know it came from voice, can't ask follow-up voice questions, can't adjust behavior based on voice vs. keyboard input.

### 3.3 Multi-Agent Standard

MCP is the emerging standard. Claude Code, Cursor, Codex CLI, Claude Desktop all support it. A voice MCP server works (in theory) with all of them. A CLI approach needs per-tool adapters.

### 3.4 Discoverability

MCP tools appear in the agent's tool list. The agent knows voice is available and can proactively use it. With CLI/hooks, voice is invisible to the agent.

---

## 4. What Spokenly Actually Does

Spokenly is the closest competitor using the MCP approach:

- **MCP tool:** `ask_user_dictation` — agent calls it when needing voice input
- **Transport:** stdio bridge for Claude Code (avoids 60s HTTP timeout), localhost:51089 for Cursor
- **Free tier:** Local models (Whisper, Parakeet) with no limits
- **Pro tier:** Cloud STT (GPT-4o Transcribe, Deepgram Nova, Groq Whisper)
- **No wake word, no TTS, no HUD** — input only, agent-initiated only

Key architectural choice: the agent decides WHEN to ask for voice. The user doesn't initiate — they respond when prompted. This is the opposite of Vox's wake-word model.

---

## 5. What VoiceMode Does

VoiceMode MCP (mbailey/voicemode) takes a different MCP approach:

- **MCP tools:** `converse` and `service` management
- **Requires:** OpenAI API key or compatible service
- **Optional:** Local STT/TTS via Whisper.cpp and Kokoro
- **Pre-approval config:** Documents how to add `mcp__voicemode__converse` to permissions.allow (acknowledging the friction)
- **LiveKit support** for room-based communication

---

## 6. The Hooks Approach (claude-code-hooks)

shanraisshan/claude-code-hooks uses Claude Code's hook system (26 hook events):

- Hooks fire on: session start, tool use, agent response, completion, etc.
- TTS triggered by hooks, not MCP tool calls
- **No tool approval needed** — hooks are native Claude Code events
- **No agent awareness** — the agent doesn't know speech is happening
- **No voice input** — output/feedback only

Other TTS hook implementations (cg/claude-code-tts, ktaletsk/claude-code-tts) follow the same pattern: hook into assistant responses, pipe to TTS engine, play audio. Simple, zero-friction, but one-directional.

---

## 7. Community Consensus Summary

| Dimension | Consensus |
|-----------|-----------|
| **Voice INPUT** | OS-level/CLI wins. Don't route through MCP — too slow, unnecessary friction. Wake word or push-to-talk -> STT -> paste. |
| **Voice OUTPUT** | MCP is the right tool. Agent needs to decide what to say. Hooks work for simple "read everything aloud" but can't do selective/contextual speech. |
| **Tool approval** | Real problem, workaround exists (permissions.allow), but UX is poor. Pre-configure or lose users. |
| **stdio limitation** | Real problem for multi-agent. HTTP transport is the future but not universally supported. Spokenly runs both. |
| **Context bloat** | Minor for a small voice server (5-6 tools). Major concern is overblown for focused servers. |
| **Native /voice** | Will commoditize basic voice input. Differentiators become: wake word, TTS, HUD, multi-agent, offline. |

---

## 8. Implications for Vox

### Vox's hybrid model is validated by community sentiment:

1. **Voice IN via OS-level** — community agrees MCP is wrong for input. Vox's wake word -> STT -> osascript paste bypasses all MCP friction.

2. **Voice OUT via MCP** — this is where MCP adds value. `voice_speak` lets the agent decide when/what to speak. No alternative exists.

3. **Pre-configure permissions** — document `mcp__voice` in permissions.allow. Make `vox setup` offer to add this automatically.

4. **Support both transports** — stdio for single-client (Claude Code), Streamable HTTP for multi-client (Cursor + Claude Code + Claude Desktop simultaneously).

5. **Keep the MCP server lean** — 4-5 tools max to minimize context overhead: `voice_speak`, `voice_status`, `voice_config`, `voice_queue`.

6. **Hooks as optional bonus** — offer a TTS hook for users who just want "read responses aloud" without MCP complexity.

7. **Wake word + HUD remain key differentiators** — native `/voice` has no wake word, no always-on listening, no visual feedback. Spokenly has no wake word, no TTS. VoiceMode requires API keys. Vox's full loop remains unique.

---

## Sources

- [Spokenly - Voice Input for Claude Code via MCP](https://spokenly.app/blog/voice-dictation-for-developers/claude-code)
- [Spokenly - Voice Input for Cursor AI](https://spokenly.app/blog/voice-dictation-for-developers/cursor)
- [Why I Switched from MCP to CLI (dev.to)](https://dev.to/allentcm/why-i-switched-from-mcp-to-cli-3ifb)
- [On CLIs vs. MCP (Hugging Face blog)](https://huggingface.co/blog/nielsr/mcp-vs-cli)
- [GitHub #10801: No way to bypass MCP tool approval prompts in VSCode](https://github.com/anthropics/claude-code/issues/10801)
- [GitHub #25966: Allow permanent auto-approval of read-only MCP tools](https://github.com/anthropics/claude-code/issues/25966)
- [GitHub #28580: MCP tools prompt for permission even when authorized](https://github.com/anthropics/claude-code/issues/28580)
- [VoiceMode MCP (mbailey/voicemode)](https://github.com/mbailey/voicemode)
- [claude-code-hooks (shanraisshan)](https://github.com/shanraisshan/claude-code-hooks)
- [mcp-voice-hooks (johnmatthewtennant)](https://github.com/johnmatthewtennant/mcp-voice-hooks)
- [Claude Code March 2026 Updates](https://pasqualepillitteri.it/en/news/381/claude-code-march-2026-updates)
- [VoiceMode MCP site](https://getvoicemode.com/)
- [MCP Architecture Overview](https://modelcontextprotocol.io/docs/learn/architecture)
- [Claude Code MCP docs](https://code.claude.com/docs/en/mcp)
- [Claude Code Permissions docs](https://code.claude.com/docs/en/permissions)
