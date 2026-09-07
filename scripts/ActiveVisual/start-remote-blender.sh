#!/usr/bin/env bash
#
# start-remote-blender.sh
#
# Bring up the full remote Blender MCP stack on a single shared X display:
#
#   Xvfb -> D-Bus -> Openbox -> Xpra -> Blender full_access
#                                    -> Blender viewport_only -> MCP health checks
#
# Two Blender instances share ONE X display but use independent runtime dirs,
# user config dirs, ports and add-ons:
#
#   full_access   -> official Blender MCP    -> FULL_ACCESS_PORT
#   viewport_only -> BlenderMCP/viewport_only -> VIEWPORT_ONLY_PORT
#
# Enter the Pixi default environment first, then run this script:
#
#   pixi shell
#   bash scripts/start-remote-blender.sh
#
# Ports / add-on paths / launch command are derived from BlenderMCP/scripts;
# nothing is guessed. This script uses the programs in the current environment
# directly and never calls `pixi run`.

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-remote-blender.sh
source "$SCRIPT_DIR/lib-remote-blender.sh"

# Remote viewing is opt-in. The Blender MCP servers only need Xvfb.
XPRA_ENABLED=0

# ---------------------------------------------------------------------------
# Options. Every one of these also has an env-var form (see the library), which
# is what the older env-only invocations used; the flags exist because
# ActiveVisual starts one stack per task and needs random ports, a GLB
# for the viewport_only scene, and the resolved ports back.
# ---------------------------------------------------------------------------
usage() {
    cat <<EOF
Usage: bash scripts/start-remote-blender.sh [options]

  --random-ports              allocate two free ports instead of 9999/8888
  --full-access-port PORT     explicit full_access MCP port
  --viewport-only-port PORT   explicit viewport_only MCP port
  --official-source PATH      pinned official blender_mcp checkout
  --glb PATH                  clear the viewport_only scene and import this .glb
                              (full_access keeps an empty grading-rig scene)
  --light-power VALUE         benchmark pass multiplier: color=1.0, grey=0.5
  --full-access-blend PATH    open this .blend in the full_access instance
                              instead of the fresh grading-rig scene (resume)
  --viewport-only-blend PATH  open this .blend in the viewport_only instance
                              instead of importing --glb (resume)
  --display N|auto            X display number (auto = first free from :100)
  --xpra                      start an xpra shadow server for remote viewing
                              (disabled by default)
  --runtime-dir DIR           state/log dir (default \$PWD/.remote-blender)
  --ports-file PATH           extra copy of the resolved ports (always written
                              to <runtime-dir>/ports.env as well)
  -h, --help                  this message

On success the resolved ports are printed as a stable one-liner:
  [remote-blender] PORTS full_access=<port> viewport_only=<port> display=<n>
EOF
}

while (( $# )); do
    case "$1" in
        --random-ports)        RANDOM_PORTS=1 ;;
        --full-access-port)    FULL_ACCESS_PORT="${2:?--full-access-port needs a value}"; shift ;;
        --viewport-only-port)  VIEWPORT_ONLY_PORT="${2:?--viewport-only-port needs a value}"; shift ;;
        --official-source)     OFFICIAL_BLENDER_MCP_SOURCE="${2:?--official-source needs a value}"; shift ;;
        --glb)                 VIEWPORT_ONLY_GLB="${2:?--glb needs a value}"; shift ;;
        --light-power)         LIGHT_POWER="${2:?--light-power needs a value}"; shift ;;
        --full-access-blend)   FULL_ACCESS_BLEND="${2:?--full-access-blend needs a value}"; shift ;;
        --viewport-only-blend) VIEWPORT_ONLY_BLEND="${2:?--viewport-only-blend needs a value}"; shift ;;
        --display)             DISPLAY_NUMBER="${2:?--display needs a value}"; shift ;;
        --xpra)                XPRA_ENABLED=1 ;;
        --runtime-dir)         RUNTIME_DIR="${2:?--runtime-dir needs a value}"; shift ;;
        --ports-file)          PORTS_FILE="${2:?--ports-file needs a value}"; shift ;;
        -h|--help)             usage; exit 0 ;;
        *)                     usage >&2; die "unknown option: $1" ;;
    esac
    shift
done

