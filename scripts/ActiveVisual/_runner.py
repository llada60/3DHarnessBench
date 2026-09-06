#!/usr/bin/env python3
"""ActiveVisual -- one CLI-agent run per task against two Blender MCP servers.

For every task under <tasks_root> this does, in order:

  1. resolve the task's inputs (prompt.txt + ref.glb) from the config, defaulting
     to the task dir itself and falling back to <input_root>/<task>;
  2. start a dedicated remote-Blender stack via scripts/start-remote-blender.sh
     with --random-ports (two free ports) and --glb <ref.glb>, and read the two
     ports back off its `PORTS ...` handoff line;
  3. so the viewport_only Blender holds the reference model plus the grading
     camera/light rig while full_access starts as an empty grading-rig workspace
     -- both verified over the add-on sockets before the agent
     is allowed to start;
  4. open a NEW CLI session wired to both MCP servers (codex+gpt-5.6-sol by
     default; Qwen Code+Qwen3.8 Max Preview, Claude Code+Fable 5/Opus 5, Kimi Code+K3,
     or agy+Gemini via config/--agent);
  5-6. send it ONE message: the port instruction (rendered from
     instruction.tmpl) with prompt.txt appended below it,
  7. wait for the CLI to finish (timeout); Qwen resumes the same session up to
     five times after a transient API failure or a missing complete script,
  8. export the session with the CLI's own mechanism, save the full_access
     .blend, copy the stack logs, and write ActiveVisual.run.json --
     everything into <tasks_root>/<agent>/<task>/.

RESUME: when the CLI stops because its account hit a usage limit (or the
harness timeout fires) the task is checkpointed instead of merely failing --
both Blenders are saved to <task>/checkpoint/ and the CLI's session id is
recorded. A usage limit always ends the current invocation; re-running the same
command later resumes the saved conversation and scenes. Qwen may automatically
start another attempt for other resumable failures.
See checkpoint.py, and `--fresh` / `--no-resume` to opt out.

SKIP: a task that already has a complete output -- its ActiveVisual.run.json
says ok, its final.blend is there, and no checkpoint is waiting -- is left
alone rather than run again, so the same sweep can simply be re-issued until
everything is done. `--rerun` (or `[run].skip_completed = false`) turns that off.

Usage:
    pixi run python scripts/ActiveVisual/run.py [options]
    pixi run python scripts/ActiveVisual/run.py --agent qwen3-8-max-preview --tasks fish
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from types import MethodType

CLI_AGENTS_DIR = Path(__file__).resolve().parents[2] / "cli_agents"
sys.path.insert(0, str(CLI_AGENTS_DIR))

import agents                                        # noqa: E402
import checkpoint                                    # noqa: E402
from blender_ipc import (BlenderIPCError, configure_gpu_environment,  # noqa: E402
                         export_official_text_block, official_scene_summary,
                         prepare_reference_scene,
                         save_blend, save_official_blend, scene_summary)
import official_source                              # noqa: E402

RAW_AGENTS_DIR = Path(__file__).resolve().parents[2] / "raw_agents"
if str(RAW_AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(RAW_AGENTS_DIR))
from usage_accounting import (agent_latency_metadata,  # noqa: E402
                              mcp_agent_seconds)

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SKILL_NAME = "blender-gt-reconstruction"
SKILL_SOURCE = REPO / "skills" / SKILL_NAME
# Runner-owned publication contract. Agents maintain reconstruction_gt.py in
# Blender; this harness exports that existing Text datablock as <instance>.py.
RECONSTRUCTION_TEXT_BLOCK = "reconstruction_gt.py"
START_SCRIPT = HERE / "start-remote-blender.sh"
STOP_SCRIPT = HERE / "stop-remote-blender.sh"


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------
def load_config(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def resolve(path_like: str | os.PathLike) -> Path:
    """Repo-relative unless already absolute."""
    path = Path(path_like)
    return path if path.is_absolute() else (REPO / path)


def file_content_digest(path: Path) -> str:
    """Hash file bytes only; staged benchmark inputs may be renamed."""
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(HERE / "config.toml"),
                        help="TOML config (default: %(default)s)")
    parser.add_argument("--task", action="append", dest="tasks", metavar="NAME",
                        help="run only this task (repeatable)")
    parser.add_argument("--agent", choices=sorted(agents.AGENTS),
                        help="override [agent].kind")
    parser.add_argument("--model", help="override the agent's model")
    parser.add_argument("--timeout", type=int, help="override [run].timeout_sec")
    parser.add_argument("--fixed-ports", action="store_true",
                        help="use the configured ports instead of random ones")
    parser.add_argument("--xpra", action="store_true", default=None,
                        help="enable xpra remote viewing (default: disabled)")
    parser.add_argument("--no-memory", dest="no_memory", action="store_true",
                        default=None,
                        help="run the CLI with its cross-session memory off, "
                             "reads and writes (default; see [agent].no_memory)")
    parser.add_argument("--memory", dest="no_memory", action="store_false",
                        help="leave the CLI's own memory enabled")
    parser.add_argument("--no-resume", dest="resume", action="store_false",
                        default=None,
                        help="ignore any checkpoint and do not write one "
                             "(default: resume, see [resume].enabled)")
    parser.add_argument("--resume", dest="resume", action="store_true",
                        help="continue an interrupted task from its checkpoint "
                             "(the default)")
    parser.add_argument("--fresh", action="store_true",
                        help="discard an existing checkpoint and run the task "
                             "from the start (previous attempts are archived "
                             "under <task>/attempts/)")
    parser.add_argument(
        "--fresh-incompatible",
        action="store_true",
        help="start over only when an existing checkpoint cannot be resumed; "
             "compatible checkpoints still resume",
    )
    parser.add_argument("--max-attempts", type=int,
                        help="override [resume].max_attempts")
    parser.add_argument("--rerun", action="store_true",
                        help="run tasks that already have a complete output "
                             "instead of skipping them (see "
                             "[run].skip_completed)")
    parser.add_argument("--dry-run", action="store_true",
                        help="resolve tasks/inputs and exit without launching")
    return parser.parse_args(argv)


# --------------------------------------------------------------------------
# step 1 -- task + input discovery
# --------------------------------------------------------------------------
def discover_tasks(cfg: dict, cli_tasks: list[str] | None) -> list[str]:
    if cli_tasks:
        return cli_tasks
    listed = cfg["run"].get("tasks") or []
    if listed:
        return list(listed)
    prompt_name = cfg["run"]["prompt_file"]
    found: list[str] = []
    # Inputs are authoritative. Output directories are run artifacts and are
    # now nested as <agent>/<task>, so they must not shadow newly added inputs
    # or be mistaken for task names during discovery.
    for root_key in ("input_root", "tasks_root"):
        root = resolve(cfg["run"][root_key])
        if not root.is_dir():
            continue
        # A leading underscore marks a utility task (`_mcpcheck`, the wiring
        # probe) -- discoverable by name, but never swept into a bare
        # "run everything" invocation. `[run] tasks` and --task still take it.
        found = sorted(d.name for d in root.iterdir()
                       if d.is_dir() and not d.name.startswith("_")
                       and (d / prompt_name).is_file())
        if found:
            break
    return found


RUN_RECORD = "ActiveVisual.run.json"
# The saved .blend is the scene artifact. ``skip_reason`` also requires the
# published <instance>.py, while completion requires a non-empty, exact-name
# reconstruction_gt.py Text datablock in the running workspace Blender.
COMPLETE_ARTIFACTS = ("final.blend",)


def skill_input_paths() -> list[Path]:
    """Files whose content defines the reconstruction prompt."""
    skill = SKILL_SOURCE / "SKILL.md"
    if not skill.is_file():
        raise RuntimeError(f"required task skill not found: {skill}")
    return sorted(path for path in SKILL_SOURCE.rglob("*") if path.is_file())


def stage_task_skill(workdir: Path) -> Path:
    """Install the bundled reconstruction skill in the task workspace."""
    destination = workdir / ".agents" / "skills" / SKILL_NAME
    shutil.copytree(SKILL_SOURCE, destination, dirs_exist_ok=True)
    return destination / "SKILL.md"


def task_prompt(prompt: Path) -> str:
    """Invoke the staged skill, retaining non-graded task constraints."""
    invocation = (
        f"Use the ${SKILL_NAME} skill installed at "
        f".agents/skills/{SKILL_NAME}/SKILL.md. Read SKILL.md and the access-mode "
        "reference it selects before touching Blender, then complete the GT "
        "reconstruction."
    )
    if prompt.resolve() == (SKILL_SOURCE / "SKILL.md").resolve():
        return invocation
    return (
        f"{invocation}\n\nAdditional task-specific instructions:\n\n"
        f"{prompt.read_text(errors='replace')}"
    )


def organize_agent_artifacts(task_dir: Path, task: str, *, prompt_name: str,
                             ref_name: str | None) -> dict[str, object]:
    """Move agent-created screenshots and scratch files into log trees."""
    task_dir = task_dir.resolve()
    protected_dirs = {
        ".agents", ".remote-blender", "attempts", "checkpoint", "logs",
        "session", "log_renders", "log_artifacts",
    }
    protected_files = {
        prompt_name, f"{task}.py", "final.blend", "final.blend1",
        "instruction.txt", "ports.env", RUN_RECORD, "usage.json",
    }
    if ref_name:
        protected_files.add(ref_name)

    candidates: list[Path] = []
    for root, dirnames, filenames in os.walk(task_dir):
        root_path = Path(root)
        if root_path == task_dir:
            dirnames[:] = [name for name in dirnames
                           if name not in protected_dirs]
        for name in filenames:
            source = root_path / name
            relative = source.relative_to(task_dir)
            if len(relative.parts) == 1 and name in protected_files:
                continue
            candidates.append(source)

    if not candidates:
        return {"renders": 0, "artifacts": 0, "run_id": ""}

    base_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_id = base_id
    counter = 1
    while ((task_dir / "log_renders" / run_id).exists() or
           (task_dir / "log_artifacts" / run_id).exists()):
        counter += 1
        run_id = f"{base_id}-{counter}"

    counts = {"renders": 0, "artifacts": 0}
    for source in sorted(candidates):
        if not source.exists():
            continue
        relative = source.relative_to(task_dir)
        category = "renders" if source.suffix.lower() == ".png" else "artifacts"
        destination = (
            task_dir
            / ("log_renders" if category == "renders" else "log_artifacts")
            / run_id / relative
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        counts[category] += 1

    for root, _dirnames, _filenames in os.walk(task_dir, topdown=False):
        root_path = Path(root)
        if root_path == task_dir:
            continue
        relative = root_path.relative_to(task_dir)
        if relative.parts[0] in protected_dirs:
            continue
        try:
            root_path.rmdir()
        except OSError:
            pass

    return {**counts, "run_id": run_id}


def skip_reason(task: str, task_dir: Path, cfg: dict,
                args: argparse.Namespace, digest: str = "",
                completed_reference_matches: bool = False) -> str:
    """Why this task is left alone, or "" when it is to be run.

    `--rerun` and `--fresh` both say "do this task again", and a utility task
    (`_mcpcheck`, the wiring probe) is never skipped: it is asked for by name to
    find out whether the wiring works *now*, so a PASS from last week is not an
    answer to that.
    """
    if args.rerun or args.fresh or task.startswith("_"):
        return ""
    if not bool(cfg["run"].get("skip_completed", True)):
        return ""
    out_of_limit = checkpoint.out_of_limit_skip_reason(
        task_dir, record=RUN_RECORD)
    if out_of_limit:
        return out_of_limit
    ignore_prompt_changes = bool(
        completed_reference_matches and
        cfg["run"].get("skip_completed_ignore_prompt_changes", False))
    # A successful graded render is a durable completion receipt. The wrapper
    # only creates it after the harness returned ok=true, so it can recover a
    # completion whose top-level record was later archived/overwritten.
    if ignore_prompt_changes:
        render_log = task_dir / "renders" / "render_log.json"
        try:
            render_ok = json.loads(render_log.read_text()).get("status") == "OK"
        except (OSError, ValueError):
            render_ok = False
        archived_script = any(
            (task_dir / "attempts").glob(f"attempt-*/{task}.py"))
        saved_scene = ((task_dir / "final.blend").is_file() or
                       (task_dir / "final.blend1").is_file())
        if render_ok and archived_script and saved_scene:
            return "successful graded render receipt survived later attempts"
    if not (task_dir / f"{task}.py").is_file():
        return ""
    if ignore_prompt_changes:
        digest = ""
    return checkpoint.already_done(task_dir, record=RUN_RECORD,
                                   artifacts=COMPLETE_ARTIFACTS, digest=digest,
                                   require_blend_python=True)


def resolve_inputs(cfg: dict, task: str, task_dir: Path) -> tuple[Path, Path | None]:
    """Resolve benchmark inputs or the source harness's task-local inputs."""
    graded = cfg.get("_graded") or {}
    if graded:
        input_dir = Path(graded["input_path"]) / task
        if Path(graded["input_path"]).name == task:
            input_dir = Path(graded["input_path"])
        suffix = ".glb" if graded["texture_renders"] else "_grey.glb"
        return Path(graded["prompt_path"]), input_dir / f"{task}{suffix}"
    run_cfg, overrides = cfg["run"], (cfg.get("tasks") or {}).get(task, {})
    input_dir = resolve(run_cfg["input_root"]) / task

    def pick(kind: str, filename: str) -> Path | None:
        if overrides.get(kind):
            path = resolve(overrides[kind])
            if not path.is_file():
                raise SystemExit(f"[{task}] configured {kind} not found: {path}")
            return path
        for candidate in (task_dir / filename, input_dir / filename):
            if candidate.is_file():
                return candidate
        return None

    prompt = pick("prompt", run_cfg["prompt_file"])
    if prompt is None:
        raise SystemExit(
            f"[{task}] no {run_cfg['prompt_file']} in {task_dir} or {input_dir}")
    return prompt, pick("ref", run_cfg["ref_file"])


