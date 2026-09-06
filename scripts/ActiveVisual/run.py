#!/usr/bin/env python3
"""Run the graded ActiveVisual experiment."""

import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
RAW_AGENTS_DIR = HERE.parents[1] / "raw_agents"
for path in (RAW_AGENTS_DIR, HERE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from project_env import load_project_env  # noqa: E402

load_project_env()

import _runner  # noqa: E402
from mcp_entry import main  # noqa: E402  # pyright: ignore[reportMissingImports]


if __name__ == "__main__":
    raise SystemExit(main("ActiveVisual", _runner, HERE))