# RUNTIME_DIR may have just changed, so re-derive everything hanging off it.
recompute_runtime_paths

# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------
resolve_display_number
derive_ports
resolve_blender_bin
resolve_env_lib_dir
validate_config

# Required: without these the two Blender + MCP instances cannot run.
require_cmd Xvfb        "Ensure the system Xvfb is on PATH (it ships at /usr/bin/Xvfb)."
require_cmd dbus-daemon "Provided by the conda 'dbus' package: pixi add dbus"
require_cmd python      "Python from the Pixi env."

# Optional layers. Openbox is only a window manager and Xpra is only for remote
# SSH viewing; neither is needed to run the two Blenders with their two MCP
# servers on two ports. They are used when present and skipped (with a warning)
# when absent. openbox is not on conda-forge at all; xpra lives in the default
# env, built from upstream master via the pypi git dep in pyproject.toml.
HAVE_OPENBOX=0; command -v openbox >/dev/null 2>&1 && HAVE_OPENBOX=1
HAVE_XPRA=0
if (( XPRA_ENABLED )); then
    command -v xpra >/dev/null 2>&1 && HAVE_XPRA=1
fi
HAVE_VGLRUN=0;  command -v vglrun  >/dev/null 2>&1 && HAVE_VGLRUN=1
(( HAVE_OPENBOX )) || warn "openbox not found — skipping the window manager (Blender still runs headless on Xvfb)"
if (( XPRA_ENABLED && ! HAVE_XPRA )); then
    warn "xpra requested but not found — skipping remote SSH viewing (MCP over TCP is unaffected)"
fi
(( HAVE_VGLRUN ))  || warn "vglrun not found — Blender GL falls back to llvmpipe (software rendering)"

# When VirtualGL is present, each Blender is launched under vglrun so its
# viewport/Eevee GL renders on the real GPU (EGL back end, no root needed)
# instead of Xvfb's llvmpipe. Cycles/CUDA is unaffected either way.
VGL_WRAP=()
if (( HAVE_VGLRUN )); then
    VGL_WRAP=(vglrun -d "$VGL_DEVICE")
    log "VirtualGL active: Blender GL on device '$VGL_DEVICE'"
fi

BLENDER_VER="$(blender_version)"

mkdir -p "$RUNTIME_DIR" "$LOG_DIR" \
         "$FULL_ACCESS_RUN_DIR" "$VIEWPORT_ONLY_RUN_DIR" \
         "$SOCKET_DIR" "$SOCKET_DIR/xpra"
chmod 700 "$RUNTIME_DIR"

# A valid, private XDG_RUNTIME_DIR keeps Blender's GHOST/Wayland probe from
# dumping a (harmless) backtrace before it falls back to X11, and gives Xpra a
# sane socket home.
# SOCKET_DIR == RUNTIME_DIR unless that path is too long for AF_UNIX (see lib).
export XDG_RUNTIME_DIR="$SOCKET_DIR"
[[ "$SOCKET_DIR" != "$RUNTIME_DIR" ]] && log "sockets in $SOCKET_DIR (runtime dir too long for AF_UNIX)"

# Tile the two Blender windows so BOTH are visible at once in the shared-screen
# xpra shadow. No window manager runs (openbox isn't available), so without an
# explicit geometry each Blender opens maximized at the same spot and they stack
# on top of each other — you only see one. Split the shared screen in half.
# Override per instance with FULL_ACCESS_WINDOW_GEOMETRY / VIEWPORT_ONLY_WINDOW_
# GEOMETRY ("X Y W H"); set WINDOW_LAYOUT=tb for a top/bottom split (default lr).
_scr_w="${SCREEN_GEOMETRY%%x*}"; _scr_rest="${SCREEN_GEOMETRY#*x}"; _scr_h="${_scr_rest%%x*}"
: "${WINDOW_LAYOUT:=lr}"
if [[ "$WINDOW_LAYOUT" == tb ]]; then
    _half_h=$(( _scr_h / 2 ))
    : "${FULL_ACCESS_WINDOW_GEOMETRY:=0 0 ${_scr_w} ${_half_h}}"
    : "${VIEWPORT_ONLY_WINDOW_GEOMETRY:=0 ${_half_h} ${_scr_w} ${_half_h}}"