def stage_inputs(task_dir: Path, prompt: Path, ref: Path | None,
                 cfg: dict) -> tuple[Path, Path | None]:
    """Copy inputs into the task dir when they came from elsewhere."""
    task_dir.mkdir(parents=True, exist_ok=True)
    staged = []
    for src, name in ((prompt, cfg["run"]["prompt_file"]), (ref, cfg["run"]["ref_file"])):
        if src is None:
            staged.append(None)
            continue
        dst = task_dir / name
        if src.resolve() != dst.resolve():
            shutil.copy2(src, dst)
        staged.append(dst)
    return staged[0], staged[1]


# --------------------------------------------------------------------------
# step 2 -- the Blender stack
# --------------------------------------------------------------------------
def start_stack(cfg: dict, task_dir: Path, ref: Path | None, *,
                random_ports: bool, xpra: bool, log_path: Path,
                official_checkout: Path,
                light_power: float,
                restore: dict[str, Path] | None = None
                ) -> tuple[dict[str, int], Path, dict]:
    """Launch both Blenders and return (ports, runtime_dir, display metadata).

    `restore` maps a role ("full_access" / "viewport_only") to a .blend the
    instance should OPEN instead of starting from the factory scene -- how a
    resumed task gets its scenes back. A restored viewport_only replaces the
    GLB import, so the reference comes back exactly as the agent left it
    (camera included) rather than being re-imported and re-framed.
    """
    restore = restore or {}
    runtime_dir = task_dir / ".remote-blender"
    ports_file = task_dir / "ports.env"
    blender_cfg = cfg["blender"]
    argv = ["bash", str(START_SCRIPT),
            "--runtime-dir", str(runtime_dir),
            "--ports-file", str(ports_file),
            "--official-source", str(official_checkout),
            "--display", str(blender_cfg.get("display", "auto")),
            "--light-power", str(light_power)]
    if random_ports:
        argv.append("--random-ports")
    else:
        argv += ["--full-access-port", str(blender_cfg["full_access_port"]),
                 "--viewport-only-port", str(blender_cfg["viewport_only_port"])]
    if xpra:
        argv.append("--xpra")
    if "full_access" in restore:
        argv += ["--full-access-blend", str(restore["full_access"])]
    if "viewport_only" in restore:
        argv += ["--viewport-only-blend", str(restore["viewport_only"])]
    elif ref is not None:
        # Clears every object in the viewport_only scene, then imports the GLB.
        argv += ["--glb", str(ref)]

    env = os.environ.copy()
    gpu_device = configure_gpu_environment(blender_cfg, env)
    if gpu_device is not None:
        print(f"    blender GPU visibility: physical device(s) {gpu_device}")
    configured = blender_cfg.get("binary", "auto")
    if configured != "auto":
        binary = Path(configured)
        binary = binary if binary.is_absolute() else REPO / binary
        if not (binary.is_file() and os.access(binary, os.X_OK)):
            raise RuntimeError(f"[blender].binary is not executable: {binary}")
        # start-remote-blender.sh has no flag for this; BLENDER_BIN is its hook,
        # and BLENDER_MCP_BLENDER_BIN outranks it, so pin both.
        env["BLENDER_BIN"] = env["BLENDER_MCP_BLENDER_BIN"] = str(binary)
        print(f"    blender: {binary}")

    print(f"    launching stack: {' '.join(argv[1:])}")
    # Port allocation is already concurrency-safe in the shell launcher, but
    # DISPLAY=auto is a check-then-start operation. Serialize only startup so
    # concurrent ActiveVisual processes cannot select the same free display;
    # their Blender/agent runs proceed concurrently after startup completes.
    start_lock = Path(os.getenv("TMPDIR", "/tmp")) / "viewport-test-stack-start.lock"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with start_lock.open("a+") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            completed = subprocess.run(argv, cwd=str(REPO), text=True,
                                       capture_output=True, timeout=900,
                                       env=env)
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        # start_stack has not returned yet, so run_task's normal finally block
        # does not own this runtime_dir. Tear down any partially launched stack
        # here as well, including on Ctrl-C during startup.
        stdout = getattr(exc, "stdout", "") or ""
        stderr = getattr(exc, "stderr", "") or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        log_path.write_text(stdout + stderr + "\nstartup interrupted; cleanup attempted\n")
        try:
            stop_stack(runtime_dir, log_path.parent / "stop-remote-blender.log")
        finally:
            try:
                archive_stack_logs(runtime_dir, log_path.parent)
            except Exception:                              # noqa: BLE001
                pass
        raise
    log_path.write_text(completed.stdout + completed.stderr)
    if completed.returncode != 0:
        # The launcher rolls back what it started, but not always completely
        # (a failed health check has left Xvfb and D-Bus up). run_task's finally
        # cannot help here -- it never received this runtime_dir -- so tear the
        # partial stack down before reporting the failure.
        try:
            stop_stack(runtime_dir, log_path.parent / "stop-remote-blender.log")
        except Exception:                                  # noqa: BLE001
            pass
        try:
            archive_stack_logs(runtime_dir, log_path.parent)
        except Exception:                                  # noqa: BLE001
            pass
        raise RuntimeError(
            f"start-remote-blender.sh failed ({completed.returncode}); see {log_path}")

    ports, display = parse_ports(completed.stdout, ports_file, runtime_dir)
    display["gpu_device"] = gpu_device if gpu_device is not None else "inherit"
    print(f"    ports: full_access={ports['full_access']} "
          f"viewport_only={ports['viewport_only']}")
    print(f"    Xvfb: DISPLAY={display['xvfb_display']}")
    if xpra:
        print(f"    xpra directory: {display['xpra_dir']}")
        print(f"    xpra socket: {display['xpra_socket']}")
        print("    xpra attach: "
              f"xpra attach ssh://USER@SERVER/{display['display_number']}")
    else:
        print("    xpra: disabled")
    return ports, runtime_dir, display


