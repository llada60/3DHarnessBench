# blender_mcp_server.py
from mcp.server.fastmcp import FastMCP, Context, Image
import logging

from blender_mcp_core.connection import (
    BlenderConnection,
    get_blender_connection as _core_get_blender_connection,
    make_server_lifespan,
)
from blender_mcp_core.runner import run_stdio
from blender_mcp_core.screenshot import capture_viewport_screenshot

from .validator import validate_camera_view_code

# Configure logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("BlenderMCPServer")

# ActiveVisual's default reference-view port;
# BLENDER_PORT / --port still override it.
DEFAULT_PORT = 8888


def get_blender_connection() -> BlenderConnection:
    """Get or create a persistent Blender connection"""
    return _core_get_blender_connection(default_port=DEFAULT_PORT)


# Create the MCP server with lifespan support. The lambda resolves
# get_blender_connection through module globals on each call, so monkeypatching
# this module's getter also redirects the lifespan's startup probe.
mcp = FastMCP(
    "BlenderMCP",
    lifespan=make_server_lifespan(lambda: get_blender_connection()),
)


@mcp.tool()
def get_viewport_screenshot(ctx: Context, max_size: int = 1000) -> Image:
    """
    Capture a screenshot of the current Blender 3D viewport.

    This captures the view displayed by the active VIEW_3D area's `region_3d`;
    it is not a render through `bpy.context.scene.camera`. Moving or rotating
    the scene Camera does not change the screenshot viewpoint unless that
    viewport's `view_perspective` is `CAMERA`. Outside Camera view, a Camera
    transform may only move the Camera wireframe visible in the screenshot.

    Parameters:
    - max_size: Maximum size in pixels for the largest dimension (default: 800)

    Returns the screenshot as an Image.
    """
    return capture_viewport_screenshot(get_blender_connection, max_size=max_size)


@mcp.tool()
def execute_blender_code(ctx: Context, code: str) -> str:
    """
    Execute camera/viewport-UI-only Python code in Blender.

    The scene Camera and the interactive 3D viewport are separate views.
    Assigning `bpy.context.scene.camera` properties changes the scene Camera,
    but it changes `get_viewport_screenshot`'s viewpoint only while the
    viewport's `region_3d.view_perspective` is `CAMERA`. To change a normal
    viewport screenshot directly, assign the allowed `region_3d` properties
    such as `view_location`, `view_rotation`, or `view_distance`. Viewport
    shading and overlay visibility are also UI-only state and may be changed.

    `validate_camera_view_code` AST-checks the code before Blender ever sees
    it, and the accepted subset is far narrower than "code that only moves
    the camera" — so spell the contract out here, because this docstring is the
    only thing an MCP client sees. Nothing can be printed/read back; use only
    allowed assignments and the explicit redraw/update calls below.

    Allowed:
    - unaliased `import bpy` / `math` / `mathutils`, or
      `from mathutils import Euler, Matrix, Quaternion, Vector`
    - assignment to a local name, e.g. `cam = bpy.context.scene.camera`
    - on the active camera object: location, matrix_world, rotation_euler,
      rotation_mode, rotation_quaternion
    - on its `.data`: lens, type, clip_start, clip_end, ortho_scale,
      sensor_fit, sensor_width, sensor_height, shift_x, shift_y
    - on a viewport's region_3d — reachable via `bpy.context.space_data` or
      `for area in bpy.context.screen.areas:` then `area.spaces.active.region_3d`
      — view_location, view_rotation, view_distance, view_matrix,
      view_perspective, view_camera_offset, view_camera_zoom, lock_rotation
    - `space.shading.type` set to WIREFRAME, SOLID, MATERIAL, or RENDERED
    - `space.overlay.show_overlays` set to a literal boolean
    - calls to math.* / mathutils constructors, `region_3d.update()`,
      `area.tag_redraw()`, and literal-text `workspace.status_text_set(...)`
    - aliases of the explicitly typed area/space/region_3d UI objects
    - at least one camera/viewport assignment or UI update must be present

    Rejected, as `Error executing code: line N: <reason>`:
    - `print(...)` and every other standalone expression or call
    - anything through bpy.data.* or bpy.ops.* (the only scene-data route is
      bpy.context.scene.camera; UI routes are explicitly allowlisted)
    - element assignment like `cam.location[0] = 0` — assign a whole
      tuple/Vector instead
    - def/class/lambda, comprehensions, while, `for` over anything but
      bpy.context.screen.areas, try/with/raise/assert/global, del, walrus,
      await/yield, `_`-prefixed attributes

    Minimal scene-Camera example that passes (but does not move a normal
    non-Camera viewport):
        import bpy
        cam = bpy.context.scene.camera
        cam.location = (0, -10, 3)

    Minimal viewport example that changes the screenshot viewpoint:
        import bpy
        from mathutils import Quaternion
        for area in bpy.context.screen.areas:
            if area.type == 'VIEW_3D':
                region_3d = area.spaces.active.region_3d
                region_3d.view_rotation = Quaternion((0.7071068, 0.7071068, 0, 0))
                region_3d.view_distance = 3.0
                region_3d.update()

    Parameters:
    - code: Python code that only changes the active camera or viewport/UI state
    """
    try:
        validate_camera_view_code(code)
        # Get the global connection
        blender = get_blender_connection()
        result = blender.send_command("execute_code", {"code": code})
        return f"Code executed successfully: {result.get('result', '')}"
    except Exception as e:
        logger.error(f"Error executing code: {str(e)}")
        return f"Error executing code: {str(e)}"

# Main execution

def main():
    run_stdio(mcp)

if __name__ == "__main__":
    main()
