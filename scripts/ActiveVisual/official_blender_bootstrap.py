"""Start Blender's official MCP bridge for the reconstruction workspace."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import bpy

SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from blender_scene_setup import (  # noqa: E402
    clear_scene,
    scene_setup_summary,
    setup_scene,
)


def arguments() -> argparse.Namespace:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--addon-root", required=True)
    parser.add_argument("--blend")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--ready-file", required=True)
    parser.add_argument("--light-power", required=True, type=float)
    parser.add_argument("--light-ref-extent", type=float, default=2.5)
    return parser.parse_args(argv)


def main() -> None:
    args = arguments()
    addon_root = str(Path(args.addon_root).resolve())
    if addon_root not in sys.path:
        sys.path.insert(0, addon_root)

    if args.blend:
        loaded = Path(args.blend).resolve()
        if Path(bpy.data.filepath).resolve() != loaded:
            bpy.ops.wm.open_mainfile(filepath=str(loaded))
        print(f"official Blender MCP: resumed from checkpoint {loaded}", flush=True)
        setup_summary = {"preserved_checkpoint": True}
    else:
        # Fresh reconstruction workspaces start from the grading rig.  There is
        # no geometry yet, so extent=1 is used until the agent creates/imports it.
        clear_scene()
        _cam, _center, _cam_r, _cam_h, extent = setup_scene(
            [], frame_viewports=True,
            light_power=args.light_power,
            light_ref_extent=args.light_ref_extent)
        setup_summary = scene_setup_summary(
            extent, args.light_power, args.light_ref_extent)

    # Import the official add-on directly from the pinned checkout. This keeps
    # the task isolated from the user's persistent Blender profile.
    from blender_mcp_addon import execute_interactive, mcp_to_blender_server

    mcp_to_blender_server.use_log = True
    mcp_to_blender_server.start("127.0.0.1", args.port)
    bpy.app.timers.register(
        execute_interactive.run,
        first_interval=mcp_to_blender_server.TIMER_INTERVAL_ACTIVE,
        persistent=True,
    )
    Path(args.ready_file).write_text(json.dumps({
        "host": "127.0.0.1",
        "port": args.port,
        "objects": len(bpy.data.objects),
        "source": addon_root,
        "resumed_from": args.blend or "",
        "scene_setup": setup_summary,
    }, indent=2))
    print(f"official Blender MCP ready on 127.0.0.1:{args.port}", flush=True)


main()
