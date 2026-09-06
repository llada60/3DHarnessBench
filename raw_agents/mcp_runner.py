"""Shared configuration and rendering helpers for graded MCP experiments."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

try:
    from .agent_pools import output_pool_alias
    from .project_env import default_blender
except ImportError:
    from agent_pools import output_pool_alias
    from project_env import default_blender
try:
    from .agent_registry import AGENT_CHOICES
    from .common_cli import boolean_value, positive_int
except ImportError:
    from agent_registry import AGENT_CHOICES
    from common_cli import boolean_value, positive_int


RAW_AGENTS_DIR = Path(__file__).resolve().parent
GRADED_EXP_DIR = RAW_AGENTS_DIR.parent
PROJECT_ROOT = GRADED_EXP_DIR
# Kept as an exported alias for existing backend imports.
REPO_ROOT = PROJECT_ROOT
DEFAULT_INPUT_PATH = PROJECT_ROOT / "data" / "benchmark"
DEFAULT_OUTPUT_PATH = GRADED_EXP_DIR / "outputs"
RENDER_DRIVER = PROJECT_ROOT / "evaluation" / "core" / "render.py"

@dataclass(frozen=True)
class MCPAgentSpec:
    cli_kind: str
    model: str


AGENT_SPECS = {
    "gpt-5-6-sol": MCPAgentSpec("codex", "gpt-5.6-sol"),
    "kimi-k3": MCPAgentSpec("kimi", "k3"),
    "opus-5": MCPAgentSpec("opus", "claude-opus-5"),
    "fable-5": MCPAgentSpec("claude", "claude-fable-5"),
    # qwen3.8-max-preview is Model Studio's token-plan API: same qwen code
    # CLI, but a different base URL and $QWEN_API_KEY, so it
    # gets its own kind ([agent.qwen-tokenplan]) and its own output directory.
    "qwen3-8-max-preview": MCPAgentSpec("qwen-tokenplan",
                                         "qwen3.8-max-preview"),
    "gemini3-1-pro": MCPAgentSpec("agy", "Gemini 3.1 Pro (High)"),
    "minimax-m3": MCPAgentSpec("minimax-m3", "MiniMax-M3"),
}


def nonnegative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"expected a non-negative integer, got {value!r}") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return number


def agent_spec(alias: str) -> MCPAgentSpec:
    return AGENT_SPECS[alias]


def texture_label(texture_renders: bool) -> str:
    return "w_texture" if texture_renders else "wo_texture"


def output_root(output_path: Path, mode: str, texture_renders: bool,
                alias: str) -> Path:
    return (output_path.resolve() / mode / texture_label(texture_renders) /
            output_pool_alias(alias))


def instance_ref(instance_dir: Path, texture_renders: bool) -> Path:
    suffix = ".glb" if texture_renders else "_grey.glb"
    return instance_dir / f"{instance_dir.name}{suffix}"


def discover_instances(input_path: Path, requested: list[str] | None = None,
                       limit: int | None = None) -> list[str]:
    root = input_path.resolve()
    if not root.is_dir():
        raise RuntimeError(f"--data-path is not a directory: {root}")
    if (root / f"{root.name}.glb").is_file():
        instances = [root.name]
    else:
        instances = sorted(
            path.name for path in root.iterdir()
            if path.is_dir() and (path / f"{path.name}.glb").is_file()
            and (path / f"{path.name}_grey.glb").is_file())
    if requested:
        wanted = set(requested)
        missing = sorted(wanted - set(instances))
        if missing:
            raise RuntimeError(f"unknown benchmark instance(s): {missing}")
        instances = [name for name in instances if name in wanted]
    if limit is not None:
        instances = instances[:limit]
    if not instances:
        raise RuntimeError(f"no benchmark instances found under {root}")
    return instances


def benchmark_instance(input_path: Path, name: str) -> Path:
    root = input_path.resolve()
    return root if root.name == name and (root / f"{name}.glb").is_file() \
        else root / name


def render_script(script: Path, renders: Path, *, blender: str,
                  timeout: int = 240, samples: int = 64,
                  resolution: int = 512,
                  engine: str = "CYCLES",
                  light_power: float = 1.0,
                  light_ref_extent: float = 2.5) -> dict:
    """Render the published script into four benchmark-aligned PNG views."""
    renders.mkdir(parents=True, exist_ok=True)
    log = renders / "render_log.json"
    command = [
        blender, "--background", "--python", str(RENDER_DRIVER), "--",
        "--blender-render", "--script", str(script),
        "--output-dir", str(renders), "--samples", str(samples),
        "--resolution", str(resolution), "--engine", engine,
        "--light-power", str(light_power),
        "--light-ref-extent", str(light_ref_extent),
    ]
    env = os.environ.copy()
    prefix = env.get("CONDA_PREFIX")
    if prefix:
        lib = str(Path(prefix) / "lib")
        old = env.get("LD_LIBRARY_PATH", "")
        if lib not in old.split(os.pathsep):
            env["LD_LIBRARY_PATH"] = lib + (os.pathsep + old if old else "")
    started = time.monotonic()
    try:
        proc = subprocess.run(command, capture_output=True, text=True,
                              timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        record = {"status": "ERR_TIMEOUT", "error":
                  f"Blender subprocess exceeded {timeout}s",
                  "latency_s": round(time.monotonic() - started, 2)}
        log.write_text(json.dumps(record, indent=2))
        return record
    if log.is_file():
        record = json.loads(log.read_text())
        record["returncode"] = proc.returncode
        return record
    record = {"status": "ERR_NOLOG", "returncode": proc.returncode,
              "error": (proc.stderr or proc.stdout or
                        "Blender wrote no render_log.json")[-4000:],
              "latency_s": round(time.monotonic() - started, 2)}
    log.write_text(json.dumps(record, indent=2))
    return record


def render_namespace(**values):
    """Compatibility helper retained for callers wanting an args object."""
    return SimpleNamespace(**values)


__all__ = [
    "AGENT_CHOICES", "AGENT_SPECS", "DEFAULT_INPUT_PATH",
    "DEFAULT_OUTPUT_PATH", "GRADED_EXP_DIR", "REPO_ROOT", "agent_spec",
    "benchmark_instance", "boolean_value", "default_blender",
    "discover_instances", "instance_ref", "nonnegative_int", "output_root",
    "positive_int", "render_script", "texture_label",
]
