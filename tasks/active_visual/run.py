#!/usr/bin/env python3
"""Run the graded ActiveVisual experiment."""

from core.harness.mcp_entry import main as run_pipeline
from core.harness.project_env import load_project_env
from . import _runner


def main(argv: list[str] | None = None) -> int:
    """Load runtime configuration and run the ActiveVisual pipeline."""
    load_project_env()
    return run_pipeline("ActiveVisual", _runner, argv)


if __name__ == "__main__":
    raise SystemExit(main())
