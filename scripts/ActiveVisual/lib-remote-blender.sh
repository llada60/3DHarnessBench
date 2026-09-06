#!/usr/bin/env bash
# lib-remote-blender.sh
#
# Shared configuration and helpers for the remote Blender MCP scripts:
#   start-remote-blender.sh, stop-remote-blender.sh
#
# This file is *sourced*, not executed. It relies on the programs found in the
# CURRENT environment (enter it first with `pixi shell`); it never calls
# `pixi run` and never starts a Pixi task.
#
# ActiveVisual owns its port defaults and uses the shared viewport bootstrap.

# ---------------------------------------------------------------------------
# User-tunable configuration (env overrides, exactly as documented in the spec)
# ---------------------------------------------------------------------------
: "${DISPLAY_NUMBER:=100}"
: "${SCREEN_GEOMETRY:=1920x1080x24}"
: "${RUNTIME_DIR:=$PWD/.remote-blender}"
# VirtualGL EGL device for GPU-accelerated Blender GL (vglrun is built into
# the default env by pixi install — see packages/virtualgl). "egl" = first
# working EGL device; "egl0"/"egl1" pin a specific GPU. Path forms like
# /dev/dri/card1 do NOT work here: the NVIDIA driver cannot report DRM device
# files without /dev/dri/renderD* access (group render), so VGL's path->device
# matching fails — the egl index form needs no DRM node access at all.
: "${VGL_DEVICE:=egl}"

# Ask the kernel for two free ports instead of using the launcher's fixed
# 9999/8888 (set by --random-ports / RANDOM_PORTS=1). Needed by harnesses that
# Start one stack per ActiveVisual task so consecutive or parallel
# runs never fight over a port, and so a run is unaffected by a stack someone
# left up on the defaults.
: "${RANDOM_PORTS:=0}"
# Optional GLB for the viewport_only instance. The bootstrap imports it, then
# scales the shared grading camera/light rig to the reference geometry.
: "${VIEWPORT_ONLY_GLB:=}"
# Benchmark pass multiplier: 1.0 for colour, 0.5 for grey-material renders.
: "${LIGHT_POWER:=1.0}"
: "${LIGHT_REF_EXTENT:=2.5}"
# Optional .blend checkpoints, one per instance. When set, Blender OPENS that
# file (it is passed as the positional file argument, so it loads before
# --python runs the bootstrap and the add-on registers into the restored scene).
# This is how a harness resumes an interrupted run: the whole file comes back --
# objects, materials, world, render settings and the saved UI/viewport state --
# not just the geometry an importer would carry. VIEWPORT_ONLY_BLEND replaces
# VIEWPORT_ONLY_GLB for that instance; setting both is an error.
: "${FULL_ACCESS_BLEND:=}"
: "${VIEWPORT_ONLY_BLEND:=}"
# Where to write the resolved ports/display/PIDs for downstream consumers.
# Always written under RUNTIME_DIR; PORTS_FILE adds a second copy anywhere.
: "${PORTS_FILE:=}"

: "${OFFICIAL_BLENDER_MCP_SOURCE:=}"
: "${VIEWPORT_ONLY_MCP_DIR:=$PWD/BlenderMCP/viewport_only}"
: "${BLENDERMCP_SCRIPTS_DIR:=$PWD/BlenderMCP/scripts}"

# Empty until port selection, so random and explicit ports remain exclusive.
: "${FULL_ACCESS_PORT:=}"
: "${VIEWPORT_ONLY_PORT:=}"

# Blender binary. The BlenderMCP launcher uses BLENDER_MCP_BLENDER_BIN; we honour
# that name plus BLENDER_BIN, then fall back to the copies shipped in this repo.
: "${STARTUP_TIMEOUT:=90}"

# ---------------------------------------------------------------------------
# Derived paths (fixed by the BlenderMCP layout, not guessed)
# ---------------------------------------------------------------------------
UPSTREAM_BOOTSTRAP="$BLENDERMCP_SCRIPTS_DIR/blender_mcp_bootstrap.py"
BOOTSTRAP="$SCRIPT_DIR/blender_bootstrap.py"
OFFICIAL_BOOTSTRAP="$SCRIPT_DIR/official_blender_bootstrap.py"
VIEWPORT_ONLY_ADDON="$VIEWPORT_ONLY_MCP_DIR/addon.py"

