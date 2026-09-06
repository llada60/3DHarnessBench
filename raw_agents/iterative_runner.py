#!/usr/bin/env python3
"""Sequential image-to-Blender runner shared by the graded experiment scripts.

There is exactly one candidate at every iteration. Iteration 0 generates a
script from the GT image(s); each later iteration edits the last runnable
script using both its rendered views and the GT image(s). Render/parse failures
are fed back to the same agent up to ``--max-render-retries`` times. There is no
candidate breadth, verifier, tournament, or selection step.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import importlib
import importlib.util
import json
import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

try:
    from .agent_pools import (QWEN3_8_MAX_PREVIEW_ALIASES,
                              output_pool_alias)
except ImportError:
    from agent_pools import (QWEN3_8_MAX_PREVIEW_ALIASES,
                             output_pool_alias)
try:
    from .agent_registry import AGENT_CHOICES
    from .common_cli import (add_agent, add_data_path, add_output_dir,
                             add_tasks, add_texture_renders, positive_int)
except ImportError:
    from agent_registry import AGENT_CHOICES
    from common_cli import (add_agent, add_data_path, add_output_dir,
                            add_tasks, add_texture_renders, positive_int)
from project_env import default_blender
from run_config import atomic_write_json, thinking_depth as configured_thinking_depth
from usage_accounting import (runtime_latency_line, write_instance_usage,
                              write_runtime_latency_summary)


RAW_AGENTS_DIR = Path(__file__).resolve().parent
GRADED_EXP_DIR = RAW_AGENTS_DIR.parent
PROJECT_ROOT = GRADED_EXP_DIR
# Backward-compatible export for backend modules; the graded experiment is now
# its own project root rather than a nested experiment directory.
REPO_ROOT = PROJECT_ROOT
DEFAULT_INPUT_PATH = PROJECT_ROOT / "data" / "benchmark"
DEFAULT_PROMPT_PATH = GRADED_EXP_DIR / "prompting" / "image_only.py"
VIEW_NAMES = ("Image_005.png", "Image_015.png",
              "Image_025.png", "Image_035.png")
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")

@dataclass(frozen=True)
class AgentSpec:
    module: str
    kind: str
    model: str
    config_agent: str


class BackendLimitExhausted(RuntimeError):
    """A provider limit remained after all configured retry attempts."""

    def __init__(self, agent: str, retries: int, last_error: BaseException):
        self.agent = agent
        self.retries = retries
        self.total_attempts = retries + 1
        self.last_error = last_error
        super().__init__(
            f"{agent} provider limit still active after {retries} retries "
            f"({self.total_attempts} total attempts): {last_error}")


class BatchLimitStop(RuntimeError):
    """A peer instance hit the provider limit; make no further model calls."""


AGENT_SPECS = {
    "gpt-5-6-sol": AgentSpec("codex_gpt_5_6_sol_raw.backend", "codex",
                              "gpt-5.6-sol", "codex"),
    "kimi-k3": AgentSpec("kimi_k3_raw.backend", "inline", "kimi-code-k3", "kimi"),
    "opus-5": AgentSpec("claude_opus_5_0_raw.backend", "inline",
                         "claude-code-opus-5", "opus"),
    "fable-5": AgentSpec("claude_fable_5_raw.backend", "inline",
                          "claude-code-fable-5", "claude"),
    "qwen3-8-max-preview": AgentSpec("qwen3_8_max_preview_raw.backend", "inline",
                                      "qwen3.8-max-preview",
                                      "qwen3-8-max-preview"),
    "gemini3-1-pro": AgentSpec("agy_gemini_3_pro_raw.backend", "agy",
                                "gemini-3.1-pro", "agy"),
    "minimax-m3": AgentSpec("minimax_m3_raw.backend", "inline",
                             "MiniMax-M3", "minimax"),
}


# Default OpenAI-compatible endpoint (base URL, model name) for each inline
# agent -- the official Messages endpoint for MiniMax-M3 and Model Studio's
# token-plan API for qwen3.8-max-preview --
# used when neither --base-url / --model-name nor the QWEN3_OPENAI_BASE_URL /
# VLLM_OPENAI_BASE_URL / QWEN3_MODEL env vars are set.
VLLM_ENDPOINT_DEFAULTS = {
    "minimax-m3": ("https://api.minimax.io/anthropic/v1", "MiniMax-M3"),
    "qwen3-8-max-preview": (
        "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
        "qwen3.8-max-preview"),
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


def output_run_root(output_dir: Path, view_mode: str,
                    texture_renders: bool, agent: str) -> Path:
    mode_dir = "Single-view" if view_mode == "single" else "Multi-view"
    texture_dir = "w_texture" if texture_renders else "wo_texture"
    return output_dir / mode_dir / texture_dir / output_pool_alias(agent)


def load_prompt(prompt_path: Path, texture_renders: bool) -> str:
    """Load ``build_prompt(bool)`` from the configured Python prompt module."""
    module_spec = importlib.util.spec_from_file_location(
        "graded_image_only_prompt", prompt_path)
    if module_spec is None or module_spec.loader is None:
        raise RuntimeError(f"Cannot load prompt module: {prompt_path}")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    builder = getattr(module, "build_prompt", None)
    if not callable(builder):
        raise RuntimeError(
            f"Prompt module {prompt_path} must define build_prompt(texture_renders)")
    prompt = builder(texture_renders)
    if not isinstance(prompt, str) or not prompt.strip():
        raise RuntimeError(
            f"Prompt module {prompt_path} returned an empty/non-string prompt")
    return prompt


def resolve_module(spec: AgentSpec):
    """Import the internal backend for one of the seven public agents."""
    return importlib.import_module(spec.module)


def supported_efforts(config_agent: str) -> tuple[str, ...]:
    if config_agent == "kimi":
        return ("low", "high", "max")
    if config_agent == "agy":
        return ("low", "high")
    return ("low", "medium", "high", "xhigh", "max")


class Backend:
    def __init__(self, agent_name: str, args,
                 batch_limit: threading.Event | None = None):
        self.agent_name = agent_name
        self.spec = AGENT_SPECS[agent_name]
        self.module = resolve_module(self.spec)
        self.args = args
        self.batch_limit = batch_limit
        self.interface = None

    def initialize(self, state_dir: Path) -> None:
        if self.spec.kind == "inline":
            if self.agent_name in VLLM_ENDPOINT_DEFAULTS:
                os.environ["QWEN3_OPENAI_BASE_URL"] = self.args.base_url
                os.environ["QWEN3_MODEL"] = self.args.model_name
                if self.agent_name == "minimax-m3":
                    os.environ["MINIMAX_OPENAI_BASE_URL"] = self.args.base_url
                    os.environ["MINIMAX_MODEL_NAME"] = self.args.model_name
                vision_model = self.module.VISION_MODEL
            else:
                vision_model = self.spec.model
                memory_off = getattr(self.module, "MEMORY_OFF", None)
                if memory_off is not None:
                    memory_off(state_dir)
            agent = self.module.build_agent(vision_model, None)
            self.interface = agent.visual_interface
            self.interface.thinking_depth = self.args.thinking_depth
        elif self.spec.kind == "codex" and not shutil.which("codex"):
            raise RuntimeError("Codex CLI was not found on PATH. Install codex before running.")

    @staticmethod
    def _named_images(scope: str, role_images):
        image_map = {}
        entries = []
        for role, paths in role_images:
            for index, path in enumerate(paths, 1):
                suffix = path.suffix.lower() or ".png"
                name = f"{scope}__{role.replace(' ', '_')}__{index}{suffix}"
                image_map[name] = path
                entries.append((role, name))
        return image_map, entries

    def _ask_once(self, prompt: str, role_images, scope: str):
        if self.spec.kind == "inline":
            images = [self.module.load_image(path)
                      for _, paths in role_images for path in paths]
            text, raw = self.module.call_model(
                self.interface, prompt, images, self.args.gen_max_tokens,
                self.spec.model)
            return text, raw, {}

        if self.spec.kind == "codex":
            image_map, entries = self._named_images(scope, role_images)
            manifest = self.module.image_manifest(entries)
            full_prompt = "\n\n".join((manifest, prompt)) if manifest else prompt
            return self.module.call_codex(
                full_prompt, image_map, self.spec.model,
                self.args.timeout, self.args.thinking_depth)

        staged = []
        for role, paths in role_images:
            staged.extend(self.module.role_views(scope, role, paths))
        return self.module.agy_turn(
            prompt, staged, self.spec.model, self.args.timeout, scope,
            self.args.thinking_depth)

    def ask(self, prompt: str, role_images, scope: str):
        """Run one model turn and fail fast on a provider limit.

        A provider/account limit normally applies to every parallel instance,
        so retrying this turn only burns time and repeats the same failure. The
        caller broadcasts the exception to the batch and checkpoints all
        in-flight instances after their already-issued calls return.
        """
        classifier = getattr(self.module, "is_backend_limit_error", None)
        try:
            result = self._ask_once(prompt, role_images, scope)
            # Some CLIs report an exhausted account as a successful, short
            # response instead of a non-zero exit/exception.
            text, raw, generated = result
            errors = raw.get("errors", []) if isinstance(raw, dict) else []
            error_evidence = "\n".join(str(item) for item in errors if item)
            text_is_limit = (
                bool(text) and callable(classifier) and
                classifier(RuntimeError(text)) and
                not self.extract_script(text, generated))
            errors_are_limit = (
                bool(error_evidence) and callable(classifier) and
                classifier(RuntimeError(error_evidence)))
            if text_is_limit or errors_are_limit:
                raise RuntimeError((error_evidence or text)[:2000])
            return result
        except Exception as exc:
            if not (callable(classifier) and classifier(exc)):
                raise
            if self.batch_limit is not None:
                self.batch_limit.set()
            raise BackendLimitExhausted(self.agent_name, 0, exc) from exc

    def extract_script(self, text: str, generated: dict) -> str | None:
        if self.spec.kind in ("codex", "agy"):
            return self.module.extract_script(text, generated)
        return self.module.extract_script(text)


def discover_tasks(input_path: Path, image_dir: str, view_mode: str,
                   single_view: str, instances: list[str] | None,
                   limit: int | None):
    if not input_path.is_dir():
        raise SystemExit(f"--input_path is not a directory: {input_path}")
    roots = ([input_path] if (input_path / image_dir).is_dir() else
             sorted(path for path in input_path.iterdir() if path.is_dir()))
    if instances:
        keep = set(instances)
        roots = [path for path in roots if path.name in keep]
        missing = sorted(keep - {path.name for path in roots})
        if missing:
            raise SystemExit(f"Unknown --instances under {input_path}: {missing}")
    tasks = []
    for root in roots:
        render_dir = root / image_dir
        if not render_dir.is_dir():
            continue
        if view_mode == "single":
            images = [render_dir / single_view]
            if not images[0].is_file():
                raise SystemExit(
                    f"Missing single GT image for {root.name}: {images[0]}")
        else:
            images = sorted(
                path for path in render_dir.iterdir()
                if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS)
            if not images:
                raise SystemExit(
                    f"No GT images found for {root.name} in {render_dir}")
        tasks.append((root.name, images))
    if limit is not None:
        tasks = tasks[:limit]
    if not tasks:
        raise SystemExit(f"No benchmark tasks with {image_dir!r} under {input_path}")
    return tasks


def initial_prompt(base_prompt: str, n_target: int) -> str:
    return (
        f"{n_target} GT reference image(s) of one target object are attached. "
        "Inspect all of them before writing the reconstruction.\n\n"
        f"{base_prompt}"
    )


def editing_prompt(previous_script: str, n_current: int, n_target: int) -> str:
    return f"""The attached images are ordered as follows: first {n_current} image(s)
