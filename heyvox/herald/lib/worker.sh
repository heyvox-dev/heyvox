#!/bin/bash
# Herald Worker — extracts <tts> block, generates WAV, enqueues for orchestrator
# Usage: worker.sh <raw_file>

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/config.sh"

SPEECH_FILE="/tmp/herald-speech-$$.txt"
META_FILE="/tmp/herald-meta-$$.json"

RAW_FILE="${1:-/tmp/herald-raw.txt}"

herald_ensure_dirs

# Ensure orchestrator is running (atomic: mkdir lock prevents duplicate launches)
if ! kill -0 "$(cat "$HERALD_ORCH_PID" 2>/dev/null)" 2>/dev/null; then
  if mkdir /tmp/herald-orch-launch.lock 2>/dev/null; then
    # Double-check after acquiring lock
    if ! kill -0 "$(cat "$HERALD_ORCH_PID" 2>/dev/null)" 2>/dev/null; then
      nohup bash "${HERALD_HOME}/lib/orchestrator.sh" </dev/null >/dev/null 2>&1 &
    fi
    rm -rf /tmp/herald-orch-launch.lock
  fi
fi

MODE="narrate"
[ -f "$HERALD_MODE_FILE" ] && MODE=$(cat "$HERALD_MODE_FILE" 2>/dev/null)

# Extract <tts> content + detect voice/language metadata
_HERALD_RAW_FILE="$RAW_FILE" _HERALD_SPEECH_FILE="$SPEECH_FILE" _HERALD_META_FILE="$META_FILE" _HERALD_MODE="$MODE" python3 -c "
import sys, re, json, os, hashlib

with open(os.environ['_HERALD_RAW_FILE']) as f:
    message = f.read()

matches = re.findall(r'^<tts>(.*?)</tts>', message, re.DOTALL | re.MULTILINE)
if not matches:
    matches = re.findall(r'<tts>(.*?)</tts>', message, re.DOTALL)
if not matches:
    sys.exit(1)

speech = matches[-1].strip()
if not speech or speech == 'SKIP' or len(speech) < 5:
    sys.exit(1)

mode = os.environ.get('_HERALD_MODE', 'narrate')

# Apply verbosity filtering (reads shared state file)
verbosity = 'full'
try:
    with open('/tmp/heyvox-verbosity') as vf:
        verbosity = vf.read().strip() or 'full'
except FileNotFoundError:
    pass

if verbosity == 'skip':
    sys.exit(1)
elif verbosity == 'short':
    m = re.search(r'[.!?]', speech)
    if m:
        speech = speech[:m.end()].strip()
    else:
        speech = speech[:100]
# 'full' and 'summary' (legacy) both play everything

if mode == 'notify':
    first = re.split(r'[.!?]', speech)[0].strip()
    speech = (first[:57] + '...') if len(first) > 60 else first

# Emotional Voice Switching
def detect_mood(text):
    t = text.lower()
    if any(w in t for w in ['error', 'fail', 'broke', 'crash', 'warning', 'careful',
                             'danger', 'critical', 'urgent', 'problem', 'bug']):
        return 'alert'
    if any(w in t for w in ['done', 'success', 'passed', 'complete', 'fixed', 'great',
                             'perfect', 'working', 'deployed', 'shipped', 'merged']):
        return 'cheerful'
    if any(w in t for w in ['should we', 'want me to', 'would you', 'what do you',
                             'how about', 'shall i', 'let me know']):
        return 'thoughtful'
    return 'neutral'

MOOD_VOICES = {
    'neutral':    'af_sarah',
    'cheerful':   'af_heart',
    'alert':      'af_nova',
    'thoughtful': 'af_sky',
}

mood = detect_mood(speech)
voice = MOOD_VOICES.get(mood, 'af_sarah')

# Multi-Agent Voice Routing
agent_name = os.environ.get('CONDUCTOR_AGENT', '') or os.environ.get('CLAUDE_AGENT_NAME', '')
if agent_name:
    agent_pool = ['af_alloy', 'af_bella', 'af_jessica', 'af_kore', 'af_nicole',
                  'af_river', 'am_adam', 'am_eric', 'am_liam', 'am_puck']
    idx = int(hashlib.md5(agent_name.encode()).hexdigest(), 16) % len(agent_pool)
    voice = agent_pool[idx]

