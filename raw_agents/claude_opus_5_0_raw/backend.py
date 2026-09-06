"""Opus 5 adapter using the shared Claude Code interface."""

from claude_fable_5_raw.backend import (
    MEMORY_OFF,
    build_agent,
    call_model,
    extract_script,
    is_backend_limit_error,
    load_image,
    render_script,
    rendered_views,
)

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