are renders of the CURRENT generated script, followed by {n_target} GT reference
image(s) of the TARGET object. They depict the current reconstruction and the
desired object respectively.

Here is the complete current Blender script:
```python
{previous_script}
```

Compare the current renders against every GT view. Identify the largest visible
geometry, proportion, placement, silhouette, or missing-part errors, then edit
the script to correct them. Preserve useful detail already present. Return the
COMPLETE corrected self-contained Blender Python script, not a diff. Do not use
ellipsis and do not omit unchanged code. Output no prose outside the script.
"""


def feedback_views(rendered: list[Path], view_mode: str,
                   single_view: str) -> list[Path]:
    """Use view-aligned current renders for the next edit.

    The renderer always produces four views. Single-image experiments expose
    only the render matching their one GT view; multi-image experiments expose
    all four.
    """
    if view_mode == "multi":
        return list(rendered)
    matched = [path for path in rendered if path.name == single_view]
    return matched or list(rendered[:1])


def render_feedback(base_prompt: str, failed_script: str, record: dict) -> str:
    error = (record.get("error") or "(no Blender error was recorded)").strip()
    return f"""{base_prompt}

--- PREVIOUS SCRIPT FAILED TO RENDER ---
The previous script failed in Blender with status {record.get('status')}.

Failed script:
```python
{failed_script}
```

