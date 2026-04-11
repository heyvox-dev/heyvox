#!/bin/bash
# Herald Orchestrator — plays queued WAV files sequentially
# Usage: Run as daemon. Controlled via `herald` CLI.
#
# Features:
#   - Audio ducking: lowers system volume during playback, then restores
#   - Workspace auto-switch: switches Conductor ONLY if it's the frontmost app
#   - Hold mode: if user is active, hold messages from other workspaces
#   - Media pause/resume (YouTube, Spotify) during playback

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/config.sh"

herald_ensure_dirs

ORIGINAL_VOL=""
ORIGINAL_VOL_FILE="/tmp/herald-original-vol"
# Track the USER's active workspace — NOT the workspace TTS messages come from.
# Seeded from Conductor DB on startup, updated only when we actually switch.
CURRENT_WORKSPACE="$(herald_get_user_workspace)"
herald_log "ORCH: seeded CURRENT_WORKSPACE=$CURRENT_WORKSPACE from DB"

# Singleton enforcement: only one orchestrator allowed
# Belt: check all running orchestrator processes via pgrep
for pid in $(pgrep -f "orchestrator.sh" 2>/dev/null); do
  [ "$pid" = "$$" ] && continue
  if ps -p "$pid" -o command= 2>/dev/null | grep -q "orchestrator"; then
    exit 0
  fi
done

