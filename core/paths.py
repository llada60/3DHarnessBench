"""Resolve bundled resources from the checkout, independent of the caller's cwd."""

from pathlib import Path

CORE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = CORE_ROOT.parent
BENCHMARK_ROOT = PROJECT_ROOT / "benchmark"
CONFIGS_ROOT = PROJECT_ROOT / "configs"
PROMPTS_ROOT = PROJECT_ROOT / "prompts"
METRICS_ROOT = PROJECT_ROOT / "metrics"
BLENDER_MCP_ROOT = CORE_ROOT / "blender_mcp"