def parse_ports(stdout: str, ports_file: Path,
                runtime_dir: Path) -> tuple[dict[str, int], dict]:
    """Read ports, X display and socket directory (ports file as fallback)."""
    fields = None
    for line in reversed(stdout.splitlines()):
        if "PORTS " in line and "full_access=" in line:
            fields = dict(part.split("=", 1) for part in line.split()
                          if "=" in part)
            break
    if fields is not None:
        full_port = fields["full_access"]
        viewport_port = fields["viewport_only"]
        display_number = fields["display"]
        socket_dir = fields.get("socket_dir", str(runtime_dir))
    elif ports_file.is_file():
        fields = dict(line.split("=", 1) for line in
                      ports_file.read_text().splitlines() if "=" in line)
        full_port = fields["FULL_ACCESS_PORT"]
        viewport_port = fields["VIEWPORT_ONLY_PORT"]
        display_number = fields["DISPLAY_NUMBER"]
        socket_dir = fields.get("SOCKET_DIR", str(runtime_dir))
    else:
        raise RuntimeError("could not determine the stack handoff from launcher output")

    xpra_dir = Path(socket_dir) / "xpra"
    return (
        {"full_access": int(full_port), "viewport_only": int(viewport_port)},
        {"display_number": int(display_number),
         "xvfb_display": f":{display_number}",
         "socket_dir": socket_dir,
         "xpra_dir": str(xpra_dir),
         "xpra_socket": str(xpra_dir / f"blender-{display_number}")},
    )


