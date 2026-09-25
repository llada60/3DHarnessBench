"""Bootstrap one Blender GUI process with a selected Blender MCP add-on."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import sys

import bpy


def parse_args() -> argparse.Namespace:
    try:
        separator = sys.argv.index("--")
    except ValueError as exc:
        raise RuntimeError("Missing '--' before bootstrap arguments") from exc

    parser = argparse.ArgumentParser()
    parser.add_argument("--addon", required=True, type=Path)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument(
        "--role", required=True, choices=("viewport_only", "full_access")
    )
    parser.add_argument("--glb", type=Path)
    return parser.parse_args(sys.argv[separator + 1 :])


def replace_scene_with_glb(glb_path: Path) -> None:
    glb_path = glb_path.expanduser().resolve(strict=True)
    if not glb_path.is_file() or glb_path.suffix.lower() != ".glb":
        raise ValueError(f"Expected an existing .glb file, got: {glb_path}")

    # Removing datablocks directly also handles hidden and unselectable objects.
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)

    bpy.ops.import_scene.gltf(filepath=str(glb_path))
    print(f"BlenderMCP launcher: imported GLB into viewport_only: {glb_path}")


def load_addon(addon_path: Path, role: str, port: int) -> None:
    addon_path = addon_path.expanduser().resolve(strict=True)
    if not addon_path.is_file():
        raise ValueError(f"Add-on path is not a file: {addon_path}")

    module_name = f"dual_blender_mcp_{role}_addon"
    spec = importlib.util.spec_from_file_location(module_name, addon_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load add-on module: {addon_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    module.DEFAULT_PORT = port
    module.register()

    # Pin the port for THIS process on every scene, and rebind the server if
    # register() auto-started it somewhere else.
    #
    # Why: the add-on's scene properties are saved inside a .blend, so a file
    # opened from an earlier run -- a resumed harness checkpoint -- still
    # carries that run's blendermcp_port, and register() starts the server on
    # whatever it reads there. The launcher then waits forever for a health
    # check on the port it actually asked for. (Those values do not show up in
    # scene.keys(): Blender stores properties an add-on defines on an ID
    # separately from user custom properties, so they cannot simply be deleted
    # before registering.) stop() + start() is exactly what the add-on's own
    # Start/Stop operators do.
    for scene in bpy.data.scenes:
        scene.blendermcp_port = port
    server = getattr(bpy.types, "blendermcp_server", None)
    if server is not None and getattr(server, "port", None) != port:
        print(f"BlenderMCP launcher: rebinding {role} from port "
              f"{server.port} to {port} (restored .blend)")
        server.stop()
        server.port = port
        server.start()
    if server is not None:
        # A stale True from the restored file would hide a server that never
        # came up from the check below.
        for scene in bpy.data.scenes:
            scene.blendermcp_server_running = bool(server.running)

    scene = bpy.context.scene
    if not getattr(scene, "blendermcp_server_running", False):
        raise RuntimeError(f"{role} Blender MCP failed to listen on port {port}")

    # Keep a strong reference for the lifetime of Blender and make diagnostics easy.
    bpy.app.driver_namespace[f"dual_blender_mcp_{role}_addon"] = module
    scene.name = f"BlenderMCP-{role}-{port}"
    print(f"BlenderMCP launcher: {role} ready on localhost:{port}")


def main() -> None:
    args = parse_args()
    if not 1024 <= args.port <= 65535:
        raise ValueError(f"Port must be between 1024 and 65535: {args.port}")
    if args.glb is not None and args.role != "viewport_only":
        raise ValueError("--glb is only valid for the viewport_only instance")

    if args.glb is not None:
        replace_scene_with_glb(args.glb)
    load_addon(args.addon, args.role, args.port)


if __name__ == "__main__":
    main()
