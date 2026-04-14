#!/bin/bash
# Herald Cleanup — tears down the pipeline on session end
# Only kills daemons if no other Claude sessions are active.
# Uses mkdir lock + sleep to avoid race conditions when multiple sessions end simultaneously.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/../lib/config.sh"

herald_log "CLEANUP FIRED: ppid=$PPID"

# Wait so all simultaneous SessionEnd hooks have fired and processes have settled
sleep 2

# Use mkdir as an atomic lock (works on macOS, no flock needed)
HERALD_RUN_DIR="${HERALD_RUN_DIR:-${TMPDIR:-/tmp}/herald}"
CLEANUP_LOCK="$HERALD_RUN_DIR/cleanup.active"
if ! mkdir "$CLEANUP_LOCK" 2>/dev/null; then
  herald_log "CLEANUP SKIPPED: another cleanup is running"
  exit 0
fi
trap 'rm -rf "$CLEANUP_LOCK"' EXIT

# Re-check after the sleep -- count all Claude Code processes (any launcher)
ACTIVE_CLAUDE=$(pgrep -f "claude-code|claude" 2>/dev/null | wc -l | tr -d ' ')

if [ "$ACTIVE_CLAUDE" -gt 0 ]; then
  herald_log "CLEANUP SKIPPED: $ACTIVE_CLAUDE Claude sessions still active"
  exit 0
fi

herald_log "CLEANUP EXECUTING: tearing down Herald"

if [ -f "$HERALD_ORCH_PID" ]; then
  kill "$(cat "$HERALD_ORCH_PID")" 2>/dev/null
  rm -f "$HERALD_ORCH_PID"
fi
rm -rf "$HERALD_ORCH_LOCK"

if [ -f "$HERALD_PLAYING_PID" ]; then
  kill "$(cat "$HERALD_PLAYING_PID")" 2>/dev/null
  rm -f "$HERALD_PLAYING_PID"
fi
pkill -f "afplay.*$HERALD_RUN_DIR" 2>/dev/null

rm -rf "$HERALD_QUEUE_DIR"
rm -f "$HERALD_RUN_DIR"/raw*.txt "$HERALD_RUN_DIR"/speech-*.txt "$HERALD_RUN_DIR"/meta-*.json "$HERALD_RUN_DIR"/recap-*.txt
# Clean up state files that can go stale and cause wrong behavior on next session
rm -f "$HERALD_RUN_DIR"/pause "$HERALD_RUN_DIR"/ambient "$HERALD_RUN_DIR"/mode "$HERALD_RUN_DIR"/last-play "$HERALD_RUN_DIR"/workspace
# Clean up temp WAVs from crashed TTS workers
rm -f "$HERALD_RUN_DIR"/generating-*.wav

exit 0