else
    _half_w=$(( _scr_w / 2 ))
    : "${FULL_ACCESS_WINDOW_GEOMETRY:=0 0 ${_half_w} ${_scr_h}}"
    : "${VIEWPORT_ONLY_WINDOW_GEOMETRY:=${_half_w} 0 ${_half_w} ${_scr_h}}"
fi
log "Window layout ${WINDOW_LAYOUT}: full_access=[${FULL_ACCESS_WINDOW_GEOMETRY}] viewport_only=[${VIEWPORT_ONLY_WINDOW_GEOMETRY}]"

# Refuse to trample a stack that is already up.
if pid_file_matches "$PID_XVFB" "Xvfb"; then
    die "Xvfb already running (PID $(_read_pid "$PID_XVFB")). Run stop-remote-blender.sh first."
fi
if port_open "$FULL_ACCESS_PORT" || port_open "$VIEWPORT_ONLY_PORT"; then
    die "port $FULL_ACCESS_PORT or $VIEWPORT_ONLY_PORT already in use. Run stop-remote-blender.sh first."
fi

# Roll back everything we started if any step fails.
STARTED=()
cleanup_on_error() {
    warn "startup failed — rolling back"
    release_port_reservation
    local item
    # Reverse order.
    for (( idx=${#STARTED[@]}-1 ; idx>=0 ; idx-- )); do
        item="${STARTED[idx]}"
        case "$item" in
            blender_viewport) stop_pid_file "$PID_BLENDER_VIEWPORT" "viewport_only/addon.py" "Blender viewport_only" ;;
            blender_full)     stop_pid_file "$PID_BLENDER_FULL" "official_blender_bootstrap.py" "Blender official workspace" ;;
            xpra)             stop_pid_file "$PID_XPRA"     "xpra"    "Xpra" ;;
            openbox)          stop_pid_file "$PID_OPENBOX"  "openbox" "Openbox" ;;
            dbus)             stop_pid_file "$PID_DBUS"     "dbus-daemon" "D-Bus" ;;
            xvfb)             stop_pid_file "$PID_XVFB"     "Xvfb"    "Xvfb" ;;
        esac
    done
}
trap cleanup_on_error ERR

# ---------------------------------------------------------------------------
# 1. Xvfb — the shared virtual X display
# ---------------------------------------------------------------------------
start_xvfb() {
    log "Starting Xvfb on ${DISPLAY_ID} (${SCREEN_GEOMETRY})"
    Xvfb "$DISPLAY_ID" -screen 0 "$SCREEN_GEOMETRY" -nolisten tcp \
        >"$LOG_XVFB" 2>&1 &
    echo $! > "$PID_XVFB"
    STARTED+=(xvfb)

    local elapsed=0
    while (( elapsed < 15 )); do
        display_is_up && { log "Xvfb ready on ${DISPLAY_ID}"; return 0; }
        _pid_alive "$(_read_pid "$PID_XVFB")" || die "Xvfb exited early. See $LOG_XVFB"
        sleep 1; elapsed=$((elapsed + 1))
    done
    die "Xvfb did not come up on ${DISPLAY_ID} within 15s. See $LOG_XVFB"
}

# ---------------------------------------------------------------------------
# 2. D-Bus — session bus for the GUI apps on this display
# ---------------------------------------------------------------------------
start_dbus() {
    log "Starting D-Bus session bus"
    local addr_file="$RUNTIME_DIR/dbus-address"
    # No --fork/--print-pid: the dbus-daemon on this box rejects --print-pid in
    # both = and space forms ("Invalid file descriptor"), and a plain background
    # job hands us the PID portably. --print-address still writes the address
    # once the bus is listening; dbus does not unlink a stale socket itself.
    rm -f "$SOCKET_DIR/dbus.socket"
    : >"$addr_file"
    dbus-daemon --session \
        --address="unix:path=$SOCKET_DIR/dbus.socket" \
        --print-address >"$addr_file" 2>>"$LOG_XVFB" &
    echo $! > "$PID_DBUS"
    local elapsed=0
    while [[ ! -s "$addr_file" ]] && (( elapsed < 10 )); do
        _pid_alive "$(_read_pid "$PID_DBUS")" || die "dbus-daemon exited early. See $LOG_XVFB"
        sleep 1; elapsed=$((elapsed + 1))
    done
    [[ -s "$addr_file" ]] || die "dbus-daemon did not print an address within 10s. See $LOG_XVFB"
    export DBUS_SESSION_BUS_ADDRESS
    DBUS_SESSION_BUS_ADDRESS="$(cat "$addr_file")"
    STARTED+=(dbus)
    log "D-Bus session bus at $DBUS_SESSION_BUS_ADDRESS (PID $(_read_pid "$PID_DBUS"))"
}

