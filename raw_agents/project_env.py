"""Project-local configuration shared by the five public entry points."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_project_env() -> None:
    """Load this checkout's .env without overriding exported shell variables.

    An explicit path prevents discovery of credentials in a parent checkout
    or an unrelated working directory. Child CLIs and Blender inherit these
    variables through the normal process environment; no shell code is run.
    """
    load_dotenv(PROJECT_ROOT / ".env", override=False, encoding="utf-8")


def default_blender() -> str:
    """Resolve BLENDER, project-local builds, PATH, then macOS app bundles."""
    if value := os.environ.get("BLENDER"):
        path = Path(value).expanduser()
        if path.is_absolute():
            return str(path)
        if len(path.parts) > 1:
            return str((PROJECT_ROOT / path).resolve())
        return shutil.which(value) or value
    candidates = sorted((PROJECT_ROOT / "tools").glob("blender-*/blender"))
    if candidates:
        return str(candidates[-1])
    if binary := shutil.which("blender"):
        return binary
    if sys.platform == "darwin":
        for applications in (Path("/Applications"), Path.home() / "Applications"):
            binary = applications / "Blender.app/Contents/MacOS/Blender"
            if binary.is_file():
                return str(binary)
    return "blender"
