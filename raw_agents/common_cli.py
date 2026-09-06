"""Reusable argparse definitions for all public experiment entry points."""

from __future__ import annotations

import argparse
from pathlib import Path

try:
    from .agent_registry import AGENT_CHOICES
except ImportError:  # direct imports with raw_agents/ on sys.path
    from agent_registry import AGENT_CHOICES


def boolean_value(value: str | bool) -> bool:
    """Parse a portable command-line boolean value."""
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError(
        f"expected true/false, yes/no, on/off, or 1/0; got {value!r}")


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"expected a positive integer, got {value!r}") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return number


def add_data_path(parser: argparse.ArgumentParser, default: Path, *,
                  dest: str = "data_path", help_text: str | None = None) -> None:
    parser.add_argument(
        "--data-path", "--data_path", "--input-path", "--input_path",
        dest=dest, type=Path, default=default,
        help=help_text or "benchmark root or one instance directory "
        "(default: %(default)s)",
    )


def add_output_dir(parser: argparse.ArgumentParser, default: Path, *,
                   dest: str = "output_dir", help_text: str | None = None,
                   legacy_flags: tuple[str, ...] = ()) -> None:
    parser.add_argument(
        "--output-dir", *legacy_flags, dest=dest, type=Path, default=default,
        help=help_text or "base output directory (default: %(default)s)",
    )


def add_agent(parser: argparse.ArgumentParser, *, required: bool = False,
              default: str = "kimi-k3") -> None:
    parser.add_argument(
        "--agent", choices=AGENT_CHOICES, required=required,
        default=None if required else default,
        help="public agent name" + ("" if required else " (default: %(default)s)"),
    )


def add_texture_renders(parser: argparse.ArgumentParser, *,
                        dest: str = "texture_renders") -> None:
    parser.add_argument(
        "--texture-renders", "--texture_renders", "--texture-render",
        "--texture_render", dest=dest, type=boolean_value, nargs="?",
        const=True, default=True, metavar="BOOL",
        help="use textured/color inputs (default: True)",
    )
    parser.add_argument(
        "--no-texture-renders", "--no-texture-render", dest=dest,
        action="store_false", help="use geometry-only/grey inputs",
    )


def add_tasks(parser: argparse.ArgumentParser, *, dest: str = "tasks") -> None:
    parser.add_argument(
        "--tasks", "--instances", dest=dest, nargs="*", default=None,
        metavar="NAME", help="only run these benchmark instance names",
    )


__all__ = [
    "AGENT_CHOICES",
    "add_agent",
    "add_data_path",
    "add_output_dir",
    "add_tasks",
    "add_texture_renders",
    "boolean_value",
    "positive_int",
]
