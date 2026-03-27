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

# TTS coordination flag (written by TTS process while speaking)
# Echo suppression reads this to mute the mic during TTS playback in speaker mode.
# Requirement: AUDIO-09
TTS_PLAYING_FLAG = "/tmp/vox-tts-playing"

# Ignore TTS flag older than this many seconds.
# Guards against permanent mic mute if TTS process crashes without cleanup.
# Requirement: AUDIO-09
TTS_PLAYING_MAX_AGE_SECS = 60.0
