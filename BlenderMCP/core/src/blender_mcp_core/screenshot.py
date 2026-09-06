"""Shared implementation of the get_viewport_screenshot tool body."""
from mcp.server.fastmcp import Image
import logging
import os
import tempfile
from typing import Callable

from .connection import BlenderConnection

logger = logging.getLogger("BlenderMCPServer")


def capture_viewport_screenshot(
    get_connection: Callable[[], BlenderConnection],
    max_size: int = 1000,
) -> Image:
    """Capture the Blender viewport via *get_connection* and return an Image."""

    try:
        blender = get_connection()

        # Create temp file path
        temp_dir = tempfile.gettempdir()
        temp_path = os.path.join(temp_dir, f"blender_screenshot_{os.getpid()}.png")

        result = blender.send_command("get_viewport_screenshot", {
            "max_size": max_size,
            "filepath": temp_path,
            "format": "png"
        })

        if "error" in result:
            raise Exception(result["error"])

        if not os.path.exists(temp_path):
            raise Exception("Screenshot file was not created")

        # Read the file
        with open(temp_path, 'rb') as f:
            image_bytes = f.read()

        # Delete the temp file
        os.remove(temp_path)

        return Image(data=image_bytes, format="png")

    except Exception as e:
        logger.error(f"Error capturing screenshot: {str(e)}")
        raise Exception(f"Screenshot failed: {str(e)}")
