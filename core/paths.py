"""Project paths. Run public commands from the repository root.

Keep resource paths relative. Capture the launch directory once for subprocesses
that need to return from an agent workspace to the repository.
"""

from pathlib import Path

PROJECT_ROOT = Path.cwd()
CORE_ROOT = Path("core")
BENCHMARK_ROOT = Path("benchmark")
CONFIGS_ROOT = Path("configs")
PROMPTS_ROOT = Path("prompts")
METRICS_ROOT = Path("metrics")
BLENDER_MCP_ROOT = CORE_ROOT / "blender_mcp"
