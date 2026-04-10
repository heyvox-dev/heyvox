#!/bin/bash
# Herald Speak — extracts <tts> from Claude response and launches worker
# Called by hooks/on-response.sh (thin shim)
# Reads Claude hook JSON from stdin.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/config.sh"

herald_is_muted && exit 0
herald_is_paused && exit 0
herald_is_skip && { herald_log "SPEAK: verbosity=skip, skipping"; exit 0; }

RESULT=$(cat | python3 -c "
import sys, json, re, os, subprocess, time, hashlib

data = json.load(sys.stdin)

if data.get('stop_hook_active', False):
    print('SKIP:stop_hook_active')
    sys.exit(0)

message = data.get('last_assistant_message', '')
if not message or len(message.strip()) < 10:
    print(f'SKIP:empty:{len(message)}')
    sys.exit(0)

matches = re.findall(r'<tts>(.*?)</tts>', message, re.DOTALL)
if not matches:
    print('SKIP:no_tts_block')
    sys.exit(0)

speech = matches[-1].strip()
if not speech or speech == 'SKIP' or len(speech) < 5:
    print(f'SKIP:skip_or_short:{speech}')
    sys.exit(0)

# Content-hash dedup
claim_dir = os.environ.get('HERALD_CLAIM_DIR', '/tmp/herald-claim')
os.makedirs(claim_dir, exist_ok=True)
speech_hash = hashlib.md5(speech.encode()).hexdigest()[:16]
claim_file = f'{claim_dir}/{speech_hash}'

now = time.time()
for f in os.listdir(claim_dir):
    fp = os.path.join(claim_dir, f)
    try:
        if now - os.path.getmtime(fp) > 60:
            os.unlink(fp)
    except Exception:
        pass

try:
    fd = os.open(claim_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, b'hook')
    os.close(fd)
except FileExistsError:
    print(f'SKIP:claimed_by_watcher:{speech_hash}')
    sys.exit(0)

hook_epoch_ms = int(time.time() * 1000)
raw_file = f'/tmp/herald-raw-{os.getpid()}.{time.time_ns()}.txt'
with open(raw_file, 'w') as f:
    f.write(message)
with open(raw_file + '.timing', 'w') as f:
    f.write(str(hook_epoch_ms))

ws = os.environ.get('CONDUCTOR_WORKSPACE_NAME', '')
label = ws
if ws:
    try:
        ws_safe = ws.replace(\"'\", \"''\")
        r = subprocess.run(
            ['sqlite3', os.path.expanduser('~/Library/Application Support/com.conductor.app/conductor.db'),
             f\"SELECT COALESCE(w.pr_title, '') FROM workspaces w WHERE w.directory_name='{ws_safe}'\"],
            capture_output=True, text=True, timeout=0.5)
        if r.stdout.strip():
            label = r.stdout.strip()
    except Exception:
        pass

preview = speech[:50] + '...' if len(speech) > 50 else speech
print(f'OK:{raw_file}|{label}|{preview}')
" 2>/dev/null)

case "$RESULT" in
  SKIP:*)
    herald_log "SPEAK: $RESULT ws=${CONDUCTOR_WORKSPACE_NAME:-unknown}"
    exit 0
    ;;
  OK:*)
    PAYLOAD="${RESULT#OK:}"
    RAW_FILE="${PAYLOAD%%|*}"
    REST="${PAYLOAD#*|}"
    TTS_LABEL="${REST%%|*}"
    PREVIEW="${REST#*|}"
    herald_log "SPEAK: fired ws=${CONDUCTOR_WORKSPACE_NAME:-unknown} tts=\"$PREVIEW\""
    ;;
  *)
    herald_log "SPEAK: unexpected result: $RESULT"
    exit 0
    ;;
esac

export TTS_LABEL

if [ -n "${CONDUCTOR_WORKSPACE_NAME:-}" ]; then
  echo "${CONDUCTOR_WORKSPACE_NAME}" > "$HERALD_WORKSPACE"
fi

nohup bash "${HERALD_HOME}/lib/worker.sh" "$RAW_FILE" </dev/null >/dev/null 2>&1 &

exit 0
