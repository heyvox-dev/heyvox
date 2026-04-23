#!/usr/bin/env bash
# scripts/install.sh — Hush native messaging host installer
#
# Usage: bash scripts/install.sh
#
# What it does:
#   1. Detects the repo root
#   2. Makes hush_host.py executable
#   3. Writes the absolute path into com.hush.bridge.json
#   4. Copies the manifest to Chrome's NativeMessagingHosts directory
#   5. Prompts for the Chrome extension ID and patches allowed_origins

set -euo pipefail

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

info()    { echo -e "${CYAN}[hush]${RESET} $*"; }
success() { echo -e "${GREEN}[hush]${RESET} $*"; }
warn()    { echo -e "${YELLOW}[hush]${RESET} $*"; }
error()   { echo -e "${RED}[hush] ERROR:${RESET} $*" >&2; }
die()     { error "$*"; exit 1; }

# ── Repo root detection ───────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

HOST_DIR="${REPO_ROOT}/host"
HOST_SCRIPT="${HOST_DIR}/hush_host.py"
MANIFEST_SRC="${HOST_DIR}/com.hush.bridge.json"
MANIFEST_NAME="com.hush.bridge.json"

# Source for the Chrome extension and its stable install location. Loading
# unpacked extensions from workspace dirs breaks when the workspace is
# archived — Chrome silently drops the extension. Installing from a stable
# path under $XDG_CONFIG_HOME avoids that.
EXT_SRC="${REPO_ROOT}/extension"
EXT_STABLE_HOME="${XDG_CONFIG_HOME:-${HOME}/.config}/heyvox/hush/extension"

info "Repo root: ${REPO_ROOT}"

# ── Sanity checks ─────────────────────────────────────────────────────────────
[[ -f "${HOST_SCRIPT}" ]]   || die "hush_host.py not found at ${HOST_SCRIPT}"
[[ -f "${MANIFEST_SRC}" ]]  || die "com.hush.bridge.json not found at ${MANIFEST_SRC}"

command -v python3 >/dev/null 2>&1 || die "python3 is required but not found in PATH"

# ── Detect OS and Chrome NativeMessagingHosts directory ──────────────────────
OS="$(uname -s)"
case "${OS}" in
  Darwin)
    NMH_DIR="${HOME}/Library/Application Support/Google/Chrome/NativeMessagingHosts"
    ;;
  Linux)
    NMH_DIR="${HOME}/.config/google-chrome/NativeMessagingHosts"
    ;;
  *)
    die "Unsupported OS: ${OS}. Only macOS and Linux are supported."
    ;;
esac

info "Chrome NativeMessagingHosts directory: ${NMH_DIR}"

# ── Step 1: make host script executable ──────────────────────────────────────
info "Making hush_host.py executable..."
chmod +x "${HOST_SCRIPT}"
success "hush_host.py is now executable."

# ── Step 1b: mirror the extension to a stable path ───────────────────────────
if [[ -d "${EXT_SRC}" ]]; then
  info "Mirroring extension to stable path: ${EXT_STABLE_HOME}"
  mkdir -p "${EXT_STABLE_HOME}"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete "${EXT_SRC}/" "${EXT_STABLE_HOME}/"
  else
    rm -rf "${EXT_STABLE_HOME:?}"/* "${EXT_STABLE_HOME:?}"/.??* 2>/dev/null || true
    cp -R "${EXT_SRC}/." "${EXT_STABLE_HOME}/"
  fi
  success "Extension mirrored — Load unpacked this path in each Chrome profile: ${EXT_STABLE_HOME}"
else
  warn "Extension source not found at ${EXT_SRC} — skipping mirror step"
fi

# ── Step 2: inject absolute path into a working copy of the manifest ─────────
MANIFEST_TMP="$(mktemp /tmp/com.hush.bridge.XXXXXX.json)"
trap 'rm -f "${MANIFEST_TMP}"' EXIT

python3 - "${HOST_SCRIPT}" "${MANIFEST_SRC}" "${MANIFEST_TMP}" <<'PYEOF'
import json, sys
host_path, src, dst = sys.argv[1], sys.argv[2], sys.argv[3]
with open(src) as f:
    manifest = json.load(f)
manifest["path"] = host_path
with open(dst, "w") as f:
    json.dump(manifest, f, indent=2)
    f.write("\n")
PYEOF

success "Manifest updated with host path: ${HOST_SCRIPT}"

# ── Step 3: prompt for Chrome extension ID ───────────────────────────────────
echo ""
echo -e "${BOLD}Chrome Extension ID${RESET}"
echo "  1. Open Chrome and go to:  chrome://extensions"
echo "  2. Enable 'Developer mode' (top-right toggle)"
echo "  3. Click 'Load unpacked' and select: ${EXT_STABLE_HOME}"
echo "  4. Copy the ID shown under the extension name (32 lowercase letters)"
echo "  (Repeat in each Chrome profile you want Hush active in.)"
echo ""
while true; do
  read -rp "  Paste your extension ID here: " EXT_ID
  EXT_ID="${EXT_ID// /}"   # strip accidental spaces
  if [[ "${EXT_ID}" =~ ^[a-z]{32}$ ]]; then
    break
  fi
  warn "That doesn't look right. Chrome extension IDs are exactly 32 lowercase letters."
done

# ── Step 4: patch allowed_origins in the temp manifest ───────────────────────
python3 - "${EXT_ID}" "${MANIFEST_TMP}" <<'PYEOF'
import json, sys
ext_id, path = sys.argv[1], sys.argv[2]
origin = f"chrome-extension://{ext_id}/"
with open(path) as f:
    manifest = json.load(f)
manifest["allowed_origins"] = [origin]
with open(path, "w") as f:
    json.dump(manifest, f, indent=2)
    f.write("\n")
PYEOF

success "allowed_origins set to: chrome-extension://${EXT_ID}/"

# ── Step 5: install the manifest ─────────────────────────────────────────────
info "Creating NativeMessagingHosts directory (if needed)..."
mkdir -p "${NMH_DIR}"

MANIFEST_DST="${NMH_DIR}/${MANIFEST_NAME}"
cp "${MANIFEST_TMP}" "${MANIFEST_DST}"
success "Manifest installed to: ${MANIFEST_DST}"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}Hush installed successfully.${RESET}"
echo ""
echo "  Next steps:"
echo "  1. Reload the extension in Chrome (chrome://extensions → click the reload icon)"
echo "  2. Chrome will launch the native host automatically when the extension connects."
echo "  3. Test with the CLI:  bash ${REPO_ROOT}/scripts/hush-cli.sh status"
echo ""
