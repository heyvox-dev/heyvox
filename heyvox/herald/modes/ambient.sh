#!/bin/bash
# Herald Ambient — plays short sound cues for state transitions
# Usage: ambient.sh <event>
# Events: start, complete, error, thinking, permission

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/../lib/config.sh"

herald_is_muted && exit 0

EVENT="${1:-start}"
CUSTOM_DIR="${TTS_SOUND_DIR:-}"
SYSTEM_SOUNDS="/System/Library/Sounds"

play_sound() {
  local event="$1"
  if [ -n "$CUSTOM_DIR" ] && [ -f "$CUSTOM_DIR/${event}.aiff" ]; then
    afplay "$CUSTOM_DIR/${event}.aiff" &
    return
  fi
  case "$event" in
    start)      afplay "$SYSTEM_SOUNDS/Tink.aiff" & ;;
    complete)   afplay "$SYSTEM_SOUNDS/Glass.aiff" & ;;
    error)      afplay "$SYSTEM_SOUNDS/Sosumi.aiff" & ;;
    thinking)   afplay "$SYSTEM_SOUNDS/Pop.aiff" & ;;
    permission) afplay "$SYSTEM_SOUNDS/Ping.aiff" & ;;
    *)          afplay "$SYSTEM_SOUNDS/Tink.aiff" & ;;
  esac
}

play_sound "$EVENT"
