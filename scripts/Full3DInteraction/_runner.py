#!/usr/bin/env python3
"""Run a fresh CLI session against Blender's one official MCP implementation."""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from types import MethodType

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SKILL_NAME = "blender-gt-reconstruction"
SKILL_SOURCE = REPO / "skills" / SKILL_NAME
# Runner-owned publication contract. Agents maintain reconstruction_gt.py in
# Blender; this harness exports that existing Text datablock as <instance>.py.
RECONSTRUCTION_TEXT_BLOCK = "reconstruction_gt.py"
CLI_AGENTS_DIR = REPO / "cli_agents"
AGENTS_FILE = CLI_AGENTS_DIR / "agents.py"
CHECKPOINT_FILE = CLI_AGENTS_DIR / "checkpoint.py"
BLENDER_IPC_FILE = CLI_AGENTS_DIR / "blender_ipc.py"
# Shared by both MCP modes so Blender stacks cannot race while selecting a
# port/display and bringing their listeners up.
START_LOCK = Path(os.getenv("TMPDIR", "/tmp")) / "active-visual-stack-start.lock"
SOURCE_LOCK = Path(os.getenv("TMPDIR", "/tmp")) / "official-blender-mcp-source.lock"

# Blender's window screenshot operators return an all-black framebuffer under
# Xvfb even though the 3D viewport itself is rendering.  Keep the official MCP
# interface and transport, but replace its VIEW_3D capture implementation with
# Blender's compositing-independent GPUOffScreen path.  Other editor types keep
# using the upstream screenshot operator.
HEADLESS_SCREENSHOT_MARKER = "# ActiveVisual Xvfb compatibility: GPUOffScreen"
HEADLESS_SCREENSHOT_STOCK = '''        with context.temp_override(window=window, area=area):
            try:
                bpy.ops.screen.screenshot_area(filepath=filepath_screenshot)
            except RuntimeError as ex:
                return Result(status="error", message=str(ex))
'''
HEADLESS_SCREENSHOT_REPLACEMENT = '''        if params.area_ui_type == "VIEW_3D":
            # ActiveVisual Xvfb compatibility: GPUOffScreen
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            space = area.spaces.active
            if region is None or space is None:
                return Result(status="error", message="No drawable VIEW_3D region")
            try:
                import gpu
                import numpy as np

                width, height = region.width, region.height
                offscreen = gpu.types.GPUOffScreen(width, height)
                try:
                    r3d = space.region_3d
                    offscreen.draw_view3d(
                        context.scene, context.view_layer, space, region,
                        r3d.view_matrix, r3d.window_matrix,
                        do_color_management=True,
                    )
                    buffer = offscreen.texture_color.read()
                finally:
                    offscreen.free()

                buffer.dimensions = width * height * 4
                pixels = np.asarray(buffer, dtype=np.float32) / 255.0
                image = bpy.data.images.new(
                    "official_mcp_viewport", width, height, alpha=True,
                )
                try:
                    image.pixels.foreach_set(pixels.ravel())
                    image.filepath_raw = filepath_screenshot
                    image.file_format = "PNG"
                    image.save()
                finally:
                    bpy.data.images.remove(image)
            except Exception as ex:  # noqa: BLE001 - returned through MCP
                return Result(status="error", message="Offscreen capture failed: " + str(ex))
        else:
            with context.temp_override(window=window, area=area):
                try:
                    bpy.ops.screen.screenshot_area(filepath=filepath_screenshot)
                except RuntimeError as ex:
                    return Result(status="error", message=str(ex))
'''


