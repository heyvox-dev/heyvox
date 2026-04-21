#!/bin/bash
# Reset the current user's HeyVox installation so `scripts/install.sh` runs
# from a clean slate. DEV TOOL — not for end users; not wrapped on heyvox.dev.
#
# Goes further than scripts/uninstall.sh:
#   - strips the installer's PATH line from shell rc files
#   - clears model caches (Whisper, Kokoro, MLX, openwakeword)
#   - with --full, `brew uninstall portaudio` so install step 3 runs
#   - with --full, uninstall Chrome extension (prints instructions; the
#     Chrome Web Store doesn't support scripted removal of installed extensions)
#
# What it does NOT touch:
#   - Homebrew itself
#   - Python 3.12 (other apps may depend on it)
#   - Xcode Command Line Tools
#   - Any other user's install on this Mac
#
# Usage:
#   scripts/dev/reset-install-test.sh [--full] [--yes]

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
removed() { printf "${DIM}rm  ${RESET}%s\n" "$1"; }
fail()    { printf "${RED}err ${RESET}%s\n" "$1"; exit 1; }

FULL=0
ASSUME_YES=0

usage() {
    cat <<EOF
Reset HeyVox install state for end-to-end install test.

Usage: $0 [FLAGS]

Flags:
    --full   Also brew-uninstall portaudio and clear model caches.
             Use when you want to exercise install steps 3 (portaudio)
             and first-run model downloads.
    --yes    Skip confirmation prompt.
    --help   Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --full)    FULL=1; shift ;;
        -y|--yes)  ASSUME_YES=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) fail "Unknown flag: $1" ;;
    esac
done

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

printf "\n${BOLD}  HeyVox Install-Test Reset${RESET}\n"
printf "${DIM}  User: %s${RESET}\n" "$(whoami)"
printf "${DIM}  Full mode: %s${RESET}\n\n" "$([[ $FULL -eq 1 ]] && echo 'yes — brew + caches' || echo 'no — per-user only')"

if [[ "$ASSUME_YES" -ne 1 ]]; then
    printf "    This will wipe HeyVox state for $(whoami). Continue? [y/N] "
    read -r REPLY </dev/tty || REPLY="N"
    [[ "$REPLY" =~ ^[Yy]$ ]] || { info "Aborted."; exit 0; }
fi

# ── Step 1: run the regular uninstaller first ─────────────────────
info "Running scripts/uninstall.sh to clear per-user state"
if [[ -x "$REPO_ROOT/scripts/uninstall.sh" ]]; then
    "$REPO_ROOT/scripts/uninstall.sh" --yes || warn "uninstall.sh returned non-zero — continuing anyway"
else
    warn "scripts/uninstall.sh not found at $REPO_ROOT — skipping"
fi

# ── Step 2: strip installer's PATH line from shell rc files ──────
info "Stripping installer PATH line from shell rc files"
for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.profile"; do
    [[ -f "$rc" ]] || continue
    if grep -q "Added by HeyVox installer" "$rc"; then
        # Remove both the comment line and the export line that follows it
        python3 - "$rc" <<'PY'
import sys, pathlib, re
p = pathlib.Path(sys.argv[1])
text = p.read_text()
# Matches the 2-line block the installer adds, optionally with a leading blank line
new = re.sub(
    r'\n?# Added by HeyVox installer\nexport PATH="\$HOME/\.local/bin:\$PATH"\n',
    '',
    text,
)
if new != text:
    p.write_text(new)
    print(f"stripped from {p}")
PY
        removed "$rc (PATH line)"
    fi
done

# ── Step 3: remove any lingering HeyVox install dir ──────────────
if [[ -d "$HOME/.heyvox" ]]; then
    rm -rf "$HOME/.heyvox"
    removed "~/.heyvox"
fi

# ── Step 4: model caches (only in --full) ────────────────────────
if [[ "$FULL" -eq 1 ]]; then
    info "Clearing model caches"
    for cache in \
        "$HOME/Library/Caches/heyvox" \
        "$HOME/.cache/huggingface/hub/models--mlx-community--whisper-large-v3-turbo" \
        "$HOME/.cache/huggingface/hub/models--hexgrad--Kokoro-82M" \
        "$HOME/.cache/mlx-community" \
        "$HOME/.config/openwakeword"; do
        if [[ -d "$cache" ]]; then
            rm -rf "$cache"
            removed "${cache/#$HOME/~}"
        fi
    done
fi

# ── Step 5: brew uninstall portaudio (only in --full) ────────────
if [[ "$FULL" -eq 1 ]]; then
    if command -v brew &>/dev/null; then
        if brew list portaudio &>/dev/null; then
            info "brew uninstall portaudio"
            if brew uninstall portaudio 2>&1; then
                success "portaudio uninstalled"
            else
                warn "brew uninstall portaudio failed — may be a dep of something else, or owned by another user"
            fi
        else
            info "portaudio not installed via brew — nothing to do"
        fi
    fi
fi

# ── Step 6: Chrome extension reminder ────────────────────────────
info "Chrome extension cannot be scripted"
printf "    ${DIM}Open chrome://extensions, find \"Hush\", click Remove${RESET}\n"
printf "    ${DIM}(Chrome blocks programmatic uninstall of user-installed extensions)${RESET}\n"

# ── Step 7: sanity report ────────────────────────────────────────
printf "\n${BOLD}Sanity check:${RESET}\n"
check_absent() {
    local path="$1"
    local label="$2"
    if [[ -e "$path" ]]; then
        printf "  ${YELLOW}still present${RESET}  %s\n" "$label"
    else
        printf "  ${GREEN}gone${RESET}           %s\n" "$label"
    fi
}
check_absent "$HOME/.heyvox"                                  "~/.heyvox"
check_absent "$HOME/.config/heyvox"                           "~/.config/heyvox"
check_absent "$HOME/.local/bin/heyvox"                        "~/.local/bin/heyvox"
check_absent "$HOME/Library/LaunchAgents/com.heyvox.listener.plist" "launchd plist"
check_absent "$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts/com.hush.bridge.json" "Hush NMH manifest"

if [[ "$FULL" -eq 1 ]]; then
    if brew list portaudio &>/dev/null 2>&1; then
        printf "  ${YELLOW}still present${RESET}  portaudio (brew)\n"
    else
        printf "  ${GREEN}gone${RESET}           portaudio (brew)\n"
    fi
fi

printf "\n${GREEN}${BOLD}  Ready for install test.${RESET}\n"
printf "${DIM}  Re-install:  curl -sSL heyvox.dev/install.sh | bash${RESET}\n"
printf "${DIM}  Or locally:  bash scripts/install.sh --source=local:\$PWD${RESET}\n\n"
