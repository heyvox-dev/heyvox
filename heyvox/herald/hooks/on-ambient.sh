#!/bin/bash
# Herald hook: on-ambient — delegates to Python worker module.
# Installed by: heyvox setup --hooks (via heyvox/setup/hooks.py)
export HERALD_HOOK_TYPE="ambient"
exec python3 -m heyvox.herald.worker "$@"
