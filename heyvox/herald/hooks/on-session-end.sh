#!/bin/bash
# Herald hook: on-session-end — delegates to Python worker module.
# Runs async so the SessionEnd hook exits immediately.
# Installed by: heyvox setup --hooks (via heyvox/setup/hooks.py)
export HERALD_HOOK_TYPE="session-end"

# DEF-111: prefer active workspace's heyvox over editable-install path.
. "$(dirname "$0")/_lib.sh"
set_heyvox_pythonpath

TMPFILE=$(mktemp /tmp/herald-hook.XXXXXX)
cat > "$TMPFILE"
# DEF-121: see on-response.sh
heyvox_run_worker "$TMPFILE"