# Language Detection
def detect_language(text):
    if re.search(r'[\u4e00-\u9fff]', text):
        return 'cmn', 'zf_xiaoxiao'
    if re.search(r'[\u3040-\u309f\u30a0-\u30ff]', text):
        return 'ja', 'jf_alpha'
    if re.search(r'\b(je suis|merci|bonjour|s.il vous|c.est|nous avons|vous avez)\b', text, re.I):
        return 'fr-fr', 'ff_siwis'
    if re.search(r'\b(grazie|buongiorno|ciao|sono|questo|quello|perch.)\b', text, re.I):
        return 'it', 'if_sara'
    if re.search(r'\b(ich|nicht|haben|werden|k.nnen|m.ssen|danke|bitte)\b', text, re.I):
        return 'en-gb', 'bf_emma'
    return 'en-us', None

lang, lang_voice = detect_language(speech)
if lang_voice and lang != 'en-us':
    voice = lang_voice

# Workspace Context Prefix
label = os.environ.get('TTS_LABEL', '')
if label:
    spoken_label = label.replace(' \u00b7 ', ', ')
    speech = f'{spoken_label}: {speech}'

with open(os.environ['_HERALD_SPEECH_FILE'], 'w') as f:
    f.write(speech)

meta = {'voice': voice, 'lang': lang, 'mood': mood, 'agent': agent_name}
with open(os.environ['_HERALD_META_FILE'], 'w') as f:
    json.dump(meta, f)
" 2>/dev/null

rm -f "$RAW_FILE"

if [ ! -s "$SPEECH_FILE" ]; then
  rm -f "$SPEECH_FILE" "$META_FILE"
  exit 0
fi

VOICE="${KOKORO_VOICE:-af_sarah}"
LANG="en-us"
if [ -f "$META_FILE" ]; then
  VOICE=$(_HERALD_META_FILE="$META_FILE" python3 -c "import json, os; print(json.load(open(os.environ['_HERALD_META_FILE']))['voice'])" 2>/dev/null || echo "$VOICE")
  LANG=$(_HERALD_META_FILE="$META_FILE" python3 -c "import json, os; print(json.load(open(os.environ['_HERALD_META_FILE']))['lang'])" 2>/dev/null || echo "$LANG")
  rm -f "$META_FILE"
fi

[ -n "${KOKORO_VOICE:-}" ] && VOICE="$KOKORO_VOICE"

TEMP_WAV="/tmp/herald-generating-$$.wav"

ensure_daemon() {
  if [ -S "$KOKORO_DAEMON_SOCK" ] && kill -0 "$(cat "$KOKORO_DAEMON_PID" 2>/dev/null)" 2>/dev/null; then
    return 0
  fi
  herald_log "WORKER: starting kokoro daemon"
  nohup "$KOKORO_DAEMON_PYTHON" "$KOKORO_DAEMON_SCRIPT" </dev/null >>"$HERALD_DEBUG_LOG" 2>&1 &
  for i in $(seq 1 80); do
    [ -S "$KOKORO_DAEMON_SOCK" ] && return 0
    sleep 0.1
  done
  herald_log "WORKER: daemon failed to start"
  return 1
}

GENERATED=false

if ensure_daemon; then
  REQ_FILE="/tmp/kokoro-req-$$.json"
  _HERALD_SPEECH_FILE="$SPEECH_FILE" _HERALD_REQ_FILE="$REQ_FILE" _HERALD_VOICE="$VOICE" _HERALD_LANG="$LANG" _HERALD_TEMP_WAV="$TEMP_WAV" python3 -c "
import json, os
text = open(os.environ['_HERALD_SPEECH_FILE']).read().strip()
with open(os.environ['_HERALD_REQ_FILE'], 'w') as f:
    json.dump({'text': text, 'voice': os.environ['_HERALD_VOICE'], 'lang': os.environ['_HERALD_LANG'], 'speed': 1.2, 'output': os.environ['_HERALD_TEMP_WAV']}, f)
