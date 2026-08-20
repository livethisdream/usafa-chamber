#!/usr/bin/env bash
#
# First-time setup for the USAFA chamber project on WSL/Linux.
#
# The companion to setup.ps1. This side is development-only: the instruments
# attach to Windows, so there are no hardware checks here. Use it to work on the
# service and frontend against the simulator.
#
# Venvs live outside the tree because this project sits under OneDrive, which
# otherwise syncs venv contents - slow, wasteful, and able to corrupt the venv.
# A symlink from the project keeps the .venv-linux name so tooling behaves.
#
# Usage:
#   ./setup.sh                # full setup
#   ./setup.sh --skip-frontend

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_NAME="usafa-chamber"
LINK_NAME=".venv-linux"
REAL_VENV="$HOME/.local/share/uv-venvs/$VENV_NAME"
SKIP_FRONTEND=0

for arg in "$@"; do
    case "$arg" in
        --skip-frontend) SKIP_FRONTEND=1 ;;
        -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

head()  { printf '\n\033[36m== %s\033[0m\n' "$1"; }
ok()    { printf '  \033[32m[ OK ]\033[0m %s\n' "$1"; }
warn()  { printf '  \033[33m[ !! ]\033[0m %s\n' "$1"; }
bad()   { printf '  \033[31m[FAIL]\033[0m %s\n' "$1"; }
info()  { printf '         \033[90m%s\033[0m\n' "$1"; }

printf '\n  USAFA Chamber - WSL/Linux setup\n'
info "$PROJECT_ROOT"

# ---------------------------------------------------------------------------
head 'Prerequisites'

if command -v uv >/dev/null 2>&1; then
    ok "uv        $(uv --version)"
else
    bad 'uv not found on PATH'
    info 'Install: https://docs.astral.sh/uv/getting-started/installation/'
    exit 1
fi

if command -v node >/dev/null 2>&1; then
    ok "node      $(node --version)"
else
    warn 'node not found; frontend setup will be skipped'
    SKIP_FRONTEND=1
fi

# ---------------------------------------------------------------------------
head 'Python environment'

cd "$PROJECT_ROOT"

# A real directory here means an in-tree venv, which is what the out-of-tree
# convention exists to prevent.
if [ -e "$LINK_NAME" ] && [ ! -L "$LINK_NAME" ]; then
    bad "$LINK_NAME exists as a real directory, not a symlink"
    info 'Venvs must stay out of the OneDrive tree. Remove it and re-run:'
    info "  rm -rf \"$PROJECT_ROOT/$LINK_NAME\""
    exit 1
fi

if [ -d "$REAL_VENV" ]; then
    if [ -f "$REAL_VENV/.origin" ]; then
        origin="$(cat "$REAL_VENV/.origin")"
        if [ "$origin" != "$PROJECT_ROOT" ]; then
            bad "venv at $REAL_VENV belongs to another project"
            info "  its .origin:  $origin"
            info "  this project: $PROJECT_ROOT"
            exit 1
        fi
        ok "venv reused  $REAL_VENV"
    else
        bad "venv at $REAL_VENV has no .origin marker"
        info "Remove it and re-run: rm -rf \"$REAL_VENV\""
        exit 1
    fi
else
    info "creating venv at $REAL_VENV"
    uv venv "$REAL_VENV"
    printf '%s' "$PROJECT_ROOT" > "$REAL_VENV/.origin"
    ok "venv created $REAL_VENV"
fi

if [ ! -L "$LINK_NAME" ]; then
    ln -s "$REAL_VENV" "$LINK_NAME"
    ok "$LINK_NAME -> $REAL_VENV"
else
    ok "$LINK_NAME symlink present"
fi

info 'syncing Python dependencies (with the sim extra)'
UV_PROJECT_ENVIRONMENT="$LINK_NAME" uv sync --extra sim
ok 'Python dependencies installed'

# ---------------------------------------------------------------------------
if [ "$SKIP_FRONTEND" -eq 0 ]; then
    head 'Frontend'
    if [ -f frontend/package.json ]; then
        (cd frontend && npm install --no-fund --no-audit)
        ok 'frontend dependencies installed'
    else
        warn 'frontend/package.json not found, skipping'
    fi
fi

# ---------------------------------------------------------------------------
head 'Hardware'
info 'Not checked here - the VNA and positioner attach to Windows.'
info 'Run setup.ps1 on the acquisition PC for driver and readiness checks.'

head 'Ready'
printf '  Run the service against the simulator:\n\n'
printf '    ./.venv-linux/bin/python chamber_service.py --sim\n\n'
if [ "$SKIP_FRONTEND" -eq 0 ]; then
    printf '  And the frontend:\n\n'
    printf '    npm run dev --prefix frontend          # http://localhost:5173\n\n'
else
    # Do not hand out a command that cannot run: node was missing above.
    warn 'Frontend was not set up (no node), so there is nothing to serve on 5173.'
    info 'Install Node.js and re-run, or drive the service from setup.ps1 on Windows.'
    printf '\n'
fi
