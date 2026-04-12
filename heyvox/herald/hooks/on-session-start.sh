#!/bin/bash
# Herald hook: on-session-start — delegates to Python worker module.
# Installed by: heyvox setup --hooks (via heyvox/setup/hooks.py)
export HERALD_HOOK_TYPE="session-start"
exec python3 -m heyvox.herald.worker "$@"
