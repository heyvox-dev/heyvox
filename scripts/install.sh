#!/bin/bash
# HeyVox installer for macOS
# Usage:
#   curl -sSL heyvox.dev/install.sh | bash
#   curl -sSL heyvox.dev/install.sh | bash -s -- --yes --skip-setup
#
# Installs HeyVox and all dependencies, then runs the setup wizard.
# Safe to re-run — skips steps that are already complete.

set -euo pipefail

SCRIPT_VERSION="1.0.0"

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

# ── Defaults / flags ────────────────────────────────────────────────
TOTAL_STEPS=5
REPO_URL="https://github.com/heyvox-dev/heyvox.git"
SOURCE="git"           # git | pypi | local:/abs/path
ASSUME_YES=0
SKIP_SETUP=0
HEYVOX_DIR="$HOME/.heyvox"

usage() {
    cat <<EOF
HeyVox installer v${SCRIPT_VERSION}

Usage:
    curl -sSL heyvox.dev/install.sh | bash
    curl -sSL heyvox.dev/install.sh | bash -s -- [FLAGS]

Flags:
    --source=git           Install from GitHub main (default)
    --source=pypi          Install from PyPI (once published)
    --source=local:/PATH   Install from a local checkout (development)
    --yes, -y              Assume yes to all prompts (non-interactive)
    --skip-setup           Skip the setup wizard after install
    --version              Print installer version and exit
    --help, -h             Show this help and exit

Examples:
    # Quick install, run setup interactively
    curl -sSL heyvox.dev/install.sh | bash

    # Headless install without setup wizard
    curl -sSL heyvox.dev/install.sh | bash -s -- --yes --skip-setup

    # Install from a local checkout for development
    bash scripts/install.sh --source=local:\$PWD --yes
EOF
}

# Parse flags (supports --key=value and --key value)
while [[ $# -gt 0 ]]; do
    case "$1" in
        --source=*)   SOURCE="${1#--source=}"; shift ;;
        --source)     SOURCE="$2"; shift 2 ;;
        -y|--yes)     ASSUME_YES=1; shift ;;
        --skip-setup) SKIP_SETUP=1; shift ;;
        --version)    echo "$SCRIPT_VERSION"; exit 0 ;;
        -h|--help)    usage; exit 0 ;;
        *)            fail "Unknown flag: $1 (see --help)" ;;
    esac
done

# Helper: prompt yes/no, honouring --yes
confirm() {
    local prompt="$1"
    local default="${2:-Y}"
    if [[ "$ASSUME_YES" -eq 1 ]]; then
        return 0
    fi
    local hint
    if [[ "$default" == "Y" ]]; then hint="[Y/n]"; else hint="[y/N]"; fi
    printf "    %s %s " "$prompt" "$hint"
    read -r REPLY </dev/tty || REPLY="$default"
    REPLY="${REPLY:-$default}"
    [[ "$REPLY" =~ ^[Yy]$ ]]
}

# ── Banner ──────────────────────────────────────────────────────────
printf "\n${BOLD}  HeyVox Installer${RESET} ${DIM}v${SCRIPT_VERSION}${RESET}\n"
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

# Xcode command line tools (required for native deps)
if ! xcode-select -p &>/dev/null; then
    warn "Xcode Command Line Tools not installed"
    info "These are required to build pyaudio. Run:"
    printf "    ${DIM}xcode-select --install${RESET}\n"
    info "After the CLT installer finishes, re-run this script."
    exit 1
fi
success "Xcode Command Line Tools installed"

# ── Step 1: Homebrew ────────────────────────────────────────────────
step 1 "Checking Homebrew"

if command -v brew &>/dev/null; then
    BREW_PREFIX="$(brew --prefix)"
    success "Homebrew found at $BREW_PREFIX"

    # Secondary-user safety: brew repo must be writable or we'll fail
    # mid-install. Only matters if a dependency is actually missing.
    BREW_REPO="$(brew --repository 2>/dev/null || echo "$BREW_PREFIX")"
    if [[ ! -w "$BREW_REPO" ]]; then
        warn "Homebrew is installed but not writable by $(whoami)"
        info "You can still continue if all dependencies are already installed."
        info "If a dependency is missing, have the brew-owning user run the install,"
        info "or re-run this script as that user."
    fi
