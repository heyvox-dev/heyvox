"""Shared constants for the vox package."""

# IPC flag file: written when recording is active, removed when done.
# Used by TTS orchestrator to pause playback during recording.
RECORDING_FLAG = "/tmp/vox-recording"

# Log file location
LOG_FILE = "/tmp/vox.log"

# launchd service label
LAUNCHD_LABEL = "com.vox.listener"

# Audio defaults (must match openwakeword requirements)
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_CHUNK_SIZE = 1280