# ---------------------------------------------------------------------------
# 3. Openbox — a minimal window manager on the shared display
# ---------------------------------------------------------------------------
start_openbox() {
    if (( ! HAVE_OPENBOX )); then
        log "Openbox: skipped (not installed)"
        return 0
    fi
    log "Starting Openbox on ${DISPLAY_ID}"
    DISPLAY="$DISPLAY_ID" openbox >"$LOG_OPENBOX" 2>&1 &
    echo $! > "$PID_OPENBOX"
    STARTED+=(openbox)
    sleep 1
    _pid_alive "$(_read_pid "$PID_OPENBOX")" || die "Openbox exited early. See $LOG_OPENBOX"
    log "Openbox running (PID $(_read_pid "$PID_OPENBOX"))"
}

# ---------------------------------------------------------------------------
# 4. Xpra — shadow the existing display; SSH-only, no public TCP bind
# ---------------------------------------------------------------------------
start_xpra() {
    if (( ! XPRA_ENABLED )); then
        log "Xpra: skipped (disabled; pass --xpra to enable remote viewing)"
        return 0
    fi
    if (( ! HAVE_XPRA )); then
        log "Xpra: skipped (not installed) — remote SSH viewing unavailable this run"
        return 0
    fi
    log "Starting Xpra (shadow ${DISPLAY_ID}, SSH-only)"
    # Verify the flags we intend to use are supported by the installed xpra,
    # per the spec's instruction to confirm syntax against `xpra --help`.
    local help; help="$(xpra --help 2>&1 || true)"
    local flag
    for flag in shadow --socket-dir --daemon --bind; do
        case "$flag" in
            shadow) grep -Eq '(^|[^a-z])shadow' <<<"$help" || warn "xpra help does not mention 'shadow'; check syntax" ;;
            *)      grep -Fq -- "$flag" <<<"$help" || warn "xpra help does not mention '$flag'; check syntax" ;;
        esac
    done

    # No --bind-tcp / no --bind-ws => only the local unix socket exists, which
    # remote clients reach exclusively through SSH:  xpra attach ssh://USER@SERVER/<n>
    xpra shadow "$DISPLAY_ID" \
        --daemon=no \
        --dbus=no \
        --socket-dir="$SOCKET_DIR/xpra" \
        --bind="$SOCKET_DIR/xpra/blender-${DISPLAY_NUMBER}" \
        --mdns=no \
        --notifications=no \
        --pulseaudio=no \
        --webcam=no \
        --html=no \
        >"$LOG_XPRA" 2>&1 &
    echo $! > "$PID_XPRA"
    STARTED+=(xpra)

    local elapsed=0
    while (( elapsed < 15 )); do
        _pid_alive "$(_read_pid "$PID_XPRA")" || die "Xpra exited early. See $LOG_XPRA"
        [[ -S "$SOCKET_DIR/xpra/blender-${DISPLAY_NUMBER}" ]] && break
        sleep 1; elapsed=$((elapsed + 1))
    done
    log "Xpra running (PID $(_read_pid "$PID_XPRA")); attach with: $(xpra_attach_hint)"
}

# ---------------------------------------------------------------------------
# Blender launch — TWO SEPARATE functions (no shared loop, different args)
# ---------------------------------------------------------------------------
# The commands below launch the official workspace and restricted reference view.
# Each Blender attaches to the display managed by this script:
#
#   BLENDER --factory-startup --python blender_mcp_bootstrap.py -- \
#       --addon <addon> --role <role> --port <port>
#
# Each instance gets its own BLENDER_USER_* dirs so the two never share config.
_blender_user_env() {
    # $1 = config root for this instance
    local root="$1/$BLENDER_VER"
    mkdir -p "$root/config" "$root/scripts" "$root/datafiles"
    export BLENDER_USER_CONFIG="$root/config"
    export BLENDER_USER_SCRIPTS="$root/scripts"
    export BLENDER_USER_DATAFILES="$root/datafiles"
}

