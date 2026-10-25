"""Start viewport-only Blender with the same scene rig as grading."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import bpy

from core.blender_mcp.scripts.blender_mcp_bootstrap import load_addon, replace_scene_with_glb
from core.blender_scene_setup import (
    clear_scene,
    geometry_objects,
    scene_setup_summary,
    setup_scene,
)


def arguments() -> argparse.Namespace:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--addon-module", default="core.blender_mcp.viewport_only.addon",
                        help="importable Blender add-on module")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument(
        "--role", required=True, choices=("viewport_only", "full_access")
    )
    parser.add_argument("--glb", type=Path)
    parser.add_argument("--light-power", required=True, type=float)
    parser.add_argument("--light-ref-extent", type=float, default=2.5)
    return parser.parse_args(argv)


def main() -> None:
    args = arguments()
    if not 1024 <= args.port <= 65535:
        raise ValueError(f"Port must be between 1024 and 65535: {args.port}")
    if args.glb is not None and args.role != "viewport_only":
        raise ValueError("--glb is only valid for the viewport_only instance")

    if args.glb is not None:
        # The upstream import clears the scene. Install the grading scene only
        # after import, using the GLB geometry's actual extent.
        replace_scene_with_glb(args.glb)
        _cam, _center, _cam_r, _cam_h, extent = setup_scene(
            geometry_objects(), frame_viewports=True,
            light_power=args.light_power,
            light_ref_extent=args.light_ref_extent)
        print(
            "BlenderMCP launcher: grading scene rig "
            f"{scene_setup_summary(extent, args.light_power, args.light_ref_extent)}",
            flush=True,
        )
    elif not bpy.data.filepath:
        # A fresh no-reference workspace gets the same extent=1 baseline.  A
        # positional .blend means resume, whose complete scene must be retained.
        clear_scene()
        _cam, _center, _cam_r, _cam_h, extent = setup_scene(
            [], frame_viewports=True,
            light_power=args.light_power,
            light_ref_extent=args.light_ref_extent)
        print(
            "BlenderMCP launcher: grading scene rig "
            f"{scene_setup_summary(extent, args.light_power, args.light_ref_extent)}",
            flush=True,
        )
    else:
        print(
            f"BlenderMCP launcher: preserving checkpoint {bpy.data.filepath}",
            flush=True,
        )

    load_addon(args.addon_module, args.role, args.port)


if __name__ == "__main__":
    main()
