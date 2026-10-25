"""Environment configuration at the host-Python/Blender process boundary."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from core.paths import PROJECT_ROOT


def blender_environment(
    *addon_roots: Path,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a child environment for Blender's --python-use-system-env mode.

    Blender has its own Python interpreter. Supply source and add-on locations
    before it starts, without changing the parent process's import path or
    installing anything into the user's Blender profile.
    """
    env = dict(os.environ if base_env is None else base_env)
    paths = [str(PROJECT_ROOT), *(str(path) for path in addon_roots)]
    paths.extend(path for path in env.get("PYTHONPATH", "").split(os.pathsep) if path)
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(paths))
    env["PYTHONNOUSERSITE"] = "1"
    # Do not substitute the host/Conda interpreter for Blender's bundled Python.
    env.pop("PYTHONHOME", None)
    if prefix := env.get("CONDA_PREFIX"):
        lib = str(Path(prefix) / "lib")
        previous = env.get("LD_LIBRARY_PATH", "")
        if lib not in previous.split(os.pathsep):
            env["LD_LIBRARY_PATH"] = lib + (os.pathsep + previous if previous else "")
    return env
