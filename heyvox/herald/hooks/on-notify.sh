#!/bin/bash
# Herald hook shim — called from ~/.claude/hooks/ on notification events
# Delegates to Herald's notify mode.
# Resolve HERALD_HOME from heyvox Python package
HERALD_HOME="${HERALD_HOME:-$(python3 -c "from heyvox.herald import get_herald_home; print(get_herald_home())" 2>/dev/null)}"
exec bash "${HERALD_HOME}/modes/notify.sh"
