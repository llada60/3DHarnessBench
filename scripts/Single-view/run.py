#!/usr/bin/env python3
"""Run the graded sequential pipeline with one GT image per object."""

import sys
from pathlib import Path


RAW_AGENTS_DIR = Path(__file__).resolve().parents[2] / "raw_agents"
if str(RAW_AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(RAW_AGENTS_DIR))

from project_env import load_project_env  # noqa: E402

load_project_env()

from iterative_runner import main  # noqa: E402  # pyright: ignore[reportMissingImports]


if __name__ == "__main__":
    raise SystemExit(main("single"))