start_full_access_blender() {
    log "Starting official Blender MCP workspace (port ${FULL_ACCESS_PORT})"
    # A checkpoint .blend is Blender's positional file argument, so it is loaded
    # before --python runs the bootstrap: the add-on then registers into the
    # restored scene exactly as it would into the factory one.
    local blend_args=()
    local resume_args=()
    local window_geometry=()
    read -r -a window_geometry <<<"$FULL_ACCESS_WINDOW_GEOMETRY"
    if [[ -n "$FULL_ACCESS_BLEND" ]]; then
        blend_args=("$FULL_ACCESS_BLEND")
        resume_args=(--blend "$FULL_ACCESS_BLEND")
        log "full_access will open the checkpoint: $FULL_ACCESS_BLEND"
    fi
    local ready_file="$FULL_ACCESS_RUN_DIR/official.ready.json"
    rm -f "$ready_file"
    # DISPLAY and LD_LIBRARY_PATH are intentionally scoped to this Blender
    # child; the parent shell must remain unchanged.
    # shellcheck disable=SC2030
    (
        export DISPLAY="$DISPLAY_ID"
        [[ -n "$ENV_LIB_DIR" ]] && export LD_LIBRARY_PATH="${ENV_LIB_DIR}:${LD_LIBRARY_PATH:-}"
        _blender_user_env "$FULL_ACCESS_CONFIG_DIR"
        exec "${VGL_WRAP[@]}" "$BLENDER_BIN" \
            --factory-startup \
            --window-geometry "${window_geometry[@]}" \
            "${blend_args[@]}" \
            --python "$OFFICIAL_BOOTSTRAP" \
            -- \
            --addon-root "$OFFICIAL_BLENDER_MCP_SOURCE/addon" \
            --port "$FULL_ACCESS_PORT" \
            --ready-file "$ready_file" \
            --light-power "$LIGHT_POWER" \
            --light-ref-extent "$LIGHT_REF_EXTENT" \
            "${resume_args[@]}" \
            >"$LOG_BLENDER_FULL" 2>&1
    ) &
    echo $! > "$PID_BLENDER_FULL"
    STARTED+=(blender_full)
    # The embedded MCP shares Blender's stdout/stderr; expose it under the
    # mandated blendermcp log name too.
    ln -sf "$(basename "$LOG_BLENDER_FULL")" "$LOG_MCP_FULL"
    log "Official workspace Blender PID $(_read_pid "$PID_BLENDER_FULL"); bridge is embedded"
}

start_viewport_only_blender() {
    log "Starting Blender viewport_only (port ${VIEWPORT_ONLY_PORT})"
    # --glb makes the bootstrap wipe every object in THIS scene and import the
    # file before the add-on registers, which is exactly the "clear the
    # viewport_only scene, then import ref.glb" step the harness needs. Without
    # it the instance keeps the factory-startup scene.
    local glb_args=()
    local window_geometry=()
    read -r -a window_geometry <<<"$VIEWPORT_ONLY_WINDOW_GEOMETRY"
    if [[ -n "$VIEWPORT_ONLY_GLB" ]]; then
        glb_args=(--glb "$VIEWPORT_ONLY_GLB")
        log "viewport_only scene will be replaced by: $VIEWPORT_ONLY_GLB"
    fi
    # ...or, on resume, the reference scene is restored wholesale from a
    # checkpoint .blend (camera position included), so nothing is imported.
    local blend_args=()
    if [[ -n "$VIEWPORT_ONLY_BLEND" ]]; then
        blend_args=("$VIEWPORT_ONLY_BLEND")
        log "viewport_only will open the checkpoint: $VIEWPORT_ONLY_BLEND"
    fi
    # This second Blender child intentionally receives the same independent
    # display/library exports as the first one.
    # shellcheck disable=SC2031
    (
        export DISPLAY="$DISPLAY_ID"
        [[ -n "$ENV_LIB_DIR" ]] && export LD_LIBRARY_PATH="${ENV_LIB_DIR}:${LD_LIBRARY_PATH:-}"
        _blender_user_env "$VIEWPORT_ONLY_CONFIG_DIR"
        exec "${VGL_WRAP[@]}" "$BLENDER_BIN" \
            --factory-startup \
            --window-geometry "${window_geometry[@]}" \
            "${blend_args[@]}" \
            --python "$BOOTSTRAP" \
            -- \
            --upstream-bootstrap "$UPSTREAM_BOOTSTRAP" \
            --addon "$VIEWPORT_ONLY_ADDON" \
            --role viewport_only \
            --port "$VIEWPORT_ONLY_PORT" \
            --light-power "$LIGHT_POWER" \
            --light-ref-extent "$LIGHT_REF_EXTENT" \
            "${glb_args[@]}" \
            >"$LOG_BLENDER_VIEWPORT" 2>&1
    ) &
    echo $! > "$PID_BLENDER_VIEWPORT"
    STARTED+=(blender_viewport)
    ln -sf "$(basename "$LOG_BLENDER_VIEWPORT")" "$LOG_MCP_VIEWPORT"
    log "Blender viewport_only PID $(_read_pid "$PID_BLENDER_VIEWPORT"); MCP is embedded (no separate PID)"
}