# Suspenders: check PID file (covers cases where pgrep command string differs)
if [ -f "$HERALD_ORCH_PID" ]; then
  OLD_PID=$(cat "$HERALD_ORCH_PID" 2>/dev/null)
  if [ -n "$OLD_PID" ] && [ "$OLD_PID" != "$$" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    exit 0
  fi
fi

echo $$ > "$HERALD_ORCH_PID"

# Audio ducking
duck_audio() {
  [ "$HERALD_DUCK_ENABLED" != "true" ] && return
  # Only capture original volume if not already ducked (avoid saving ducked level on restart)
  if [ -f "$ORIGINAL_VOL_FILE" ]; then
    ORIGINAL_VOL=$(cat "$ORIGINAL_VOL_FILE" 2>/dev/null)
  else
    ORIGINAL_VOL=$(timeout 3 osascript -e 'output volume of (get volume settings)' 2>/dev/null)
    if [ -n "$ORIGINAL_VOL" ]; then
      echo "$ORIGINAL_VOL" > "$ORIGINAL_VOL_FILE"
    fi
  fi
  if [ -n "$ORIGINAL_VOL" ]; then
    timeout 3 osascript -e "set volume output volume $HERALD_DUCK_LEVEL" 2>/dev/null
    sleep 0.15
  fi
}

set_tts_volume() {
  [ "$HERALD_DUCK_ENABLED" != "true" ] && return
  if [ -n "$ORIGINAL_VOL" ]; then
    timeout 3 osascript -e "set volume output volume $ORIGINAL_VOL" 2>/dev/null
  fi
}

restore_audio() {
  [ "$HERALD_DUCK_ENABLED" != "true" ] && return
  # Read from file if in-memory value is empty (e.g. after restart)
  if [ -z "$ORIGINAL_VOL" ] && [ -f "$ORIGINAL_VOL_FILE" ]; then
    ORIGINAL_VOL=$(cat "$ORIGINAL_VOL_FILE" 2>/dev/null)
  fi
  if [ -n "$ORIGINAL_VOL" ]; then
    timeout 3 osascript -e "set volume output volume $ORIGINAL_VOL" 2>/dev/null
    ORIGINAL_VOL=""
    rm -f "$ORIGINAL_VOL_FILE"
  fi
}

# WAV normalization — RMS-based loudness matching
normalize_wav() {
  local wav="$1"
  # Pass path as argv[1] to avoid shell injection via filename interpolation (C9)
  python3 - "$wav" <<'PYEOF' 2>/dev/null
import wave, struct, sys, math
wav = sys.argv[1]
with wave.open(wav, 'rb') as w:
    params = w.getparams()
    frames = w.readframes(params.nframes)
samples = list(struct.unpack('<%dh' % params.nframes, frames))
if not samples:
    sys.exit(0)
rms = math.sqrt(sum(s*s for s in samples) / len(samples))
if rms < 50:
    sys.exit(0)
target_rms = 3000
scale = target_rms / rms if rms > 0 else 1.0
scale = min(scale, 3.0)
peak_limit = 24000
scaled = [s * scale for s in samples]
out = []
for s in scaled:
    if s > peak_limit:
        s = peak_limit + (s - peak_limit) * 0.2
    elif s < -peak_limit:
        s = -peak_limit + (s + peak_limit) * 0.2
    out.append(max(-32768, min(32767, int(s))))
normalized = struct.pack('<%dh' % len(out), *out)
with wave.open(wav, 'wb') as w:
    w.setparams(params)
    w.writeframes(normalized)
PYEOF
}

cleanup() {
  herald_log "ORCH DYING: signal=$? pid=$$"
  if [ "$HERALD_MEDIA_PAUSE" = "true" ] && [ -x "${HERALD_HOME}/lib/media.sh" ]; then
    "${HERALD_HOME}/lib/media.sh" play
    herald_log "ORCH: media RESUMED (cleanup)"
  fi
  restore_audio
  # Only remove PID file if it still contains our PID (another orchestrator may have taken over)
  if [ "$(cat "$HERALD_ORCH_PID" 2>/dev/null)" = "$$" ]; then
    rm -f "$HERALD_ORCH_PID"
  fi
  rm -f "$HERALD_PLAYING_PID" "$HERALD_PLAY_NEXT"
  rm -rf "$HERALD_ORCH_LOCK"
  exit 0
}
trap cleanup EXIT TERM INT HUP

user_is_active() {
  herald_is_paused && return 0
  if [ -f "$HERALD_LAST_PLAY" ]; then
    local last_play=$(cat "$HERALD_LAST_PLAY" 2>/dev/null)
    local now=$(date +%s)
    local diff=$((now - last_play))
    [ "$diff" -lt 15 ] && return 0
  fi
  return 1
}

notify_held() {
  local workspace="$1"
  local ws_lua="${workspace//\'/\\\'}"
  local count=$(find "$HERALD_HOLD_DIR" -maxdepth 1 -name '*.wav' 2>/dev/null | wc -l | tr -d ' ')
  /opt/homebrew/bin/hs -c "
    hs.notify.new({
      title='Workspace message held',
      informativeText='${ws_lua} has a message (${count} pending). Press Cmd+Shift+N to play.',
      withdrawAfter=10
    }):send()
    hs.alert.show('${ws_lua}: message held (${count})', 2)
  " 2>/dev/null &
}

LAST_MSG_PREFIX=""

play_wav() {
  local wav_file="$1"
  local workspace_file="${wav_file%.wav}.workspace"
  local basename=$(basename "$wav_file")

  local msg_prefix="${basename%%-*}"
  local is_continuation=false
  if [ -n "$LAST_MSG_PREFIX" ] && [ "$msg_prefix" = "$LAST_MSG_PREFIX" ]; then
    is_continuation=true
  fi
  LAST_MSG_PREFIX="$msg_prefix"

  # Wait while Herald is paused
  while herald_is_paused; do
    herald_log "ORCH: waiting (paused) for $basename"
    sleep 0.3
  done

  if [ "$is_continuation" != "true" ]; then
    # Switch Conductor to the workspace that produced TTS
    if [ -f "$workspace_file" ]; then
      local ws=$(cat "$workspace_file")
      CURRENT_WORKSPACE="$ws"
      # Always record last TTS workspace for jump-to-speaker shortcut
      echo "$ws" > /tmp/herald-last-tts-workspace
      "$CONDUCTOR_SWITCH" "$ws" >> "$HERALD_DEBUG_LOG" 2>&1
      local switch_exit=$?
      if [ "$switch_exit" -eq 0 ]; then
        sleep 0.3
        # Scroll to bottom so user sees the TTS message context (only if switch happened)
        /opt/homebrew/bin/hs -c "
          local app = hs.application.get('com.conductor.app')
          if app then
            local win = app:mainWindow()
            if win then hs.eventtap.keyStroke({'cmd'}, 'down', 0, app) end
          end
        " 2>/dev/null &
      fi
      rm -f "$workspace_file"
    fi

    if [ "$HERALD_MEDIA_PAUSE" = "true" ] && [ -x "${HERALD_HOME}/lib/media.sh" ]; then
      "${HERALD_HOME}/lib/media.sh" pause
      if [ -f "/tmp/herald-media-paused-orch" ]; then
        herald_log "ORCH: media PAUSED ($(cat /tmp/herald-media-paused-orch 2>/dev/null))"
      else
        herald_log "ORCH: no media to pause"
      fi
    fi
    duck_audio
    set_tts_volume
  else
    rm -f "$workspace_file"
    # Continuation part: re-pause media if it was resumed during the gap
    # between parts (e.g. grace period hadn't been added, or media resumed
    # for another reason).
    if [ "$HERALD_MEDIA_PAUSE" = "true" ] && [ -x "${HERALD_HOME}/lib/media.sh" ]; then
      if [ ! -f "/tmp/herald-media-paused-orch" ]; then
        "${HERALD_HOME}/lib/media.sh" pause
        if [ -f "/tmp/herald-media-paused-orch" ]; then
          herald_log "ORCH: media RE-PAUSED for continuation ($(cat /tmp/herald-media-paused-orch 2>/dev/null))"
        fi
      fi
    fi
  fi

  herald_log "ORCH: playing $wav_file size=$(stat -f%z "$wav_file" 2>/dev/null) cont=$is_continuation ws=$CURRENT_WORKSPACE"

  mkdir -p "$HERALD_HISTORY_DIR"
  cp "$wav_file" "$HERALD_HISTORY_DIR/$(date +%Y%m%d-%H%M%S)-$basename" 2>/dev/null
  if [ "$is_continuation" != "true" ]; then
    find "$HERALD_HISTORY_DIR" -maxdepth 1 -name '*.wav' -exec stat -f '%m %N' {} + 2>/dev/null | sort -rn | tail -n +51 | cut -d' ' -f2- | xargs rm -f 2>/dev/null
  fi

  # Final pause check right before playback — catch races
  if herald_is_paused; then
    herald_log "ORCH: BLOCKED at afplay gate (pause detected) for $basename"
    while herald_is_paused; do
      sleep 0.3
    done
    herald_log "ORCH: unblocked, proceeding with $basename"
  fi

  # Violation check: detect if recording started between checks
  if herald_violation_check "orchestrator:pre-play:$basename"; then
    herald_log "ORCH: VIOLATION DETECTED — playing during recording! Waiting..."
    while herald_is_paused; do
      sleep 0.3
    done
  fi

  # Pipeline timing: compute end-to-end latency
  local timing_file="${wav_file%.wav}.timing"
  if [ -f "$timing_file" ]; then
    local play_ms=$(python3 -c "import time; print(int(time.time()*1000))" 2>/dev/null)
    local timing_data=$(cat "$timing_file" 2>/dev/null)
    local hook_ms=$(echo "$timing_data" | cut -d'|' -f1)
    local worker_ms=$(echo "$timing_data" | cut -d'|' -f2)
    local tts_start_ms=$(echo "$timing_data" | cut -d'|' -f3)
    local enqueue_ms=$(echo "$timing_data" | cut -d'|' -f4)
    if [ "$hook_ms" != "0" ] && [ -n "$hook_ms" ]; then
      herald_log "TIMING: PIPELINE hook->play = $((play_ms - hook_ms))ms (hook->worker=$((worker_ms - hook_ms))ms, worker_prep=$((tts_start_ms - worker_ms))ms, tts=$((enqueue_ms - tts_start_ms))ms, queue_wait=$((play_ms - enqueue_ms))ms) file=$basename"
    fi
    rm -f "$timing_file"
  fi

  normalize_wav "$wav_file"
  afplay "$wav_file" &
  PLAY_PID=$!
  echo "$PLAY_PID" > "$HERALD_PLAYING_PID"

  # Recording watchdog: kill afplay immediately if recording starts mid-playback
  (
    while kill -0 "$PLAY_PID" 2>/dev/null; do
      if herald_is_paused; then
        kill "$PLAY_PID" 2>/dev/null
        herald_violation_check "orchestrator:watchdog-kill:$basename"
        herald_log "ORCH: WATCHDOG killed afplay (recording started during playback)"
        break
      fi
      sleep 0.1
    done
  ) &
  WATCHDOG_PID=$!

  wait "$PLAY_PID" 2>/dev/null
  PLAY_EXIT=$?
  kill "$WATCHDOG_PID" 2>/dev/null; wait "$WATCHDOG_PID" 2>/dev/null
  rm -f "$HERALD_PLAYING_PID"

  # If watchdog killed playback, wait for recording to finish before continuing
  if [ "$PLAY_EXIT" -ne 0 ] && herald_is_paused; then
    herald_log "ORCH: playback interrupted, waiting for pause to clear"
    while herald_is_paused; do
      sleep 0.3
    done
  fi
  rm -f "$wav_file"

  date +%s > "$HERALD_LAST_PLAY"

  if [ "$(find "$HERALD_QUEUE_DIR" -maxdepth 1 -name '*.wav' 2>/dev/null | wc -l)" -eq 0 ] \
     && [ "$(find "$HERALD_HOLD_DIR" -maxdepth 1 -name '*.wav' 2>/dev/null | wc -l)" -eq 0 ]; then
    # Grace period: wait for more parts before resuming media.
    # Multi-part TTS has gaps where queue is momentarily empty while
    # the next sentence is still generating.
    local grace_secs=5
    herald_log "ORCH: queue empty, waiting ${grace_secs}s grace period before resuming media"
    local waited=0
    while [ "$waited" -lt "$grace_secs" ]; do
      sleep 1
      waited=$((waited + 1))
      if [ "$(find "$HERALD_QUEUE_DIR" -maxdepth 1 -name '*.wav' 2>/dev/null | wc -l)" -gt 0 ]; then
        herald_log "ORCH: new WAV arrived during grace period, skipping resume"
        return
      fi
    done
    if [ "$HERALD_MEDIA_PAUSE" = "true" ] && [ -x "${HERALD_HOME}/lib/media.sh" ]; then
      "${HERALD_HOME}/lib/media.sh" play
      herald_log "ORCH: media RESUMED (after grace period)"
    fi
    restore_audio
  fi
}

# Main loop
while true; do
  if [ -f "$HERALD_PLAY_NEXT" ]; then
    rm -f "$HERALD_PLAY_NEXT"
    NEXT_HELD=$(find "$HERALD_HOLD_DIR" -maxdepth 1 -name '*.wav' 2>/dev/null | sort | head -1)
    if [ -n "$NEXT_HELD" ] && [ -f "$NEXT_HELD" ]; then
      play_wav "$NEXT_HELD"
      REMAINING=$(find "$HERALD_HOLD_DIR" -maxdepth 1 -name '*.wav' 2>/dev/null | wc -l | tr -d ' ')
      if [ "$REMAINING" -gt 0 ]; then
        /opt/homebrew/bin/hs -c "hs.alert.show('${REMAINING} more pending', 1.5)" 2>/dev/null &
      fi
      continue
    fi
  fi

  NEXT=$(find "$HERALD_QUEUE_DIR" -maxdepth 1 -name '*.wav' 2>/dev/null | sort | head -1)

  if [ -n "$NEXT" ] && [ -f "$NEXT" ]; then
    if herald_is_muted || herald_is_skip; then
      rm -f "$NEXT" "${NEXT%.wav}.workspace" "${NEXT%.wav}.timing"
      continue
    fi

    WORKSPACE_FILE="${NEXT%.wav}.workspace"
    NEXT_WORKSPACE=""
    [ -f "$WORKSPACE_FILE" ] && NEXT_WORKSPACE=$(cat "$WORKSPACE_FILE")

    # Never hold continuation parts of a message that already started playing.
    # Without this, part 2 gets held for 15s while part 1 already played.
    NEXT_BASENAME=$(basename "$NEXT")
    NEXT_MSG_PREFIX="${NEXT_BASENAME%%-*}"
    IS_CONTINUATION=false
    if [ -n "$LAST_MSG_PREFIX" ] && [ "$NEXT_MSG_PREFIX" = "$LAST_MSG_PREFIX" ]; then
      IS_CONTINUATION=true
    fi

    if [ "$IS_CONTINUATION" != "true" ] \
       && [ -n "$NEXT_WORKSPACE" ] && [ -n "$CURRENT_WORKSPACE" ] \
       && [ "$NEXT_WORKSPACE" != "$CURRENT_WORKSPACE" ] && user_is_active; then
      BASENAME=$(basename "$NEXT")
      mv "$NEXT" "$HERALD_HOLD_DIR/$BASENAME"
      [ -f "$WORKSPACE_FILE" ] && mv "$WORKSPACE_FILE" "$HERALD_HOLD_DIR/${BASENAME%.wav}.workspace"
      [ -f "${NEXT%.wav}.timing" ] && mv "${NEXT%.wav}.timing" "$HERALD_HOLD_DIR/${BASENAME%.wav}.timing"
      herald_log "ORCH: held $BASENAME from $NEXT_WORKSPACE (user active on $CURRENT_WORKSPACE)"
      if ! herald_is_paused; then
        notify_held "$NEXT_WORKSPACE"
      fi
      HELD_COUNT=$(find "$HERALD_HOLD_DIR" -maxdepth 1 -name '*.wav' 2>/dev/null | wc -l | tr -d " ")
      if [ "$HELD_COUNT" -gt "$HERALD_MAX_HELD" ]; then
        EXCESS=$((HELD_COUNT - HERALD_MAX_HELD))
        find "$HERALD_HOLD_DIR" -maxdepth 1 -name '*.wav' -exec stat -f '%m %N' {} + 2>/dev/null | sort -n | head -n "$EXCESS" | cut -d' ' -f2- | while read OLD; do
          rm -f "$OLD" "${OLD%.wav}.workspace"
          herald_log "ORCH: dropped oldest held $(basename "$OLD") (cap=$HERALD_MAX_HELD)"
        done
      fi
      continue
    fi

    play_wav "$NEXT"
  else
    NEXT_HELD=$(find "$HERALD_HOLD_DIR" -maxdepth 1 -name '*.wav' 2>/dev/null | sort | head -1)
    if [ -n "$NEXT_HELD" ] && [ -f "$NEXT_HELD" ] && ! user_is_active; then
      HELD_COUNT=$(find "$HERALD_HOLD_DIR" -maxdepth 1 -name '*.wav' 2>/dev/null | wc -l | tr -d ' ')
      herald_log "ORCH: auto-draining held queue ($HELD_COUNT pending)"
      play_wav "$NEXT_HELD"
      REMAINING=$(find "$HERALD_HOLD_DIR" -maxdepth 1 -name '*.wav' 2>/dev/null | wc -l | tr -d ' ')
      if [ "$REMAINING" -gt 0 ]; then
        /opt/homebrew/bin/hs -c "hs.alert.show('${REMAINING} more pending', 1.5)" 2>/dev/null &
        sleep 1
      fi
    else
      sleep 0.3
      # Periodic cleanup: purge stale claim files (older than 1 hour)
      if [ -d "$HERALD_CLAIM_DIR" ]; then
        find "$HERALD_CLAIM_DIR" -maxdepth 1 -type f -mmin +60 -delete 2>/dev/null
      fi
    fi
  fi
done
