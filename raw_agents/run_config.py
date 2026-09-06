"""Reasoning configuration and atomic checkpoints for experiment runners."""

from pathlib import Path
import json
import os
import tempfile
import tomllib


CONFIG_PATH = Path(__file__).with_name("config.toml")
THINKING_DEPTHS = ("low", "medium", "high", "xhigh", "max")
# Backends whose own effort scale is narrower than the five above: agy's pinned
# 1.0.16 knows low/medium/high, and Kimi K3 advertises low/high/max.
AGENT_THINKING_DEPTHS = {
    "agy": ("low", "medium", "high"),
    "kimi": ("low", "high", "max"),
}


def thinking_depth(agent: str) -> str:
    """Return and validate an agent's configured reasoning effort."""
    with CONFIG_PATH.open("rb") as handle:
        config = tomllib.load(handle)
    value = str(
        config.get("agent", {}).get(agent, {}).get("thinking_depth", "high")
    ).lower()
    supported = AGENT_THINKING_DEPTHS.get(agent, THINKING_DEPTHS)
    if value not in supported:
        raise ValueError(
            f"invalid [agent.{agent}].thinking_depth {value!r} in {CONFIG_PATH}; "
            f"choose from {supported}"
        )
    return value


def atomic_write_json(path: Path, value: dict) -> None:
    """Atomically replace a JSON checkpoint, preserving the old file on crash."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
