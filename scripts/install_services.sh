#!/usr/bin/env bash
# install_services.sh — one-shot Janus systemd deployment.
#
# WHAT THIS DOES:
#   1. Verifies prerequisites (systemd, janus on PATH, linger)
#   2. Stops any nohup-launched janus processes (telegram / web)
#   3. Writes ~/.janus/.env from your current shell env (idempotent —
#      preserves existing values when re-run)
#   4. Enables loginctl linger so user-systemd survives logout
#   5. Runs `janus service install --force` (creates units for
#      telegram + web + daemon)
#   6. Runs `janus service enable` (start now + auto-start on boot)
#   7. Shows status + tail commands for each service
#
# USAGE:
#   Set your env vars in the shell, then run:
#     bash scripts/install_services.sh
#
#   OR pass them inline:
#     JANUS_API_KEY=sk-... \
#     JANUS_API_BASE=https://api.x.com/v1 \
#     JANUS_MODEL=gpt-oss-120b \
#     JANUS_TELEGRAM_TOKEN=12345:ABC... \
#     bash scripts/install_services.sh
#
# IDEMPOTENT — running twice is safe. Existing .env values are
# preserved unless you pass FORCE_ENV=1 to overwrite them.
#
# DEFAULT BIND for janus-web is 127.0.0.1:8765 (localhost-only).
# Set JANUS_WEB_HOST=0.0.0.0 + JANUS_WEB_HOST_OK=1 in your env or
# .env to expose publicly. Use SSH tunnel by default for safety.

set -euo pipefail

# ---------- ANSI helpers (pure POSIX-friendly) ----------
RED=$'\033[31m'
GREEN=$'\033[32m'
YELLOW=$'\033[33m'
BOLD=$'\033[1m'
DIM=$'\033[2m'
RESET=$'\033[0m'

step() { printf '\n%s==>%s %s%s%s\n' "$BOLD" "$RESET" "$BOLD" "$1" "$RESET"; }
ok()   { printf '   %s✓%s %s\n' "$GREEN" "$RESET" "$1"; }
warn() { printf '   %s⚠%s %s\n' "$YELLOW" "$RESET" "$1"; }
err()  { printf '   %s✗%s %s\n' "$RED" "$RESET" "$1"; }
hint() { printf '   %s%s%s\n' "$DIM" "$1" "$RESET"; }

# ---------- 0. Preconditions ----------

step "0. Checking prerequisites"

if [[ "$(uname -s)" != "Linux" ]]; then
    err "This script is Linux-only (systemd required)."
    exit 1
fi
ok "Linux detected"

if ! command -v systemctl >/dev/null 2>&1; then
    err "systemctl not found. Install systemd or use a different init."
    exit 1
fi
ok "systemctl on PATH"

if ! command -v janus >/dev/null 2>&1; then
    err "janus not on PATH."
    hint "Try: pipx install '/opt/janus[all]' or pip install janus-agent"
    exit 1
fi
JANUS_BIN="$(command -v janus)"
JANUS_VERSION="$(janus --version 2>/dev/null || echo unknown)"
ok "janus at $JANUS_BIN ($JANUS_VERSION)"

# Required env vars. JANUS_TELEGRAM_TOKEN is optional (telegram service
# fails to start without it but the install still works).
REQUIRED_VARS=("JANUS_API_KEY" "JANUS_API_BASE" "JANUS_MODEL")
OPTIONAL_VARS=("JANUS_TELEGRAM_TOKEN" "JANUS_TELEGRAM_VERBOSE" "JANUS_WEB_HOST" "JANUS_WEB_PORT" "JANUS_WEB_HOST_OK")

ENV_FILE="${HOME}/.janus/.env"
ENV_DIR="$(dirname "$ENV_FILE")"
mkdir -p "$ENV_DIR"

# ---------- 1. Stop nohup-launched janus processes ----------

step "1. Stopping any nohup-launched janus processes"

# Find PIDs of janus telegram + janus web that aren't systemd units.
# (systemd-managed processes have a different cgroup; we filter by
# the nohup-style command line.)
NOHUP_PIDS=()
while read -r pid cmd; do
    if [[ -n "$pid" && -n "$cmd" ]]; then
        NOHUP_PIDS+=("$pid")
    fi
done < <(ps -eo pid,cmd 2>/dev/null | grep -E 'janus (telegram|web|daemon)' | grep -v grep | grep -v '/run/systemd' | awk '{print $1, $0}' || true)

