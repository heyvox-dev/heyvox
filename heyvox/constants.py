"""Shared constants for the heyvox package."""

import glob
import os
import tempfile

# User-scoped temp directory. On macOS this is $TMPDIR (/var/folders/<uid>/...),
# avoiding /tmp which is shared across all users and can cause permission clashes
# or sandboxing issues when running as a LaunchAgent.
_TMP = tempfile.gettempdir()

# IPC flag file: written when recording is active, removed when done.
# Used by TTS orchestrator to pause playback during recording.
# Requirement: DECP-04
RECORDING_FLAG = f"{_TMP}/heyvox-recording"

# Default log file location
LOG_FILE_DEFAULT = f"{_TMP}/heyvox.log"

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
TTS_PLAYING_FLAG = f"{_TMP}/heyvox-tts-playing"

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
TTS_CMD_FILE = f"{_TMP}/heyvox-tts-cmd"

# Verbosity state file — shared across all processes (main, Herald, watcher).
# Contains one of: full, summary, short, skip.
# Absent = full (default).
VERBOSITY_FILE = f"{_TMP}/heyvox-verbosity"

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

# DEF-078: Cross-process echo journal. Herald's TTS worker runs in a separate
# process (spawned by Claude Code hooks), so the in-process echo buffer never
# sees Herald-initiated TTS. Every TTS producer appends to this JSONL file;
# filter_tts_echo() reads it in addition to the in-memory buffer.
TTS_ECHO_JOURNAL = f"{_TMP}/heyvox-tts-echo.jsonl"

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
STT_DEBUG_DIR = f"{_TMP}/heyvox-debug"

# Structured debug log for STT pipeline analysis
STT_DEBUG_LOG = f"{_TMP}/heyvox-stt-debug.log"

# ---------------------------------------------------------------------------
# HUD overlay Unix socket path — single source of truth.
# Used by HUDClient (sender in main.py/tts.py) and HUDServer (receiver in overlay.py).
# ipc.py uses its own DEFAULT_SOCKET_PATH as a module-level fallback for standalone use.
# Requirement: HUD-08
HUD_SOCKET_PATH = f"{_TMP}/heyvox-hud.sock"

# Mic mute flag — when this file exists, wake word detection is paused.
# Written/removed by HUD menu toggle or CLI.
MIC_MUTE_FLAG = f"{_TMP}/heyvox-mic-mute"

# Mic warning file (DEF-101) — written by recording.py when energy gate
# rejects audio as too quiet. HUD overlay reads this on every menu bar
# refresh and surfaces the warning until auto-expiry.
# Format: single line plain text. mtime drives expiry.
MIC_WARN_FILE = f"{_TMP}/heyvox-mic-warn"
MIC_WARN_TTL_SECS = 60

# Active mic name file — written by main.py on startup and device switch.
# Read by HUD overlay to display the current mic in the menu bar.
ACTIVE_MIC_FILE = f"{_TMP}/heyvox-active-mic"

# Mic switch request file — written by HUD overlay menu action.
# Contains the device name substring to switch to. Read and deleted by main.py.
MIC_SWITCH_REQUEST_FILE = f"{_TMP}/heyvox-mic-switch"

# ---------------------------------------------------------------------------
# Herald TTS orchestration constants (Phase 7)
# ---------------------------------------------------------------------------

# Herald queue/hold/history directories — WAV files pass through these.
# Queue: ready to play. Hold: from inactive workspace, held until user idle.
# History: last 50 played (for debugging).
HERALD_QUEUE_DIR = f"{_TMP}/herald-queue"
HERALD_HOLD_DIR = f"{_TMP}/herald-hold"
HERALD_HISTORY_DIR = f"{_TMP}/herald-history"
HERALD_CLAIM_DIR = f"{_TMP}/herald-claim"

# Herald log files
HERALD_DEBUG_LOG = f"{_TMP}/herald-debug.log"
HERALD_VIOLATIONS_LOG = f"{_TMP}/herald-violations.log"

# Herald PID files
HERALD_ORCH_PID = f"{_TMP}/herald-orchestrator.pid"
HERALD_PLAYING_PID = f"{_TMP}/herald-playing.pid"

# Herald state flag files
HERALD_PAUSE_FLAG = f"{_TMP}/herald-pause"
HERALD_MUTE_FLAG = f"{_TMP}/herald-mute"
HERALD_MODE_FILE = f"{_TMP}/herald-mode"
HERALD_LAST_PLAY = f"{_TMP}/herald-last-play"
HERALD_PLAY_NEXT = f"{_TMP}/herald-play-next"

# Kokoro TTS daemon IPC — Unix socket + PID file
KOKORO_DAEMON_SOCK = f"{_TMP}/kokoro-daemon.sock"
KOKORO_DAEMON_PID = f"{_TMP}/kokoro-daemon.pid"

# Qwen3-TTS daemon IPC — German + other non-Kokoro languages. Lazy-started.
QWEN_DAEMON_SOCK = f"{_TMP}/qwen-daemon.sock"
QWEN_DAEMON_PID = f"{_TMP}/qwen-daemon.pid"

# ---------------------------------------------------------------------------
# Core process files (IPC-01)
# ---------------------------------------------------------------------------

HEYVOX_PID_FILE = f"{_TMP}/heyvox.pid"
HEYVOX_HEARTBEAT_FILE = f"{_TMP}/heyvox-heartbeat"
HEYVOX_RESTART_LOG = f"{_TMP}/heyvox-restart.log"

