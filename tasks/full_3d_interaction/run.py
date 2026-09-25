#!/usr/bin/env python3
"""Run the graded official full-access Blender MCP experiment."""

from pathlib import Path

from core.harness.mcp_entry import main as run_pipeline
from core.harness.project_env import load_project_env
from tasks.full_3d_interaction import _runner

HERE = Path(__file__).resolve().parent


def main(argv: list[str] | None = None) -> int:
    """Load runtime configuration and run the Full3DInteraction pipeline."""
    load_project_env()
    return run_pipeline("Full3DInteraction", _runner, HERE, argv)


if __name__ == "__main__":
    raise SystemExit(main())
