#!/usr/bin/env bash
#
# stop-remote-blender.sh
#
# Tear the stack down in the reverse of the startup order:
#
#   1. viewport_only MCP server  ┐ both are embedded in their Blender process,
#   2. full_access   MCP server  ┘ so stopping the Blender stops its MCP
#   3. viewport_only Blender
#   4. full_access   Blender
#   5. Xpra
#   6. Openbox
#   7. Xvfb
#   (+ the supporting D-Bus session bus, last)
#
# Each process is stopped with TERM, then KILL on timeout, and ONLY if its
# recorded PID is still alive and its /proc cmdline matches what we launched.
# pkill / killall are never used.
#
#   pixi shell
#   bash scripts/stop-remote-blender.sh

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-remote-blender.sh
source "$SCRIPT_DIR/lib-remote-blender.sh"

log "Stopping remote Blender MCP stack (reverse order)"

# 1 & 3: viewport_only MCP is embedded in the viewport_only Blender, so
#        stopping that Blender stops its MCP.
log "viewport_only MCP is embedded in its Blender; stopping the Blender stops the MCP"
stop_pid_file "$PID_BLENDER_VIEWPORT" "viewport_only/addon.py" "Blender viewport_only (+ MCP)"

# 2 & 4: official bridge is embedded in the workspace Blender.
log "official MCP bridge is embedded in its Blender; stopping Blender stops it"
stop_pid_file "$PID_BLENDER_FULL" "official_blender_bootstrap.py" "Blender official workspace (+ MCP)"

# 5: Xpra
stop_pid_file "$PID_XPRA" "xpra" "Xpra"

# 6: Openbox
stop_pid_file "$PID_OPENBOX" "openbox" "Openbox"

# 7: Xvfb
stop_pid_file "$PID_XVFB" "Xvfb" "Xvfb"

# Supporting session bus (started after Xvfb; torn down last).
stop_pid_file "$PID_DBUS" "dbus-daemon" "D-Bus"

# Clean up the embedded-MCP log symlinks and transient sockets we own.
rm -f "$LOG_MCP_FULL" "$LOG_MCP_VIEWPORT" \
      "$RUNTIME_DIR/dbus.socket" "$RUNTIME_DIR/dbus-address" 2>/dev/null || true

log "Stop complete."