def load_module(name: str, path: Path):
    """Load one of ActiveVisual's modules by path.

    The CLI adapters and resume/checkpoint machinery are shared by both MCP
    modes because they drive the same CLIs and handle the same quota failures.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load shared module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


AGENTS = load_module("official_test_agents", AGENTS_FILE)
CHECKPOINT = load_module("official_test_checkpoint", CHECKPOINT_FILE)
# Only for the Blender-side source it builds; the transport here is send_code,
# not the BlenderMCP add-on socket blender_ipc otherwise speaks.
BLENDER_IPC = load_module("official_test_blender_ipc", BLENDER_IPC_FILE)

if str(REPO / "raw_agents") not in sys.path:
    sys.path.insert(0, str(REPO / "raw_agents"))
from usage_accounting import (agent_latency_metadata,  # noqa: E402
                              mcp_agent_seconds)


def resolve(value: str | os.PathLike) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPO / path


def load_config(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(HERE / "config.toml"))
    parser.add_argument("--task", action="append", dest="tasks")
    parser.add_argument("--agent", choices=sorted(AGENTS.AGENTS))
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=int)
    parser.add_argument("--xpra", action="store_true", default=None)
    parser.add_argument("--no-memory", dest="no_memory", action="store_true",
                        default=None,
                        help="run the CLI with its cross-session memory off, "
                             "reads and writes (default; see [agent].no_memory)")
    parser.add_argument("--memory", dest="no_memory", action="store_false",
                        help="leave the CLI's own memory enabled")
    parser.add_argument("--no-download", action="store_true",
                        help="do not automatically download a compatible official Blender")
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
                             "under <output>/attempts/)")
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
                             "instead of skipping them (see [run].skip_completed)")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


RUN_RECORD = "Full3DInteraction.run.json"
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


def organize_agent_artifacts(output: Path, task: str, *, prompt_name: str,
                             ref_name: str) -> dict[str, object]:
    """Move an agent's scratch output out of the instance root.

    The CLI runs with ``output`` as its cwd, so screenshots, helper scripts and
    ad-hoc exports otherwise remain mixed with the benchmark deliverables.
    Keep runner-owned state in place and preserve relative paths beneath a
    unique run directory so repeated runs neither collide nor lose evidence.
    """
    output = output.resolve()
    protected_dirs = {
        ".agents", "attempts", "checkpoint", "logs", "session",
        "log_renders", "log_artifacts",
    }
    protected_files = {
        prompt_name, ref_name, f"{task}.py", "final.blend", "final.blend1",
        RUN_RECORD, "usage.json",
    }
    candidates: list[Path] = []
    for root, dirnames, filenames in os.walk(output):
        root_path = Path(root)
        if root_path == output:
            dirnames[:] = [
                name for name in dirnames
                if name not in protected_dirs and not name.startswith(".runtime-")
            ]
        for name in filenames:
            source = root_path / name
            relative = source.relative_to(output)
            if len(relative.parts) == 1 and name in protected_files:
                continue
            candidates.append(source)

    if not candidates:
        return {"renders": 0, "artifacts": 0, "run_id": ""}

    base_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_id = base_id
    counter = 1
    while ((output / "log_renders" / run_id).exists() or
           (output / "log_artifacts" / run_id).exists()):
        counter += 1
        run_id = f"{base_id}-{counter}"

    counts = {"renders": 0, "artifacts": 0}
    for source in sorted(candidates):
        if not source.exists():
            continue
        relative = source.relative_to(output)
        category = "renders" if source.suffix.lower() == ".png" else "artifacts"
        destination_root = (
            output / ("log_renders" if category == "renders" else "log_artifacts")
            / run_id
        )
        destination = destination_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        counts[category] += 1

    # Remove only directories made empty by the moves. Runner-owned trees were
    # pruned above and are therefore never considered here.
    for root, dirnames, _filenames in os.walk(output, topdown=False):
        root_path = Path(root)
        if root_path == output:
            continue
        relative = root_path.relative_to(output)
        if relative.parts[0] in protected_dirs or \
                relative.parts[0].startswith(".runtime-"):
            continue
        try:
            root_path.rmdir()
        except OSError:
            pass

    return {**counts, "run_id": run_id}


def skip_reason(task: str, output: Path, cfg: dict, *, rerun: bool, fresh: bool,
                skip_completed: bool, digest: str = "",
                completed_reference_matches: bool = False) -> str:
    """Why this task is left alone, or "" when it is to be run.

    `--rerun` and `--fresh` both say "do this task again", and a utility task
    (`_mcpcheck`) is never skipped: it is run by name to find out whether the
    wiring works *now*.
    """
    if rerun or fresh or task.startswith("_") or not skip_completed:
        return ""
    out_of_limit = CHECKPOINT.out_of_limit_skip_reason(output, record=RUN_RECORD)
    if out_of_limit:
        return out_of_limit
    ignore_prompt_changes = bool(
        completed_reference_matches and
        cfg.get("run", {}).get(
            "skip_completed_ignore_prompt_changes", False))
    # Older graded runs can lose their top-level run record when a later
    # attempt is started. A successful final render is a durable receipt: the
    # graded wrapper only invokes it after the harness returned ok=true. Accept
    # that receipt when the matching scene backup and published script survived
    # in the attempt archive.
    if ignore_prompt_changes:
        render_log = output / "renders" / "render_log.json"
        try:
            render_ok = json.loads(render_log.read_text()).get("status") == "OK"
        except (OSError, ValueError):
            render_ok = False
        archived_script = any(
            (output / "attempts").glob(f"attempt-*/{task}.py"))
        saved_scene = ((output / "final.blend").is_file() or
                       (output / "final.blend1").is_file())
        if render_ok and archived_script and saved_scene:
            return "successful graded render receipt survived later attempts"
    if not (output / f"{task}.py").is_file():
        return ""
    # Graded sweeps treat a successful instance as immutable progress across
    # prompt/skill edits, provided the selected reference file still matches.
    # CHECKPOINT.decide still receives the full digest, so an interrupted
    # session cannot resume against changed inputs.
    if ignore_prompt_changes:
        digest = ""
    return CHECKPOINT.already_done(output, record=RUN_RECORD,
                                   artifacts=COMPLETE_ARTIFACTS, digest=digest,
                                   require_blend_python=True)


def tasks(cfg: dict, requested: list[str] | None) -> list[str]:
    if requested:
        return requested
    configured = cfg["run"].get("tasks") or []
    if configured:
        return list(configured)
    root, prompt = resolve(cfg["run"]["input_root"]), cfg["run"]["prompt_file"]
    return sorted(p.name for p in root.iterdir()
                  if p.is_dir() and (p / prompt).is_file())


def run_command(argv: list[str], *, log: Path | None = None,
                timeout: int = 900) -> subprocess.CompletedProcess:
    if log:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("w") as sink:
            return subprocess.run(argv, cwd=REPO, stdout=sink,
                                  stderr=subprocess.STDOUT, text=True,
                                  timeout=timeout)
    return subprocess.run(argv, cwd=REPO, capture_output=True, text=True,
                          timeout=timeout)


def ensure_source(cfg: dict) -> Path:
    official = cfg["official"]
    cache = resolve(official["cache_dir"])
    checkout = cache / f"blender_mcp-{official['source_ref'].replace('/', '_')}"
    marker = checkout / "mcp" / "pyproject.toml"
    if marker.is_file():
        with SOURCE_LOCK.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            ensure_headless_screenshot_compat(checkout)
        return checkout
    cache.mkdir(parents=True, exist_ok=True)
    with SOURCE_LOCK.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if marker.is_file():
            ensure_headless_screenshot_compat(checkout)
            return checkout
        partial = cache / f".{checkout.name}.partial-{os.getpid()}"
        if partial.exists():
            shutil.rmtree(partial)
        completed = run_command([
            "git", "clone", "--depth", "1", "--branch", official["source_ref"],
            official["source_url"], str(partial),
        ])
        if completed.returncode:
            raise RuntimeError(f"official source clone failed: {completed.stderr.strip()}")
        partial.rename(checkout)
    ensure_headless_screenshot_compat(checkout)
    return checkout


def ensure_headless_screenshot_compat(checkout: Path) -> None:
    """Patch the pinned official VIEW_3D screenshot for Xvfb, idempotently."""
    target = (checkout / "mcp" / "blmcp" / "tools" /
              "get_screenshot_of_area_as_image_toolcode.py")
    text = target.read_text()
    if HEADLESS_SCREENSHOT_MARKER in text:
        return
    if HEADLESS_SCREENSHOT_STOCK not in text:
        raise RuntimeError(
            "official screenshot source no longer matches the pinned compatibility patch")
    target.write_text(text.replace(
        HEADLESS_SCREENSHOT_STOCK, HEADLESS_SCREENSHOT_REPLACEMENT, 1))


def version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in text.split(".")[:3])


def blender_version(path: Path) -> tuple[int, ...] | None:
    match = re.search(r"blender-(\d+)\.(\d+)(?:\.(\d+))?", str(path))
    if match:
        return tuple(int(p or 0) for p in match.groups())
    env = os.environ.copy()
    lib = REPO / ".pixi" / "envs" / "default" / "lib"
    if (lib / "libXfixes.so.3").exists():
        env["LD_LIBRARY_PATH"] = f"{lib}:{env.get('LD_LIBRARY_PATH', '')}"
    try:
        completed = subprocess.run([path, "--version"], capture_output=True,
                                   text=True, timeout=30, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"^Blender\s+(\d+)\.(\d+)(?:\.(\d+))?",
                      completed.stdout, re.MULTILINE)
    return tuple(int(p or 0) for p in match.groups()) if match else None


def safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with tarfile.open(archive) as handle:
        for member in handle.getmembers():
            target = (destination / member.name).resolve()
            if root != target and root not in target.parents:
                raise RuntimeError(f"unsafe archive member: {member.name}")
        handle.extractall(destination, filter="data")


def download(url: str, destination: Path) -> None:
    # download.blender.org sits behind Cloudflare, which 403s urllib's default UA.
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    partial = destination.with_suffix(destination.suffix + ".partial")
    partial.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(request) as response, partial.open("wb") as sink:
        shutil.copyfileobj(response, sink)
    partial.rename(destination)


def ensure_blender(cfg: dict, no_download: bool) -> Path:
    bcfg = cfg["blender"]
    minimum = version_tuple(bcfg.get("minimum_version", "5.1"))
    rejected: list[str] = []
    candidates: list[Path] = []
    configured = bcfg.get("binary", "auto")
    if configured != "auto":
        candidates.append(resolve(configured))
    env_binary = os.environ.get("BLENDER_BIN") or os.environ.get("BLENDER_MCP_BLENDER_BIN")
    if env_binary:
        candidates.insert(0, Path(env_binary))
    candidates += sorted((REPO / "tools").glob("blender-*-linux-x64/blender"), reverse=True)
    system = shutil.which("blender")
    if system:
        candidates.append(Path(system))
    for candidate in candidates:
        version = blender_version(candidate)
        if candidate.is_file() and os.access(candidate, os.X_OK) and version and version >= minimum:
            return candidate.resolve()
        if candidate.is_file() and version and version < minimum:
            rejected.append(f"{candidate} is {'.'.join(map(str, version))}")
    for note in rejected:
        print(f"    too old for the official add-on: {note}")

    if no_download or not bcfg.get("auto_download", True):
        raise RuntimeError(
            f"official Blender MCP requires Blender {bcfg['minimum_version']}+; "
            "set [blender].binary or allow the configured official download")

    install_dir = resolve(bcfg["download_dir"])
    binary = install_dir / "blender"
    if binary.is_file():
        return binary
    archive = install_dir.parent / Path(bcfg["download_url"]).name
    print(f"    downloading official Blender: {bcfg['download_url']}")
    download(bcfg["download_url"], archive)
    safe_extract(archive, install_dir.parent)
    archive.unlink(missing_ok=True)
    if not binary.is_file():
        raise RuntimeError(f"download did not produce {binary}")
    return binary


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def free_display(start: int = 100) -> int:
    for number in range(start, start + 200):
        if not Path(f"/tmp/.X11-unix/X{number}").exists() and not Path(f"/tmp/.X{number}-lock").exists():
            return number
    raise RuntimeError("no unused X display found")


def send_code(port: int, code: str, timeout: float = 300.0) -> dict:
    payload = json.dumps({"type": "execute", "code": code,
                          "strict_json": True}).encode() + b"\0"
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(payload)
        data = bytearray()
        while b"\0" not in data:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data.extend(chunk)
    if not data:
        raise ConnectionError("empty response from official Blender bridge")
    response = json.loads(bytes(data).partition(b"\0")[0])
    if response.get("status") != "ok":
        raise RuntimeError(response.get("message", response))
    return response


class Stack:
    def __init__(self, cfg: dict, source: Path, blender: Path, ref: Path,
                 runtime: Path, logs: Path, xpra: bool,
                 restore: Path | None = None):
        self.cfg, self.source, self.blender, self.ref = cfg, source, blender, ref
        self.runtime, self.logs, self.xpra = runtime, logs, xpra
        # A checkpoint .blend from an interrupted attempt. Blender opens it
        # instead of importing ref.glb, so the scene comes back whole -- the
        # agent's objects, materials and render settings included.
        self.restore = restore
        self.processes: list[tuple[str, subprocess.Popen, object]] = []
        self.port = self.display = None
        self.gpu_device = None
        self.socket_dir = Path(tempfile.mkdtemp(prefix="3dcot-s-"))

    def _start(self, name: str, argv: list[str], env: dict) -> subprocess.Popen:
        handle = (self.logs / f"{name}.log").open("w")
        proc = subprocess.Popen(argv, cwd=REPO, stdout=handle,
                                stderr=subprocess.STDOUT, env=env,
                                start_new_session=True)
        self.processes.append((name, proc, handle))
        (self.runtime / f"{name}.pid").write_text(str(proc.pid))
        return proc

    def start_locked(self) -> None:
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)
        self.socket_dir.mkdir(parents=True, exist_ok=True)
        self.socket_dir.chmod(0o700)
        with START_LOCK.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            self.port = free_port()
            requested_display = self.cfg["blender"].get("display", "auto")
            self.display = free_display() if requested_display == "auto" else int(requested_display)
            env = os.environ.copy()
            self.gpu_device = BLENDER_IPC.configure_gpu_environment(
                self.cfg["blender"], env)
            if self.gpu_device is not None:
                print("    blender GPU visibility: physical device(s) "
                      f"{self.gpu_device}")
            env.update({"DISPLAY": f":{self.display}",
                        "XDG_RUNTIME_DIR": str(self.socket_dir)})
            lib = REPO / ".pixi" / "envs" / "default" / "lib"
            if (lib / "libXfixes.so.3").exists():
                env["LD_LIBRARY_PATH"] = f"{lib}:{env.get('LD_LIBRARY_PATH', '')}"

            self._start("xvfb", ["Xvfb", f":{self.display}", "-screen", "0",
                                      self.cfg["blender"].get("screen", "1920x1080x24"),
                                      "-nolisten", "tcp"], env)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not Path(f"/tmp/.X11-unix/X{self.display}").exists():
                time.sleep(.2)
            if not Path(f"/tmp/.X11-unix/X{self.display}").exists():
                raise RuntimeError(f"Xvfb :{self.display} did not start")

            if self.xpra:
                xpra_bin = shutil.which("xpra")
                if not xpra_bin:
                    raise RuntimeError("--xpra requested but xpra is not on PATH")
                xpra_dir = self.socket_dir / "xpra"
                xpra_dir.mkdir(exist_ok=True)
                xpra_socket = xpra_dir / f"blender-{self.display}"
                xpra_proc = self._start("xpra", [xpra_bin, "shadow", f":{self.display}",
                            "--daemon=no", f"--socket-dir={xpra_dir}",
                            f"--bind={xpra_socket}",
                            "--mdns=no", "--notifications=no", "--pulseaudio=no",
                            "--webcam=no", "--html=no"], env)
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and not xpra_socket.exists():
                    if xpra_proc.poll() is not None:
                        raise RuntimeError(f"xpra exited during startup; see {self.logs / 'xpra.log'}")
                    time.sleep(.2)
                if not xpra_socket.exists():
                    raise RuntimeError(f"xpra socket did not appear: {xpra_socket}")

            blender_root = self.runtime / "blender-user"
            env.update({
                "BLENDER_USER_CONFIG": str(blender_root / "config"),
                "BLENDER_USER_SCRIPTS": str(blender_root / "scripts"),
                "BLENDER_USER_DATAFILES": str(blender_root / "datafiles"),
            })
            ready = self.runtime / "blender.ready.json"
            ready.unlink(missing_ok=True)
            # The .blend goes in as Blender's positional file argument so it is
            # loaded before --python runs the bootstrap; --blend then tells the
            # bootstrap to keep that scene instead of importing the reference.
            argv = [str(self.blender), "--factory-startup", "--online-mode"]
            if self.restore is not None:
                argv.append(str(self.restore))
            argv += ["--python", str(HERE / "blender_bootstrap.py"), "--",
                     "--addon-root", str(self.source / "addon"),
                     "--glb", str(self.ref), "--port", str(self.port),
                     "--ready-file", str(ready),
                     "--light-power",
                     "0.5" if self.ref.name.endswith("_grey.glb") else "1.0",
                     "--light-ref-extent", "2.5"]
            if self.restore is not None:
                argv += ["--blend", str(self.restore)]
            blender_proc = self._start("blender", argv, env)
            deadline = time.monotonic() + 180
            last_error = ""
            while time.monotonic() < deadline:
                if blender_proc.poll() is not None:
                    raise RuntimeError(f"Blender exited during startup; see {self.logs / 'blender.log'}")
                if ready.is_file():
                    try:
                        send_code(self.port, "import bpy\nresult={'version': bpy.app.version_string, 'objects': len(bpy.data.objects)}", 3)
                        break
                    except Exception as exc:  # bridge may not be polling yet
                        last_error = str(exc)
                time.sleep(.5)
            else:
                raise RuntimeError(f"official MCP bridge did not become healthy: {last_error}")

        print(f"    official bridge port: {self.port}")
        print(f"    Xvfb: DISPLAY=:{self.display}")
        print(f"    runtime directory: {self.runtime}")
        if self.xpra:
            print(f"    xpra directory: {self.socket_dir / 'xpra'}")
            print(f"    xpra attach: xpra attach ssh://USER@SERVER/{self.display}")
        else:
            print("    xpra: disabled")

    def save(self, destination: Path) -> None:
        # copy=True: the running Blender keeps its own filepath, so saving an
        # artifact (or a checkpoint) never makes the agent's next save_mainfile
        # write into it.
        code = ("import bpy, os\npath=" + repr(str(destination.resolve())) +
                "\nos.makedirs(os.path.dirname(path), exist_ok=True)"
                "\nbpy.ops.wm.save_as_mainfile(filepath=path, copy=True)"
                "\nresult={'path': path}")
        send_code(int(self.port), code)

    def export_text(self, destination: Path) -> dict:
        """Publish Blender's exact reconstruction_gt.py as plain Python."""
        response = send_code(int(self.port), BLENDER_IPC.text_block_export_code(
            str(destination.resolve()), RECONSTRUCTION_TEXT_BLOCK,
            allow_fallback=False))
        result = response.get("result")
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except ValueError:
                return {}
        return result if isinstance(result, dict) else {}

    def scene_info(self) -> dict:
        """Scene name + object names, for the resume message. Never fatal."""
        try:
            response = send_code(int(self.port), (
                "import bpy\n"
                "result={'scene': bpy.context.scene.name,\n"
                "        'objects': sorted(o.name for o in bpy.data.objects),\n"
                "        'filepath': bpy.data.filepath}"))
        except Exception:                                     # noqa: BLE001
            return {}
        result = response.get("result")
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except ValueError:
                return {}
        return result if isinstance(result, dict) else {}

    def close(self) -> None:
        for _name, proc, _handle in reversed(self.processes):
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, 15)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 15
        for _name, proc, _handle in reversed(self.processes):
            remaining = max(0, deadline - time.monotonic())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, 9)
                except ProcessLookupError:
                    pass
                proc.wait()
        for _name, _proc, handle in self.processes:
            handle.close()
        shutil.rmtree(self.socket_dir, ignore_errors=True)


