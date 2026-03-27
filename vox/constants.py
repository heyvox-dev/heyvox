"""Shared constants for the vox package."""

# IPC flag file: written when recording is active, removed when done.
# Used by TTS orchestrator to pause playback during recording.
# Requirement: DECP-04
RECORDING_FLAG = "/tmp/vox-recording"

# Default log file location
LOG_FILE_DEFAULT = "/tmp/vox.log"

# Keep LOG_FILE as alias for backward compatibility within package
LOG_FILE = LOG_FILE_DEFAULT

# launchd service label
LAUNCHD_LABEL = "com.vox.listener"

# Audio defaults (must match openwakeword requirements)
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_CHUNK_SIZE = 1280
