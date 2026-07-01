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
# DEF-121: heyvox_run_worker resolves a python with heyvox importable so
# Conductor / project-virtualenv PATH prepends can't silently crash the
# worker with ModuleNotFoundError under the /dev/null redirect.
heyvox_run_worker "$TMPFILE"