if [[ ${#NOHUP_PIDS[@]} -eq 0 ]]; then
    ok "No nohup-style janus processes to stop"
else
    for pid in "${NOHUP_PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            kill -TERM "$pid" 2>/dev/null || true
            warn "Sent SIGTERM to PID $pid"
        fi
    done
    sleep 3
    # Force-kill any survivors.
    for pid in "${NOHUP_PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            kill -KILL "$pid" 2>/dev/null || true
            warn "Force-killed PID $pid (didn't honor SIGTERM)"
        fi
    done
    ok "Stopped ${#NOHUP_PIDS[@]} process(es)"
fi

# ---------- 2. Write ~/.janus/.env ----------

step "2. Writing $ENV_FILE"

# Build a fresh in-memory map: existing-env-file values, then current
# shell env overrides. The OUTPUT is what we write back. ``FORCE_ENV=1``
# inverts: shell overrides existing file.

declare -A EXISTING
if [[ -f "$ENV_FILE" ]]; then
    while IFS='=' read -r key val; do
        # Skip comments + blank lines
        if [[ -z "$key" || "$key" =~ ^[[:space:]]*# ]]; then
            continue
        fi
        # Strip leading whitespace from key
        key="${key#"${key%%[![:space:]]*}"}"
        # Strip surrounding quotes from value
        val="${val%\"}"
        val="${val#\"}"
        val="${val%\'}"
        val="${val#\'}"
        EXISTING["$key"]="$val"
    done < "$ENV_FILE"
    ok "Read existing $ENV_FILE (${#EXISTING[@]} keys)"
fi

# Merge: shell env wins over file when FORCE_ENV=1; otherwise file wins.
declare -A MERGED
for key in "${!EXISTING[@]}"; do
    MERGED["$key"]="${EXISTING[$key]}"
done

FORCE_ENV="${FORCE_ENV:-0}"
for var in "${REQUIRED_VARS[@]}" "${OPTIONAL_VARS[@]}"; do
    shell_val="${!var:-}"
    if [[ -n "$shell_val" ]]; then
        if [[ -z "${MERGED[$var]:-}" || "$FORCE_ENV" == "1" ]]; then
            MERGED["$var"]="$shell_val"
        fi
    fi
done

# Validate required vars are now present.
MISSING=()
for var in "${REQUIRED_VARS[@]}"; do
    if [[ -z "${MERGED[$var]:-}" ]]; then
        MISSING+=("$var")
    fi
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
    err "Required env vars missing: ${MISSING[*]}"
    hint "Set them in your shell and re-run:"
    hint "  export JANUS_API_KEY=sk-..."
    hint "  export JANUS_API_BASE=https://api.your-provider.com/v1"
    hint "  export JANUS_MODEL=your-model-id"
    hint "  bash scripts/install_services.sh"
    exit 1
fi

# Write the merged file. Known REQUIRED + OPTIONAL vars come first
# in stable order so the file is readable; any other JANUS_* keys
# the user already had (e.g. JANUS_TELEGRAM_CHATS, JANUS_BRAVE_API_KEY,
# JANUS_WHATSAPP_*) are preserved verbatim afterward — we don't drop
# user-defined keys just because they're not on our whitelist.
{
    echo "# Janus environment file — auto-managed by install_services.sh"
    echo "# Edit values directly; re-run with FORCE_ENV=1 to overwrite from shell."
    echo "# This file is read by systemd via EnvironmentFile=-${ENV_FILE}"
    echo
    EMITTED=()
    for var in "${REQUIRED_VARS[@]}" "${OPTIONAL_VARS[@]}"; do
        if [[ -n "${MERGED[$var]:-}" ]]; then
            val="${MERGED[$var]}"
            echo "${var}=${val}"
            EMITTED+=("$var")
        fi
    done
    # Preserve any other JANUS_* keys the user had (e.g. CHATS,
    # BRAVE_API_KEY, WHATSAPP_*) so we don't silently strip user
    # config. Sort for deterministic output.
    declare -a EXTRA_KEYS
    for key in "${!MERGED[@]}"; do
        is_known=0
        for known in "${EMITTED[@]}"; do
            if [[ "$key" == "$known" ]]; then is_known=1; break; fi
        done
        if [[ "$is_known" == "0" && "$key" =~ ^JANUS_ ]]; then
            EXTRA_KEYS+=("$key")
        fi
    done
    if [[ ${#EXTRA_KEYS[@]} -gt 0 ]]; then
        echo
        echo "# Other JANUS_* vars (preserved from existing .env / shell)"
        # Bash doesn't have a clean way to sort an array; pipe through sort.
        printf '%s\n' "${EXTRA_KEYS[@]}" | sort | while read -r key; do
            echo "${key}=${MERGED[$key]}"
        done
    fi
} > "$ENV_FILE"
chmod 600 "$ENV_FILE"
ok "Wrote $ENV_FILE (mode 0600, ${#MERGED[@]} variables)"

if [[ -z "${MERGED[JANUS_TELEGRAM_TOKEN]:-}" ]]; then
    warn "JANUS_TELEGRAM_TOKEN not set — janus-telegram will fail to start"
    hint "Add it to $ENV_FILE or set in shell + re-run with FORCE_ENV=1"
fi

# ---------- 3. Enable linger so user-systemd survives logout ----------

step "3. Enabling linger for user-systemd persistence"

USER_NAME="$(id -un)"
if loginctl show-user "$USER_NAME" 2>/dev/null | grep -q 'Linger=yes'; then
    ok "Linger already enabled for $USER_NAME"
else
    if [[ "$EUID" -eq 0 ]]; then
        loginctl enable-linger "$USER_NAME"
        ok "Enabled linger for $USER_NAME"
    else
        # Non-root: try with sudo, fall back to a hint.
        if command -v sudo >/dev/null 2>&1; then
            sudo loginctl enable-linger "$USER_NAME" || {
                warn "Couldn't run sudo loginctl enable-linger"
                hint "Run manually: sudo loginctl enable-linger $USER_NAME"
            }
            ok "Enabled linger for $USER_NAME (via sudo)"
        else
            warn "Not root and no sudo — linger NOT enabled"
            hint "Without linger, services stop when you log out."
            hint "Run manually: sudo loginctl enable-linger $USER_NAME"
        fi
    fi
fi

# ---------- 4. Install janus systemd units ----------

step "4. Installing janus systemd units"

# --force: overwrite existing units so v1.31.17 picks up the new
# janus-web entry on first run after an upgrade.
janus service install --force
ok "janus service install completed"

# Show what got written.
UNIT_DIR="${HOME}/.config/systemd/user"
if [[ -d "$UNIT_DIR" ]]; then
    while read -r unit; do
        ok "  $(basename "$unit")"
    done < <(ls "$UNIT_DIR"/janus-*.service 2>/dev/null || true)
fi

# ---------- 5. Enable + start ----------

step "5. Enabling + starting services"

janus service enable
ok "janus service enable completed"

# Brief settle so is-active queries reflect actual state.
sleep 2

# ---------- 6. Status report ----------

step "6. Wiring git post-merge auto-restart"

# Find the repo this script lives in (resolves symlinks; works whether
# you ran it from /opt/janus or piped it). The hooks dir is alongside
# the script's parent directory.
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]:-$0}")")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HOOKS_DIR="${SCRIPT_DIR}/git-hooks"

if [[ -d "$REPO_ROOT/.git" && -d "$HOOKS_DIR" ]]; then
    if (cd "$REPO_ROOT" && git config core.hooksPath "scripts/git-hooks"); then
        ok "git core.hooksPath → scripts/git-hooks (post-merge auto-restart wired)"
        # Make sure the hook is executable. git tracks the +x bit but
        # a fresh fetch on Windows + crlf-mangled clone can lose it.
        if [[ -f "$HOOKS_DIR/post-merge" ]]; then
            chmod +x "$HOOKS_DIR/post-merge"
            ok "  post-merge hook executable"
        fi
        hint "Bypass with JANUS_NO_AUTO_RESTART=1 git pull"
    else
        warn "Couldn't set core.hooksPath — auto-restart not wired"
    fi
else
    hint "No .git/ at $REPO_ROOT — skipping hook wiring (running outside the repo?)"
fi

step "7. Final status"

for svc in janus-telegram janus-web janus-daemon; do
    if systemctl --user is-active "${svc}.service" >/dev/null 2>&1; then
        ok "$svc: $(systemctl --user is-active "${svc}.service")"
    else
        state="$(systemctl --user is-active "${svc}.service" 2>&1 || true)"
        warn "$svc: $state"
        hint "  Check logs: journalctl --user -u $svc -n 50 --no-pager"
    fi
done

step "Done"

cat <<'EOM'
Useful commands:

  Status:        janus service status
  Restart one:   systemctl --user restart janus-web
  Live logs:     journalctl --user -u janus-web -f
  Last 50 lines: journalctl --user -u janus-telegram -n 50 --no-pager
  Stop all:      janus service disable
  Re-deploy:     FORCE_ENV=1 bash scripts/install_services.sh

  After-pull is now AUTOMATIC: a git post-merge hook restarts the
  services whenever janus/*.py changes. Bypass with:
    JANUS_NO_AUTO_RESTART=1 git pull

The web UI binds 127.0.0.1:8765 by default. To reach it from your
desktop browser:

  ssh -L 8765:127.0.0.1:8765 your-vps     # (then visit localhost:8765)

To expose publicly (HTTP only — use Caddy/nginx for TLS):
  Set JANUS_WEB_HOST=0.0.0.0 and JANUS_WEB_HOST_OK=1 in ~/.janus/.env,
  then: systemctl --user restart janus-web
EOM
