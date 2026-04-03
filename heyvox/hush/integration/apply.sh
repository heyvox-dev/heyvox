#!/bin/bash
# Apply Hush integration to Herald and Vox.
# Run from the hush repo root: bash integration/apply.sh
#
# What it does:
#   - Adds Hush socket client to Herald's media.sh
#   - Adds Hush socket client to Vox's media.py
#   - Both projects fall back to existing behavior if Hush isn't running

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Resolve paths relative to the monorepo (heyvox/hush/integration/ → heyvox/)
HEYVOX_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
HERALD_MEDIA="${HERALD_HOME:-${HEYVOX_ROOT}/heyvox/herald}/lib/media.sh"
VOX_MEDIA="${VOX_HOME:-${HEYVOX_ROOT}}/heyvox/audio/media.py"

echo "Hush Integration Installer"
echo "=========================="

# --- Herald ---
if [ -f "$HERALD_MEDIA" ]; then
  echo ""
  echo "Herald: $HERALD_MEDIA"
  if grep -q "HUSH_SOCK" "$HERALD_MEDIA" 2>/dev/null; then
    echo "  ✓ Already integrated (HUSH_SOCK found)"
  else
    echo "  Applying Hush integration..."
    cd "$(dirname "$HERALD_MEDIA")/.."
    git diff --quiet lib/media.sh 2>/dev/null || {
      echo "  ⚠ media.sh has uncommitted changes — backing up to media.sh.bak"
      cp "$HERALD_MEDIA" "${HERALD_MEDIA}.bak"
    }
    cp "${SCRIPT_DIR}/herald-media.sh" "$HERALD_MEDIA"
    echo "  ✓ Updated media.sh with Hush support"
  fi
else
  echo "Herald not found at $HERALD_MEDIA — skipping"
fi

# --- Vox ---
if [ -f "$VOX_MEDIA" ]; then
  echo ""
  echo "Vox: $VOX_MEDIA"
  if grep -q "_HUSH_SOCK" "$VOX_MEDIA" 2>/dev/null; then
    echo "  ✓ Already integrated (_HUSH_SOCK found)"
  else
    echo "  Applying Hush integration..."
    cd "$(dirname "$VOX_MEDIA")/../.."
    git diff --quiet heyvox/audio/media.py 2>/dev/null || {
      echo "  ⚠ media.py has uncommitted changes — backing up to media.py.bak"
      cp "$VOX_MEDIA" "${VOX_MEDIA}.bak"
    }
    cp "${SCRIPT_DIR}/vox-media.py" "$VOX_MEDIA"
    echo "  ✓ Updated media.py with Hush support"
  fi
else
  echo "Vox not found at $VOX_MEDIA — skipping"
fi

echo ""
echo "Done. Hush integration is optional — if the extension isn't running,"
echo "both projects fall back to their existing MediaRemote/media key behavior."
