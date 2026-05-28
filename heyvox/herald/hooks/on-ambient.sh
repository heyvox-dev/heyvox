#!/bin/bash
# Herald hook: on-ambient — delegates to Python worker module.
# Installed by: heyvox setup --hooks (via heyvox/setup/hooks.py)
export HERALD_HOOK_TYPE="ambient"

# DEF-111: prefer active workspace's heyvox over editable-install path.
. "$(dirname "$0")/_lib.sh"
set_heyvox_pythonpath

# DEF-121: prefer a python with heyvox importable; on-ambient is synchronous
# (exec replaces the shell, no background spawn), so we resolve and exec.
HEYVOX_PY=$(find_heyvox_python || echo python3)
exec "$HEYVOX_PY" -m heyvox.herald.worker "$@"
