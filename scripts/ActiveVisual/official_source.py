"""Resolve the pinned official Blender MCP source used by ActiveVisual runs."""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
from pathlib import Path


SOURCE_LOCK = Path(os.getenv("TMPDIR", "/tmp")) / "official-blender-mcp-source.lock"
HEADLESS_SCREENSHOT_MARKER = "# ActiveVisual Xvfb compatibility: GPUOffScreen"
HEADLESS_SCREENSHOT_STOCK = '''        with context.temp_override(window=window, area=area):
            try:
                bpy.ops.screen.screenshot_area(filepath=filepath_screenshot)
            except RuntimeError as ex:
                return Result(status="error", message=str(ex))
'''
HEADLESS_SCREENSHOT_REPLACEMENT = '''        if params.area_ui_type == "VIEW_3D":
            # ActiveVisual Xvfb compatibility: GPUOffScreen
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            space = area.spaces.active
            if region is None or space is None:
                return Result(status="error", message="No drawable VIEW_3D region")
            try:
                import gpu
                import numpy as np

                width, height = region.width, region.height
                offscreen = gpu.types.GPUOffScreen(width, height)
                try:
                    r3d = space.region_3d
                    offscreen.draw_view3d(
                        context.scene, context.view_layer, space, region,
                        r3d.view_matrix, r3d.window_matrix,
                        do_color_management=True,
                    )
                    buffer = offscreen.texture_color.read()
                finally:
                    offscreen.free()

                buffer.dimensions = width * height * 4
                pixels = np.asarray(buffer, dtype=np.float32) / 255.0
                image = bpy.data.images.new(
                    "official_mcp_viewport", width, height, alpha=True,
                )
                try:
                    image.pixels.foreach_set(pixels.ravel())
                    image.filepath_raw = filepath_screenshot
                    image.file_format = "PNG"
                    image.save()
                finally:
                    bpy.data.images.remove(image)
            except Exception as ex:  # noqa: BLE001 - returned through MCP
                return Result(status="error", message="Offscreen capture failed: " + str(ex))
        else:
            with context.temp_override(window=window, area=area):
                try:
                    bpy.ops.screen.screenshot_area(filepath=filepath_screenshot)
                except RuntimeError as ex:
                    return Result(status="error", message=str(ex))
'''
MCP_DEPENDENCY_STOCK = '"mcp[cli]>=1.2.0",'
MCP_DEPENDENCY_COMPAT_MAJOR = '"mcp[cli]>=1.2.0,<2",'
MCP_DEPENDENCY_PINNED = '"mcp[cli]==1.29.0",'


def _resolve(repo: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else repo / path


def _patch_headless_screenshot(checkout: Path) -> None:
    target = (checkout / "mcp" / "blmcp" / "tools" /
              "get_screenshot_of_area_as_image_toolcode.py")
    text = target.read_text()
    if HEADLESS_SCREENSHOT_MARKER in text:
        return
    if HEADLESS_SCREENSHOT_STOCK not in text:
        raise RuntimeError(
            "official screenshot source no longer matches the pinned patch")
    target.write_text(text.replace(
        HEADLESS_SCREENSHOT_STOCK, HEADLESS_SCREENSHOT_REPLACEMENT, 1))


def _pin_compatible_mcp_sdk(checkout: Path) -> None:
    """Pin the SDK version verified with the official v1.0.0 server."""
    target = checkout / "mcp" / "pyproject.toml"
    text = target.read_text()
    if MCP_DEPENDENCY_PINNED in text:
        return
    old = (MCP_DEPENDENCY_STOCK if MCP_DEPENDENCY_STOCK in text
           else MCP_DEPENDENCY_COMPAT_MAJOR)
    if old not in text:
        raise RuntimeError("official MCP dependency declaration changed")
    target.write_text(text.replace(
        old, MCP_DEPENDENCY_PINNED, 1))


def ensure_source(repo: Path, cfg: dict) -> Path:
    """Clone the configured official tag once and return its checkout."""
    official = cfg["official"]
    cache = _resolve(repo, official["cache_dir"])
    checkout = cache / f"blender_mcp-{official['source_ref'].replace('/', '_')}"
    marker = checkout / "mcp" / "pyproject.toml"
    cache.mkdir(parents=True, exist_ok=True)
    with SOURCE_LOCK.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if not marker.is_file():
            partial = cache / f".{checkout.name}.partial-{os.getpid()}"
            if partial.exists():
                shutil.rmtree(partial)
            completed = subprocess.run([
                "git", "clone", "--depth", "1", "--branch",
                official["source_ref"], official["source_url"], str(partial),
            ], capture_output=True, text=True)
            if completed.returncode:
                raise RuntimeError(
                    f"official source clone failed: {completed.stderr.strip()}")
            partial.rename(checkout)
        _patch_headless_screenshot(checkout)
        _pin_compatible_mcp_sdk(checkout)
    return checkout
