#!/bin/bash
# Herald Media Control — pause/resume system media during TTS.
# Uses Hush (Chrome extension) for browser media when available,
# falls back to macOS MediaRemote framework + media key.
# Only resumes media if WE paused it
# Usage: media.sh pause|play [caller-id]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/config.sh"

HEYVOX_RUN_DIR="${HEYVOX_RUN_DIR:-${TMPDIR:-/tmp}/heyvox}"
HERALD_RUN_DIR="${HERALD_RUN_DIR:-${TMPDIR:-/tmp}/herald}"

ACTION="${1:-pause}"
CALLER="${2:-orch}"
FLAG="$HERALD_RUN_DIR/media-paused-${CALLER}"

HUSH_SOCK="$HEYVOX_RUN_DIR/hush.sock"
HUSH_REWIND_SECS=3
HUSH_FADE_IN_MS=1000

# Try Hush (Chrome extension) for browser media — returns 0 on success
# Usage: hush_cmd <action> [extra_json_fields]
# e.g.: hush_cmd resume '"rewindSecs":3,"fadeInMs":1000'
hush_cmd() {
  local action="$1"
  local extra="${2:-}"
  [ ! -S "$HUSH_SOCK" ] && return 1
  local json_msg="{\"action\": \"$action\""
  [ -n "$extra" ] && json_msg="$json_msg, $extra"
  json_msg="$json_msg}"
  local resp
  resp=$(python3 - "$HUSH_SOCK" "$json_msg" <<'PYEOF'
import json, socket, sys
sock_path, msg = sys.argv[1], sys.argv[2]
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.settimeout(3.0)
try:
    sock.connect(sock_path)
    sock.sendall((msg + "\n").encode("utf-8"))
    response = b""
    while True:
        chunk = sock.recv(4096)
        if not chunk: break
        response += chunk
        if b"\n" in response: break
    print(response.strip().decode("utf-8"))
except: sys.exit(1)
finally: sock.close()
PYEOF
  ) 2>/dev/null || return 1
  echo "$resp" | grep -q '"error"' && return 1
  return 0
}

send_media_key() {
  python3 -c "
import Quartz, time
def send_play_pause():
    e1 = Quartz.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
        14, (0,0), 0xa00, 0, 0, None, 8, (16 << 16) | (0xa << 8), -1)
    Quartz.CGEventPost(0, e1.CGEvent())
    time.sleep(0.05)
    e2 = Quartz.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
        14, (0,0), 0xb00, 0, 0, None, 8, (16 << 16) | (0xb << 8), -1)
    Quartz.CGEventPost(0, e2.CGEvent())
send_play_pause()
" 2>/dev/null
}

send_mr_command() {
  local cmd="$1"
  python3 -c "
import ctypes
mr = ctypes.cdll.LoadLibrary('/System/Library/PrivateFrameworks/MediaRemote.framework/MediaRemote')
mr.MRMediaRemoteSendCommand.argtypes = [ctypes.c_int, ctypes.c_void_p]
mr.MRMediaRemoteSendCommand.restype = ctypes.c_bool
mr.MRMediaRemoteSendCommand($cmd, None)
" 2>/dev/null
}

send_rewind() {
  local secs="$1"
  [ ! -x "$NP_CLI" ] && return
  local elapsed
  elapsed=$("$NP_CLI" get-raw 2>/dev/null | python3 -c "
import sys,json
try:
    d=json.load(sys.stdin)
    e=d.get('kMRMediaRemoteNowPlayingInfoElapsedTime',0)
    print(f'{e:.1f}')
except: print('0')
" 2>/dev/null)
  [ -z "$elapsed" ] && return
  local target
  target=$(python3 -c "print(max(0, float('$elapsed') - $secs))" 2>/dev/null)
  [ -z "$target" ] && return
  "$NP_CLI" seek "$target" 2>/dev/null
}

save_pause_position() {
  [ ! -x "$NP_CLI" ] && return
  "$NP_CLI" get-raw 2>/dev/null | python3 -c "
import sys,json
try:
    d=json.load(sys.stdin)
    print(f'{d.get(\"kMRMediaRemoteNowPlayingInfoElapsedTime\",0):.1f}')
except: pass
" > "${FLAG}.pos" 2>/dev/null
}

if [ "$ACTION" = "pause" ]; then
    [ -f "$FLAG" ] && exit 0

    # Tier 1: Try Hush for browser media (most reliable)
    if hush_cmd pause; then
        echo "hush" > "$FLAG"
    else
        # Tier 2: Try native MediaRemote (Spotify, Music, Podcasts)
        RATE=$(nowplaying-cli get playbackRate 2>/dev/null)
        if [ "$RATE" != "null" ] && [ "$RATE" != "0" ] && [ -n "$RATE" ]; then
            save_pause_position
            send_mr_command 1
            echo "mr" > "$FLAG"
        else
            # Tier 3: Media key fallback
            send_media_key
            echo "key" > "$FLAG"
        fi
    fi
else
    [ ! -f "$FLAG" ] && exit 0
    HAS_OTHER=false
    for f in "$HERALD_RUN_DIR"/media-paused-*; do
        [ "$f" != "$FLAG" ] && [ -f "$f" ] && HAS_OTHER=true && break
    done
    if [ "$HAS_OTHER" = "true" ]; then
        rm -f "$FLAG" "${FLAG}.pos"
        exit 0
    fi
    METHOD=$(cat "$FLAG" 2>/dev/null)
    sleep "$HERALD_RESUME_DELAY"
    if [ "$METHOD" = "hush" ]; then
        if ! hush_cmd resume "\"rewindSecs\":$HUSH_REWIND_SECS,\"fadeInMs\":$HUSH_FADE_IN_MS"; then
            # Hush unavailable — try MediaRemote as fallback
            send_mr_command 0
        fi
    elif [ "$METHOD" = "mr" ]; then
        send_rewind "$HERALD_REWIND_SECS"
        sleep 0.3
        send_mr_command 0
    else
        send_media_key
    fi
    rm -f "$FLAG" "${FLAG}.pos"
fi
