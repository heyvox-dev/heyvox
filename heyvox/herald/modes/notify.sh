#!/bin/bash
# Herald Notify — handles permission prompts and alerts
# Reads Claude hook JSON from stdin.
# Safety Voice Gate: urgent tone for destructive operations

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/../lib/config.sh"

herald_is_muted && exit 0

INPUT=$(cat)

eval "$(echo "$INPUT" | python3 -c "
import sys, json, re
data = json.load(sys.stdin)
ntype = data.get('notification_type', '')
message = data.get('message', '')

dangerous_patterns = ['rm -rf', 'rm -r', 'git reset --hard', 'git push --force',
    'DROP TABLE', 'DELETE FROM', 'force push', 'git clean', '--no-verify',
    'pkill', 'kill -9', 'shutdown', 'reboot']
is_dangerous = any(p in message.lower() for p in dangerous_patterns)

# Sanitize ntype for safe shell assignment: allow only alphanumeric, underscore, hyphen
safe_ntype = re.sub(r'[^a-zA-Z0-9_-]', '', ntype)

print(f'NOTIFY_TYPE=\"{safe_ntype}\"')
print(f'IS_DANGEROUS={\"true\" if is_dangerous else \"false\"}')
" 2>/dev/null)"

if [ -f "$HERALD_AMBIENT_FLAG" ]; then
  case "$NOTIFY_TYPE" in
    permission_prompt)
      bash "${HERALD_HOME}/modes/ambient.sh" permission ;;
    error*)
      bash "${HERALD_HOME}/modes/ambient.sh" error ;;
  esac
fi

case "$NOTIFY_TYPE" in
  permission_prompt)
    pkill -f "afplay.*/tmp/herald" 2>/dev/null
    if [ "$IS_DANGEROUS" = "true" ]; then
      say -v Samantha -r 180 "Warning! Destructive operation detected. Please review carefully before approving." &
    else
      say -v Samantha -r 200 "Attention, I need your confirmation." &
    fi
    ;;
  idle_prompt)
    say -v Samantha -r 200 "I'm waiting for your input." &
    ;;
  elicitation_dialog)
    say -v Samantha -r 200 "A tool is asking for your input." &
    ;;
esac

exit 0
