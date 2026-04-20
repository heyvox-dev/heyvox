#!/bin/bash
# Package the Hush Chrome extension into a .zip for Chrome Web Store submission.
# Usage:
#   scripts/package_chrome_extension.sh [--out PATH]
#
# Produces dist/hush-<version>.zip ready to upload to
# https://chrome.google.com/webstore/devconsole

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
DIM='\033[2m'
RESET='\033[0m'

info()    { printf "${BLUE}==> ${RESET}%s\n" "$1"; }
success() { printf "${GREEN} ok ${RESET}%s\n" "$1"; }
warn()    { printf "${YELLOW} !! ${RESET}%s\n" "$1"; }
fail()    { printf "${RED}err ${RESET}%s\n" "$1"; exit 1; }

# Repo root (two parents up from scripts/)
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXT_DIR="$REPO_ROOT/heyvox/hush/extension"
OUT_DIR="$REPO_ROOT/dist"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --out)     OUT_DIR="$2"; shift 2 ;;
        --out=*)   OUT_DIR="${1#--out=}"; shift ;;
        -h|--help)
            cat <<EOF
Usage: $0 [--out DIR]

Builds dist/hush-<version>.zip from heyvox/hush/extension/
EOF
            exit 0 ;;
        *) fail "Unknown flag: $1" ;;
    esac
done

[[ -d "$EXT_DIR" ]] || fail "Extension directory not found: $EXT_DIR"
[[ -f "$EXT_DIR/manifest.json" ]] || fail "manifest.json missing at $EXT_DIR"

# Extract version from manifest.json (no jq dependency — pure grep/sed)
VERSION="$(sed -nE 's/^[[:space:]]*"version"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' "$EXT_DIR/manifest.json" | head -1)"
[[ -n "$VERSION" ]] || fail "Could not read version from manifest.json"

info "Packaging Hush v$VERSION"

# ── Pre-flight: verify required files ────────────────────────────
REQUIRED_FILES=(
    "manifest.json"
    "background.js"
    "content.js"
    "popup.html"
    "popup.js"
    "icons/icon16.png"
    "icons/icon48.png"
    "icons/icon128.png"
)
for f in "${REQUIRED_FILES[@]}"; do
    [[ -f "$EXT_DIR/$f" ]] || fail "Missing required file: $f"
done
success "All required files present"

# ── Pre-flight: sanity-check manifest ────────────────────────────
if ! python3 -c "import json; json.load(open('$EXT_DIR/manifest.json'))" 2>/dev/null; then
    fail "manifest.json is not valid JSON"
fi
success "manifest.json is valid JSON"

# Warn (not fail) if the extension still carries the "key" field.
# Chrome Web Store requires the field to be absent; the store assigns
# its own ID. Sideload installs need "key" to pin the ID. We keep
# two builds by stripping key for the store upload.
if grep -q '"key"' "$EXT_DIR/manifest.json"; then
    info "manifest.json contains a 'key' field — will strip it for the Web Store build"
    STRIP_KEY=1
else
    STRIP_KEY=0
fi

# ── Build ─────────────────────────────────────────────────────────
mkdir -p "$OUT_DIR"
STAGE_DIR="$(mktemp -d -t hush-package-XXXXXX)"
trap 'rm -rf "$STAGE_DIR"' EXIT

info "Staging in $STAGE_DIR"
cp -R "$EXT_DIR/" "$STAGE_DIR/hush/"

# Strip "key" from the staged manifest for Web Store submission.
# (Leaves the tracked file untouched for local sideloading.)
if [[ "$STRIP_KEY" -eq 1 ]]; then
    python3 - <<PY
import json, pathlib
p = pathlib.Path("$STAGE_DIR/hush/manifest.json")
data = json.loads(p.read_text())
data.pop("key", None)
p.write_text(json.dumps(data, indent=2) + "\n")
PY
    success "Stripped 'key' field from staged manifest"
fi

# Remove any macOS/editor droppings
find "$STAGE_DIR/hush" \( \
    -name ".DS_Store" -o \
    -name "._*" -o \
    -name "*.swp" -o \
    -name "Thumbs.db" \
    \) -delete 2>/dev/null || true

OUT_ZIP="$OUT_DIR/hush-$VERSION.zip"
rm -f "$OUT_ZIP"

# Zip with relative paths (cd in so the zip root is the extension dir)
(cd "$STAGE_DIR/hush" && zip -r -q "$OUT_ZIP" . -x "*.DS_Store" "._*")

SIZE_BYTES="$(stat -f%z "$OUT_ZIP" 2>/dev/null || stat -c%s "$OUT_ZIP")"
SIZE_KB=$(( SIZE_BYTES / 1024 ))

success "Built $OUT_ZIP (${SIZE_KB} KB)"

# ── Sanity check the zip ──────────────────────────────────────────
info "Contents of $OUT_ZIP:"
unzip -l "$OUT_ZIP" | sed 's/^/    /'

printf "\n${GREEN}${BOLD}  Ready for Chrome Web Store upload${RESET}\n"
printf "${DIM}  Upload at: https://chrome.google.com/webstore/devconsole${RESET}\n"
printf "${DIM}  Build:     %s${RESET}\n\n" "$OUT_ZIP"
