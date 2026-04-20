#!/bin/bash
# HeyVox uninstaller for macOS
# Usage:
#   curl -sSL heyvox.dev/uninstall.sh | bash
#   curl -sSL heyvox.dev/uninstall.sh | bash -s -- --keep-config --yes
#
# Removes the HeyVox venv, CLI symlinks, launchd agent, herald hooks, and
# Hush native messaging host. Shared system packages (Homebrew, portaudio,
# Python) are left alone. Config and model caches optional.

set -euo pipefail

SCRIPT_VERSION="1.0.0"

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
removed() { printf "${DIM}rm  ${RESET}%s\n" "$1"; }
fail()    { printf "${RED}err ${RESET}%s\n" "$1"; exit 1; }

# ── Defaults / flags ────────────────────────────────────────────────
ASSUME_YES=0
KEEP_CONFIG=0
KEEP_MODELS=0

usage() {
    cat <<EOF
HeyVox uninstaller v${SCRIPT_VERSION}

Usage:
    curl -sSL heyvox.dev/uninstall.sh | bash
    curl -sSL heyvox.dev/uninstall.sh | bash -s -- [FLAGS]

Flags:
    --keep-config    Keep ~/.config/heyvox (config.yaml, history, etc.)
    --keep-models    Keep STT/wake-word model caches
    --yes, -y        Assume yes to confirmation prompt
    --version        Print uninstaller version and exit
    --help, -h       Show this help

Removes:
    - ~/.heyvox/venv                         (Python virtualenv)
    - ~/.local/bin/{heyvox,herald,heyvox-chrome-bridge}
    - ~/Library/LaunchAgents/com.heyvox.listener.plist  (launchd autostart)
    - Herald hooks from ~/.claude/settings.json
    - ~/Library/Application Support/Google/Chrome/NativeMessagingHosts/com.hush.bridge.json
    - IPC sockets in \$TMPDIR and /tmp (heyvox-hud.sock, hush.sock, etc.)

Leaves alone:
    - Homebrew packages (portaudio, python)
    - Chrome extensions (remove via chrome://extensions)
    - ~/.config/heyvox                       (unless you omit --keep-config)
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --keep-config) KEEP_CONFIG=1; shift ;;
        --keep-models) KEEP_MODELS=1; shift ;;
        -y|--yes)      ASSUME_YES=1; shift ;;
        --version)     echo "$SCRIPT_VERSION"; exit 0 ;;
        -h|--help)     usage; exit 0 ;;
        *)             fail "Unknown flag: $1 (see --help)" ;;
    esac
done

printf "\n${BOLD}  HeyVox Uninstaller${RESET} ${DIM}v${SCRIPT_VERSION}${RESET}\n\n"

if [[ "$ASSUME_YES" -ne 1 ]]; then
    printf "    This will remove HeyVox from $(whoami)'s account.\n"
    printf "    Continue? [y/N] "
    read -r REPLY </dev/tty || REPLY="N"
    if [[ ! "$REPLY" =~ ^[Yy]$ ]]; then
        info "Aborted."
        exit 0
    fi
fi

# ── Stop running processes first ──────────────────────────────────
info "Stopping HeyVox processes"
AGENT_PLIST="$HOME/Library/LaunchAgents/com.heyvox.listener.plist"
if [[ -f "$AGENT_PLIST" ]]; then
    launchctl bootout "gui/$(id -u)" "$AGENT_PLIST" 2>/dev/null || true
    success "Unloaded launchd agent"
fi

# Best-effort kill of anything left over
pkill -f "heyvox" 2>/dev/null || true
pkill -f "kokoro-daemon" 2>/dev/null || true
pkill -f "herald" 2>/dev/null || true

# ── Remove venv ───────────────────────────────────────────────────
if [[ -d "$HOME/.heyvox/venv" ]]; then
    rm -rf "$HOME/.heyvox/venv"
    removed "~/.heyvox/venv"
fi

# If .heyvox is now empty (or only has cache), take it too
if [[ -d "$HOME/.heyvox" ]] && [[ -z "$(ls -A "$HOME/.heyvox" 2>/dev/null)" ]]; then
    rmdir "$HOME/.heyvox"
    removed "~/.heyvox (empty)"
fi

# ── Remove CLI symlinks ───────────────────────────────────────────
for cmd in heyvox herald heyvox-chrome-bridge; do
    link="$HOME/.local/bin/$cmd"
    if [[ -L "$link" ]] || [[ -f "$link" ]]; then
        rm -f "$link"
        removed "~/.local/bin/$cmd"
    fi
done

# ── Remove launchd agent plist ────────────────────────────────────
if [[ -f "$AGENT_PLIST" ]]; then
    rm -f "$AGENT_PLIST"
    removed "~/Library/LaunchAgents/com.heyvox.listener.plist"
