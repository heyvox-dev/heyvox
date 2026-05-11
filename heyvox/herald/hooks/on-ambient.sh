#!/bin/bash
# Herald hook: on-ambient — delegates to Python worker module.
# Installed by: heyvox setup --hooks (via heyvox/setup/hooks.py)
export HERALD_HOOK_TYPE="ambient"

# DEF-111: prefer active workspace's heyvox over editable-install path.
. "$(dirname "$0")/_lib.sh"
set_heyvox_pythonpath

exec python3 -m heyvox.herald.worker "$@"
