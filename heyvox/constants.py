"""Shared constants for the heyvox package."""

# IPC flag file: written when recording is active, removed when done.
# Used by TTS orchestrator to pause playback during recording.
# Requirement: DECP-04
RECORDING_FLAG = "/tmp/heyvox-recording"

# Default log file location
LOG_FILE_DEFAULT = "/tmp/heyvox.log"

# Keep LOG_FILE as alias for backward compatibility within package
LOG_FILE = LOG_FILE_DEFAULT

# launchd service label
LAUNCHD_LABEL = "com.heyvox.listener"

# Audio defaults (must match openwakeword requirements)
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_CHUNK_SIZE = 1280

# TTS coordination flag (written by TTS process while speaking)
# Echo suppression reads this to mute the mic during TTS playback in speaker mode.
# Requirement: AUDIO-09
TTS_PLAYING_FLAG = "/tmp/heyvox-tts-playing"

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

# Grace periods (seconds) — breathing room between audio transitions.
# Prevents jarring jumps between dictation, TTS, and media.
GRACE_AFTER_RECORDING = 1.0   # Pause after dictation ends before TTS starts
GRACE_BETWEEN_TTS = 0.5       # Pause between consecutive TTS messages
GRACE_BEFORE_MEDIA_RESUME = 1.5  # Pause after TTS ends before resuming YouTube/Spotify

# Command file for cross-process CLI control (skip, mute-toggle, quiet, stop)
# Written by heyvox skip/mute/quiet CLI commands; read and deleted by TTS worker.
TTS_CMD_FILE = "/tmp/heyvox-tts-cmd"

# ---------------------------------------------------------------------------
# Echo suppression constants (ECHO-01 through ECHO-06)
# ---------------------------------------------------------------------------

# Grace period (seconds) after TTS flag clears before re-enabling wake word.
# Handles room reverb tail — TTS audio lingers after playback stops.
# Requirement: ECHO-01
GRACE_AFTER_TTS = 0.6

# Wake word threshold multiplier when in speaker mode (no headset detected).
# Higher = fewer false triggers from ambient audio/TTS bleed.
# Requirement: ECHO-02
SPEAKER_MODE_THRESHOLD_MULT = 1.4

# How many seconds of recently spoken TTS text to retain for echo filtering.
# STT output is compared against this buffer to strip echoed TTS fragments.
# Requirement: ECHO-03
TTS_ECHO_BUFFER_SECS = 30.0

# Default AEC stream delay (ms) for built-in speakers.
# Used by livekit WebRTC APM when no calibrated value is configured.
# Requirement: ECHO-06
AEC_DEFAULT_DELAY_MS = 50

# ---------------------------------------------------------------------------
# HUD overlay constants (Phase 5)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# STT Debug constants
# ---------------------------------------------------------------------------

# Directory for saving raw audio recordings and debug logs
STT_DEBUG_DIR = "/tmp/heyvox-debug"

# Structured debug log for STT pipeline analysis
STT_DEBUG_LOG = "/tmp/heyvox-stt-debug.log"

# ---------------------------------------------------------------------------
# HUD overlay Unix socket path — single source of truth.
# Used by HUDClient (sender in main.py/tts.py) and HUDServer (receiver in overlay.py).
# ipc.py uses its own DEFAULT_SOCKET_PATH as a module-level fallback for standalone use.
# Requirement: HUD-08
HUD_SOCKET_PATH = "/tmp/heyvox-hud.sock"
