#!/usr/bin/env python3
"""Run a command with the checkout's .env, preserving its arguments and exit code."""

import os
import sys
from core.harness.project_env import load_project_env


def main(argv: list[str] | None = None) -> int:
    """Replace this process with the requested command after loading .env."""
    args = sys.argv[1:] if argv is None else argv
    if not args:
        raise SystemExit("Usage: pixi run env-run COMMAND [ARG ...]")
    load_project_env()
    os.execvp(args[0], args)


if __name__ == "__main__":
    raise SystemExit(main())
