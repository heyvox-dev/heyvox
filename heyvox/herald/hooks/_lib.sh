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

# find_heyvox_python
#
# DEF-121: Conductor / Claude Code may prepend project-specific virtualenvs
# (poetry, conda, venv) to PATH that don't have heyvox installed. The hook
# shim's bare `python3 -m heyvox.herald.worker` then dies with
# ModuleNotFoundError under the shim's `</dev/null >/dev/null 2>&1` redirect,
# leaving zero trace. Symptom: hook fires, TMPFILE accumulates in /tmp/,
# user hears no TTS, herald-debug.log is silent.
#
# Returns: absolute path of a python3 that can `import heyvox` (after the
# current PYTHONPATH adjustments from set_heyvox_pythonpath), or empty on
# total failure. Caller is expected to fall back to plain `python3` and
# accept the ModuleNotFoundError if no candidate works.
#
# Honors HEYVOX_PYTHON env var if set and importable.
find_heyvox_python() {
    if [ -n "$HEYVOX_PYTHON" ] && [ -x "$HEYVOX_PYTHON" ] && \
       "$HEYVOX_PYTHON" -c "import heyvox" 2>/dev/null; then
        echo "$HEYVOX_PYTHON"
        return 0
    fi
    # Candidate list — pyenv shim first so we skip any project virtualenv
    # that Conductor / Claude Code prepended to PATH.
    local candidates=(
        "$HOME/.pyenv/shims/python3"
        "/opt/homebrew/bin/python3"
        "/usr/local/bin/python3"
        "$(command -v python3 2>/dev/null)"
        "/usr/bin/python3"
    )
    local cand
    for cand in "${candidates[@]}"; do
        [ -z "$cand" ] && continue
        [ ! -x "$cand" ] && continue
        if "$cand" -c "import heyvox" 2>/dev/null; then
            echo "$cand"
            return 0
        fi
    done
    return 1
}

# heyvox_run_worker <tmpfile>
#
# DEF-121: Single entry point all hook shims call. Resolves a usable Python,
# spawns the worker async, and on total failure leaves a breadcrumb in the
# herald-debug.log file so the next "no TTS" diagnosis is one grep away
# instead of forensic guessing.
heyvox_run_worker() {
    local tmpfile="$1"
    local hook_type="${HERALD_HOOK_TYPE:-?}"
    local py
    py=$(find_heyvox_python)
    if [ -z "$py" ]; then
        # All candidates failed — surface the failure via herald-debug.log
        # so the user has evidence, not silence. Pattern P-detector-without-action.
        local logfile="${TMPDIR:-/tmp}/herald-debug.log"
        local ts
        ts=$(date '+%Y-%m-%d %H:%M:%S')
        echo "[$ts] HOOK: DEF-121 cannot find python with heyvox importable" \
             "(hook=$hook_type cwd=${PWD} path_head=${PATH:0:160})" \
             >> "$logfile" 2>/dev/null
        py=python3
    fi
    "$py" -m heyvox.herald.worker "$tmpfile" </dev/null >/dev/null 2>&1 &
    disown
}