def official_agent(cfg: dict, kind: str, timeout: int, source: Path, port: int):
    acfg = dict(cfg["agent"].get(kind, {}))
    acfg["full_access_server"] = cfg["official"]["server_name"]
    acfg["viewport_only_server"] = cfg["official"]["server_name"]
    # Resolved in main() from --no-memory / --memory over [agent].no_memory; on
    # by default so a task neither reads nor writes the CLI's own memory.
    acfg["no_memory"] = bool(cfg["agent"].get("no_memory", True))
    acfg["token_total_limit"] = cfg["agent"].get("token_total_limit", 0)
    agent = AGENTS.build(kind, acfg, REPO, timeout)
    uv = shutil.which("uv") or "/usr/local/bin/uv"

    def servers(_self, _ports):
        return {cfg["official"]["server_name"]: {
            "command": uv,
            "args": ["run", "--directory", str(source / "mcp"), "blender-mcp"],
            "env": {"BLENDER_MCP_HOST": "127.0.0.1",
                    "BLENDER_MCP_PORT": str(port)},
        }}

    agent.servers = MethodType(servers, agent)
    return agent


def run_task(cfg: dict, task: str, kind: str, model: str | None,
             timeout: int, xpra: bool, no_download: bool,
             settings: dict, fresh: bool = False, rerun: bool = False,
             skip_completed: bool = True,
             fresh_incompatible: bool = False) -> dict:
    graded = cfg.get("_graded") or {}
    if graded:
        input_dir = Path(graded["input_path"]) / task
        if Path(graded["input_path"]).name == task:
            input_dir = Path(graded["input_path"])
        prompt = Path(graded["prompt_path"])
        suffix = ".glb" if graded["texture_renders"] else "_grey.glb"
        ref = input_dir / f"{task}{suffix}"
    else:
        input_dir = resolve(cfg["run"]["input_root"]) / task
        prompt = input_dir / cfg["run"]["prompt_file"]
        ref = input_dir / cfg["run"]["ref_file"]
    if not prompt.is_file() or not ref.is_file():
        raise RuntimeError(f"[{task}] requires {prompt} and {ref}")
    output_agent = cfg["run"].get("output_agent", kind)
    output = resolve(cfg["run"]["output_root"]) / output_agent / task
    # Same digest either way (name + content, and the copies keep their names),
    # so it can be taken from the inputs before anything is written -- a skipped
    # task's output directory is then left completely untouched.
    digest = CHECKPOINT.inputs_digest([prompt, ref, *skill_input_paths()])
    reference_digest = CHECKPOINT.inputs_digest([ref])
    staged_ref = output / ref.name
    completed_reference_matches = (
        staged_ref.is_file() and
        CHECKPOINT.inputs_digest([staged_ref]) == reference_digest)

    # Already done? No Blender, no official source checkout, no CLI session, and
    # nothing written over the run record and artifacts already sitting there.
    skip = skip_reason(task, output, cfg, rerun=rerun, fresh=fresh,
                       skip_completed=skip_completed, digest=digest,
                       completed_reference_matches=completed_reference_matches)
    if skip:
        print(f"  [{task}] SKIP: already completed — {skip}")
        print("    pass --rerun (or [run].skip_completed = false) to run it again")
        return {"task": task, "agent": kind, "ok": True, "skipped": True,
                "output": str(output), "skip_reason": skip}

    output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(prompt, output / prompt.name)
    shutil.copy2(ref, output / ref.name)
    stage_task_skill(output)

    # Resume: is there an interrupted run of this exact task to continue?
    resolved_model = model or cfg["agent"].get(kind, {}).get("model", "")
    checkpoint_kind = (cfg.get("_graded") or {}).get("resume_kind", kind)
    checkpoint_digest = CHECKPOINT.resume_input_digest(
        output,
        digest,
        allow_prompt_changes=bool(
            (cfg.get("_graded") or {}).get("resume_across_prompt_changes")),
        reference_matches=completed_reference_matches,
    )
    decision = CHECKPOINT.decide(output, kind=checkpoint_kind,
                                 model=resolved_model,
                                 digest=checkpoint_digest, settings=settings,
                                 fresh=fresh)
    if decision["mode"] == "blocked" and fresh_incompatible:
        print(
            f"    resume: {decision['note']}; --fresh-incompatible starts "
            "this task over"
        )
        decision = CHECKPOINT.decide(
            output,
            kind=checkpoint_kind,
            model=resolved_model,
            digest=checkpoint_digest,
            settings=settings,
            fresh=True,
        )
    if decision["note"]:
        print(f"    resume: {decision['note']}")
    if decision["mode"] == "blocked":
        print("    BLOCKED: a checkpoint for this task is waiting but cannot be "
              "used by this invocation. Pass --fresh to start over.")
        return {"task": task, "agent": kind, "ok": False, "output": str(output),
                "error": f"checkpoint unusable: {decision['note']}",
                "resume": {"mode": "blocked", "note": decision["note"]}}
    resuming = decision["mode"] == "resume"
    # Every attempt keeps its own complete artifacts: the finished one is moved
    # aside before this one starts writing the same file names.
    if decision.get("archive"):
        archived = CHECKPOINT.rotate(
            output, decision["archive"], keep=AGENTS.PERSISTENT_SESSION_ENTRIES,
            files=(RUN_RECORD, "final.blend",
                   f"{task}.py", "usage.json"))
        print(f"    archived attempt {decision['archive']}: {archived}")
    restore = None
    if resuming:
        # Bump the counter before the work starts, so a crash during startup
        # cannot make the next run reuse this attempt's archive slot.
        CHECKPOINT.mark(output, CHECKPOINT.RESUMABLE,
                        attempts=decision["attempt"], resumed_utc=CHECKPOINT.now())
        staged = CHECKPOINT.stage_restore(
            output, CHECKPOINT.blend_paths(output, decision["state"]))
        restore = staged.get("scene")
        print(f"    resuming attempt {decision['attempt']} of session "
              f"{decision['state']['session'].get('resume_target')} "
              f"(scene: {restore or 'not in the checkpoint — starting from ref.glb'})")

    logs, session = output / "logs", output / "session"
    runtime = output / f".runtime-{os.getpid()}"
    for directory in (logs, session):
        directory.mkdir(parents=True, exist_ok=True)

    source = ensure_source(cfg)
    blender = ensure_blender(cfg, no_download)
    stack = Stack(cfg, source, blender, ref, runtime, logs, xpra, restore=restore)
    record = {"task": task, "agent": kind, "ok": False,
              "no_memory": bool(cfg["agent"].get("no_memory", True)),
              "started_at": datetime.now(timezone.utc).isoformat(),
              # Content digest of prompt.txt+ref.glb: what lets a later run tell
              # "this task is done" from "this task was done, but from a
              # different prompt". See CHECKPOINT.already_done.
              "inputs_digest": digest,
              "reference_digest": reference_digest,
              "official": cfg["official"], "output": str(output),
              "resume": {"mode": decision["mode"], "attempt": decision["attempt"],
                         "restored": str(restore or ""), "note": decision["note"]}}
    try:
        stack.start_locked()
        record.update({"port": stack.port, "display": stack.display,
                       "gpu_device": stack.gpu_device or "inherit",
                       "xpra": xpra, "blender": str(blender)})
        agent = official_agent(cfg, kind, timeout, source, int(stack.port))
        if model:
            agent.model = model
        task_text = task_prompt(prompt)
        if resuming:
            scene = stack.scene_info()
            record["resume"]["scene_before"] = scene
            template = (HERE / "resume_instruction.tmpl").read_text()
            composed = template.format(
                server_name=cfg["official"]["server_name"],
                port=stack.port, task=task, attempt=decision["attempt"],
                scene_name=scene.get("scene", "?"),
                object_count=len(scene.get("objects") or []),
                ref_name=ref.name,
                task_prompt=task_text if settings["repeat_prompt"] else
                "(unchanged — see the earlier turns of this conversation)")
        else:
            composed = (HERE / "instruction.tmpl").read_text().format(
                server_name=cfg["official"]["server_name"],
                documentation_url=cfg["official"]["documentation_url"],
                port=stack.port, task=task, task_prompt=task_text)
        (session / "agent_input.txt").write_text(composed)
        print(f"    running {kind} ({agent.model}) — "
              f"{'resumed' if resuming else 'new'} session, {len(composed)} chars")
        reconstruction_path = output / f"{task}.py"

        def completion_check(current):
            try:
                return stack.export_text(reconstruction_path)
            except Exception as exc:  # reported as an incomplete live artifact
                return {"path": None, "matched": False, "candidates": [],
                        "error": f"{type(exc).__name__}: {exc}"}

        result, completion_summary = AGENTS.run_with_continuations(
            agent, composed, {"official": int(stack.port)}, output, session,
            resume=decision["state"].get("session") if resuming else None,
            completion_check=completion_check)
        AGENTS.classify(result, settings["extra_limit_patterns"])
        (session / "tool_calls.json").write_text(json.dumps(result.tool_calls, indent=2))
        # What the session cost, from the CLI's own accounting (see the USAGE
        # section in agents.py). Mirrored at the top level because it is a
        # headline number of the run; `scope` says whether it covers the whole
        # (possibly resumed) session or just this attempt.
        record["usage"] = result.usage or None
        print(f"    {AGENTS.usage_line(result.usage)}")
        for note in (result.usage or {}).get("notes") or []:
            print(f"      note: {note}")
        if result.limit_reason:
            print(f"    stopped early — {result.limit_reason}: "
                  f"{result.limit_evidence[:200] or 'no message captured'}")
        summary = completion_summary
        record["reconstruction"] = summary
        try:
            if summary.get("error"):
                raise RuntimeError(summary["error"])
            if summary.get("matched"):
                print(f"    {reconstruction_path.name}: {summary['bytes']} bytes from "
                      f"text block '{summary['name']}' ({summary['how']})")
            else:
                print("    warning: no non-empty reconstruction_gt.py Text datablock")
        except Exception as exc:
            record["reconstruction"] = {
                "path": None, "error": f"{type(exc).__name__}: {exc}"
            }
            print(f"    warning: could not publish reconstruction source: {exc}")
        if AGENTS.accept_token_limited_completion(result, record["reconstruction"]):
            record["token_limit_completion"] = True
            print("    token total limit: accepting the latest exported "
                  "reconstruction script as final")
        out_of_limit = bool(
            result.limit_reason == "token_total_limit" and
            not record["reconstruction"].get("matched"))
        if out_of_limit:
            record["out_of_limit"] = True
            record["out_of_limit_reason"] = result.limit_evidence
            path = CHECKPOINT.mark_out_of_limit(
                output, reason=result.limit_reason, evidence=result.limit_evidence)
            record["resume"].update({"checkpoint": "out_of_limit",
                                     "reason": result.limit_reason,
                                     "path": str(path)})
            print("    OUT_OF_LIMIT: no reconstruction script was available; "
                  "future runs will skip this instance")
        if not task.startswith("_") and not record["reconstruction"].get("matched"):
            candidates = record["reconstruction"].get("candidates") or []
            if (not result.completion_error and
                    not result.limit_hit and
                    result.limit_reason not in ("api_error", "api_timeout")):
                result.completion_error = "missing complete reconstruction script"
                result.limit_reason = "incomplete_artifact"
                result.limit_evidence = (
                    f"Blender capture block: {RECONSTRUCTION_TEXT_BLOCK}; "
                    f"candidates: {', '.join(candidates) or 'none'}"
                )
            if result.completion_error:
                print("    incomplete: no non-empty reconstruction_gt.py Text "
                      "datablock; keeping the session resumable")
        record["agent_run"] = result.as_dict()
        record["ok"] = result.ok
        # While Blender is still up: save the checkpoint (or close it).
        if not out_of_limit:
            record["resume"].update(CHECKPOINT.commit(
                output, result, harness="Full3DInteraction", task=task,
                kind=checkpoint_kind,
                model=agent.model, digest=digest, decision=decision,
                settings=settings, roles=("scene",),
                save_scene=lambda _role, path: stack.save(path)))
    except Exception as exc:  # always preserve a diagnostic record and clean up
        record["error"] = f"{type(exc).__name__}: {exc}"
        print(f"    ERROR: {record['error']}", file=sys.stderr)
    finally:
        # Preserve Blender's current state even if the CLI timed out or raised.
        blend = output / "final.blend"
        if stack.port is not None and any(name == "blender" and proc.poll() is None
                                          for name, proc, _handle in stack.processes):
            try:
                stack.save(blend)
                record["blend"] = str(blend)
            except Exception as exc:
                record["save_error"] = f"{type(exc).__name__}: {exc}"
            if "reconstruction" not in record:
                try:
                    record["reconstruction"] = stack.export_text(
                        output / f"{task}.py")
                except Exception as exc:
                    record["reconstruction"] = {
                        "path": None, "error": f"{type(exc).__name__}: {exc}"
                    }
        stack.close()
        # A resumable conversation may refer to helper files it created in the
        # task cwd, so leave those untouched until the session genuinely ends.
        # On completion (or a terminal failure), separate all scratch material
        # from the stable instance artifacts before the graded wrapper writes
        # its canonical ``renders/`` directory.
        if record.get("resume", {}).get("checkpoint") != "written":
            try:
                organized = organize_agent_artifacts(
                    output, task, prompt_name=prompt.name, ref_name=ref.name)
                record["organized_agent_artifacts"] = organized
                if organized["renders"] or organized["artifacts"]:
                    print("    organized agent workspace: "
                          f"{organized['renders']} PNG(s) -> log_renders, "
                          f"{organized['artifacts']} other file(s) -> "
                          f"log_artifacts ({organized['run_id']})")
            except Exception as exc:
                record["organize_error"] = f"{type(exc).__name__}: {exc}"
                print(f"    warning: could not organize agent workspace: {exc}")
        record["finished_at"] = datetime.now(timezone.utc).isoformat()
        (output / RUN_RECORD).write_text(json.dumps(record, indent=2))
        usage = record.get("usage") or {}
        agent_seconds = mcp_agent_seconds(output, RUN_RECORD)
        (output / "usage.json").write_text(json.dumps({
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
        print("    stopped: Blender, Xvfb, xpra")
    return record


def main(argv=None) -> int:
    args = parse_args(argv)
    cfg = load_config(Path(args.config).resolve())
    selected = tasks(cfg, args.tasks)
    kind = args.agent or cfg["agent"]["kind"]
    timeout = args.timeout or int(cfg["run"]["timeout_sec"])
    xpra = cfg["blender"].get("xpra", False) if args.xpra is None else args.xpra
    if args.no_memory is not None:                 # CLI beats [agent].no_memory
        cfg["agent"]["no_memory"] = args.no_memory
    settings = CHECKPOINT.settings(cfg, enabled=args.resume,
                                   max_attempts=args.max_attempts)
    skip_completed = bool(cfg["run"].get("skip_completed", True))
    print(f"Full3DInteraction: {len(selected)} task(s): {', '.join(selected)}")
    if args.dry_run:
        for task in selected:
            output = resolve(cfg["run"]["output_root"]) / kind / task
            # No digest: this mode reads only, and the digest of a task's inputs
            # is what the record itself was written from.
            skip = skip_reason(task, output, cfg, rerun=args.rerun,
                               fresh=args.fresh, skip_completed=skip_completed)
            print(f"  {task}: {resolve(cfg['run']['input_root']) / task}"
                  f"{' SKIP: ' + skip if skip else ''}")
        return 0
    records = []
    for task in selected:
        fresh = args.fresh
        while True:
            try:
                record = run_task(cfg, task, kind, args.model, timeout, xpra,
                                  args.no_download, settings, fresh=fresh,
                                  rerun=args.rerun,
                                  skip_completed=skip_completed,
                                  fresh_incompatible=args.fresh_incompatible)
            except Exception as exc:
                output = resolve(cfg["run"]["output_root"]) / kind / task
                record = {"task": task, "agent": kind, "ok": False,
                          "output": str(output),
                          "error": f"{type(exc).__name__}: {exc}"}
                print(f"  [{task}] ERROR: {record['error']}", file=sys.stderr)
                break
            if (kind != "qwen-tokenplan" or
                    record.get("resume", {}).get("checkpoint") != "written" or
                    record.get("resume", {}).get("reason") == "usage_limit"):
                break
            attempt = int(record.get("resume", {}).get("attempt", 1))
            if settings["max_attempts"] and attempt >= settings["max_attempts"]:
                print(f"  [{task}] automatic resume stopped at the total "
                      f"attempt limit ({settings['max_attempts']})")
                break
            print(f"  [{task}] checkpoint saved; automatically starting "
                  f"attempt {attempt + 1} in the same Qwen session")
            fresh = False
        records.append(record)
    checkpointed = [r["task"] for r in records
                    if r.get("resume", {}).get("checkpoint") == "written"]
    skipped = [r["task"] for r in records if r.get("skipped")]
    for record in records:
        resume = record.get("resume", {})
        state = ""
        if record.get("skipped"):
            state = " SKIPPED (already completed)"
        elif resume.get("checkpoint") == "written":
            state = f" CHECKPOINTED ({resume.get('reason')})"
        elif resume.get("mode") == "resume":
            state = f" resumed attempt {resume.get('attempt')}"
        usage = record.get("usage") or {}
        total = (usage.get("tokens") or {}).get("total")
        requests = usage.get("api_requests")
        print(f"  {record['task']}: ok={record['ok']} "
              f"tokens={'?' if total is None else format(total, ',')} "
              f"api={'?' if requests is None else requests} "
              f"output={record['output']}{state}")
    if skipped:
        print(f"skipped, already complete ({len(skipped)}/{len(records)}): "
              f"{', '.join(skipped)}")
    if checkpointed:
        if kind == "qwen-tokenplan":
            print("Qwen checkpoint retained after reaching the configured "
                  f"attempt limit: {', '.join(checkpointed)}")
        else:
            print("checkpointed, re-run the same command to continue: "
                  f"{', '.join(checkpointed)}")
    return 0 if all(r["ok"] for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