else
    info "Homebrew is required but not installed."
    if confirm "Install Homebrew now?"; then
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
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
    if confirm "Install Python 3.12 via Homebrew?"; then
        brew install python@3.12 || fail "brew install python@3.12 failed. Fix the brew issue and re-run."
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
    if ! brew install portaudio; then
        fail "brew install portaudio failed. If another user owns Homebrew, have them run 'brew install portaudio' and re-run this script."
    fi
    success "portaudio installed"
fi

# ── Step 4: Install HeyVox ──────────────────────────────────────────
step 4 "Installing HeyVox"

if command -v heyvox &>/dev/null; then
    EXISTING_VERSION="$(heyvox --version 2>/dev/null || echo 'unknown')"
    warn "HeyVox is already installed ($EXISTING_VERSION)"
    if ! confirm "Reinstall / upgrade?"; then
        info "Skipping install"
        if [[ "$SKIP_SETUP" -eq 0 ]]; then
            info "Jumping to setup wizard"
            exec heyvox setup
        fi
        exit 0
    fi
fi

# Resolve install spec
case "$SOURCE" in
    git)
        INSTALL_SPEC="heyvox[$PIP_EXTRAS] @ git+$REPO_URL"
        info "Installing from GitHub main with extras: [$PIP_EXTRAS]"
        ;;
    pypi)
        INSTALL_SPEC="heyvox[$PIP_EXTRAS]"
        info "Installing from PyPI with extras: [$PIP_EXTRAS]"
        ;;
    local:*)
        LOCAL_PATH="${SOURCE#local:}"
        if [[ ! -f "$LOCAL_PATH/pyproject.toml" ]]; then
            fail "Local source missing pyproject.toml at: $LOCAL_PATH"
        fi
        INSTALL_SPEC="$LOCAL_PATH[$PIP_EXTRAS]"
        info "Installing from local checkout at $LOCAL_PATH with extras: [$PIP_EXTRAS]"
        ;;
    *)
        fail "Unknown --source: $SOURCE (use git|pypi|local:/PATH)"
        ;;
esac

info "This may take a few minutes (downloading models and dependencies)"

# venv to avoid system package conflicts
VENV_DIR="$HEYVOX_DIR/venv"
if [[ ! -d "$VENV_DIR" ]]; then
    "$PYTHON" -m venv "$VENV_DIR" || fail "Failed to create venv at $VENV_DIR"
    success "Created virtual environment at $VENV_DIR"
else
    success "Virtual environment exists at $VENV_DIR"
fi

"$VENV_DIR/bin/pip" install --upgrade pip --quiet \
    || fail "pip upgrade failed inside venv"

if ! "$VENV_DIR/bin/pip" install "$INSTALL_SPEC" --quiet; then
    warn "Install failed. Retrying with verbose output for diagnostics…"
    "$VENV_DIR/bin/pip" install "$INSTALL_SPEC" || \
        fail "pip install failed. Check the error above (common causes: no network, brew portaudio missing, Python ABI mismatch)."
fi

success "HeyVox installed"

# Symlink binaries to a location on PATH
BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"
for cmd in heyvox herald heyvox-chrome-bridge; do
    if [[ -f "$VENV_DIR/bin/$cmd" ]]; then
        ln -sf "$VENV_DIR/bin/$cmd" "$BIN_DIR/$cmd"
    fi
done

# Ensure BIN_DIR is on PATH
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
    export PATH="$BIN_DIR:$PATH"
fi

success "Commands available: heyvox, herald"

# ── Step 5: Setup wizard ────────────────────────────────────────────
if [[ "$SKIP_SETUP" -eq 1 ]]; then
    info "Skipping setup wizard (--skip-setup)"
    info "Run 'heyvox setup' when you're ready to configure"
    printf "\n${GREEN}${BOLD}  HeyVox is installed.${RESET}\n"
    printf "${DIM}  Next: heyvox setup${RESET}\n\n"
    exit 0
fi

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
