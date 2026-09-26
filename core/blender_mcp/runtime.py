"""MCP process configuration shared by both interaction tasks."""

from pathlib import Path

from core.paths import PROJECT_ROOT


RUNTIME_REQUIREMENTS = "core/blender_mcp/requirements.txt"


def server_spec(source: Path, port: int, *, uv_bin: str) -> dict:
    """Install the MCP package and harness SDK requirement without editing source."""
    return {
        "command": uv_bin,
        "args": [
            "run", "--directory", str(PROJECT_ROOT), "--no-project",
            "--with", str(source / "mcp"),
            "--with-requirements", RUNTIME_REQUIREMENTS,
            "blender-mcp",
        ],
        "env": {"BLENDER_MCP_HOST": "127.0.0.1", "BLENDER_MCP_PORT": str(port)},
    }