# Everything below hangs off RUNTIME_DIR, which the scripts may override from a
# command-line flag AFTER this file is sourced (ActiveVisual gives each task its
# own runtime dir), so keep it in one re-runnable function.
recompute_runtime_paths() {
LOG_DIR="$RUNTIME_DIR/logs"
FULL_ACCESS_RUN_DIR="$RUNTIME_DIR/full_access"
VIEWPORT_ONLY_RUN_DIR="$RUNTIME_DIR/viewport_only"

FULL_ACCESS_CONFIG_DIR="$FULL_ACCESS_RUN_DIR/config"
VIEWPORT_ONLY_CONFIG_DIR="$VIEWPORT_ONLY_RUN_DIR/config"

# PID files (spec-mandated set). No blendermcp-*.pid is created because the MCP
# server is embedded inside each Blender process (see notes at bottom).
PID_XVFB="$RUNTIME_DIR/xvfb.pid"
PID_OPENBOX="$RUNTIME_DIR/openbox.pid"
PID_XPRA="$RUNTIME_DIR/xpra.pid"
PID_BLENDER_FULL="$RUNTIME_DIR/blender-full-access.pid"
PID_BLENDER_VIEWPORT="$RUNTIME_DIR/blender-viewport-only.pid"
# Supporting session bus (not in the mandated set; tracked only so stop can
# clean it up instead of leaking it).
PID_DBUS="$RUNTIME_DIR/dbus.pid"

# Log files (spec-mandated set). The blendermcp-*.log files hold the embedded
# server's stdout/stderr, which is the same stream as the owning Blender.
LOG_XVFB="$LOG_DIR/xvfb.log"
LOG_OPENBOX="$LOG_DIR/openbox.log"
LOG_XPRA="$LOG_DIR/xpra.log"
LOG_BLENDER_FULL="$LOG_DIR/blender-full-access.log"
LOG_BLENDER_VIEWPORT="$LOG_DIR/blender-viewport-only.log"
LOG_MCP_FULL="$LOG_DIR/blendermcp-full-access.log"
LOG_MCP_VIEWPORT="$LOG_DIR/blendermcp-viewport-only.log"

# AF_UNIX paths cap at ~107 bytes, so a deep RUNTIME_DIR (a scratch dir, a CI
# workspace) makes dbus-daemon and xpra fail to bind. Sockets therefore live in
# RUNTIME_DIR only while it is short enough, otherwise in a short /tmp dir keyed
# to it; logs and PID files always stay where the caller asked for them. The
# longest socket subpath we create is "/xpra/blender-<display>".
SOCKET_DIR="$RUNTIME_DIR"
if (( ${#RUNTIME_DIR} > 70 )); then
    SOCKET_DIR="/tmp/remote-blender-$(printf '%s' "$RUNTIME_DIR" | md5sum | cut -c1-8)"
fi
}
recompute_runtime_paths

# The X display shared by BOTH Blender instances. DISPLAY_NUMBER=auto is
# resolved to a concrete free display by resolve_display_number().
DISPLAY_ID=":${DISPLAY_NUMBER}"

# The external Blender binary is not a conda package, so it has no RPATH to the
# X11 / GL libraries the Pixi env provides (libXfixes.so.3, etc.). `pixi shell`
# does NOT put the env's lib dir on LD_LIBRARY_PATH, so we must add it ourselves
# when launching Blender. Resolve it from the active env (CONDA_PREFIX) with a
# repo-relative fallback.
resolve_env_lib_dir() {
    local d
    for d in "${CONDA_PREFIX:-}/lib" "$PWD/.pixi/envs/default/lib"; do
        if [[ -n "$d" && -e "$d/libXfixes.so.3" ]]; then ENV_LIB_DIR="$d"; return 0; fi
    done
    ENV_LIB_DIR=""   # fall back to whatever is already on LD_LIBRARY_PATH
    return 0
}

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
log()  { printf '[remote-blender] %s\n' "$*"; }
warn() { printf '[remote-blender] warning: %s\n' "$*" >&2; }
die()  { printf '[remote-blender] error: %s\n' "$*" >&2; exit 1; }

require_cmd() {
    # $1 = command name, $2 = human hint for how to install it
    command -v "$1" >/dev/null 2>&1 || die "required program '$1' not found on PATH. $2"
}

# Two distinct free TCP ports from the ephemeral range, verified unused. Asking
# the kernel (bind port 0) is the only race-free way to find one; we re-check
# with a connect afterwards so a port some other process is listening on but did
# not bind through us is still rejected.
#
# Concurrency: several stacks may start at once (ActiveVisual running tasks in
# parallel, two people on one box). A lock around the pick alone would not be
# enough -- our ports stay unbound until Blender comes up ~30s later, so a
# concurrent picker would see them free and hand out the same numbers. So the
# pick takes an exclusive flock AND records a reservation; other pickers skip
# reserved ports until we release them (after the MCP health checks pass) or
# they go stale. Only the pick is serialised, never the slow startup.
: "${PORT_LOCK_FILE:=${TMPDIR:-/tmp}/remote-blender-ports.lock}"
: "${PORT_RESERVATION_FILE:=${TMPDIR:-/tmp}/remote-blender-ports.reserved}"
: "${PORT_RESERVATION_TTL:=600}"

pick_random_ports() {
    local pair
    pair="$(python - "$PORT_LOCK_FILE" "$PORT_RESERVATION_FILE" \
                     "$PORT_RESERVATION_TTL" "$$" <<'PY'
import errno, fcntl, os, socket, sys, time

lock_path, reservation_path, ttl, owner = sys.argv[1:5]
ttl, owner = int(ttl), int(owner)


def free_port():
    """A port the kernel currently considers free (bind 0, read it, release)."""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def unused(port):
    s = socket.socket()
    s.settimeout(0.3)
    try:
        return s.connect_ex(("127.0.0.1", port)) != 0
    finally:
        s.close()


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def live_reservations(handle):
    """Reservations still worth honouring: owner alive and not past the TTL."""
    handle.seek(0)
    now, kept = time.time(), []
    for line in handle.read().splitlines():
        fields = line.split()
        if len(fields) != 3:
            continue
        try:
            port, pid, stamp = int(fields[0]), int(fields[1]), float(fields[2])
        except ValueError:
            continue
        if now - stamp <= ttl and alive(pid):
            kept.append((port, pid, stamp))
    return kept


os.umask(0o000)
with open(lock_path, "a+") as lock:
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)          # released on close
    with open(reservation_path, "a+") as reservations:
        kept = live_reservations(reservations)
        taken = {port for port, _, _ in kept}
        chosen = []
        for _ in range(200):
            port = free_port()
            if port >= 1024 and port not in taken and unused(port):
                taken.add(port)
                chosen.append(port)
                if len(chosen) == 2:
                    break
        else:
            sys.exit(1)
        now = time.time()
        kept += [(port, owner, now) for port in chosen]
        reservations.seek(0)
        reservations.truncate()
        reservations.write("".join(f"{p} {pid} {stamp:.0f}\n"
                                   for p, pid, stamp in kept))
        reservations.flush()
        os.fsync(reservations.fileno())
    print(*chosen)
PY
)" || die "could not allocate two free ports"
    FULL_ACCESS_PORT="${pair%% *}"
    VIEWPORT_ONLY_PORT="${pair##* }"
    PORTS_RESERVED=1
    log "Allocated random ports (reserved under $(basename "$PORT_LOCK_FILE")): full_access=${FULL_ACCESS_PORT} viewport_only=${VIEWPORT_ONLY_PORT}"
}

# Drop this shell's reservations: once the MCP servers answer, the ports are
# held by real listeners and the bookkeeping is only in the way. Also called
# from the rollback path so a failed start does not park two ports for the TTL.
release_port_reservation() {
    [[ "${PORTS_RESERVED:-0}" == 1 ]] || return 0
    PORTS_RESERVED=0
    python - "$PORT_LOCK_FILE" "$PORT_RESERVATION_FILE" "$$" <<'PY' || true
import fcntl, sys

lock_path, reservation_path, owner = sys.argv[1], sys.argv[2], int(sys.argv[3])
with open(lock_path, "a+") as lock:
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    try:
        with open(reservation_path, "r+") as reservations:
            lines = [line for line in reservations.read().splitlines()
                     if len(line.split()) == 3 and int(line.split()[1]) != owner]
            reservations.seek(0)
            reservations.truncate()
            reservations.write("".join(line + "\n" for line in lines))
    except FileNotFoundError:
        pass
PY
}

# The lowest unused X display number >= DISPLAY_NUMBER (DISPLAY_NUMBER=auto
# starts the scan at 100). Lets a per-task harness run while another stack holds
# the default display.
resolve_display_number() {
    local start="$DISPLAY_NUMBER" n
    if [[ "$start" == auto ]]; then start=100; fi
    [[ "$start" =~ ^[0-9]+$ ]] || die "DISPLAY_NUMBER must be numeric or 'auto': $DISPLAY_NUMBER"
    if [[ "$DISPLAY_NUMBER" != auto ]]; then
        DISPLAY_ID=":${DISPLAY_NUMBER}"
        return 0
    fi
    for (( n = start; n < start + 100; n++ )); do
        if [[ ! -S "/tmp/.X11-unix/X${n}" && ! -e "/tmp/.X${n}-lock" ]]; then
            DISPLAY_NUMBER="$n"
            DISPLAY_ID=":${n}"
            log "Auto-selected free X display ${DISPLAY_ID}"
            return 0
        fi
    done
    die "no free X display found in range ${start}..$((start + 99))"
}

# Machine-readable handoff for whatever starts the stack. `PORTS ...` on stdout
# is the contract used by ActiveVisual; the file is for shells that
# would rather `source` it.
write_ports_file() {
    local target
    for target in "$RUNTIME_DIR/ports.env" ${PORTS_FILE:+"$PORTS_FILE"}; do
        mkdir -p "$(dirname "$target")"
        cat >"$target" <<EOF
FULL_ACCESS_PORT=$FULL_ACCESS_PORT
VIEWPORT_ONLY_PORT=$VIEWPORT_ONLY_PORT
DISPLAY_NUMBER=$DISPLAY_NUMBER
RUNTIME_DIR=$RUNTIME_DIR
SOCKET_DIR=$SOCKET_DIR
BLENDER_FULL_ACCESS_PID=$(_read_pid "$PID_BLENDER_FULL")
BLENDER_VIEWPORT_ONLY_PID=$(_read_pid "$PID_BLENDER_VIEWPORT")
EOF
    done
}

derive_ports() {
    if (( RANDOM_PORTS )); then
        if [[ -n "$FULL_ACCESS_PORT" || -n "$VIEWPORT_ONLY_PORT" ]]; then
            die "--random-ports conflicts with an explicit port (full_access='$FULL_ACCESS_PORT' viewport_only='$VIEWPORT_ONLY_PORT')"
        fi
        pick_random_ports
        return 0
    fi
    : "${FULL_ACCESS_PORT:=9999}"
    : "${VIEWPORT_ONLY_PORT:=8888}"
}

# ---------------------------------------------------------------------------
# Blender binary + version
# ---------------------------------------------------------------------------
resolve_blender_bin() {
    if [[ -n "${BLENDER_MCP_BLENDER_BIN:-}" ]]; then
        BLENDER_BIN="$BLENDER_MCP_BLENDER_BIN"
    elif [[ -z "${BLENDER_BIN:-}" ]]; then
        # Newest tools/blender-* first, so a repo with several installs picks the
        # highest version rather than whichever happens to be listed here.
        local cand
        for cand in $(printf '%s\n' "$PWD"/tools/blender-*-linux-x64/blender | sort -Vr) \
            "$PWD/infinigen/blender/blender"; do
            if [[ -x "$cand" ]]; then BLENDER_BIN="$cand"; break; fi
        done
    fi
    [[ -n "${BLENDER_BIN:-}" && -x "$BLENDER_BIN" ]] \
        || die "Blender binary not found/executable. Set BLENDER_BIN or BLENDER_MCP_BLENDER_BIN."
}

# major.minor, e.g. "5.0" — used to lay out the per-instance user dirs.
blender_version() {
    local v
    v="$("$BLENDER_BIN" --version 2>/dev/null | sed -nE 's/^Blender[[:space:]]+([0-9]+\.[0-9]+).*/\1/p' | head -n1)"
    if [[ -z "$v" ]]; then
        # Fall back to parsing the install directory name (blender-5.0.1-...).
        v="$(sed -nE 's/.*blender-([0-9]+\.[0-9]+).*/\1/p' <<<"$BLENDER_BIN" | head -n1)"
    fi
    printf '%s' "${v:-5.0}"
}

# ---------------------------------------------------------------------------
# Validation (the checks the spec requires before anything is launched)
# ---------------------------------------------------------------------------
validate_config() {
    test -d "$OFFICIAL_BLENDER_MCP_SOURCE/mcp" || die "official MCP source missing: $OFFICIAL_BLENDER_MCP_SOURCE"
    test -d "$OFFICIAL_BLENDER_MCP_SOURCE/addon" || die "official MCP add-on missing: $OFFICIAL_BLENDER_MCP_SOURCE/addon"
    test -d "$VIEWPORT_ONLY_MCP_DIR"  || die "viewport_only MCP dir missing: $VIEWPORT_ONLY_MCP_DIR"
    test -d "$BLENDERMCP_SCRIPTS_DIR" || die "BlenderMCP scripts dir missing: $BLENDERMCP_SCRIPTS_DIR"
    test -f "$BOOTSTRAP"              || die "graded bootstrap script missing: $BOOTSTRAP"
    test -f "$UPSTREAM_BOOTSTRAP"     || die "upstream bootstrap script missing: $UPSTREAM_BOOTSTRAP"
    test -f "$OFFICIAL_BOOTSTRAP"     || die "official bootstrap missing: $OFFICIAL_BOOTSTRAP"
    test -f "$VIEWPORT_ONLY_ADDON"    || die "viewport_only add-on missing: $VIEWPORT_ONLY_ADDON"

    [[ "$FULL_ACCESS_PORT"   =~ ^[0-9]+$ ]] || die "full_access port is not numeric: $FULL_ACCESS_PORT"
    [[ "$VIEWPORT_ONLY_PORT" =~ ^[0-9]+$ ]] || die "viewport_only port is not numeric: $VIEWPORT_ONLY_PORT"
    (( FULL_ACCESS_PORT   >= 1024 && FULL_ACCESS_PORT   <= 65535 )) || die "full_access port out of range: $FULL_ACCESS_PORT"
    (( VIEWPORT_ONLY_PORT >= 1024 && VIEWPORT_ONLY_PORT <= 65535 )) || die "viewport_only port out of range: $VIEWPORT_ONLY_PORT"
    test "$FULL_ACCESS_PORT" != "$VIEWPORT_ONLY_PORT" || die "full_access and viewport_only ports must differ ($FULL_ACCESS_PORT)"

    [[ "$DISPLAY_NUMBER" =~ ^[0-9]+$ ]] || die "DISPLAY_NUMBER must be numeric: $DISPLAY_NUMBER"

    if [[ -n "$VIEWPORT_ONLY_GLB" ]]; then
        test -f "$VIEWPORT_ONLY_GLB" || die "viewport_only GLB does not exist: $VIEWPORT_ONLY_GLB"
        [[ "${VIEWPORT_ONLY_GLB,,}" == *.glb ]] || die "viewport_only GLB must end in .glb: $VIEWPORT_ONLY_GLB"
        VIEWPORT_ONLY_GLB="$(realpath -- "$VIEWPORT_ONLY_GLB")"
    fi

    # A resumed instance opens a .blend instead of starting from the factory
    # scene. Both forms of the viewport_only scene at once is a contradiction.
    if [[ -n "$VIEWPORT_ONLY_GLB" && -n "$VIEWPORT_ONLY_BLEND" ]]; then
        die "--glb and --viewport-only-blend are mutually exclusive"
    fi
    if [[ -n "$FULL_ACCESS_BLEND" ]]; then
        test -f "$FULL_ACCESS_BLEND" || die "full_access .blend does not exist: $FULL_ACCESS_BLEND"
        [[ "${FULL_ACCESS_BLEND,,}" == *.blend ]] || die "full_access checkpoint must end in .blend: $FULL_ACCESS_BLEND"
        FULL_ACCESS_BLEND="$(realpath -- "$FULL_ACCESS_BLEND")"
    fi
    if [[ -n "$VIEWPORT_ONLY_BLEND" ]]; then
        test -f "$VIEWPORT_ONLY_BLEND" || die "viewport_only .blend does not exist: $VIEWPORT_ONLY_BLEND"
        [[ "${VIEWPORT_ONLY_BLEND,,}" == *.blend ]] || die "viewport_only checkpoint must end in .blend: $VIEWPORT_ONLY_BLEND"
        VIEWPORT_ONLY_BLEND="$(realpath -- "$VIEWPORT_ONLY_BLEND")"
    fi
}

# ---------------------------------------------------------------------------
# PID-file helpers — only ever touch a process whose PID we recorded AND whose
# /proc cmdline still matches what we launched. Never pkill/killall.
# ---------------------------------------------------------------------------
_read_pid() { [[ -f "$1" ]] && tr -dc '0-9' < "$1"; }

_pid_alive() { local p="$1"; [[ -n "$p" ]] && kill -0 "$p" 2>/dev/null; }

_cmdline_of() {
    local p="$1"
    [[ -r "/proc/$p/cmdline" ]] || return 1
    tr '\0' ' ' < "/proc/$p/cmdline"
}

# Is the process recorded in $1 alive AND does its cmdline contain $2 ?
pid_file_matches() {
    local pidfile="$1" needle="$2" p
    p="$(_read_pid "$pidfile")" || return 1
    _pid_alive "$p" || return 1
    _cmdline_of "$p" 2>/dev/null | grep -Fq -- "$needle"
}

# Graceful stop of the process in $1 iff its cmdline matches $2. TERM, wait,
# then KILL. Removes the PID file when the process is gone.
stop_pid_file() {
    local pidfile="$1" needle="$2" label="$3" p elapsed=0
    p="$(_read_pid "$pidfile")" || { log "$label: no PID file"; return 0; }
    if [[ -z "$p" ]]; then log "$label: empty PID file"; rm -f "$pidfile"; return 0; fi
    if ! _pid_alive "$p"; then
        log "$label: PID $p not running (stale PID file)"
        rm -f "$pidfile"; return 0
    fi
    if ! _cmdline_of "$p" 2>/dev/null | grep -Fq -- "$needle"; then
        warn "$label: PID $p cmdline does not match '$needle' — refusing to kill it"
        return 0
    fi
    log "$label: sending TERM to PID $p"
    kill -TERM "$p" 2>/dev/null || true
    while (( elapsed < 15 )); do
        _pid_alive "$p" || break
        sleep 1; elapsed=$((elapsed + 1))
    done
    if _pid_alive "$p"; then
        warn "$label: PID $p still alive after 15s — sending KILL"
        kill -KILL "$p" 2>/dev/null || true
        sleep 1
    fi
    if _pid_alive "$p"; then
        warn "$label: PID $p could not be stopped"
    else
        log "$label: stopped"
        rm -f "$pidfile"
    fi
}

# ---------------------------------------------------------------------------
# Port + MCP health checks (functional, not just "is the port open")
# ---------------------------------------------------------------------------
# TCP connect only.
port_open() {
    local port="$1"
    python - "$port" <<'PY' 2>/dev/null
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(2.0)
try:
    s.connect(("127.0.0.1", int(sys.argv[1])))
    sys.exit(0)
except Exception:
    sys.exit(1)
finally:
    s.close()
PY
}

# Real handshake with the embedded Blender MCP server. This exercises the
# bridge/add-on command loop inside Blender, so it proves MCP actually works —
# not merely that a socket is listening. The two add-ons expose different
# command surfaces, so the probe is role-specific:
#   full_access   -> official execute protocol (null-delimited, status "ok")
#   viewport_only -> legacy execute_code protocol (status "success")
mcp_health_check() {
    local port="$1" role="${2:-full_access}"
    python - "$port" "$role" <<'PY' 2>/dev/null
import socket, json, sys
port = int(sys.argv[1])
role = sys.argv[2] if len(sys.argv) > 2 else "full_access"
if role == "viewport_only":
    cmd = {"type": "execute_code", "params": {"code": "print('mcp-ok')"}}
    payload = json.dumps(cmd).encode("utf-8")
else:
    cmd = {"type": "execute", "code": "result={'mcp': 'ok'}", "strict_json": True}
    payload = json.dumps(cmd).encode("utf-8") + b"\0"
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(15.0)
try:
    s.connect(("127.0.0.1", port))
    s.sendall(payload)
    buf = b""
    while True:
        chunk = s.recv(8192)
        if not chunk:
            break
        buf += chunk
        try:
            resp = json.loads(buf.partition(b"\0")[0].decode("utf-8"))
        except json.JSONDecodeError:
            continue
        expected = "success" if role == "viewport_only" else "ok"
        sys.exit(0 if resp.get("status") == expected else 2)
    sys.exit(3)
except SystemExit:
    raise
except Exception:
    sys.exit(1)
finally:
    s.close()
PY
}

# ---------------------------------------------------------------------------
# Display probe
# ---------------------------------------------------------------------------
display_is_up() {
    if command -v xdpyinfo >/dev/null 2>&1; then
        xdpyinfo -display "$DISPLAY_ID" >/dev/null 2>&1
    else
        # Fallback when xdpyinfo is unavailable: the X socket must exist.
        [[ -S "/tmp/.X11-unix/X${DISPLAY_NUMBER}" ]]
    fi
}

# The exact command a local user runs to attach over SSH (SSH-only; no public
# unauthenticated TCP port). Confirm the precise syntax against `xpra --help`
# on the server, per the spec.
xpra_attach_hint() {
    printf 'xpra attach ssh://USER@SERVER/%s' "$DISPLAY_NUMBER"
}
