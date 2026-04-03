#!/usr/bin/env bash
# scripts/uninstall.sh — Remove Hush native messaging host

set -euo pipefail

CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RESET='\033[0m'

info()    { echo -e "${CYAN}[hush]${RESET} $*"; }
success() { echo -e "${GREEN}[hush]${RESET} $*"; }
warn()    { echo -e "${YELLOW}[hush]${RESET} $*"; }

MANIFEST_NAME="com.hush.bridge.json"

# ── Detect OS ─────────────────────────────────────────────────────────────────
OS="$(uname -s)"
case "${OS}" in
  Darwin)
    NMH_DIR="${HOME}/Library/Application Support/Google/Chrome/NativeMessagingHosts"
    ;;
  Linux)
    NMH_DIR="${HOME}/.config/google-chrome/NativeMessagingHosts"
    ;;
  *)
    echo "Unsupported OS: ${OS}" >&2; exit 1
    ;;
esac

# ── Remove manifest ───────────────────────────────────────────────────────────
MANIFEST_PATH="${NMH_DIR}/${MANIFEST_NAME}"
if [[ -f "${MANIFEST_PATH}" ]]; then
  rm "${MANIFEST_PATH}"
  success "Removed: ${MANIFEST_PATH}"
else
  warn "Manifest not found (already removed?): ${MANIFEST_PATH}"
fi

# ── Remove Unix socket ────────────────────────────────────────────────────────
SOCK_PATH="/tmp/hush.sock"
if [[ -S "${SOCK_PATH}" ]]; then
  rm "${SOCK_PATH}"
  success "Removed socket: ${SOCK_PATH}"
else
  info "Socket not present: ${SOCK_PATH}"
fi

echo ""
success "Hush uninstalled. Reload Chrome extensions to complete removal."
