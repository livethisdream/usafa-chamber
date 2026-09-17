#!/usr/bin/env bash
#
# Bring the chamber up on Linux: S2VNA, the service, the dashboard.
#
# Checks each piece in the order it is needed and stops with the reason when
# one is missing, rather than letting the service fail with a Windows-shaped
# hint. Ctrl+C stops the service and frontend together; S2VNA is left running,
# so its calibration and state survive a restart of the service.
#
# S2VNA on Linux is CMT's AppImage: the Windows S2VNA under a bundled Wine, which
# reaches USB through CMT's UTPServer helper. The stock Windows installer run
# under Wine cannot, and the native cmtvna tarball offers only a demo RP5. As of
# 25.1.2 the AppImage still does not open the A2202-Fx (36bf:1413) and falls back
# to a demo C1209, so until CMT ships a newer one, run S2VNA on a Windows PC and
# point VNA= at it. The socket server has no command-line switch; turn it on once
# in the GUI and it is remembered in ~/.vna-portable.
#
# Usage:
#   ./start.sh                 # real rig
#   ./start.sh --sim           # simulator, no instruments needed
#   ./start.sh --no-browser
#
# Overrides (environment):
#   VNA=TCPIP0::127.0.0.1::5025::SOCKET   POS=ASRL/dev/emcenter::INSTR
#   S2VNA=/path/to/CMT_S2VNA_*.appimage

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$PROJECT_ROOT/.venv-linux/bin/python"
VNA="${VNA:-TCPIP0::127.0.0.1::5025::SOCKET}"
POS="${POS:-ASRL/dev/emcenter::INSTR}"
SERVICE_PORT=8766
FRONTEND_PORT=5173
LOG_DIR="$HOME/.local/state/usafa-chamber"
SIM=0
BROWSER=1

for arg in "$@"; do
    case "$arg" in
        --sim) SIM=1 ;;
        --no-browser) BROWSER=0 ;;
        -h|--help) sed -n '2,18p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

head()  { printf '\n\033[36m== %s\033[0m\n' "$1"; }
ok()    { printf '  \033[32m[ OK ]\033[0m %s\n' "$1"; }
warn()  { printf '  \033[33m[ !! ]\033[0m %s\n' "$1"; }
bad()   { printf '  \033[31m[FAIL]\033[0m %s\n' "$1"; }
info()  { printf '         \033[90m%s\033[0m\n' "$1"; }

listening() { ss -ltnH "sport = :$1" 2>/dev/null | grep -q .; }

