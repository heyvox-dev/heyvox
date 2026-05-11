#!/bin/bash
# Herald hook: on-response — delegates to Python worker module.
# Runs async so the Stop hook exits immediately (session doesn't show "running").
# Installed by: heyvox setup --hooks (via heyvox/setup/hooks.py)
export HERALD_HOOK_TYPE="response"

# DEF-111: prefer the active workspace's heyvox over the editable-install
# path, so changes in a Conductor worktree take effect without merging.
. "$(dirname "$0")/_lib.sh"
set_heyvox_pythonpath

# Fork worker into background — stdin is already consumed by bash, so pass via temp file
TMPFILE=$(mktemp /tmp/herald-hook.XXXXXX)
cat > "$TMPFILE"
python3 -m heyvox.herald.worker "$TMPFILE" </dev/null >/dev/null 2>&1 &
disown
