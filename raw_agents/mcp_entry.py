"""Shared command-line orchestration for ActiveVisual and Full3DInteraction."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import shutil
import sys
from pathlib import Path

try:  # package import: ``import raw_agents.mcp_entry``
    from .mcp_runner import (
        DEFAULT_INPUT_PATH, DEFAULT_OUTPUT_PATH,
        GRADED_EXP_DIR, agent_spec, benchmark_instance,
        default_blender, discover_instances, nonnegative_int, output_root,
        positive_int, render_script,
    )
except ImportError:  # script entry points put raw_agents/ itself on sys.path
    from mcp_runner import (
        DEFAULT_INPUT_PATH, DEFAULT_OUTPUT_PATH,
        GRADED_EXP_DIR, agent_spec, benchmark_instance,
        default_blender, discover_instances, nonnegative_int, output_root,
        positive_int, render_script,
    )
try:
    from .common_cli import (add_agent, add_data_path, add_output_dir,
                             add_tasks, add_texture_renders)
except ImportError:
    from common_cli import (add_agent, add_data_path, add_output_dir,
                            add_tasks, add_texture_renders)

try:
    from .process_janitor import RunJanitor
except ImportError:
    from process_janitor import RunJanitor

try:
    from .usage_accounting import (runtime_latency_line,
                                   write_runtime_latency_summary)
except ImportError:
    from usage_accounting import (runtime_latency_line,
                                  write_runtime_latency_summary)


def build_parser(mode: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=f"Run the graded {mode} Blender MCP experiment")
    add_data_path(parser, DEFAULT_INPUT_PATH, dest="input_path")
    add_output_dir(parser, DEFAULT_OUTPUT_PATH)
    add_agent(parser)
    add_texture_renders(parser)
    add_tasks(parser)
    parser.add_argument("--limit", type=positive_int, default=None,
                        help="run only the first N selected instances")
    parser.add_argument("--num-parallel", "--num_parallel", type=positive_int,
                        default=4,
                        help="number of benchmark instances to run at once "
                             "(default: %(default)s)")
    parser.add_argument("--timeout", type=positive_int, default=9600,
                        help="seconds per CLI attempt (default: %(default)s)")
    parser.add_argument("--max-retries", type=nonnegative_int, default=5,
                        help="provider session reconnections after a timeout "
                             "before checkpointing (default: %(default)s)")
    parser.add_argument("--retry-delay", type=float, default=5.0,
                        help="initial seconds to wait before a provider session "
                             "reconnection (default: %(default)s)")
    parser.add_argument("--fresh", action="store_true",
                        help="start over and archive any compatible checkpoint")
    checkpoint_conflicts = parser.add_mutually_exclusive_group()
    checkpoint_conflicts.add_argument(
        "--fresh-incompatible",
        dest="fresh_incompatible",
        action="store_true",
        default=True,
        help="automatically archive and restart checkpoints that cannot be "
             "resumed; compatible checkpoints still resume (default)",
    )
    checkpoint_conflicts.add_argument(
        "--block-incompatible",
        dest="fresh_incompatible",
        action="store_false",
        help="leave incompatible checkpoints untouched and report BLOCKED",
    )
    parser.add_argument("--rerun", action="store_true",
                        help="run even if this configuration already completed")
    parser.add_argument("--resume", dest="resume", action="store_true",
                        default=True)
    parser.add_argument("--no-resume", dest="resume", action="store_false")
    parser.add_argument("--memory", dest="no_memory", action="store_false",
                        default=True)
    parser.add_argument("--no-memory", dest="no_memory", action="store_true")
    parser.add_argument("--xpra", action="store_true", default=False)
    parser.add_argument("--blender", default=default_blender())
    parser.add_argument("--render-timeout", type=positive_int, default=240)
    parser.add_argument("--render-samples", type=positive_int, default=64)
    parser.add_argument("--render-resolution", type=positive_int, default=512)
    parser.add_argument("--render-engine", default="CYCLES",
                        choices=("CYCLES", "BLENDER_EEVEE"))
    parser.add_argument("--dry-run", action="store_true")
    if mode == "Full3DInteraction":
        parser.add_argument("--no-download", action="store_true")
    return parser


def _resolve_blender(value: str, parser: argparse.ArgumentParser) -> str:
    found = shutil.which(value) if "/" not in value else value
    if not found or not Path(found).is_file():
        parser.error(f"Blender executable not found: {value!r}")
    return str(Path(found).resolve())


def configure(mode: str, runner, script_dir: Path, args) -> tuple[dict, list[str]]:
    cfg = runner.load_config(script_dir / "config.toml")
    spec = agent_spec(args.agent)
    # Both MCP modes share one task skill.  The runner stages the complete
    # directory (including references/) into each isolated task workspace and
    # sends a short, explicit invocation; keeping SKILL.md here as prompt_path
    # also makes the skill part of the checkpoint input identity.
    prompt = (GRADED_EXP_DIR / "skills" / "blender-gt-reconstruction" /
              "SKILL.md")
    run_root = output_root(args.output_dir, mode, args.texture_renders, args.agent)
    output_agent = run_root.name
    cfg["_graded"] = {
        "input_path": str(args.input_path.resolve()),
        "prompt_path": str(prompt.resolve()),
        "texture_renders": args.texture_renders,
        "agent_alias": args.agent,
        "output_agent": output_agent,
        "resume_kind": spec.cli_kind,
        "resume_across_prompt_changes": False,
    }
    cfg["run"]["input_root"] = str(args.input_path.resolve())
    output_key = "tasks_root" if mode == "ActiveVisual" else "output_root"
    # The harness appends output_agent/task; output_agent is the public alias.
    cfg["run"][output_key] = str(run_root.parent)
    cfg["run"]["output_agent"] = output_agent
    cfg["run"]["timeout_sec"] = args.timeout
    cfg["run"]["skip_completed"] = not args.rerun
    # A graded sweep's output namespace already fixes the mode, texture choice,
    # and public agent alias. Once an instance has completed successfully, keep
    # it complete even if the staged benchmark skill is edited before the sweep
    # is re-issued. Interrupted checkpoints remain digest-checked below.
    cfg["run"]["skip_completed_ignore_prompt_changes"] = True
    cfg["agent"]["kind"] = spec.cli_kind
    cfg["agent"]["no_memory"] = args.no_memory
    agent_cfg = cfg["agent"].setdefault(spec.cli_kind, {})
    agent_cfg["model"] = spec.model
    # A timed-out CLI process cannot be reused.  Re-open its persisted chat
    # session up to this many times before checkpointing the Blender scene and
    # releasing this parallel worker for the next instance.
    # `run_with_continuations` starts a fresh CLI process for each reconnect,
    # while retaining the task-local session store/session id.
    agent_cfg["timeout_reconnect_max"] = args.max_retries
    agent_cfg["session_retry_delay_sec"] = args.retry_delay
    if spec.cli_kind in ("qwen-tokenplan", "minimax-m3"):
        # Qwen and mmx additionally continue the same chat after a clean exit
        # before the required .py Text datablock exists. Qwen also uses this
        # budget for transient provider errors.
        agent_cfg["session_continue_max"] = args.max_retries
    cfg["resume"].update({
        "enabled": args.resume,
        "on_timeout": True,
        "on_error": True,
        # Zero leaves the checkpoint reusable across later invocations.
        "max_attempts": 0,
        "repeat_prompt": True,
        "defer_resume_to_next_run": True,
    })
    cfg["blender"]["binary"] = args.blender
    cfg["blender"]["xpra"] = args.xpra
    if "official" in cfg:
        cfg["official"]["cache_dir"] = str(script_dir / ".cache")
    instances = discover_instances(args.input_path, args.tasks, args.limit)
    return cfg, instances


def _active_visual_args(args, spec) -> argparse.Namespace:
    return argparse.Namespace(
        agent=spec.cli_kind, model=spec.model, timeout=args.timeout,
        xpra=args.xpra, no_memory=args.no_memory, resume=args.resume,
        max_attempts=None, fixed_ports=False, fresh=args.fresh,
        fresh_incompatible=args.fresh_incompatible,
        rerun=args.rerun, dry_run=args.dry_run,
    )


def _run_once(mode: str, runner, cfg: dict, instance: str, args,
              spec, fresh: bool) -> dict:
    if mode == "ActiveVisual":
        task_args = _active_visual_args(args, spec)
        task_args.fresh = fresh
        return runner.run_task(instance, cfg, task_args)
    checkpoint = runner.CHECKPOINT.settings(cfg, enabled=args.resume,
                                            max_attempts=None)
    return runner.run_task(
        cfg, instance, spec.cli_kind, spec.model, args.timeout, args.xpra,
        args.no_download, checkpoint, fresh=fresh, rerun=args.rerun,
        skip_completed=not args.rerun,
        fresh_incompatible=args.fresh_incompatible)


def _record_path(mode: str, directory: Path) -> Path:
    name = ("ActiveVisual.run.json" if mode == "ActiveVisual" else
            "Full3DInteraction.run.json")
    return directory / name


def _agent_limit_stop(record: dict) -> bool:
    """Whether this attempt hit provider quota and must wait for a later run."""
    run = record.get("run") or {}
    resume = record.get("resume") or {}
    return bool(run.get("limit_hit") or
                run.get("limit_reason") == "usage_limit" or
                resume.get("reason") == "usage_limit")


def _render_completed(mode: str, instance: str, directory: Path, args,
                      record: dict) -> bool:
    script = directory / f"{instance}.py"
    if not script.is_file():
        return False
    render_record = render_script(
        script, directory / "renders", blender=args.blender,
        timeout=args.render_timeout, samples=args.render_samples,
        resolution=args.render_resolution, engine=args.render_engine,
        light_power=1.0 if args.texture_renders else 0.5)
    record["final_render"] = render_record
    record_path = _record_path(mode, directory)
    if record_path.is_file():
        try:
            persisted = json.loads(record_path.read_text())
        except (OSError, json.JSONDecodeError):
            persisted = record
        persisted["final_render"] = render_record
        record_path.write_text(json.dumps(persisted, indent=2))
    return bool(render_record and render_record.get("status") == "OK")


def _run_instance(mode: str, runner, cfg: dict, instance: str, args, spec,
                  run_root: Path) -> tuple[str, bool]:
    """Run and render one instance without affecting sibling instances."""
    directory = run_root / instance
    try:
        record = _run_once(
            mode, runner, cfg, instance, args, spec, args.fresh)
    except BaseException as exc:  # preserve batch progress on one task
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        record = {"task": instance, "ok": False,
                  "output": str(directory),
                  "error": f"{type(exc).__name__}: {exc}"}
        print(f"  [{instance}] ERROR: {record['error']}", file=sys.stderr)
    if _agent_limit_stop(record):
        checkpoint_state = record.get("resume", {}).get("checkpoint")
        print(
            f"  [{instance}] agent limit reached; "
            f"checkpoint={checkpoint_state or 'unavailable'}; "
            "stopped. Re-run the same configuration to resume the saved "
            "session.",
            file=sys.stderr,
        )
    if record.get("skipped"):
        print(f"  [{instance}] SKIP: already completed")
        return instance, True
    render_ok = False
    if record.get("ok"):
        render_ok = _render_completed(mode, instance, directory, args, record)
    ok = bool(record.get("ok") and render_ok)
    if not ok:
        checkpoint = directory / "checkpoint" / "checkpoint.json"
        suffix = f"; checkpoint={checkpoint}" if checkpoint.is_file() else ""
        print(f"  [{instance}] FAILED{suffix}", file=sys.stderr)
    else:
        print(f"  [{instance}] OK: {directory / (instance + '.py')}")
    return instance, ok


def main(mode: str, runner, script_dir: Path, argv=None) -> int:
    if mode not in ("ActiveVisual", "Full3DInteraction"):
        raise ValueError(mode)
    parser = build_parser(mode)
    args = parser.parse_args(argv)
    if args.retry_delay < 0:
        parser.error("--retry-delay must be non-negative")
    if not args.dry_run:
        args.blender = _resolve_blender(args.blender, parser)
    cfg, instances = configure(mode, runner, script_dir, args)
    spec = agent_spec(args.agent)
    run_root = output_root(args.output_dir, mode, args.texture_renders,
                           args.agent)
    print(f"Mode:       {mode}")
    print(f"Agent:      {args.agent} -> {spec.cli_kind} ({spec.model})")
    print(f"Input:      {args.input_path.resolve()}")
    print(f"Output:     {run_root}")
    print(f"Reference:  {'<instance>.glb' if args.texture_renders else '<instance>_grey.glb'}")
    print("Retries:    disabled; every attempt stops and checkpoints once")
    print(f"Tasks:      {len(instances)}")
    print(f"Parallel:   {args.num_parallel}")

    # Keep dry-run handling here so both MCP modes have identical behavior.
    if args.dry_run:
        suffix = ".glb" if args.texture_renders else "_grey.glb"
        for instance in instances:
            source = benchmark_instance(args.input_path, instance)
            print(f"  [{instance}] ref={source / (instance + suffix)} "
                  f"output={run_root / instance}")
        return 0

    results = {}
    janitor_log = run_root / f".process-janitor-{os.getpid()}.log"
    with RunJanitor(janitor_log) as janitor:
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=args.num_parallel)
        futures = [
            executor.submit(_run_instance, mode, runner, cfg, instance, args,
                            spec, run_root)
            for instance in instances
        ]
        try:
            for future in concurrent.futures.as_completed(futures):
                instance, ok = future.result()
                results[instance] = ok
        except BaseException:
            # ThreadPoolExecutor.__exit__ waits for running jobs. Reap their
            # external processes first so Ctrl-C can actually unwind instead
            # of hanging behind a blocked model request.
            for future in futures:
                future.cancel()
            janitor.cleanup()
            executor.shutdown(wait=True, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True)
    latency = write_runtime_latency_summary(run_root)
    print(runtime_latency_line(latency))
    failed = [instance for instance in instances if not results[instance]]
    return 1 if failed else 0


__all__ = ["build_parser", "configure", "main"]
