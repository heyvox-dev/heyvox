#!/bin/bash
# Herald Recap — speaks a brief session recap

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/../lib/config.sh"

SPEECH_FILE="/tmp/herald-recap-$$.txt"

herald_is_muted && exit 0
herald_ensure_dirs

if ! kill -0 "$(cat "$HERALD_ORCH_PID" 2>/dev/null)" 2>/dev/null; then
  if mkdir /tmp/herald-orch-launch.lock 2>/dev/null; then
    if ! kill -0 "$(cat "$HERALD_ORCH_PID" 2>/dev/null)" 2>/dev/null; then
      nohup bash "${HERALD_HOME}/lib/orchestrator.sh" </dev/null >/dev/null 2>&1 &
    fi
    rm -rf /tmp/herald-orch-launch.lock
  fi
fi

python3 -c "
import subprocess, os, datetime

parts = []
now = datetime.datetime.now()
hour = now.hour

if hour < 6:
    parts.append('Late night session.')
elif hour < 12:
    parts.append('Good morning.')
elif hour < 17:
    parts.append('Good afternoon.')
elif hour < 21:
    parts.append('Good evening.')
else:
    parts.append('Late session.')

try:
    branch = subprocess.check_output(['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
        stderr=subprocess.DEVNULL, text=True).strip()
    if branch and branch != 'HEAD':
        parts.append(f'You are on the {branch} branch.')
except:
    pass

try:
    log = subprocess.check_output(['git', 'log', '-1', '--format=%s', '--no-merges'],
        stderr=subprocess.DEVNULL, text=True).strip()
    if log:
        parts.append(f'Last commit was: {log}.')
except:
    pass

try:
    status = subprocess.check_output(['git', 'status', '--porcelain'],
        stderr=subprocess.DEVNULL, text=True).strip()
    if status:
        count = len(status.splitlines())
        parts.append(f'There are {count} uncommitted changes.')
except:
    pass

recap = ' '.join(parts)
if len(recap) > 300:
    recap = recap[:297] + '...'

with open('$SPEECH_FILE', 'w') as f:
    f.write(recap)
" 2>/dev/null

if [ ! -s "$SPEECH_FILE" ]; then
  rm -f "$SPEECH_FILE"
  exit 0
fi

WAV_FILE="$HERALD_QUEUE_DIR/$(date +%s%N).wav"
cd "$KOKORO_DIR"
$KOKORO_CLI "$SPEECH_FILE" "$WAV_FILE" --voice af_sarah --lang en-us --speed 1.2 &>/dev/null
rm -f "$SPEECH_FILE"
