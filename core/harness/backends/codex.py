"""Codex CLI backend for iterative image runs."""

import json
import os
import shutil
import subprocess
import tempfile

from core.harness import no_memory

from .common import (
    parse_python_script,
)
from .common import (
    render_script as render_script,
)
from .common import (
    rendered_views as rendered_views,
)

CLI_WORKSPACE_ROOT = "/tmp/agent_generation"

CODEX_WORKSPACE_ARTIFACTS = (".git", "codex-capture.jsonl")


def image_manifest(entries):
    """Preamble pointing codex's read tooling at the workspace-local images.

    entries: ordered list of (role, dest_name). role is a short human label
    such as "reference" / "current render" / "left" / "right"."""
    if not entries:
        return ""
    parts = [
        "The relevant image(s) are saved in your current working "
        "directory. Inspect each one (open it by its bare filename) "
        "before answering."
    ]
    parts += [f"- {role}: {name}" for role, name in entries]
    return "\n".join(parts)


def parse_codex_json_events(transcript):
    """Parse `codex exec --json` stdout. Returns (text, usage, errors): the
    last agent_message text, the summed turn.completed usage, and any
    error-event messages (same shape as providers._parse_codex_json_events)."""
    text, errors = "", []
    usage = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    for line in transcript.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        kind = obj.get("type")
        if kind == "item.completed":
            item = obj.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                text = item["text"]
        elif kind == "turn.completed":
            m = obj.get("usage") or {}
            for k in usage:
                usage[k] += m.get(k) or 0
        elif kind == "error":
            errors.append(obj.get("message") or json.dumps(obj))
    return text, usage, errors


def call_codex(prompt, image_map, model, timeout, thinking_depth):
    """One `codex exec --json` turn in a fresh workspace.

    image_map maps dest_filename -> source Path; each is copied into the
    workspace under dest_filename (so callers control collision-free names).
    Returns (text, raw_dict, generated_files) where generated_files maps
    relative name -> bytes for anything the model wrote into the workspace
    (codex is agentic and sometimes writes the script to a file instead of
    printing it)."""
    os.makedirs(CLI_WORKSPACE_ROOT, exist_ok=True)
    workspace = tempfile.mkdtemp(prefix="codex_raw_", dir=CLI_WORKSPACE_ROOT)
    try:
        for dest_name, src in image_map.items():
            shutil.copy2(src, os.path.join(workspace, dest_name))

        command = [
            "codex",
            "exec",
            "--model",
            model,
            "--sandbox",
            "danger-full-access",
            "--json",
            "--skip-git-repo-check",
            "-c",
            f'model_reasoning_effort="{thinking_depth}"',
            *no_memory.CODEX_FLAGS,
            "-",
        ]
        result = subprocess.run(
            command,
            input=prompt,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        skip = set(image_map) | set(CODEX_WORKSPACE_ARTIFACTS)
        generated = {}
        for entry in sorted(os.listdir(workspace)):
            path = os.path.join(workspace, entry)
            if entry in skip or not os.path.isfile(path):
                continue
            with open(path, "rb") as f:
                generated[entry] = f.read()

        text, usage, errors = parse_codex_json_events(result.stdout)
        if result.returncode and result.stderr.strip():
            errors.append(result.stderr.strip())
        raw = {
            "backend": "codex",
            "model": model,
            "thinking_depth": thinking_depth,
            "exit_status": result.returncode,
            "text": text,
            "transcript": result.stdout,
            "stderr": result.stderr,
            "usage": usage,
            "errors": errors,
            "generated_files": sorted(generated),
        }
        if not text and not generated:
            raise RuntimeError(
                "codex exec --json returned no agent message "
                f"(exit_status={result.returncode}). "
                "Ensure codex is authenticated (OPENAI_API_KEY or `codex "
                "login`). "
                + (
                    f"Errors: {'; '.join(errors[:3])}"
                    if errors
                    else f"Transcript head:\n{result.stdout[:500]}"
                )
            )
        return text, raw, generated
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def extract_script(text, generated_files):
    """Parse the reply text; if that fails, fall back to the largest .py file
    the model wrote into its workspace."""
    script = parse_python_script(text)
    if script:
        return script
    py_files = {n: b for n, b in generated_files.items() if n.endswith(".py")}
    if py_files:
        name = max(py_files, key=lambda n: len(py_files[n]))
        return py_files[name].decode("utf-8", errors="replace").strip()
    return None


def is_backend_limit_error(exc):
    """Quota / rate-limit / auth-missing style failures that should halt the
    run rather than be silently swallowed (mirrors is_response_limit_error)."""
    msg = str(exc).lower()
    indicators = [
        "rate limit",
        "usage limit",
        "quota",
        "429",
        "530",
        "cloudflare",
        "token limit",
        "context length",
        "too many tokens",
        "returned no agent message",
        "returned no output",
    ]
    return any(ind in msg for ind in indicators)