# Each child runs in its own process group so Ctrl+C can take down everything
# it spawned (npm -> node, sg -> python), not just the direct child.
PIDS=()
cleanup() {
    trap - EXIT INT TERM
    if [ "${#PIDS[@]}" -gt 0 ]; then
        printf '\n'
        info 'stopping service and frontend'
        for pid in "${PIDS[@]}"; do kill -TERM -- "-$pid" 2>/dev/null || true; done
        wait 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM

cd "$PROJECT_ROOT"
mkdir -p "$LOG_DIR"

# ---------------------------------------------------------------------------
head 'Environment'

if [ ! -x "$PY" ]; then
    bad 'Python environment missing'
    info 'Run: bash setup.sh'
    exit 1
fi
ok 'Python environment'

if ! command -v npm >/dev/null 2>&1 && [ -s "$HOME/.nvm/nvm.sh" ]; then
    # nvm only loads in interactive shells; a script has to ask for it.
    set +u; . "$HOME/.nvm/nvm.sh"; set -u
fi
if ! command -v npm >/dev/null 2>&1; then
    bad 'npm not found'
    info 'Install Node (nvm install --lts), then: bash setup.sh'
    exit 1
fi
if [ ! -x frontend/node_modules/.bin/vite ]; then
    bad 'frontend dependencies missing'
    info 'Run: bash setup.sh'
    exit 1
fi
ok "node $(node --version)"

for port in "$SERVICE_PORT" "$FRONTEND_PORT"; do
    if listening "$port"; then
        bad "port $port is already in use - is the chamber already running?"
        info "$(ss -ltnpH "sport = :$port" | head -1)"
        exit 1
    fi
done

SERVICE_ARGS=(chamber_service.py --port "$SERVICE_PORT")
NEED_SG=0

if [ "$SIM" -eq 1 ]; then
    SERVICE_ARGS+=(--sim)
    warn 'simulator - no instruments will be used'
else
    SERVICE_ARGS+=(--no-fallback --vna "$VNA" --pos "$POS")

    # -----------------------------------------------------------------------
    head 'VNA (S2VNA)'

    vna_port=5025
    if [[ "$VNA" =~ ::([0-9]+)::SOCKET$ ]]; then vna_port="${BASH_REMATCH[1]}"; fi

    if ! listening "$vna_port"; then
        if pgrep -f 'S2VNA\.exe' >/dev/null; then
            warn "S2VNA is running but nothing is listening on $vna_port"
            info 'Turn its socket server on: System -> Misc Setup -> Network Setup -> Socket Server -> On'
        else
            s2vna="${S2VNA:-}"
            if [ -z "$s2vna" ]; then
                for candidate in "$HOME"/src/CMT_S2VNA_*.appimage "$HOME"/Downloads/CMT_S2VNA_*.appimage \
                                 "$HOME"/CMT_S2VNA_*.appimage /opt/CMT_S2VNA_*.appimage; do
                    if [ -x "$candidate" ]; then s2vna="$candidate"; fi   # last match is the newest
                done
            fi
            if [ -z "$s2vna" ]; then
                bad 'S2VNA is not running and was not found'
                info 'Start it by hand, or point at it: S2VNA=/path/to/CMT_S2VNA_*.appimage ./start.sh'
                exit 1
            fi
            info "launching $s2vna"
            if ! ldconfig -p | grep -q 'libfuse\.so\.2'; then
                # Ubuntu 24.04 ships only FUSE 3; AppImages mount with FUSE 2.
                # Unpacking to /tmp each launch works without it, just slower.
                warn 'libfuse2 missing - unpacking the AppImage instead of mounting it'
                info 'For faster starts: sudo apt install libfuse2t64'
                export APPIMAGE_EXTRACT_AND_RUN=1
            fi
            # Detached, so it outlives this script and Ctrl+C.
            (setsid "$s2vna" >"$LOG_DIR/s2vna.log" 2>&1 </dev/null &)
        fi
        info "waiting for port $vna_port"
        waited=0
        until listening "$vna_port"; do
            sleep 1
            waited=$((waited + 1))
            if [ "$waited" -eq 45 ]; then
                warn "still nothing on $vna_port after 45 s"
                info 'If S2VNA is up, turn its socket server on: System -> Misc Setup -> Network Setup -> Socket Server -> On'
                info "S2VNA's output: $LOG_DIR/s2vna.log"
            fi
        done
    fi
    ok "listening on $vna_port"

    if [ "$vna_port" = 5025 ] && listening 5026; then
        warn 'port 5026 is also open - a second copy of S2VNA is running'
        info 'Close the extra copy so the service cannot talk to the wrong one.'
    fi

    # -----------------------------------------------------------------------
    head 'Positioner (EMCenter)'

    dev="${POS#ASRL}"; dev="${dev%::INSTR}"
    if [ ! -e "$dev" ]; then
        bad "$dev does not exist"
        if lsusb -d 0403:8570 >/dev/null 2>&1; then
            info 'The chassis is on USB but has no serial port: the udev rule is missing.'
            info "  sudo cp $PROJECT_ROOT/99-emcenter.rules /etc/udev/rules.d/"
            info '  sudo udevadm control --reload-rules    then replug the USB'
        else
            info 'The chassis is not on USB. Check it is powered and the cable is in.'
        fi
        exit 1
    fi

    if [ -r "$dev" ] && [ -w "$dev" ]; then
        ok "$dev ($(readlink -f "$dev"))"
    elif id -nG | tr ' ' '\n' | grep -qx dialout; then
        bad "$dev is not accessible even with dialout"
        info "$(ls -l "$(readlink -f "$dev")")"
        exit 1
    elif getent group dialout | cut -d: -f4 | tr ',' '\n' | grep -qx "$USER"; then
        # Added to dialout, but this login predates it. sg picks the group up
        # without a password, so there is no need to stop and reboot.
        NEED_SG=1
        ok "$dev (via sg dialout - this login predates the group; a reboot clears this)"
    else
        bad "no permission to open $dev"
        info "Run: sudo usermod -aG dialout $USER   then log out and back in"
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
head 'Service'

export PYTHONUNBUFFERED=1
if [ "$NEED_SG" -eq 1 ]; then
    cmd="$(printf '%q ' "$PY" "${SERVICE_ARGS[@]}")"
    setsid sg dialout -c "$cmd" > >(sed -u 's/^/  [service] /') 2>&1 &
else
    setsid "$PY" "${SERVICE_ARGS[@]}" > >(sed -u 's/^/  [service] /') 2>&1 &
fi
service_pid=$!
PIDS+=("$service_pid")

until listening "$SERVICE_PORT"; do
    if ! kill -0 "$service_pid" 2>/dev/null; then
        sleep 0.2   # let the last of its output through the prefixer
        bad 'the service exited during startup (reason above)'
        exit 1
    fi
    sleep 0.5
done
ok "ws://localhost:$SERVICE_PORT"

# ---------------------------------------------------------------------------
head 'Frontend'

# --strictPort: fail rather than drift to 5174 while the browser opens 5173.
setsid npm run dev --prefix frontend -- --strictPort > >(sed -u 's/^/  [frontend] /') 2>&1 &
frontend_pid=$!
PIDS+=("$frontend_pid")

until listening "$FRONTEND_PORT"; do
    if ! kill -0 "$frontend_pid" 2>/dev/null; then
        sleep 0.2
        bad 'the frontend exited during startup (reason above)'
        exit 1
    fi
    sleep 0.5
done
ok "http://localhost:$FRONTEND_PORT"

if [ "$BROWSER" -eq 1 ] && command -v xdg-open >/dev/null 2>&1; then
    xdg-open "http://localhost:$FRONTEND_PORT" >/dev/null 2>&1 || true
fi

head 'Running'
info 'Ctrl+C stops the service and frontend. S2VNA stays open.'

# Either one dying takes the other down, so a dead service never leaves a
# dashboard up that looks alive.
wait -n "${PIDS[@]}" || true
bad 'a process exited - shutting down'
exit 1