" 2>/dev/null

  if [ -s "$REQ_FILE" ]; then
    TIMESTAMP="$(date +%s%N)"
    (
      for attempt in $(seq 1 100); do
        if [ -s "$TEMP_WAV" ]; then
          WAV_NAME="${TIMESTAMP}-01.wav"
          cp "$TEMP_WAV" "$HERALD_QUEUE_DIR/$WAV_NAME"
          if [ -n "${CONDUCTOR_WORKSPACE_NAME:-}" ]; then
            echo "${CONDUCTOR_WORKSPACE_NAME}" > "$HERALD_QUEUE_DIR/${WAV_NAME%.wav}.workspace"
          fi
          herald_log "WORKER: early-enqueued part 1 -> $WAV_NAME"
          break
        fi
        sleep 0.1
      done
    ) &
    WATCHER_PID=$!

    RESULT=$("$KOKORO_DAEMON_PYTHON" -c "
import socket, json, sys
with open(sys.argv[1]) as f:
    req = f.read()
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    s.connect(sys.argv[2])
    s.sendall(req.encode())
    s.shutdown(socket.SHUT_WR)
    resp = b''
    while True:
        chunk = s.recv(4096)
        if not chunk: break
        resp += chunk
    s.close()
    data = json.loads(resp)
    if data.get('ok'):
        print('OK:%.2fs:parts=%d' % (data.get('duration', 0), data.get('parts', 1)))
    else:
        print('ERR:%s' % data.get('error', 'unknown'), file=sys.stderr)
        sys.exit(1)
except Exception as e:
    print('ERR:%s' % e, file=sys.stderr)
    sys.exit(1)
" "$REQ_FILE" "$KOKORO_DAEMON_SOCK" 2>>"$HERALD_DEBUG_LOG")
    DAEMON_OK=$?
    wait "$WATCHER_PID" 2>/dev/null
    rm -f "$REQ_FILE"

    if [ $DAEMON_OK -eq 0 ]; then
      herald_log "WORKER: daemon completed ${RESULT}"
      GENERATED=true
      TEMP_WAV_BASE="${TEMP_WAV%.wav}"
      PART_NUM=2
      while [ -f "${TEMP_WAV_BASE}.part${PART_NUM}.wav" ]; do
        PART_WAV="${TEMP_WAV_BASE}.part${PART_NUM}.wav"
        WAV_NAME="${TIMESTAMP}-$(printf '%02d' $PART_NUM).wav"
        mv "$PART_WAV" "$HERALD_QUEUE_DIR/$WAV_NAME"
        if [ -n "${CONDUCTOR_WORKSPACE_NAME:-}" ]; then
          echo "${CONDUCTOR_WORKSPACE_NAME}" > "$HERALD_QUEUE_DIR/${WAV_NAME%.wav}.workspace"
        fi
        herald_log "WORKER: enqueued part $PART_NUM -> $WAV_NAME"
        PART_NUM=$((PART_NUM + 1))
      done
      rm -f "$TEMP_WAV"
    fi
  fi
fi

if [ "$GENERATED" != "true" ]; then
  herald_log "WORKER: falling back to CLI"
  cd "$KOKORO_DIR"
  $KOKORO_CLI "$SPEECH_FILE" "$TEMP_WAV" --voice "$VOICE" --lang "$LANG" --speed 1.2 &>/dev/null
fi

if [ "$GENERATED" != "true" ] && [ -s "$TEMP_WAV" ]; then
  WAV_NAME="$(date +%s%N).wav"
  mv "$TEMP_WAV" "$HERALD_QUEUE_DIR/$WAV_NAME"
  if [ -n "${CONDUCTOR_WORKSPACE_NAME:-}" ]; then
    echo "${CONDUCTOR_WORKSPACE_NAME}" > "$HERALD_QUEUE_DIR/${WAV_NAME%.wav}.workspace"
  fi
else
  rm -f "$TEMP_WAV"
fi

rm -f "$SPEECH_FILE"