def stop_stack(runtime_dir: Path, log_path: Path) -> None:
    completed = subprocess.run(["bash", str(STOP_SCRIPT)], cwd=str(REPO), text=True,
                               capture_output=True, timeout=300,
                               env={**os.environ, "RUNTIME_DIR": str(runtime_dir)})
    log_path.write_text(completed.stdout + completed.stderr)
    if completed.returncode != 0:
        raise RuntimeError(
            f"stop-remote-blender.sh failed ({completed.returncode}); see {log_path}")


def archive_stack_logs(runtime_dir: Path, log_dir: Path) -> None:
    """Replace the previous stack-log snapshot with this run's complete logs.

    ``copytree(..., dirs_exist_ok=True, symlinks=True)`` cannot overwrite a
    symlink left by an earlier run. Replacing this exact generated directory
    also prevents stale files from being mistaken for current-run output.
    """
    source = runtime_dir / "logs"
    if not source.is_dir():
        return
    destination = log_dir / "stack"
    if destination.is_symlink() or destination.is_file():
        destination.unlink()
    elif destination.is_dir():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, symlinks=True)


# --------------------------------------------------------------------------
# step 3 -- verify the two scenes are what the task expects
# --------------------------------------------------------------------------
def check_scenes(ports: dict[str, int], ref: Path | None,
                 phase: str = "before") -> dict:
    """Snapshot both scenes. Only the pre-run snapshot of a FRESH run is checked
    against expectations -- afterwards the agent is *supposed* to have changed
    full_access (changing the initial grading rig is normal), so warning there would be
    noise, and a resumed run starts from the agent's own scenes by design."""
    full = official_scene_summary(ports["full_access"])
    viewport = scene_summary(ports["viewport_only"])
    notes = []
    if phase != "before":
        return {"full_access": full, "viewport_only": viewport, "warnings": notes}
    if ref is not None and not viewport["meshes"]:
        notes.append("viewport_only holds no mesh after the GLB import")
    if ref is not None and "Cube" in viewport["objects"]:
        notes.append("viewport_only still has the factory Cube — GLB import "
                     "may not have replaced the scene")
    expected_rig = {"EvalCam", "Fill", "Key", "Rim"}
    if not expected_rig.issubset(full["objects"]):
        notes.append("full_access is missing part of the grading scene rig")
    for note in notes:
        print(f"    warning: {note}")
    return {"full_access": full, "viewport_only": viewport, "warnings": notes}


def use_official_workspace_server(agent: agents.BaseAgent, source: Path) -> None:
    """Replace only the reconstruction-side MCP with the official server."""
    uv = shutil.which("uv") or "/usr/local/bin/uv"

    def servers(_self, ports: dict[str, int]) -> dict[str, dict]:
        return agents.mcp_server_specs(
            REPO, ports["full_access"], ports["viewport_only"],
            agent.full_name, agent.viewport_name, uv_bin=uv,
            official_source=source)

    agent.servers = MethodType(servers, agent)


