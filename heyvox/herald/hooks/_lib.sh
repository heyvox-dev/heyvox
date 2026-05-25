#!/bin/bash
# Shared helpers for Herald hook shims.
# Sourced by on-response.sh, on-notify.sh, on-session-start.sh, etc.

# set_heyvox_pythonpath
#
# DEF-111 follow-up: prepend the user's active heyvox checkout to PYTHONPATH
# so the hook-spawned worker loads code from the workspace the user is
# currently working in, not from the editable-install path that pip pinned
# at install time.
#
# Without this, a fix made in a Conductor worktree silently no-ops until
# the user merges it into the branch the editable install tracks.
#
# Strategy: walk upward from $PWD, find the nearest directory containing
# heyvox/__init__.py, and prepend it to PYTHONPATH. If no such directory
# is found (user is in an unrelated repo), leave PYTHONPATH alone so
# Python falls back to the editable / pip-installed package.
set_heyvox_pythonpath() {
    local dir="${PWD:-$(pwd)}"
    while [ -n "$dir" ] && [ "$dir" != "/" ]; do
        if [ -f "$dir/heyvox/__init__.py" ]; then
            if [ -n "$PYTHONPATH" ]; then
                export PYTHONPATH="$dir:$PYTHONPATH"
            else
                export PYTHONPATH="$dir"
            fi
            return 0
        fi
        dir="$(dirname "$dir")"
    done
    return 1
}
