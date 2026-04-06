#!/bin/bash
# Herald — shared configuration
# Source this from all Herald scripts: source "$(dirname "$0")/../lib/config.sh"

# Auto-detect HERALD_HOME from this script's location
HERALD_HOME="${HERALD_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Runtime directories
HERALD_QUEUE_DIR="/tmp/herald-queue"
HERALD_HOLD_DIR="/tmp/herald-hold"
HERALD_HISTORY_DIR="/tmp/herald-history"
HERALD_CLAIM_DIR="/tmp/herald-claim"
HERALD_DEBUG_LOG="/tmp/herald-debug.log"
HERALD_VIOLATIONS_LOG="/tmp/herald-violations.log"

# PID files
HERALD_ORCH_PID="/tmp/herald-orchestrator.pid"
HERALD_ORCH_LOCK="/tmp/herald-orchestrator.lock"
HERALD_PLAYING_PID="/tmp/herald-playing.pid"

# State files
HEYVOX_VERBOSITY_FILE="/tmp/heyvox-verbosity"
HERALD_MUTE_FLAG="/tmp/herald-mute"
HERALD_PAUSE_FLAG="/tmp/herald-pause"
HEYVOX_RECORDING_FLAG="/tmp/heyvox-recording"
HERALD_MODE_FILE="/tmp/herald-mode"
HERALD_AMBIENT_FLAG="/tmp/herald-ambient"
HERALD_LAST_PLAY="/tmp/herald-last-play"
HERALD_PLAY_NEXT="/tmp/herald-play-next"
HERALD_WORKSPACE="/tmp/herald-workspace"

# Audio settings
HERALD_DUCK_ENABLED="${AUDIO_DUCK_ENABLED:-true}"
HERALD_DUCK_LEVEL="${DUCK_LEVEL:-3}"
HERALD_MAX_HELD="${MAX_HELD:-5}"
HERALD_MEDIA_PAUSE="${MEDIA_PAUSE:-true}"
HERALD_RESUME_DELAY="${MEDIA_RESUME_DELAY:-1.0}"
HERALD_REWIND_SECS="${MEDIA_REWIND_SECS:-4}"

# Kokoro TTS — resolve paths dynamically (no hardcoded user paths)
KOKORO_CLI="${KOKORO_CLI:-$(command -v kokoro-tts 2>/dev/null || echo "$HOME/.local/bin/kokoro-tts")}"
KOKORO_DIR="${KOKORO_DIR:-$HOME/.kokoro-tts}"
KOKORO_DAEMON_SOCK="/tmp/kokoro-daemon.sock"
KOKORO_DAEMON_PID="/tmp/kokoro-daemon.pid"
KOKORO_DAEMON_SCRIPT="${HERALD_HOME}/daemon/kokoro-daemon.py"
# Find Kokoro Python: prefer uv tool venv, fall back to system python3
if [ -z "${KOKORO_DAEMON_PYTHON:-}" ]; then
  _kokoro_venv="$HOME/.local/share/uv/tools/kokoro-tts/bin/python"
  if [ -x "$_kokoro_venv" ]; then
    KOKORO_DAEMON_PYTHON="$_kokoro_venv"
  else
    KOKORO_DAEMON_PYTHON="$(command -v python3)"
  fi
  unset _kokoro_venv
fi
KOKORO_IDLE_TIMEOUT="${KOKORO_IDLE_TIMEOUT:-300}"

# Conductor integration (optional — works without Conductor)
CONDUCTOR_DB="${CONDUCTOR_DB:-$HOME/Library/Application Support/com.conductor.app/conductor.db}"
CONDUCTOR_SWITCH="${CONDUCTOR_SWITCH:-$(command -v conductor-switch-workspace 2>/dev/null || echo "$HOME/.local/bin/conductor-switch-workspace")}"
NP_CLI="${NP_CLI:-$(command -v nowplaying-cli-dev 2>/dev/null || echo "/usr/local/bin/nowplaying-cli-dev")}"

# --- Helper functions ---

herald_log() {
  echo "[$(date)] $1" >> "$HERALD_DEBUG_LOG"
  # Rotate at ~2MB to prevent unbounded growth
  if [ -f "$HERALD_DEBUG_LOG" ]; then
    local size=$(stat -f%z "$HERALD_DEBUG_LOG" 2>/dev/null || echo 0)
    if [ "$size" -gt 2097152 ]; then
      mv -f "$HERALD_DEBUG_LOG" "${HERALD_DEBUG_LOG}.1" 2>/dev/null
    fi
  fi
}

