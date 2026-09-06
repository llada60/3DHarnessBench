"""Kimi K3 adapter with image/render helpers shared with Claude Code."""

from claude_fable_5_raw.backend import (
    extract_script,
    is_backend_limit_error,
    load_image,
    render_script,
    rendered_views,
)
from kimi_code import build_agent, call_model
from kimi_code import memory_off as MEMORY_OFF

__all__ = [
    "MEMORY_OFF",
    "build_agent",
    "call_model",
    "extract_script",
    "is_backend_limit_error",
    "load_image",
    "render_script",
    "rendered_views",
]
