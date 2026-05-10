#!/usr/bin/env sh
# install.sh — one-line Janus installer (Phase 5.3).
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/samalgotrader7-ops/janus/main/scripts/install.sh | sh
#
# What this does:
#   1. Detects platform (Linux / macOS) and Python availability.
#   2. Installs pipx if missing (via `python3 -m pip install --user pipx`).
#   3. Installs janus-agent — prefers PyPI, falls back to git+URL when
#      the PyPI release isn't available yet.
#   4. Prints next-step instructions including `janus onboard`.
#
# Environment overrides:
#   JANUS_INSTALL_SOURCE=pypi|git  Force install source (default: auto)
#   JANUS_INSTALL_REF=v1.32.1      Pin to a specific tag/branch when
#                                  using git source (default: main)
#   JANUS_INSTALL_EXTRAS=all       Optional extras to include (default: all)
#
# This script is POSIX-compatible (works under /bin/sh, dash, bash, zsh)
# so users can pipe with any of:
#   curl ... | sh    curl ... | bash    curl ... | zsh

set -eu

# ---------- color helpers (only when stdout is a TTY) ----------
if [ -t 1 ] && command -v tput >/dev/null 2>&1; then
    BOLD="$(tput bold 2>/dev/null || echo '')"
    GREEN="$(tput setaf 2 2>/dev/null || echo '')"
    YELLOW="$(tput setaf 3 2>/dev/null || echo '')"
    RED="$(tput setaf 1 2>/dev/null || echo '')"
    DIM="$(tput dim 2>/dev/null || echo '')"
    RESET="$(tput sgr0 2>/dev/null || echo '')"
else
    BOLD=""
    GREEN=""
    YELLOW=""
    RED=""
    DIM=""
    RESET=""
fi

step() { printf '\n%s==>%s %s%s%s\n' "$BOLD" "$RESET" "$BOLD" "$1" "$RESET"; }
ok()   { printf '   %s\xE2\x9C\x93%s %s\n' "$GREEN" "$RESET" "$1"; }
warn() { printf '   %s\xE2\x9A\xA0%s %s\n' "$YELLOW" "$RESET" "$1"; }
err()  { printf '   %s\xE2\x9C\x97%s %s\n' "$RED" "$RESET" "$1"; }
hint() { printf '   %s%s%s\n' "$DIM" "$1" "$RESET"; }

# ---------- 1. Platform + Python check ----------

step "1. Checking environment"

OS="$(uname -s)"
case "$OS" in
    Linux*)   PLATFORM="linux";;
    Darwin*)  PLATFORM="macos";;
    *)
        err "Unsupported platform: $OS"
        hint "Janus supports Linux and macOS. Windows users: install via WSL or pipx."
        exit 1
        ;;
esac
ok "$PLATFORM detected"

if ! command -v python3 >/dev/null 2>&1; then
    err "python3 not found"
    if [ "$PLATFORM" = "macos" ]; then
        hint "Install with: brew install python@3.12"
    else
        hint "Install with: sudo apt install python3 python3-pip   # or your package manager"
    fi
    exit 1
fi

PY_VERSION="$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || echo "unknown")"
PY_MAJOR="$(python3 -c 'import sys; print(sys.version_info[0])' 2>/dev/null || echo 0)"
PY_MINOR="$(python3 -c 'import sys; print(sys.version_info[1])' 2>/dev/null || echo 0)"

# Janus requires Python 3.10+
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    err "Python $PY_VERSION found — Janus requires 3.10+"
    exit 1
fi
ok "python3 $PY_VERSION"

# ---------- 2. Install pipx if missing ----------

step "2. Ensuring pipx is available"

if command -v pipx >/dev/null 2>&1; then
    PIPX_VERSION="$(pipx --version 2>/dev/null || echo unknown)"
    ok "pipx $PIPX_VERSION already installed"
