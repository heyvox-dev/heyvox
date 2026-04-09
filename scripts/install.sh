#!/bin/bash
# HeyVox installer for macOS
# Usage: curl -sSL heyvox.dev/install.sh | bash
#
# Installs HeyVox and all dependencies, then runs the setup wizard.
# Safe to re-run — skips steps that are already complete.

set -euo pipefail

# ── Colors ──────────────────────────────────────────────────────────
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
step()    { printf "\n${BOLD}[%s/%s] %s${RESET}\n" "$1" "$TOTAL_STEPS" "$2"; }

TOTAL_STEPS=5
REPO_URL="https://github.com/heyvox-dev/heyvox.git"
MIN_PYTHON="3.12"

# ── Banner ──────────────────────────────────────────────────────────
printf "\n${BOLD}  HeyVox Installer${RESET}\n"
printf "${DIM}  Voice Coding, not Vibe Coding${RESET}\n"
printf "${DIM}  https://heyvox.dev${RESET}\n\n"

# ── Step 0: Platform checks ────────────────────────────────────────
if [[ "$(uname)" != "Darwin" ]]; then
    fail "HeyVox requires macOS. Detected: $(uname)"
fi

ARCH="$(uname -m)"
if [[ "$ARCH" == "arm64" ]]; then
    success "Apple Silicon detected"
    PIP_EXTRAS="apple-silicon,tts,chrome"
else
    warn "Intel Mac detected — MLX Whisper unavailable, using sherpa-onnx"
    PIP_EXTRAS="tts,chrome"
fi

MACOS_VERSION="$(sw_vers -productVersion)"
MACOS_MAJOR="$(echo "$MACOS_VERSION" | cut -d. -f1)"
if [[ "$MACOS_MAJOR" -lt 14 ]]; then
    fail "HeyVox requires macOS 14 (Sonoma) or later. Detected: $MACOS_VERSION"
fi
success "macOS $MACOS_VERSION"

# ── Step 1: Homebrew ────────────────────────────────────────────────
step 1 "Checking Homebrew"

if command -v brew &>/dev/null; then
    success "Homebrew found at $(brew --prefix)"
else
    info "Homebrew is required but not installed."
    printf "    Install now? [Y/n] "
    read -r REPLY
    REPLY="${REPLY:-Y}"
    if [[ "$REPLY" =~ ^[Yy]$ ]]; then
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
        # Add brew to PATH for this session
        if [[ "$ARCH" == "arm64" ]]; then
            eval "$(/opt/homebrew/bin/brew shellenv)"
        else
            eval "$(/usr/local/bin/brew shellenv)"
        fi
        success "Homebrew installed"
    else
        fail "Homebrew is required. Install it from https://brew.sh and re-run this script."
    fi
fi

# ── Step 2: Python 3.12+ ───────────────────────────────────────────
step 2 "Checking Python"

check_python_version() {
    local py="$1"
    if ! command -v "$py" &>/dev/null; then
        return 1
    fi
    local ver
    ver="$("$py" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)" || return 1
    local major minor
    major="$(echo "$ver" | cut -d. -f1)"
    minor="$(echo "$ver" | cut -d. -f2)"
    [[ "$major" -ge 3 && "$minor" -ge 12 ]]
}

PYTHON=""
for candidate in python3.13 python3.12 python3; do
    if check_python_version "$candidate"; then
        PYTHON="$candidate"
        break
    fi
done

if [[ -n "$PYTHON" ]]; then
    PY_VERSION="$("$PYTHON" --version 2>&1)"
    success "$PY_VERSION ($(command -v "$PYTHON"))"
else
    info "Python 3.12+ is required but not found."
    printf "    Install Python 3.12 via Homebrew? [Y/n] "
    read -r REPLY
    REPLY="${REPLY:-Y}"
    if [[ "$REPLY" =~ ^[Yy]$ ]]; then
        brew install python@3.12
        PYTHON="python3.12"
        success "Python 3.12 installed"
    else
        fail "Python 3.12+ is required. Install it and re-run this script."
    fi
fi

# ── Step 3: System dependencies ─────────────────────────────────────
step 3 "Installing system dependencies"

