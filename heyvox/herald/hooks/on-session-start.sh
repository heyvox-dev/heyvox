#!/bin/bash
# Herald hook: on-session-start — delegates to Python worker module.
# Runs async so the SessionStart hook exits immediately.
# Installed by: heyvox setup --hooks (via heyvox/setup/hooks.py)
export HERALD_HOOK_TYPE="session-start"

# DEF-111: prefer active workspace's heyvox over editable-install path.
. "$(dirname "$0")/_lib.sh"
set_heyvox_pythonpath

TMPFILE=$(mktemp /tmp/herald-hook.XXXXXX)
cat > "$TMPFILE"
python3 -m heyvox.herald.worker "$TMPFILE" </dev/null >/dev/null 2>&1 &
disown
