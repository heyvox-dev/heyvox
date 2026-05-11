#!/bin/bash
# Tests for heyvox/herald/hooks/_lib.sh (DEF-111 follow-up).
# Run from any CWD; the test creates fixtures in /tmp.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LIB_PATH="$SCRIPT_DIR/../heyvox/herald/hooks/_lib.sh"

if [ ! -f "$LIB_PATH" ]; then
    echo "FAIL: _lib.sh not found at $LIB_PATH"
    exit 1
fi

. "$LIB_PATH"

fail() { echo "FAIL: $1"; exit 1; }
pass() { echo "PASS: $1"; }

# Test 1: Finds heyvox in current dir
TEST_ROOT=$(mktemp -d)
mkdir -p "$TEST_ROOT/heyvox"
touch "$TEST_ROOT/heyvox/__init__.py"
(
    cd "$TEST_ROOT"
    unset PYTHONPATH
    set_heyvox_pythonpath
    [ "$PYTHONPATH" = "$TEST_ROOT" ] || fail "exact-match: got '$PYTHONPATH', expected '$TEST_ROOT'"
)
pass "exact-match in CWD"

# Test 2: Walks up from subdir
mkdir -p "$TEST_ROOT/sub/deep/nested"
(
    cd "$TEST_ROOT/sub/deep/nested"
    unset PYTHONPATH
    set_heyvox_pythonpath
    [ "$PYTHONPATH" = "$TEST_ROOT" ] || fail "walk-up: got '$PYTHONPATH', expected '$TEST_ROOT'"
)
pass "walk-up from subdir"

# Test 3: No heyvox above → PYTHONPATH stays unset
EMPTY_ROOT=$(mktemp -d)
(
    cd "$EMPTY_ROOT"
    unset PYTHONPATH
    set_heyvox_pythonpath || true
    [ -z "$PYTHONPATH" ] || fail "no-match: PYTHONPATH should be empty, got '$PYTHONPATH'"
)
pass "leaves PYTHONPATH alone when no heyvox found"

# Test 4: Prepends to existing PYTHONPATH (preserves rest)
(
    cd "$TEST_ROOT"
    export PYTHONPATH="/existing/path"
    set_heyvox_pythonpath
    [ "$PYTHONPATH" = "$TEST_ROOT:/existing/path" ] || fail "prepend: got '$PYTHONPATH'"
)
pass "prepends to existing PYTHONPATH"

# Cleanup
rm -rf "$TEST_ROOT" "$EMPTY_ROOT"

echo "All 4 tests passed."
