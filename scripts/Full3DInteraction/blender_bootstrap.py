"""Load one task scene and start Blender's official MCP bridge."""

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
    configure_cycles_gpu,
    geometry_objects,
    scene_setup_summary,
    setup_scene,
)


def arguments() -> argparse.Namespace:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--addon-root", required=True)
    parser.add_argument("--glb", required=True)
    # A checkpoint from an interrupted run of this task.  When given, the scene
    # comes from this file and --glb is not imported: the reference was already
    # in there, together with everything the agent built on top of it.  Blender
    # is started with the .blend as its positional file argument, so it is
    # loaded before this script runs; all this flag does is skip the import and
    # the initial framing (the saved file carries its own viewport state).
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
            # Blender did not receive it as the file argument after all.
            bpy.ops.wm.open_mainfile(filepath=str(loaded))
        print(f"official Blender MCP: resumed from checkpoint {loaded}", flush=True)
        if bpy.context.scene.render.engine == "CYCLES":
            configure_cycles_gpu()
        setup_summary = {"preserved_checkpoint": True}
    else:
        # Import first, then install exactly the same extent-scaled scene rig
        # used by evaluation/core/render.py.
        clear_scene()
        bpy.ops.import_scene.gltf(filepath=str(Path(args.glb).resolve()))

        visible = geometry_objects()
        _cam, _center, _cam_r, _cam_h, extent = setup_scene(
            visible, frame_viewports=True,
            light_power=args.light_power,
            light_ref_extent=args.light_ref_extent)
        setup_summary = scene_setup_summary(
            extent, args.light_power, args.light_ref_extent)

    # These are the official add-on modules, imported straight from the pinned
    # official checkout.  Direct startup avoids installing anything in the
    # user's persistent Blender profile.
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
