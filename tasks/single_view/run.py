#!/usr/bin/env python3
"""Run the graded sequential pipeline with one GT image per object."""

from core.harness.iterative_runner import main as run_pipeline
from core.harness.project_env import load_project_env


def main(argv: list[str] | None = None) -> int:
    """Load runtime configuration and run the single-view pipeline."""
    load_project_env()
    return run_pipeline("single", argv)


if __name__ == "__main__":
    raise SystemExit(main())