fi

# ── Remove Hush native messaging host manifest ────────────────────
HUSH_NMH_DIRS=(
    "$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts"
    "$HOME/Library/Application Support/Google/Chrome Beta/NativeMessagingHosts"
    "$HOME/Library/Application Support/Chromium/NativeMessagingHosts"
    "$HOME/Library/Application Support/BraveSoftware/Brave-Browser/NativeMessagingHosts"
)
for dir in "${HUSH_NMH_DIRS[@]}"; do
    manifest="$dir/com.hush.bridge.json"
    if [[ -f "$manifest" ]]; then
        rm -f "$manifest"
        removed "${manifest/#$HOME/~}"
    fi
done

# ── Remove IPC sockets / flags ────────────────────────────────────
for sock in \
    "${TMPDIR:-/tmp}/heyvox-hud.sock" \
    "${TMPDIR:-/tmp}/heyvox-recording" \
    "${TMPDIR:-/tmp}/kokoro-daemon.sock" \
    "${TMPDIR:-/tmp}/hush.sock" \
    "/tmp/heyvox-hud.sock" \
    "/tmp/heyvox-recording" \
    "/tmp/kokoro-daemon.sock" \
    "/tmp/hush.sock"; do
    if [[ -e "$sock" ]] || [[ -L "$sock" ]]; then
        rm -f "$sock"
        removed "$sock"
    fi
done

# ── Remove Herald hooks from Claude Code settings ─────────────────
CLAUDE_SETTINGS="$HOME/.claude/settings.json"
if [[ -f "$CLAUDE_SETTINGS" ]]; then
    if command -v python3 &>/dev/null && python3 -c "import json,sys; d=json.load(open('$CLAUDE_SETTINGS')); h=d.get('hooks',{}); sys.exit(0 if any('herald' in str(v) for v in h.values()) else 1)" 2>/dev/null; then
        python3 - <<PY
import json, pathlib
p = pathlib.Path("$CLAUDE_SETTINGS")
data = json.loads(p.read_text())
hooks = data.get("hooks", {})
for key in list(hooks.keys()):
    entries = hooks[key]
    if isinstance(entries, list):
        hooks[key] = [e for e in entries if "herald" not in json.dumps(e)]
        if not hooks[key]:
            del hooks[key]
    elif "herald" in json.dumps(entries):
        del hooks[key]
if hooks:
    data["hooks"] = hooks
else:
    data.pop("hooks", None)
p.write_text(json.dumps(data, indent=2) + "\n")
PY
        success "Stripped Herald hooks from ~/.claude/settings.json"
    fi
fi

# Also remove any herald hook shims installed under ~/.claude/hooks/herald/
if [[ -d "$HOME/.claude/hooks/herald" ]]; then
    rm -rf "$HOME/.claude/hooks/herald"
    removed "~/.claude/hooks/herald"
fi

# ── Optional: config and models ───────────────────────────────────
if [[ "$KEEP_CONFIG" -eq 1 ]]; then
    info "Keeping ~/.config/heyvox (per --keep-config)"
else
    if [[ -d "$HOME/.config/heyvox" ]]; then
        rm -rf "$HOME/.config/heyvox"
        removed "~/.config/heyvox"
    fi
fi

if [[ "$KEEP_MODELS" -eq 1 ]]; then
    info "Keeping model caches (per --keep-models)"
else
    for cache in \
        "$HOME/Library/Caches/heyvox" \
        "$HOME/.cache/huggingface/hub/models--openai--whisper-large-v3-turbo" \
        "$HOME/.cache/mlx-community"; do
        if [[ -d "$cache" ]]; then
            # Only touch the heyvox cache automatically; HF and MLX are shared.
            if [[ "$cache" == "$HOME/Library/Caches/heyvox" ]]; then
                rm -rf "$cache"
                removed "${cache/#$HOME/~}"
            else
                info "Keeping shared cache: ${cache/#$HOME/~} (used by other apps)"
            fi
        fi
    done
fi

# ── PATH cleanup note ─────────────────────────────────────────────
for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.profile"; do
    if [[ -f "$rc" ]] && grep -q "Added by HeyVox installer" "$rc" 2>/dev/null; then
        warn "$rc still has the PATH line from the installer"
        info "Remove it manually if you no longer want ~/.local/bin on PATH"
    fi
done

printf "\n${GREEN}${BOLD}  HeyVox uninstalled.${RESET}\n"
printf "${DIM}  To remove the Hush Chrome extension, go to chrome://extensions${RESET}\n"
printf "${DIM}  Homebrew, portaudio, and Python were left installed${RESET}\n\n"
