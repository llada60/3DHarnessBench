#!/usr/bin/env python3
"""Run a command with the checkout's .env, preserving its arguments and exit code."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "raw_agents"))
from project_env import load_project_env  # noqa: E402


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Usage: pixi run env-run COMMAND [ARG ...]")
    load_project_env()
    os.execvp(sys.argv[1], sys.argv[1:])