herald_is_muted() {
  [ -f "$HERALD_MUTE_FLAG" ] && return 0
  # Respect macOS system mute (volume 0 or output muted)
  local sys_muted
  sys_muted=$(osascript -e 'output muted of (get volume settings)' 2>/dev/null)
  [ "$sys_muted" = "true" ] && return 0
  return 1
}

herald_get_verbosity() {
  if [ -f "$HEYVOX_VERBOSITY_FILE" ]; then
    cat "$HEYVOX_VERBOSITY_FILE" 2>/dev/null
  else
    echo "full"
  fi
}

herald_is_skip() {
  [ "$(herald_get_verbosity)" = "skip" ]
}

herald_is_paused() {
  local pause_flag=false heyvox_flag=false
  [ -f "$HERALD_PAUSE_FLAG" ] && pause_flag=true
  if [ -f "$HEYVOX_RECORDING_FLAG" ]; then
    # Age-based staleness: recording flags older than 120s are stale (crash leftover)
    local flag_age=$(( $(date +%s) - $(stat -f%m "$HEYVOX_RECORDING_FLAG" 2>/dev/null || echo 0) ))
    if [ "$flag_age" -gt 120 ]; then
      rm -f "$HEYVOX_RECORDING_FLAG"
      herald_log "PAUSED: removed stale recording flag (age=${flag_age}s)"
    else
      heyvox_flag=true
    fi
  fi
  if $pause_flag || $heyvox_flag; then
    return 0
  fi
  return 1
}

# Log a violation when TTS plays during recording — for diagnostics
herald_violation_check() {
  local context="${1:-unknown}"
  local violated=false reason=""
  if [ -f "$HERALD_PAUSE_FLAG" ]; then
    violated=true
    reason="herald-pause flag present"
  fi
  if [ -f "$HEYVOX_RECORDING_FLAG" ]; then
    violated=true
    reason="${reason:+$reason + }heyvox-recording flag present"
  fi
  if $violated; then
    local ts
    ts="$(date '+%Y-%m-%d %H:%M:%S')"
    local entry="[$ts] VIOLATION in $context: $reason"
    # Log to both files
    echo "$entry" >> "$HERALD_VIOLATIONS_LOG"
    herald_log "VIOLATION: $context — $reason"
    # Also log process state for forensics
    {
      echo "  pause_flag: $(ls -la "$HERALD_PAUSE_FLAG" 2>/dev/null || echo 'absent')"
      echo "  heyvox_flag: $(ls -la "$HEYVOX_RECORDING_FLAG" 2>/dev/null || echo 'absent')"
      echo "  afplay_procs: $(pgrep -f 'afplay.*/tmp/herald' 2>/dev/null | tr '\n' ' ' || echo 'none')"
      echo "  herald_pause_age: $([ -f "$HERALD_PAUSE_FLAG" ] && echo "$(( $(date +%s) - $(stat -f %m "$HERALD_PAUSE_FLAG") ))s" || echo 'n/a')"
      echo "  heyvox_flag_age: $([ -f "$HEYVOX_RECORDING_FLAG" ] && echo "$(( $(date +%s) - $(stat -f %m "$HEYVOX_RECORDING_FLAG") ))s" || echo 'n/a')"
    } >> "$HERALD_VIOLATIONS_LOG"
    return 0  # violation detected
  fi
  return 1  # no violation
}

# Check if Conductor is the frontmost app
herald_conductor_is_frontmost() {
  local frontmost
  frontmost=$(osascript -e 'tell application "System Events" to get name of first application process whose frontmost is true' 2>/dev/null)
  [ "$frontmost" = "Conductor" ]
}

# Get TTS label from Conductor DB
herald_get_label() {
  local ws="${1:-}"
  if [ -z "$ws" ]; then
    echo ""
    return
  fi
  local label
  label=$(sqlite3 "$CONDUCTOR_DB" "SELECT COALESCE(w.pr_title, '') FROM workspaces w WHERE w.directory_name='$ws'" 2>/dev/null)
  echo "${label:-$ws}"
}

# Ensure runtime directories exist
herald_ensure_dirs() {
  mkdir -p "$HERALD_QUEUE_DIR" "$HERALD_HOLD_DIR" "$HERALD_HISTORY_DIR" "$HERALD_CLAIM_DIR"
}