# ---------------------------------------------------------------------------
# Legacy compatibility (v1.0 claude-tts paths)
# ---------------------------------------------------------------------------

CLAUDE_TTS_MUTE_FLAG = f"{_TMP}/claude-tts-mute"
CLAUDE_TTS_PLAYING_PID = f"{_TMP}/claude-tts-playing.pid"

# ---------------------------------------------------------------------------
# Herald workspace/ambient (IPC-01)
# ---------------------------------------------------------------------------

HERALD_AMBIENT_FLAG = f"{_TMP}/herald-ambient"
HERALD_WORKSPACE_FILE = f"{_TMP}/herald-workspace"
HERALD_ORIGINAL_VOL_FILE = f"{_TMP}/herald-original-vol"
HERALD_GENERATING_WAV_PREFIX = f"{_TMP}/herald-generating-"
HERALD_WATCHER_PID = f"{_TMP}/herald-watcher.pid"
HERALD_WATCHER_HANDLED_DIR = f"{_TMP}/herald-watcher-handled"
HERALD_MEDIA_PAUSED_PREFIX = f"{_TMP}/herald-media-paused-"

# ---------------------------------------------------------------------------
# HUD files (IPC-01)
# ---------------------------------------------------------------------------

HUD_POSITION_FILE = f"{_TMP}/heyvox-hud-position.json"
HUD_STDERR_LOG = f"{_TMP}/heyvox-hud-stderr.log"

# ---------------------------------------------------------------------------
# TTS style (IPC-01)
# ---------------------------------------------------------------------------

TTS_STYLE_FILE = f"{_TMP}/heyvox-tts-style"

# ---------------------------------------------------------------------------
# Media pause coordination (IPC-01)
# ---------------------------------------------------------------------------

HEYVOX_MEDIA_PAUSED_REC = f"{_TMP}/heyvox-media-paused-rec"
HEYVOX_MEDIA_PAUSED_PREFIX = f"{_TMP}/heyvox-media-paused-"

# ---------------------------------------------------------------------------
# Hush (browser media control) (IPC-01)
# ---------------------------------------------------------------------------

HUSH_SOCK = f"{_TMP}/hush.sock"  # legacy symlink, last-binder wins
HUSH_SOCK_GLOB = f"{_TMP}/hush-*.sock"  # DEF-105: per-host PID-suffixed sockets
HUSH_LOG = f"{_TMP}/hush.log"

# ---------------------------------------------------------------------------
# Atomic state file (IPC-02)
# ---------------------------------------------------------------------------

HEYVOX_STATE_FILE = f"{_TMP}/heyvox-state.json"


# ---------------------------------------------------------------------------
# IPC lifecycle helpers
# ---------------------------------------------------------------------------

def ensure_run_dirs():
    """Create IPC directories if they don't exist."""
    for d in (HERALD_QUEUE_DIR, HERALD_HOLD_DIR, HERALD_HISTORY_DIR,
              HERALD_CLAIM_DIR, HERALD_WATCHER_HANDLED_DIR, STT_DEBUG_DIR):
        os.makedirs(d, exist_ok=True)


def cleanup_ipc_files(herald_too: bool = True):
    """Remove all HeyVox IPC flag/socket/PID files.

    Called on clean shutdown or via ``heyvox cleanup`` CLI command.
    Does NOT remove log files or debug dirs.
    """
    # NOTE: Do NOT include HUSH_SOCK here. The Hush host is a Chrome-launched
    # native-messaging process with its own socket lifecycle (bind on startup,
    # atexit cleanup). If HeyVox unlinks the file while the host process is
    # still alive, the listener keeps its in-kernel bind but the socket file
    # vanishes from the filesystem — no client can connect, and browser media
    # simply won't be paused during TTS. See DEF-039.
    for path in (RECORDING_FLAG, TTS_PLAYING_FLAG, TTS_CMD_FILE,
                 VERBOSITY_FILE, HUD_SOCKET_PATH, ACTIVE_MIC_FILE,
                 MIC_SWITCH_REQUEST_FILE, HEYVOX_PID_FILE,
                 HEYVOX_HEARTBEAT_FILE, HEYVOX_STATE_FILE,
                 HUD_POSITION_FILE, TTS_STYLE_FILE,
                 TTS_ECHO_JOURNAL,
                 HEYVOX_MEDIA_PAUSED_REC):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    for pattern in (HEYVOX_MEDIA_PAUSED_PREFIX + "*",):
        for f in glob.glob(pattern):
            try:
                os.unlink(f)
            except FileNotFoundError:
                pass

    if not herald_too:
        return
    for path in (HERALD_ORCH_PID, HERALD_PLAYING_PID,
                 HERALD_PAUSE_FLAG, HERALD_MUTE_FLAG,
                 HERALD_MODE_FILE, HERALD_LAST_PLAY,
                 HERALD_PLAY_NEXT, KOKORO_DAEMON_SOCK,
                 KOKORO_DAEMON_PID, HERALD_AMBIENT_FLAG,
                 HERALD_WORKSPACE_FILE, HERALD_ORIGINAL_VOL_FILE,
                 HERALD_WATCHER_PID):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    for pattern in (HERALD_GENERATING_WAV_PREFIX + "*.wav",
                    HERALD_MEDIA_PAUSED_PREFIX + "*"):
        for f in glob.glob(pattern):
            try:
                os.unlink(f)
            except FileNotFoundError:
                pass