# --------------------------------------------------------------------------
# steps 5-6 -- the instruction that names the ports
# --------------------------------------------------------------------------
def compose_input(instruction: str, prompt: str) -> str:
    """The single message the CLI receives: instruction, rule, task prompt."""
    return f"{instruction.rstrip()}\n\n{'-' * 60}\n\n{prompt.lstrip()}"


def render_instruction(agent: agents.BaseAgent, ports: dict[str, int],
                       scenes: dict, ref: Path | None) -> str:
    template = (HERE / "instruction.tmpl").read_text()
    return template.format(
        full_access_server=agent.full_name,
        viewport_only_server=agent.viewport_name,
        full_access_port=ports["full_access"],
        viewport_only_port=ports["viewport_only"],
        full_access_scene=scenes["full_access"]["scene"],
        viewport_only_scene=scenes["viewport_only"]["scene"],
        ref_name=ref.name if ref else "(none)",
    )


def render_resume_instruction(agent: agents.BaseAgent, ports: dict[str, int],
                             scenes: dict, ref: Path | None, *, attempt: int,
                             restored: dict[str, Path]) -> str:
    """The message a resumed session gets: the SAME conversation, new ports.

    Both Blenders are new processes on new ports, so the MCP servers behind the
    two tool names are freshly connected -- the tool names are unchanged, which
    is why the agent can simply carry on. The wording deliberately avoids the
    phrases `agents.classify` looks for: every CLI echoes its input back into
    the event stream it also writes its errors to.
    """
    template = (HERE / "resume_instruction.tmpl").read_text()
    return template.format(
        full_access_server=agent.full_name,
        viewport_only_server=agent.viewport_name,
        full_access_port=ports["full_access"],
        viewport_only_port=ports["viewport_only"],
        full_access_scene=scenes["full_access"]["scene"],
        viewport_only_scene=scenes["viewport_only"]["scene"],
        full_access_objects=len(scenes["full_access"]["objects"]),
        ref_name=ref.name if ref else "(none)",
        attempt=attempt,
        restored=", ".join(sorted(restored)) or "(nothing)",
    )


