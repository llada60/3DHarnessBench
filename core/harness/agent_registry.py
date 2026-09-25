"""Public experiment names shared by runners and evaluation.

Only names in :data:`AGENT_CHOICES` are part of the public CLI contract. The
backend-specific names used by individual providers remain implementation
details and must not be accepted as experiment aliases.
"""

from __future__ import annotations


AGENT_CHOICES = (
    "gpt-6-astra",
    "gpt-5-6-sol",
    "kimi-k3",
    "opus-5",
    "fable-5",
    "qwen3-8-max-preview",
    "gemini3-1-pro",
    "minimax-m3",
)

SETTING_CHOICES = (
    "Single-view",
    "Multi-view",
    "ActiveVisual",
    "Full3DInteraction",
)


__all__ = ["AGENT_CHOICES", "SETTING_CHOICES"]
