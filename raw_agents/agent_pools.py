"""Stable output/resume identities for public graded-agent aliases."""

from __future__ import annotations


QWEN3_8_MAX_PREVIEW_POOL = "qwen3-8-max-preview"
QWEN3_8_MAX_PREVIEW_ALIASES = frozenset({QWEN3_8_MAX_PREVIEW_POOL})


def output_pool_alias(alias: str) -> str:
    """Return the directory/checkpoint identity shared by backend aliases."""
    if alias in QWEN3_8_MAX_PREVIEW_ALIASES:
        return QWEN3_8_MAX_PREVIEW_POOL
    return alias