# ---------------------------------------------------------------------------
# MCP health checks
# ---------------------------------------------------------------------------
wait_for_mcp() {
    local label="$1" port="$2" pidfile="$3" log_file="$4" elapsed=0
    log "Waiting for ${label} MCP on 127.0.0.1:${port}"
    while (( elapsed < STARTUP_TIMEOUT )); do
        _pid_alive "$(_read_pid "$pidfile")" || die "${label} Blender exited before its MCP came up. See $log_file"
        if mcp_health_check "$port" "$label"; then
            log "${label} MCP healthy (functional command -> success) on port ${port}"
            return 0
        fi
        sleep 1; elapsed=$((elapsed + 1))
    done
    die "${label} MCP did not become healthy on port ${port} within ${STARTUP_TIMEOUT}s. See $log_file"
}

# ---------------------------------------------------------------------------
# Orchestration — strict order per the spec
# ---------------------------------------------------------------------------
start_xvfb
start_dbus
start_openbox
start_xpra
start_full_access_blender
start_viewport_only_blender
wait_for_mcp "full_access"   "$FULL_ACCESS_PORT"   "$PID_BLENDER_FULL"     "$LOG_BLENDER_FULL"
wait_for_mcp "viewport_only" "$VIEWPORT_ONLY_PORT" "$PID_BLENDER_VIEWPORT" "$LOG_BLENDER_VIEWPORT"

trap - ERR

# Both servers answered, so the ports are now held by real listeners — the
# reservation that kept concurrent starts off them has done its job.
release_port_reservation

write_ports_file

log "----------------------------------------------------------------"
log "Remote Blender MCP stack is up."
# Stable, greppable handoff line — the contract the ActiveVisual runner parses to
# learn which ports this run got (see also ${RUNTIME_DIR}/ports.env).
log "PORTS full_access=${FULL_ACCESS_PORT} viewport_only=${VIEWPORT_ONLY_PORT} display=${DISPLAY_NUMBER} socket_dir=${SOCKET_DIR}"
log "  display          : ${DISPLAY_ID} (${SCREEN_GEOMETRY})"
log "  official MCP workspace: 127.0.0.1:${FULL_ACCESS_PORT} (Blender PID $(_read_pid "$PID_BLENDER_FULL"))"
log "  viewport_only MCP: 127.0.0.1:${VIEWPORT_ONLY_PORT} (Blender PID $(_read_pid "$PID_BLENDER_VIEWPORT"))"
log "  logs             : ${LOG_DIR}"
if (( HAVE_XPRA )); then
    log "  xpra directory   : ${SOCKET_DIR}/xpra"
    log "  attach locally   : $(xpra_attach_hint)"
elif (( ! XPRA_ENABLED )); then
    log "  attach locally   : (xpra disabled; pass --xpra to enable)"
else
    log "  attach locally   : (xpra not installed — remote viewing unavailable)"
fi
log "Stop with: bash scripts/ActiveVisual/stop-remote-blender.sh --runtime-dir $RUNTIME_DIR"
