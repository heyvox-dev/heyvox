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
CLEANUP_LOCK="/tmp/herald-cleanup.active"
if ! mkdir "$CLEANUP_LOCK" 2>/dev/null; then
  herald_log "CLEANUP SKIPPED: another cleanup is running"
  exit 0
fi
trap 'rm -rf "$CLEANUP_LOCK"' EXIT

# Re-check after the sleep — count all Claude processes
ACTIVE_CLAUDE=$(pgrep -f "com.conductor.app/bin/claude|claude-code" 2>/dev/null | wc -l | tr -d ' ')

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
pkill -f "afplay.*/tmp/herald" 2>/dev/null

rm -rf "$HERALD_QUEUE_DIR"
rm -f /tmp/herald-raw*.txt /tmp/herald-speech-*.txt /tmp/herald-meta-*.json /tmp/herald-recap-*.txt

exit 0