if brew list portaudio &>/dev/null; then
    success "portaudio already installed"
else
    info "Installing portaudio (required for microphone access)"
    brew install portaudio
    success "portaudio installed"
fi

# ── Step 4: Install HeyVox ──────────────────────────────────────────
step 4 "Installing HeyVox"

HEYVOX_DIR="$HOME/.heyvox"

if command -v heyvox &>/dev/null; then
    EXISTING_VERSION="$(heyvox --version 2>/dev/null || echo 'unknown')"
    warn "HeyVox is already installed ($EXISTING_VERSION)"
    printf "    Reinstall / upgrade? [Y/n] "
    read -r REPLY
    REPLY="${REPLY:-Y}"
    if [[ ! "$REPLY" =~ ^[Yy]$ ]]; then
        info "Skipping install, jumping to setup"
        step 5 "Running setup wizard"
        exec heyvox setup
    fi
fi

info "Installing from GitHub with extras: [$PIP_EXTRAS]"
info "This may take a few minutes (downloading models and dependencies)"

# Use a venv to avoid system package conflicts
VENV_DIR="$HEYVOX_DIR/venv"
if [[ ! -d "$VENV_DIR" ]]; then
    "$PYTHON" -m venv "$VENV_DIR"
    success "Created virtual environment at $VENV_DIR"
else
    success "Virtual environment exists at $VENV_DIR"
fi

# Install into the venv
"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install "heyvox[$PIP_EXTRAS] @ git+$REPO_URL" --quiet

success "HeyVox installed"

# Symlink binaries to a location on PATH
BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"
for cmd in heyvox herald heyvox-chrome-bridge; do
    if [[ -f "$VENV_DIR/bin/$cmd" ]]; then
        ln -sf "$VENV_DIR/bin/$cmd" "$BIN_DIR/$cmd"
    fi
done

# Check if BIN_DIR is on PATH — fix it automatically
if ! echo "$PATH" | tr ':' '\n' | grep -q "^$BIN_DIR$"; then
    SHELL_RC=""
    case "$(basename "$SHELL")" in
        zsh)  SHELL_RC="$HOME/.zshrc" ;;
        bash) SHELL_RC="$HOME/.bashrc" ;;
        *)    SHELL_RC="$HOME/.profile" ;;
    esac

    if [[ -n "$SHELL_RC" ]] && ! grep -q '.local/bin' "$SHELL_RC" 2>/dev/null; then
        printf '\n# Added by HeyVox installer\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$SHELL_RC"
        success "Added $BIN_DIR to PATH in $SHELL_RC"
        info "Run 'source $SHELL_RC' or open a new terminal to use the heyvox command"
    else
        warn "$BIN_DIR is not on your PATH"
        info "Add this to your shell profile ($SHELL_RC):"
        printf "    ${DIM}export PATH=\"\$HOME/.local/bin:\$PATH\"${RESET}\n"
    fi
    # Add it for this session
    export PATH="$BIN_DIR:$PATH"
fi

success "Commands available: heyvox, herald"

# ── Step 5: Setup wizard ────────────────────────────────────────────
step 5 "Running setup wizard"

printf "\n${DIM}The setup wizard will walk you through:${RESET}\n"
printf "${DIM}  - macOS permissions (Microphone, Accessibility)${RESET}\n"
printf "${DIM}  - STT model download${RESET}\n"
printf "${DIM}  - Microphone test${RESET}\n"
printf "${DIM}  - Configuration${RESET}\n"
printf "${DIM}  - Herald TTS hooks (for Claude Code)${RESET}\n"
printf "${DIM}  - Hush Chrome extension (browser media pause)${RESET}\n"
printf "${DIM}  - MCP server registration${RESET}\n"
printf "${DIM}  - launchd auto-start${RESET}\n\n"

"$VENV_DIR/bin/heyvox" setup

printf "\n${GREEN}${BOLD}  HeyVox is ready.${RESET}\n"
printf "${DIM}  Start with: heyvox start${RESET}\n"
printf "${DIM}  Docs: https://heyvox.dev${RESET}\n"
printf "${DIM}  Issues: https://github.com/heyvox-dev/heyvox/issues${RESET}\n\n"
