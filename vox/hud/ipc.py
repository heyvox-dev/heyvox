"""
Unix socket IPC for HUD communication.

Provides the communication channel between vox.main and the HUD overlay process.
Protocol: newline-delimited JSON messages over a Unix domain socket.

Message types:
- {"type": "state", "state": "recording|idle|transcribing|speaking"}
- {"type": "audio_level", "level": 0.0-1.0}
- {"type": "transcript", "text": "..."}
- {"type": "tts_start", "text": "..."}
- {"type": "tts_end"}
- {"type": "queue_update", "count": N}
- {"type": "error", "message": "..."}

Implemented in Phase 5 (HUD).
"""

SOCKET_PATH = "/tmp/vox-hud.sock"
