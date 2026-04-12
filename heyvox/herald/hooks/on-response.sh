#!/bin/bash
# Herald hook: on-response — delegates to Python worker module.
# Installed by: heyvox setup --hooks (via heyvox/setup/hooks.py)
export HERALD_HOOK_TYPE="response"
exec python3 -m heyvox.herald.worker "$@"