Blender error:
{error}

Fix the failure and return the COMPLETE corrected script. Output no prose
outside the script.
"""


def parse_feedback(base_prompt: str, reply: str) -> str:
    return f"""{base_prompt}

--- PREVIOUS REPLY WAS NOT A PARSABLE PYTHON SCRIPT ---
The previous reply could not be extracted as Python. Return one complete,
self-contained Blender Python script and no prose. Do not abbreviate it.

Previous reply:
{reply[-4000:]}
"""


def json_dump(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False,
                               default=str) + "\n")


def checkpoint_settings(args, view_mode: str, prompt_text: str) -> dict:
    pooled_qwen = args.agent in QWEN3_8_MAX_PREVIEW_ALIASES
    return {
        # Regional qwen3.8-max-preview endpoints are interchangeable capacity
        # for the same model run. Keep endpoint selection out of checkpoint
        # identity so either --agent alias can continue the shared pool.
        "agent": output_pool_alias(args.agent),
        "view_mode": view_mode,
        "editing_iterations": args.iterations,
        "total_iterations": args.iterations + 1,
        "max_render_retries": args.max_render_retries,
        "thinking_depth": args.thinking_depth,
        "texture_renders": args.texture_renders,
        "image_dir": args.image_dir,
        "single_view": args.single_view if view_mode == "single" else None,
        "render_samples": args.render_samples,
        "render_resolution": args.render_resolution,
        "render_engine": args.render_engine,
        "prompt_sha256": hashlib.sha256(prompt_text.encode()).hexdigest(),
        "qwen_base_url": (args.base_url if args.agent in VLLM_ENDPOINT_DEFAULTS
                          and not pooled_qwen
                          else None),
        "qwen_model": (args.model_name if args.agent in VLLM_ENDPOINT_DEFAULTS
                       else None),
    }


def load_checkpoint(path: Path, settings: dict, overwrite: bool):
    if overwrite or not path.is_file():
        return {"settings": settings, "iterations": [], "status": "incomplete"}
    try:
        saved = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read checkpoint {path}: {exc}") from exc
    saved_settings = saved.get("settings")
    if isinstance(saved_settings, dict):
        # Limit retries are now deliberately disabled batch-wide. They do not
        # affect generated artifacts, so discard the legacy settings to keep
        # old checkpoints resumable.
        saved_settings.pop("max_limit_retries", None)
        saved_settings.pop("limit_retry_delay", None)
        saved_agent = saved_settings.get("agent")
        if saved_agent in QWEN3_8_MAX_PREVIEW_ALIASES:
            # Normalize checkpoints written before the two regional aliases
            # began sharing one pool.
            saved_settings["agent"] = output_pool_alias(saved_agent)
            saved_settings["qwen_base_url"] = None
    if saved_settings != settings:
        raise RuntimeError(
            f"Checkpoint settings differ at {path}; pass --overwrite to restart")
    return saved


def restore_previous(module, log: dict):
    if not log.get("iterations"):
        return None
    previous = log["iterations"][-1]
    code_path = Path(previous["code_path"])
    renders_dir = Path(previous["renders_dir"])
    views = module.rendered_views(renders_dir)
    if not code_path.is_file() or not views:
        raise RuntimeError(
            "Checkpoint's last committed script/render is missing; "
            "pass --overwrite to restart")
    return {"code": code_path.read_text().rstrip(), "code_path": code_path,
            "renders_dir": renders_dir, "views": views}


def publish_latest(task: str, out_dir: Path, accepted: dict,
                   prompt_text: str) -> None:
    (out_dir / f"{task}.py").write_text(accepted["code"] + "\n")
    (out_dir / "prompt.txt").write_text(prompt_text.rstrip() + "\n")
    final_renders = out_dir / "renders"
    final_renders.mkdir(exist_ok=True)
    for old in final_renders.iterdir():
        if old.is_file():
            old.unlink()
    for source in accepted["renders_dir"].iterdir():
        if source.is_file():
            shutil.copy2(source, final_renders / source.name)


def run_task(backend: Backend, task: str, target_views: list[Path],
             prompt_text: str, out_dir: Path, args, view_mode: str,
             batch_limit: threading.Event | None = None) -> str:
    out_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = out_dir / "process.json"
    settings = checkpoint_settings(args, view_mode, prompt_text)
    log = load_checkpoint(checkpoint_path, settings, args.overwrite)
    # Also backfill completed tasks created before usage.json existed.
    write_instance_usage(out_dir, task, output_pool_alias(args.agent))
    total_iterations = args.iterations + 1
    if log.get("status") == "complete" and \
            len(log.get("iterations", [])) == total_iterations:
        print(f"  [{task}] skip (complete; use --overwrite to rerun)")
        return "SKIPPED"

    previous = restore_previous(backend.module, log)
    start = len(log.get("iterations", []))
    if start:
        print(f"  [{task}] resume at iteration {start}/{total_iterations}")

    current_entry = None
    try:
        for iteration in range(start, total_iterations):
            if iteration == 0:
                base_prompt = initial_prompt(prompt_text, len(target_views))
                role_images = [("GT reference", target_views)]
                phase = "generation"
            else:
                assert previous is not None
                current_views = feedback_views(
                    previous["views"], view_mode, args.single_view)
                base_prompt = editing_prompt(
                    previous["code"], len(current_views), len(target_views))
                role_images = [("current render", current_views),
                               ("GT reference", target_views)]
                phase = "editing"

            current_entry = {"iteration": iteration, "phase": phase,
                             "attempts": []}
            attempt_prompt = base_prompt
            accepted = None
            for attempt in range(args.max_render_retries + 1):
                scope = f"{task}.iter{iteration}.attempt{attempt}"
                if batch_limit is not None and batch_limit.is_set():
                    raise BatchLimitStop(
                        "another parallel instance hit the provider limit")
                print(f"  [{task}] iter {iteration}/{args.iterations} "
                      f"attempt {attempt}/{args.max_render_retries}: "
                      f"{phase} with {args.agent}", flush=True)
                agent_started = time.monotonic()
                try:
                    text, raw, generated = backend.ask(
                        attempt_prompt, role_images, scope)
                finally:
                    agent_seconds = time.monotonic() - agent_started
                    log["agent_seconds"] = (
                        float(log.get("agent_seconds") or 0.0) + agent_seconds)
                raw_path = out_dir / f"{task}.iter{iteration}.attempt{attempt}.json"
                if isinstance(raw, dict):
                    raw["agent_seconds"] = round(agent_seconds, 3)
                json_dump(raw_path, raw)

                attempt_record = {
                    "attempt": attempt,
                    "raw_path": str(raw_path),
                    "agent_seconds": round(agent_seconds, 3),
                }
                script = backend.extract_script(text, generated)
                if not script:
                    attempt_record["status"] = "ERR_PARSE"
                    current_entry["attempts"].append(attempt_record)
                    attempt_prompt = parse_feedback(base_prompt, text)
                    continue

                code_path = out_dir / f"{task}.iter{iteration}.attempt{attempt}.py"
                code_path.write_text(script.rstrip() + "\n")
                renders_dir = (out_dir / "iteration_renders" /
                               f"iter{iteration}" / f"attempt{attempt}")
                record = backend.module.render_script(code_path, renders_dir, args)
                views = backend.module.rendered_views(renders_dir)
                attempt_record.update({
                    "status": record.get("status"),
                    "code_path": str(code_path),
                    "renders_dir": str(renders_dir),
                    "n_meshes": record.get("n_meshes"),
                    "n_views_rendered": record.get("n_views_rendered"),
                    "error": record.get("error"),
                    "latency_s": record.get("latency_s"),
                })
                current_entry["attempts"].append(attempt_record)
                print(f"  [{task}] iter {iteration} attempt {attempt}: "
                      f"render {record.get('status')}", flush=True)
                if record.get("status") == "OK" and views:
                    accepted = {"code": script.rstrip(), "code_path": code_path,
                                "renders_dir": renders_dir, "views": views}
                    break
                attempt_prompt = render_feedback(base_prompt, script, record)

            if accepted is None:
                current_entry["status"] = "ERR_NO_RUNNABLE"
                log["pending_iteration"] = current_entry
                log["status"] = "incomplete"
                atomic_write_json(checkpoint_path, log)
                write_instance_usage(out_dir, task, output_pool_alias(args.agent))
                return "ERR_NO_RUNNABLE"

            current_entry.update({
                "status": "OK",
                "code_path": str(accepted["code_path"]),
                "renders_dir": str(accepted["renders_dir"]),
            })
            log["iterations"].append(current_entry)
            log.pop("pending_iteration", None)
            log.pop("error", None)
            previous = accepted
            publish_latest(task, out_dir, accepted, prompt_text)
            atomic_write_json(checkpoint_path, log)
            write_instance_usage(out_dir, task, output_pool_alias(args.agent))
            current_entry = None

        log["status"] = "complete"
        atomic_write_json(checkpoint_path, log)
        write_instance_usage(out_dir, task, output_pool_alias(args.agent))
        return "OK"
    except BatchLimitStop as exc:
        log["status"] = "paused_limit"
        if current_entry is not None:
            current_entry["status"] = "PAUSED_BATCH_LIMIT"
            log["pending_iteration"] = current_entry
        log["error"] = {
            "type": type(exc).__name__,
            "message": str(exc),
            "reason": "peer_provider_limit",
            "limit_retries": 0,
            "total_provider_attempts": 0,
            "resume_iteration": len(log.get("iterations", [])),
        }
        atomic_write_json(checkpoint_path, log)
        write_instance_usage(out_dir, task, output_pool_alias(args.agent))
        raise
    except BackendLimitExhausted as exc:
        log["status"] = "paused_limit"
        if current_entry is not None:
            current_entry["status"] = "PAUSED_LIMIT"
            log["pending_iteration"] = current_entry
        log["error"] = {
            "type": type(exc).__name__,
            "message": str(exc),
            "reason": "provider_limit",
            "limit_retries": exc.retries,
            "total_provider_attempts": exc.total_attempts,
            "resume_iteration": len(log.get("iterations", [])),
        }
        atomic_write_json(checkpoint_path, log)
        write_instance_usage(out_dir, task, output_pool_alias(args.agent))
        raise
    except BaseException as exc:
        log["status"] = "incomplete"
        if current_entry is not None:
            log["pending_iteration"] = current_entry
        log["error"] = {"type": type(exc).__name__, "message": str(exc),
                        "resume_iteration": len(log.get("iterations", []))}
        atomic_write_json(checkpoint_path, log)
        write_instance_usage(out_dir, task, output_pool_alias(args.agent))
        raise


def build_parser(view_mode: str) -> argparse.ArgumentParser:
    mode_name = "single-image" if view_mode == "single" else "multi-image"
    parser = argparse.ArgumentParser(
        description=f"Sequential {mode_name} image-to-3D generation and editing")
    add_data_path(parser, DEFAULT_INPUT_PATH, dest="input_path")
    add_output_dir(
        parser, GRADED_EXP_DIR / "outputs",
        help_text="Base output directory; setting, texture mode, agent, and "
        "instance subdirectories are appended (default: %(default)s)",
    )
    add_agent(parser)
    parser.add_argument("--iterations", type=nonnegative_int, default=3,
                        help="Number of sequential editing iterations after "
                             "one initial generation (default: %(default)s)")
    parser.add_argument("--max-render-retries", type=nonnegative_int, default=3,
                        help="Retries after a parse/render failure; 3 means at "
                             "most 4 attempts per iteration (default: %(default)s)")
    parser.add_argument("--max-limit-retries", type=nonnegative_int, default=3,
                        help=argparse.SUPPRESS)
    parser.add_argument("--limit-retry-delay", type=float, default=60.0,
                        help=argparse.SUPPRESS)
    add_texture_renders(parser)
    parser.add_argument("--single-view", default=VIEW_NAMES[0],
                        help="Filename used by the single-image runner")
    parser.add_argument("--prompt-path", type=Path, default=DEFAULT_PROMPT_PATH,
                        help="Python module defining build_prompt(texture_renders) "
                             "(default: %(default)s)")
    add_tasks(parser, dest="instances")
    parser.add_argument("--limit", type=positive_int, default=None)
    parser.add_argument("--num-parallel", "--num_parallel", type=positive_int,
                        default=8,
                        help="Number of benchmark instances to run at once "
                             "(default: %(default)s)")
    parser.add_argument("--thinking-depth", default=None)
    parser.add_argument("--gen-max-tokens", type=positive_int, default=16384)
    parser.add_argument("--timeout", type=positive_int, default=1800)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--blender", default=default_blender())
    parser.add_argument("--render-timeout", type=positive_int, default=240)
    parser.add_argument("--render-samples", type=positive_int, default=64)
    parser.add_argument("--render-resolution", type=positive_int, default=512)
    parser.add_argument("--render-engine", default="CYCLES",
                        choices=("CYCLES", "BLENDER_EEVEE"))
    parser.add_argument("--base-url",
                        default=None,
                        help="provider base URL for Qwen or MiniMax (default: "
                             "provider environment or built-in official URL)")
    parser.add_argument("--model-name",
                        default=None,
                        help="provider model name (default: provider "
                             "environment or built-in model)")
    parser.add_argument("--dry-run", action="store_true",
                        help="resolve tasks and output paths without running")
    return parser


def validate_runtime(args, parser: argparse.ArgumentParser) -> None:
    spec = AGENT_SPECS[args.agent]
    if args.agent in VLLM_ENDPOINT_DEFAULTS:
        default_url, default_model = VLLM_ENDPOINT_DEFAULTS[args.agent]
        if args.agent == "minimax-m3":
            args.base_url = (args.base_url
                             or os.environ.get("MINIMAX_OPENAI_BASE_URL")
                             or default_url)
            args.model_name = (args.model_name
                               or os.environ.get("MINIMAX_MODEL_NAME")
                               or default_model)
        else:
            args.base_url = (args.base_url
                             or os.environ.get("QWEN3_OPENAI_BASE_URL")
                             or default_url)
            args.model_name = (args.model_name
                               or os.environ.get("QWEN3_MODEL")
                               or default_model)
    if args.thinking_depth is None:
        args.thinking_depth = configured_thinking_depth(spec.config_agent)
    efforts = supported_efforts(spec.config_agent)
    if args.thinking_depth not in efforts:
        parser.error(f"--thinking-depth for {args.agent} must be one of {efforts}")
    if args.limit_retry_delay < 0:
        parser.error("--limit-retry-delay must be non-negative")
    if not args.dry_run:
        blender = (shutil.which(args.blender)
                   if os.sep not in args.blender else args.blender)
        if not blender or not Path(blender).is_file():
            parser.error(f"Blender executable not found: {args.blender!r}")
        args.blender = str(Path(blender).resolve())
    if not args.prompt_path.is_file():
        parser.error(f"Prompt file not found: {args.prompt_path}")


def main(view_mode: str, argv=None) -> int:
    if view_mode not in ("single", "multi"):
        raise ValueError(f"unknown view mode: {view_mode!r}")
    try:
        getattr(sys.stdout, "reconfigure")(line_buffering=True)
        getattr(sys.stderr, "reconfigure")(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    parser = build_parser(view_mode)
    args = parser.parse_args(argv)
    args.image_dir = ("color_renders" if args.texture_renders
                      else "grey_renders")
    validate_runtime(args, parser)
    prompt_text = load_prompt(args.prompt_path, args.texture_renders)
    tasks = discover_tasks(args.input_path, args.image_dir, view_mode,
                           args.single_view, args.instances, args.limit)

    run_root = output_run_root(
        args.output_dir, view_mode, args.texture_renders, args.agent)
    spec = AGENT_SPECS[args.agent]
    print(f"Agent:      {args.agent} ({spec.model})")
    print(f"Input:      {args.input_path}")
    print(f"Output:     {run_root}")
    print(f"Views:      {view_mode} ({args.image_dir}; "
          f"texture_renders={args.texture_renders})")
    print(f"Iterations: {args.iterations + 1} total (1 generation + "
          f"{args.iterations} edits; one candidate each)")
    print(f"Retries:    {args.max_render_retries} after initial attempt")
    print("Limits:     fail fast; stop queue and checkpoint in-flight instances")
    print(f"Tasks:      {len(tasks)}")
    print(f"Parallel:   {args.num_parallel}")

    if args.dry_run:
        for task, target_views in tasks:
            print(f"  [{task}] views={len(target_views)} output={run_root / task}")
        return 0

    batch_limit = threading.Event()

    def run_one(item):
        task, target_views = item
        out_dir = run_root / task
        if batch_limit.is_set():
            print(f"  [{task}] skip (batch stopped by provider limit)")
            return task, "SKIPPED_BATCH_LIMIT"
        # Backends may hold a live CLI/API interface, so each concurrent
        # instance gets its own backend object. The shared state directory only
        # contains agent-wide no-memory settings and is safe to initialize
        # idempotently.
        backend = Backend(args.agent, args, batch_limit)
        backend.initialize(run_root / "_state")
        try:
            status = run_task(
                backend, task, target_views, prompt_text, out_dir, args,
                view_mode, batch_limit)
        except BackendLimitExhausted as exc:
            # Broadcast before this worker returns, so the executor cannot
            # start a queued task and reach its first model call in the gap.
            batch_limit.set()
            status = f"PAUSED_LIMIT: {exc}"
            print(f"  [{task}] {status}", file=sys.stderr)
            print(f"  [{task}] resumable checkpoint: {out_dir / 'process.json'}",
                  file=sys.stderr)
        except BatchLimitStop as exc:
            status = f"PAUSED_LIMIT: {exc}"
            print(f"  [{task}] {status}", file=sys.stderr)
            print(f"  [{task}] resumable checkpoint: {out_dir / 'process.json'}",
                  file=sys.stderr)
        except Exception as exc:
            status = f"ERR_BACKEND: {type(exc).__name__}: {exc}"
            print(f"  [{task}] {status}", file=sys.stderr)
        return task, status

    statuses = {}
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=args.num_parallel) as executor:
        futures = [executor.submit(run_one, item) for item in tasks]
        for future in concurrent.futures.as_completed(futures):
            task, status = future.result()
            statuses[task] = status

    print("\nSummary:")
    for task, _ in tasks:
        status = statuses[task]
        print(f"  {task:<45} {status}")
    latency = write_runtime_latency_summary(run_root)
    print(runtime_latency_line(latency))
    if any(str(status).startswith("PAUSED_LIMIT")
           for status in statuses.values()):
        return 75
    return 1 if any(str(status).startswith("ERR")
                    for status in statuses.values()) else 0


__all__ = ["AGENT_CHOICES", "BackendLimitExhausted", "BatchLimitStop",
           "DEFAULT_INPUT_PATH", "main"]
