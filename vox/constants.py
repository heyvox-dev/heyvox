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

# ---------------------------------------------------------------------------
# TTS engine constants (Phase 3)
# ---------------------------------------------------------------------------

# Maximum number of queued TTS messages; oldest is dropped when cap exceeded.
TTS_MAX_HELD = 5

# Kokoro native output sample rate (Hz)
TTS_SAMPLE_RATE = 24000

# Default Kokoro voice — US English female
TTS_DEFAULT_VOICE = "af_heart"

# Default playback speed multiplier
TTS_DEFAULT_SPEED = 1.0

# Volume added to current system volume before TTS playback (capped at 100)
# Requirement: AUDIO-12
TTS_DEFAULT_VOLUME_BOOST = 10

# System volume is reduced to this percentage of original during TTS playback.
# 0 = mute others entirely, 100 = no ducking, 60 = reduce to 60% of original.
# Requirement: TTS-04
TTS_DEFAULT_DUCKING_PERCENT = 60

# Command file for cross-process CLI control (skip, mute-toggle, quiet, stop)
# Written by vox skip/mute/quiet CLI commands; read and deleted by TTS worker.
TTS_CMD_FILE = "/tmp/vox-tts-cmd"