# --------------------------------------------------------------------------
# one task, end to end
# --------------------------------------------------------------------------
def run_task(task: str, cfg: dict, args: argparse.Namespace) -> dict:
    started = time.time()
    kind = args.agent or cfg["agent"]["kind"]
    output_agent = cfg["run"].get("output_agent", kind)
    task_dir = resolve(cfg["run"]["tasks_root"]) / output_agent / task
    prompt_src, ref_src = resolve_inputs(cfg, task, task_dir)
    staged_ref_before = task_dir / cfg["run"]["ref_file"]
    reference_digest = (file_content_digest(ref_src)
                        if ref_src is not None else "")
    completed_reference_matches = bool(
        ref_src is not None and staged_ref_before.is_file() and
        file_content_digest(staged_ref_before) == reference_digest)
    if args.dry_run:                     # resolve only — never touch the task dir
        # No digest: it is taken over the STAGED inputs, and staging is the one
        # thing this mode must not do. A finished task whose prompt has since
        # been edited therefore shows up here as a skip and still runs for real.
        skip = skip_reason(
            task, task_dir, cfg, args,
            completed_reference_matches=completed_reference_matches)
        print(f"[{task}] output={task_dir.relative_to(REPO)} "
              f"prompt={prompt_src.relative_to(REPO)} "
              f"ref={ref_src.relative_to(REPO) if ref_src else '(none)'}"
              f"{' SKIP: ' + skip if skip else ''}")
        return {"task": task, "dry_run": True,
                "output_dir": str(task_dir), "skipped": bool(skip),
                "skip_reason": skip,
                "prompt": str(prompt_src), "ref": str(ref_src or "")}

    # In graded mode, completion intentionally survives later prompt/skill
    # edits. Check before staging so a skipped task is genuinely untouched.
    if cfg["run"].get("skip_completed_ignore_prompt_changes", False):
        skip = skip_reason(
            task, task_dir, cfg, args,
            completed_reference_matches=completed_reference_matches)
        if skip:
            print(f"[{task}] SKIP: already completed — {skip}")
            return {"task": task, "ok": True, "skipped": True,
                    "output_dir": str(task_dir), "skip_reason": skip,
                    "seconds": round(time.time() - started, 1)}

    prompt_path, ref_path = stage_inputs(task_dir, prompt_src, ref_src, cfg)
    digest = checkpoint.inputs_digest(
        [prompt_path, ref_path, *skill_input_paths()])

    # Already done? Then this task is not touched again: no Blender stack, no
    # CLI session, and above all no write into the existing output -- the run
    # record and artifacts that are already there stay exactly as they are.
    skip = skip_reason(
        task, task_dir, cfg, args, digest,
        completed_reference_matches=completed_reference_matches)
    if skip:
        print(f"[{task}] SKIP: already completed — {skip}")
        print("    pass --rerun (or [run].skip_completed = false) to run it again")
        return {"task": task, "ok": True, "skipped": True,
                "output_dir": str(task_dir), "skip_reason": skip,
                "seconds": round(time.time() - started, 1)}

    stage_task_skill(task_dir)

    session_dir = task_dir / "session"
    log_dir = task_dir / "logs"

    print(f"[{task}] output={task_dir.relative_to(REPO)} "
          f"prompt={prompt_path.relative_to(REPO)} "
          f"ref={ref_path.relative_to(REPO) if ref_path else '(none)'}")

    agent_cfg = dict(cfg["agent"].get(kind, {}))
    if args.model:
        agent_cfg["model"] = args.model
    # --no-memory / --memory beat [agent].no_memory, which defaults to on: a
    # task must not answer from -- or leave anything in -- the CLI's own
    # cross-session memory. See agents.py for what each CLI disables.
    agent_cfg["no_memory"] = (args.no_memory if args.no_memory is not None
                              else bool(cfg["agent"].get("no_memory", True)))
    agent_cfg["token_total_limit"] = cfg["agent"].get("token_total_limit", 0)
    timeout = args.timeout or int(cfg["run"]["timeout_sec"])
    agent = agents.build(kind, agent_cfg, REPO, timeout)
    official_checkout = official_source.ensure_source(REPO, cfg)
    use_official_workspace_server(agent, official_checkout)
    xpra_enabled = (args.xpra if args.xpra is not None
                    else bool(cfg["blender"].get("xpra", False)))

    # Resume: is there an interrupted run of this exact task to continue?
    settings = checkpoint.settings(cfg, enabled=args.resume,
                                  max_attempts=args.max_attempts)
    checkpoint_kind = (cfg.get("_graded") or {}).get("resume_kind", kind)
    checkpoint_digest = checkpoint.resume_input_digest(
        task_dir,
        digest,
        allow_prompt_changes=bool(
            (cfg.get("_graded") or {}).get("resume_across_prompt_changes")),
        reference_matches=completed_reference_matches,
    )
    decision = checkpoint.decide(task_dir, kind=checkpoint_kind,
                                 model=agent_cfg.get("model", ""),
                                 digest=checkpoint_digest,
                                 settings=settings, fresh=args.fresh)
    if (decision["mode"] == "blocked" and
            getattr(args, "fresh_incompatible", False)):
        print(
            f"    resume: {decision['note']}; --fresh-incompatible starts "
            "this task over"
        )
        decision = checkpoint.decide(
            task_dir,
            kind=checkpoint_kind,
            model=agent_cfg.get("model", ""),
            digest=checkpoint_digest,
            settings=settings,
            fresh=True,
        )
    if decision["note"]:
        print(f"    resume: {decision['note']}")
    if decision["mode"] == "blocked":
        print("    BLOCKED: a checkpoint for this task is waiting but cannot be "
              "used by this invocation.")
        print("    Pass --fresh to start the task over (previous attempts are "
              "archived, not deleted), or match the checkpoint above.")
        return {"task": task, "ok": False, "output_dir": str(task_dir),
                "error": f"checkpoint unusable: {decision['note']}",
                "resume": {"mode": "blocked", "note": decision["note"]},
                "seconds": round(time.time() - started, 1)}
    resuming = decision["mode"] == "resume"
    # The finished attempt's session export, logs, run record and .blend are
    # moved aside before this one starts: every adapter writes fixed file names,
    # so otherwise the new attempt would overwrite the evidence of the old.
    if decision.get("archive"):
        archived = checkpoint.rotate(
            task_dir, decision["archive"],
            keep=agents.PERSISTENT_SESSION_ENTRIES,
            files=(RUN_RECORD, "final.blend",
                   f"{task}.py", "instruction.txt", "ports.env", "usage.json"))
        print(f"    archived attempt {decision['archive']}: "
              f"{archived.relative_to(REPO)}")
    restore: dict[str, Path] = {}
    if resuming:
        # Bump the attempt counter before the work starts, so a crash during
        # startup cannot make the next run reuse this attempt's archive slot.
        checkpoint.mark(task_dir, checkpoint.RESUMABLE,
                        attempts=decision["attempt"],
                        resumed_utc=checkpoint.now())
        restore = checkpoint.stage_restore(
            task_dir, checkpoint.blend_paths(task_dir, decision["state"]))
        if not restore:
            print("    warning: the checkpoint holds no .blend — the session is "
                  "resumed but both Blenders start from a default scene")
        print(f"    resuming attempt {decision['attempt']} of session "
              f"{decision['state']['session'].get('resume_target')} "
              f"(restored: {', '.join(sorted(restore)) or 'nothing'})")

    record: dict = {
        "task": task,
        "output_dir": str(task_dir),
        "started_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "inputs": {"prompt": str(prompt_path), "ref": str(ref_path or "")},
        # Content digest of exactly those inputs: what makes a later run able to
        # tell "this task is done" from "this task was done, but with a
        # different prompt". See checkpoint.already_done.
        "inputs_digest": digest,
        "reference_digest": reference_digest,
        "agent": {"kind": kind, "model": agent_cfg.get("model", ""),
                  "thinking_depth": agent_cfg.get("thinking_depth", "high"),
                  "no_memory": agent_cfg["no_memory"]},
        "blender": {"xpra": xpra_enabled},
        "resume": {"mode": decision["mode"], "attempt": decision["attempt"],
                   "restored": sorted(restore),
                   "note": decision["note"]},
    }
    runtime_dir = None
    try:
        ports, runtime_dir, display = start_stack(
            cfg, task_dir, ref_path,
            random_ports=(not args.fixed_ports) and bool(cfg["blender"]["random_ports"]),
            xpra=xpra_enabled,
            log_path=log_dir / "start-remote-blender.log",
            official_checkout=official_checkout,
            light_power=(1.0 if (cfg.get("_graded") or {}).get(
                "texture_renders", True) else 0.5),
            restore=restore)
        record["ports"] = ports
        record["blender"].update(display)
        # Fresh bootstrap already installed EvalCam + the grading lights. This
        # follow-up selects the reference and frames only the interactive view;
        # a restored scene stays exactly where the agent last left it.
        if (ref_path is not None and cfg["blender"].get("prepare_reference", True)
                and "viewport_only" not in restore):
            record["reference_scene"] = prepare_reference_scene(
                ports["viewport_only"],
                add_light=bool(cfg["blender"].get("reference_light", False)))
            print(f"    reference framed: "
                  f"{record['reference_scene']['meshes']} "
                  f"dims={record['reference_scene']['max_dimensions']}")
        record["scenes_before"] = check_scenes(
            ports, ref_path, phase="resume" if resuming else "before")

        if resuming:
            # The conversation already holds the task; what it cannot know is
            # that both Blenders are new processes on new ports.
            instruction = render_resume_instruction(
                agent, ports, record["scenes_before"], ref_path,
                attempt=decision["attempt"], restored=restore)
        else:
            instruction = render_instruction(
                agent, ports, record["scenes_before"], ref_path)
        (task_dir / "instruction.txt").write_text(instruction)
        # One message, not two turns: the port instruction is prepended to the
        # task prompt so the CLI sees the wiring and the task together.
        agent_input = (instruction if resuming and not settings["repeat_prompt"]
                       else compose_input(instruction, task_prompt(prompt_src)))
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "agent_input.txt").write_text(agent_input)

        print(f"    running {kind} ({agent_cfg.get('model', '?')}) — "
              f"{len(agent_input)} chars "
              f"(instruction + {prompt_path.name})")
        recon_path = task_dir / f"{task}.py"

        def completion_check(current):
            try:
                return export_official_text_block(
                    ports["full_access"], str(recon_path),
                    preferred=RECONSTRUCTION_TEXT_BLOCK,
                    allow_fallback=False)
            except BlenderIPCError as exc:
                return {"path": None, "matched": False, "candidates": [],
                        "error": str(exc)}

        run, completion_summary = agents.run_with_continuations(
            agent, agent_input, ports, workdir=task_dir,
            session_dir=session_dir,
            resume=decision["state"].get("session") if resuming else None,
            completion_check=completion_check)
        agents.classify(run, settings["extra_limit_patterns"])
        record["run"] = run.as_dict()
        (session_dir / "tool_calls.json").write_text(
            json.dumps(run.tool_calls, indent=2))
        record["tool_calls_file"] = str(session_dir / "tool_calls.json")
        # What the session cost, from the CLI's own accounting. Mirrored at the
        # top level because it is a headline number of the run, and `scope` says
        # whether it covers the whole (possibly resumed) session or one attempt.
        record["usage"] = run.usage or None
        for turn in run.turns:
            print(f"    turn {turn.label}: rc={turn.returncode} "
                  f"{turn.seconds:.0f}s"
                  f"{' (timed out)' if turn.timed_out else ''}")
        print(f"    {agents.usage_line(run.usage)}")
        for note in (run.usage or {}).get("notes") or []:
            print(f"      note: {note}")
        if run.limit_reason:
            print(f"    stopped early — {run.limit_reason}: "
                  f"{run.limit_evidence[:200] or 'no message captured'}")
        # `mcp` rather than `server`: agy's transcript records an MCP call
        # without naming the server it went to, and this run's config holds
        # nothing but the two Blenders (see AgyAgent._tool_calls).
        mcp_calls = [c for c in run.tool_calls if c.get("mcp") or c.get("server")]
        record["mcp_tool_call_count"] = len(mcp_calls)
        print(f"    tool calls: {len(run.tool_calls)} "
              f"({len(mcp_calls)} to the Blender MCP servers)")
        if not mcp_calls:
            # Either the task genuinely needed no Blender work, or the CLI never
            # got the servers. Worth shouting about: the run "succeeds" either way.
            print("    warning: no MCP tool calls attributed to either Blender "
                  "server — check that this CLI actually loaded them")

        # step 8 -- artifacts
        blend_path = task_dir / "final.blend"
        try:
            record["blend"] = {"path": str(blend_path),
                               "detail": save_official_blend(
                                   ports["full_access"], str(blend_path))}
        except BlenderIPCError as exc:
            record["blend"] = {"path": None, "error": str(exc)}
            print(f"    warning: could not save .blend: {exc}")
        # Publish the exact existing `reconstruction_gt.py` Text datablock as
        # <instance>.py. The exporter never creates or modifies the datablock.
        record["reconstruction"] = completion_summary
        if completion_summary.get("error"):
            print(f"    warning: could not export {recon_path.name}: "
                  f"{completion_summary['error']}")
        else:
            summary = record["reconstruction"]
            if summary["matched"]:
                print(f"    {recon_path.name}: {summary['bytes']} bytes from "
                      f"text block '{summary['name']}' ({summary['how']})")
            else:
                print(f"    warning: no '{RECONSTRUCTION_TEXT_BLOCK}' text block "
                      f"in the .blend — {recon_path.name} not written "
                      f"(text blocks: {', '.join(summary['candidates']) or 'none'})")
        # `run`, not `agent`: the helper reads AgentRun.limit_reason, mirroring
        # the official harness's call.
        if agents.accept_token_limited_completion(run, record["reconstruction"]):
            record["token_limit_completion"] = True
            print("    token total limit: accepting the latest exported "
                  "reconstruction script as final")
        out_of_limit = bool(
            run.limit_reason == "token_total_limit" and
            not record["reconstruction"].get("matched"))
        if out_of_limit:
            record["out_of_limit"] = True
            record["out_of_limit_reason"] = run.limit_evidence
            path = checkpoint.mark_out_of_limit(
                task_dir, reason=run.limit_reason, evidence=run.limit_evidence)
            record["resume"].update({"checkpoint": "out_of_limit",
                                     "reason": run.limit_reason,
                                     "path": str(path)})
            print("    OUT_OF_LIMIT: no reconstruction script was available; "
                  "future runs will skip this instance")
        if not task.startswith("_") and not record["reconstruction"].get("matched"):
            candidates = record["reconstruction"].get("candidates") or []
            if (not run.completion_error and
                    not run.limit_hit and
                    run.limit_reason not in ("api_error", "api_timeout")):
                run.completion_error = "missing complete reconstruction script"
                run.limit_reason = "incomplete_artifact"
                run.limit_evidence = (
                    f"Blender capture block: {RECONSTRUCTION_TEXT_BLOCK}; "
                    f"candidates: {', '.join(candidates) or 'none'}"
                )
            if run.completion_error:
                print("    incomplete: no non-empty reconstruction_gt.py Text "
                      "datablock; keeping the session resumable")
        try:
            record["scenes_after"] = check_scenes(ports, ref_path, phase="after")
        except BlenderIPCError as exc:
            record["scenes_after"] = {"error": str(exc)}
        # Still inside the try, and before the finally stops the stack: saving
        # the checkpoint means saving both live Blenders.
        if not out_of_limit:
            record["resume"].update(checkpoint.commit(
                task_dir, run, harness="ActiveVisual", task=task,
                kind=checkpoint_kind,
                model=agent_cfg.get("model", ""), digest=digest,
                decision=decision, settings=settings,
                roles=("full_access", "viewport_only"),
                save_scene=lambda role, path: (
                    save_official_blend(ports[role], str(path))
                    if role == "full_access"
                    else save_blend(ports[role], str(path)))))
        record["run"] = run.as_dict()
        record["ok"] = bool(run.ok)
    except Exception as exc:                              # noqa: BLE001
        record["ok"] = False
        record["error"] = f"{type(exc).__name__}: {exc}"
        print(f"    ERROR: {record['error']}")
    finally:
        if runtime_dir is not None:
            try:
                stop_stack(runtime_dir, log_dir / "stop-remote-blender.log")
                record["cleanup"] = {"stack_stopped": True}
            except Exception as exc:                          # noqa: BLE001
                record["ok"] = False
                message = f"{type(exc).__name__}: {exc}"
                record["cleanup"] = {"stack_stopped": False,
                                     "stop_error": message}
                print(f"    ERROR stopping stack: {message}")
            # Archive only after shutdown so Blender/Xvfb/xpra have flushed
            # their final output. A copy failure is recorded but must never
            # prevent shutdown or the final run record from being written.
            try:
                archive_stack_logs(runtime_dir, log_dir)
                record.setdefault("cleanup", {})["logs_archived"] = True
            except Exception as exc:                          # noqa: BLE001
                record["ok"] = False
                message = f"{type(exc).__name__}: {exc}"
                record.setdefault("cleanup", {}).update(
                    {"logs_archived": False, "archive_error": message})
                print(f"    ERROR archiving stack logs: {message}")

    # Keep helper files available while a CLI conversation is resumable. Once
    # it has genuinely ended, separate its inspection images and other scratch
    # output before the graded wrapper creates the canonical ``renders/``.
    if record.get("resume", {}).get("checkpoint") != "written":
        try:
            organized = organize_agent_artifacts(
                task_dir, task, prompt_name=prompt_path.name,
                ref_name=ref_path.name if ref_path is not None else None)
            record["organized_agent_artifacts"] = organized
            if organized["renders"] or organized["artifacts"]:
                print("    organized agent workspace: "
                      f"{organized['renders']} PNG(s) -> log_renders, "
                      f"{organized['artifacts']} other file(s) -> "
                      f"log_artifacts ({organized['run_id']})")
        except Exception as exc:                              # noqa: BLE001
            record["organize_error"] = f"{type(exc).__name__}: {exc}"
            print(f"    warning: could not organize agent workspace: {exc}")

    record["seconds"] = round(time.time() - started, 1)
    (task_dir / RUN_RECORD).write_text(json.dumps(record, indent=2))
    usage = record.get("usage") or {}
    agent_seconds = mcp_agent_seconds(task_dir, RUN_RECORD)
    (task_dir / "usage.json").write_text(json.dumps({
        "version": 1,
        "instance": task,
        "agent": kind,
        "agent_seconds": agent_seconds,
        "timing": agent_latency_metadata(),
        "api_calls": usage.get("api_requests"),
        "tokens": usage.get("tokens"),
        "cost_usd": usage.get("cost_usd"),
        "source": usage.get("source"),
        "scope": usage.get("scope"),
    }, indent=2) + "\n")
    return record


