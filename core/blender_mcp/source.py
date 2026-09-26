"""Load Blender MCP from an explicit local checkout or a verified upstream commit.

This module never patches source files. Compatibility changes belong in the
configured MCP repository, with their own revision and tests.
"""

from __future__ import annotations

import fcntl
import hashlib
import re
import subprocess
import tempfile
from pathlib import Path


def _resolve(repo: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else repo / path


def _git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
    )
    if result.returncode:
        raise RuntimeError(f"Blender MCP source: git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _validate_layout(source: Path) -> None:
    required = (
        "mcp/pyproject.toml",
        "mcp/blmcp/__init__.py",
        "addon/blender_mcp_addon/__init__.py",
    )
    missing = [name for name in required if not (source / name).is_file()]
    if missing:
        raise RuntimeError(f"Invalid Blender MCP source at {source}: missing {', '.join(missing)}")


def _validate_checkout(source: Path, url: str, commit: str) -> None:
    _validate_layout(source)
    if not (source / ".git").is_dir():
        raise RuntimeError(f"Blender MCP cache is not a Git checkout: {source}")
    if _git("rev-parse", "HEAD", cwd=source) != commit:
        raise RuntimeError(f"Blender MCP cache does not match source_commit: {source}")
    if _git("remote", "get-url", "origin", cwd=source) != url:
        raise RuntimeError(f"Blender MCP cache does not match source_url: {source}")
    if _git("status", "--porcelain", "--untracked-files=normal", cwd=source):
        raise RuntimeError(
            f"Blender MCP cache has local changes: {source}. "
            "Use [official].source_path for an intentionally modified checkout, "
            "or move the cache aside to download a clean copy."
        )


def ensure_source(repo: Path, cfg: dict) -> Path:
    """Return source without modifying it; cache remote sources by URL and commit.

    ``source_path`` selects a caller-maintained checkout, including a development
    fork. Otherwise ``source_url``, ``source_ref`` and a full ``source_commit``
    identify a remote dependency. The ref must resolve to the pinned commit.
    """
    official = cfg["official"]
    if local := official.get("source_path"):
        source = _resolve(repo, local)
        _validate_layout(source)
        return source

    commit = official.get("source_commit", "").lower()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("[official].source_commit must be a full 40-character Git commit SHA")
    url, ref = official["source_url"], official["source_ref"]
    identity = hashlib.sha256(f"{url}\n{commit}".encode()).hexdigest()[:16]
    cache = _resolve(repo, official["cache_dir"])
    checkout = cache / f"blender_mcp-{identity}"
    cache.mkdir(parents=True, exist_ok=True)

    # Each dependency has its own lock; unrelated repositories need not wait.
    with (cache / f".{checkout.name}.lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if checkout.exists():
            _validate_checkout(checkout, url, commit)
            return checkout
        with tempfile.TemporaryDirectory(prefix=f".{checkout.name}-", dir=cache) as staging:
            partial = Path(staging) / "source"
            _git("clone", "--depth", "1", "--branch", ref, "--", url, str(partial))
            _validate_checkout(partial, url, commit)
            _git("checkout", "--detach", commit, cwd=partial)
            partial.rename(checkout)
    return checkout