else
    warn "pipx not found — installing"
    # Use --user install so we don't need sudo. PEP 668 protected
    # systems (recent Debian / Ubuntu) need --break-system-packages
    # for the pipx bootstrap; pipx itself then manages a venv.
    if ! python3 -m pip install --user --break-system-packages pipx 2>/dev/null; then
        # Older Python without --break-system-packages — try without.
        python3 -m pip install --user pipx
    fi
    # Ensure pipx-managed bin dir is on PATH for this session and
    # future ones.
    python3 -m pipx ensurepath >/dev/null 2>&1 || true
    # Refresh PATH for the rest of this script.
    USER_BIN="$(python3 -c 'import site; print(site.USER_BASE)')/bin"
    case ":$PATH:" in
        *":$USER_BIN:"*) ;;
        *) PATH="$USER_BIN:$PATH"; export PATH;;
    esac
    if command -v pipx >/dev/null 2>&1; then
        ok "pipx installed"
    else
        err "pipx install succeeded but binary not on PATH"
        hint "Add this to your shell rc:"
        hint "    export PATH=\"\$HOME/.local/bin:\$PATH\""
        hint "Then re-run this installer."
        exit 1
    fi
fi

# ---------- 3. Install Janus ----------

step "3. Installing Janus"

EXTRAS="${JANUS_INSTALL_EXTRAS:-all}"
SOURCE="${JANUS_INSTALL_SOURCE:-auto}"
REF="${JANUS_INSTALL_REF:-main}"

# `janus-agent` is the PyPI distribution name (per pyproject.toml).
# `janus` is the executable command.
PYPI_SPEC="janus-agent[$EXTRAS]"
GIT_URL="git+https://github.com/samalgotrader7-ops/janus.git@$REF"
GIT_SPEC="janus-agent[$EXTRAS] @ $GIT_URL"

install_from_pypi() {
    pipx install --force "$PYPI_SPEC"
}

install_from_git() {
    pipx install --force "$GIT_SPEC"
}

case "$SOURCE" in
    pypi)
        ok "Forcing PyPI source"
        install_from_pypi
        ;;
    git)
        ok "Forcing git source ($REF)"
        install_from_git
        ;;
    auto|*)
        # Try PyPI first; fall back to git when the package isn't
        # there yet (Phase 5.1 ships the workflow but real publish
        # waits on Sam adding the secret + reserving the namespace).
        ok "Auto-detecting install source"
        if install_from_pypi 2>/dev/null; then
            ok "Installed from PyPI"
        else
            warn "PyPI install failed — falling back to git ($REF)"
            install_from_git
            ok "Installed from git+URL ($REF)"
        fi
        ;;
esac

# ---------- 4. Confirm + next steps ----------

step "4. Verifying installation"

if command -v janus >/dev/null 2>&1; then
    JANUS_VERSION="$(janus --version 2>/dev/null || echo unknown)"
    ok "janus $JANUS_VERSION on PATH"
else
    warn "janus binary not yet on PATH"
    hint "Restart your shell, OR run:"
    hint "    export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

step "Done"

cat <<'EOM'
Next steps:

  1. Set required env vars in your shell or ~/.janus/.env:
       JANUS_API_KEY        OpenAI / Anthropic / OpenRouter key
       JANUS_API_BASE       endpoint (e.g. https://api.openai.com/v1)
       JANUS_MODEL          model id (e.g. anthropic/claude-sonnet-4-6)

  2. Run the onboarding wizard:
       janus onboard

  3. Start a chat:
       janus

  Optional gateways:
       janus telegram      Telegram bot (set JANUS_TELEGRAM_TOKEN first)
       janus web           Local web UI on http://localhost:8765
       janus daemon        Background trigger daemon

  For VPS deployment with systemd auto-restart:
       bash scripts/install_services.sh

Documentation: https://github.com/samalgotrader7-ops/janus#readme
Issues:        https://github.com/samalgotrader7-ops/janus/issues
EOM
