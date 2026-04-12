#!/bin/bash
# Herald hook: on-session-end — delegates to Python worker module.
# Installed by: heyvox setup --hooks (via heyvox/setup/hooks.py)
export HERALD_HOOK_TYPE="session-end"
exec python3 -m heyvox.herald.worker "$@"