def main(argv=None) -> int:
    args = parse_args(argv)
    cfg = load_config(resolve(args.config))
    tasks = discover_tasks(cfg, args.tasks)
    if not tasks:
        print("no tasks found (need <input_root>/<task>/prompt.txt, an explicit "
              "--task, or [run].tasks)", file=sys.stderr)
        return 2
    print(f"ActiveVisual: {len(tasks)} task(s): {', '.join(tasks)}")

    records = []
    kind = args.agent or cfg["agent"]["kind"]
    resume_settings = checkpoint.settings(
        cfg, enabled=args.resume, max_attempts=args.max_attempts)
    for task in tasks:
        task_args = argparse.Namespace(**vars(args))
        while True:
            record = run_task(task, cfg, task_args)
            if (kind != "qwen-tokenplan" or
                    record.get("resume", {}).get("checkpoint") != "written" or
                    record.get("resume", {}).get("reason") == "usage_limit"):
                break
            attempt = int(record.get("resume", {}).get("attempt", 1))
            if (resume_settings["max_attempts"] and
                    attempt >= resume_settings["max_attempts"]):
                print(f"[{task}] automatic resume stopped at the total attempt "
                      f"limit ({resume_settings['max_attempts']})")
                break
            print(f"[{task}] checkpoint saved; automatically starting attempt "
                  f"{attempt + 1} in the same Qwen session")
            # --fresh applies to the user-requested first attempt only. The
            # checkpoint just written by that attempt must now be resumed.
            task_args.fresh = False
        records.append(record)
    if args.dry_run:
        return 0

    failed = [r["task"] for r in records if not r.get("ok")]
    resumable = [r["task"] for r in records
                 if r.get("resume", {}).get("checkpoint") == "written"]
    skipped = [r["task"] for r in records if r.get("skipped")]
    print("\n=== summary")
    for record in records:
        if record.get("skipped"):
            print(f"  {record['task']}: SKIPPED — already completed "
                  f"({record.get('skip_reason', '')})")
            continue
        resume = record.get("resume", {})
        state = ""
        if resume.get("checkpoint") == "written":
            state = f" CHECKPOINTED ({resume.get('reason')})"
        elif resume.get("mode") == "resume":
            state = f" resumed attempt {resume.get('attempt')}"
        elif resume.get("mode") == "blocked":
            state = " BLOCKED by an unusable checkpoint"
        usage = record.get("usage") or {}
        total = (usage.get("tokens") or {}).get("total")
        requests = usage.get("api_requests")
        print(f"  {record['task']}: ok={record.get('ok')} "
              f"{record.get('seconds')}s "
              f"tools={record.get('run', {}).get('tool_call_count', 0)} "
              f"tokens={'?' if total is None else format(total, ',')} "
              f"api={'?' if requests is None else requests}{state}")
    if skipped:
        print(f"skipped, already complete ({len(skipped)}/{len(records)}): "
              f"{', '.join(skipped)}")
    if resumable:
        if kind == "qwen-tokenplan":
            print("Qwen checkpoint retained after reaching the configured "
                  f"attempt limit: {', '.join(resumable)}")
        else:
            print(f"checkpointed, re-run the same command to continue: "
                  f"{', '.join(resumable)}")
    if failed:
        print(f"failed: {', '.join(failed)}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
