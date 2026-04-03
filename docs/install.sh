#!/bin/bash
# HeyVox installer wrapper — fetches and runs the latest install script.
# Usage: curl -sSL heyvox.dev/install.sh | bash
exec bash <(curl -fsSL https://raw.githubusercontent.com/heyvox-dev/heyvox/main/scripts/install.sh)
