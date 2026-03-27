"""
MCP voice server for vox.

Exposes voice control tools to LLM agents via the Model Context Protocol.
Agents call voice_speak to have text read aloud; other tools control
recording state and TTS playback.

MCP tools (planned):
- voice_speak(text, verbosity="full") — speak text via Kokoro TTS
- voice_listen() — trigger a recording session programmatically
- voice_skip() — skip current TTS utterance
- voice_mute(muted) — toggle TTS mute
- voice_status() — return current vox state

Implemented in Phase 4 (MCP Integration).
"""
