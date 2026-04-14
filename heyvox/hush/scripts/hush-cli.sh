#!/usr/bin/env bash
# scripts/hush-cli.sh — CLI client for the Hush socket server
#
# Usage: hush-cli.sh <command>
#
# Commands:
#   pause    Pause all playing media
#   resume   Resume paused media
#   status   Show current server status
#
# Requires socat OR python3 (used as fallback automatically).

set -euo pipefail

HEYVOX_RUN_DIR="${HEYVOX_RUN_DIR:-${TMPDIR:-/tmp}/heyvox}"
SOCK_PATH="$HEYVOX_RUN_DIR/hush.sock"

# ── Usage ─────────────────────────────────────────────────────────────────────
usage() {
  echo "Usage: $(basename "$0") pause|resume|status" >&2
  exit 1
}

[[ $# -eq 1 ]] || usage

CMD="${1}"
case "${CMD}" in
  pause|resume|status) ;;
  *) echo "Unknown command: ${CMD}" >&2; usage ;;
esac

# ── Check socket exists ───────────────────────────────────────────────────────
if [[ ! -S "${SOCK_PATH}" ]]; then
  echo "Error: Hush is not running (socket not found: ${SOCK_PATH})" >&2
  echo "Make sure the Chrome extension is loaded and connected." >&2
  exit 1
fi

JSON_MSG="{\"action\": \"${CMD}\"}"

# ── Send command (socat preferred, python3 as fallback) ───────────────────────
if command -v socat >/dev/null 2>&1; then
  echo "${JSON_MSG}" | socat - "UNIX-CONNECT:${SOCK_PATH}"
  echo  # newline after socat output
else
  python3 - "${SOCK_PATH}" "${JSON_MSG}" <<'PYEOF'
import json, socket, sys

sock_path, msg = sys.argv[1], sys.argv[2]

sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.settimeout(3.0)
try:
    sock.connect(sock_path)
    sock.sendall((msg + "\n").encode("utf-8"))
    response = b""
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        response += chunk
        if b"\n" in response:
            break
    print(response.strip().decode("utf-8"))
except socket.timeout:
    print(json.dumps({"ok": False, "error": "timeout waiting for server response"}))
    sys.exit(1)
finally:
    sock.close()
PYEOF
fi
