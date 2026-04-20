#!/bin/bash
# HeyVox uninstaller wrapper — fetches and runs the latest uninstall script.
# Usage: curl -sSL heyvox.dev/uninstall.sh | bash
exec bash <(curl -fsSL https://raw.githubusercontent.com/heyvox-dev/heyvox/main/scripts/uninstall.sh)
