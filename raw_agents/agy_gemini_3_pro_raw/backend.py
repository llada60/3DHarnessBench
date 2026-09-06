"""Internal backend for agy_gemini_3_pro_raw; invoked by the shared iterative runner."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent

AGENTS_DIR = PKG_DIR.parent

import no_memory

RENDER_PY = AGENTS_DIR.parent / "evaluation" / "core" / "render.py"

CLI_WORKSPACE_ROOT = "/tmp/agent_generation"

AGY_WORKSPACE_ARTIFACTS = (".git", "pyagy-capture.jsonl", "pyagy-shim.log")

RENDER_VIEW_NAMES = ("Image_005.png", "Image_015.png", "Image_025.png", "Image_035.png")

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def slugify(text):
    """Filesystem-safe token for workspace and staged-image names."""
    return _SLUG_RE.sub("_", str(text)).strip("._-") or "x"


def image_manifest(entries):
    """Preamble pointing agy's file-reading tool at this turn's images.

    entries: ordered list of (role, absolute_path). role is a short human label
    such as "reference" / "current render" / "left" / "right".

    The absolute paths are load-bearing. agy's ReadFile refuses relative paths
    ("Image_005.png must be an absolute path"), and its tools do not run inside
    the workspace pyagy is given — their cwd is agy's own app data dir, i.e.
    `<data_dir>/.gemini/antigravity-cli` now that each turn gets a private HOME
    (~/.gemini/antigravity-cli before that) — so a bare filename sends the model
    looking for the file with `find / -name ...`.
    Every task under input/ ships the same four view names (Image_005/015/025/
    035.png), and so does every candidate render of every agent, so that search
    returns dozens of wrong hits: the fish tasks ended up reading
    input/multi_view-bird-trellis/images/* (and a stale copy in agy's own
    scratch dir) and modelled a bird. Naming the exact path removes the search;
    task-scoped filenames (see role_views) make a stray search harmless."""
    if not entries:
        return ""
    parts = [
        "The image(s) for THIS task are listed below by absolute path. "
        "Open each one with your file-reading tool using exactly the path "
        "given, before answering.",
        "Do not look for them any other way — no find/ls/glob by "
        "filename, no guessing a directory. This machine holds images "
        "from other tasks under similar names, and opening one of those "
        "would make your answer describe the wrong object. Use only the "
        "paths listed here.",
    ]
    parts += [f"- {role}: {path}" for role, path in entries]
    return "\n".join(parts)


def git_init_workspace(workspace):
    """Give the throwaway workspace the git shape agy expects (initial commit
    over whatever is already there — i.e. the copied images)."""
    import pygit2

    repo = pygit2.init_repository(workspace)
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    sig = pygit2.Signature("raw-wrapper", "raw-wrapper@local")
    repo.create_commit("HEAD", sig, sig, "init", tree, [])


def new_workspace(scope):
    """A fresh throwaway git-able workspace, named after the turn it serves so
    a leftover directory under /tmp/agent_generation/ is traceable."""
    os.makedirs(CLI_WORKSPACE_ROOT, exist_ok=True)
    return tempfile.mkdtemp(prefix=f"agy_{slugify(scope)}_", dir=CLI_WORKSPACE_ROOT)


def agy_turn(body, images, model, timeout, scope, thinking_depth):
    """One agy turn over `images`: stage them in a fresh workspace, prepend the
    absolute-path manifest to `body`, ask.

    images: ordered [(role, dest_name, src_path)] as built by role_views. The
    manifest is assembled here, against the workspace that is about to run, so
    a re-issued body (render/parse feedback) can never quote a path from an
    already-deleted workspace."""
    workspace = new_workspace(scope)
    entries = [(role, os.path.join(workspace, dest)) for role, dest, _ in images]
    image_map = {dest: src for _, dest, src in images}
    manifest = image_manifest(entries)
    prompt = "\n\n".join([manifest, body]) if manifest else body
    return call_agy(
        prompt,
        image_map,
        model,
        timeout,
        workspace,
        thinking_depth,
        no_memory.agy_home(CLI_WORKSPACE_ROOT, slugify(scope)),
    )


def call_agy(prompt, image_map, model, timeout, workspace, thinking_depth, data_dir):
    """One `agy --print` turn in `workspace` (created by the caller, since the
    prompt has to name absolute paths inside it), removed again afterwards.

    image_map maps dest_filename -> source Path; each is copied into the
    workspace under dest_filename (so callers control collision-free names).
    Returns (text, raw_dict, generated_files) where generated_files maps
    relative name -> bytes for anything the model wrote into the workspace
    (agy is agentic and sometimes writes the script to a file instead of
    printing it).

    `data_dir` is this turn's private HOME (see no_memory.py): it scopes agy's
    whole store -- brain, knowledge, conversations -- to the turn, so nothing
    carries over from an earlier turn and nothing is left in ~/.gemini. pyagy
    seeds it with symlinks to the real login token, so auth still works. It is
    removed with the workspace."""
    try:
        from pyagy import ask as agy_ask

        for dest_name, src in image_map.items():
            shutil.copy2(src, os.path.join(workspace, dest_name))
        git_init_workspace(workspace)

        # Each turn uses a disposable workspace and private HOME.  Headless
        # agy cannot prompt for the image-reading permission, so approve tools
        # inside that isolated scope and pass the effort required by 3.1 Pro.
        r = agy_ask(
            prompt,
            model=model,
            workspace=workspace,
            timeout=timeout,
            data_dir=data_dir,
            skip_permissions=True,
            extra_flags=["--effort", thinking_depth],
        )

        skip = set(image_map) | set(AGY_WORKSPACE_ARTIFACTS)
        generated = {}
        for entry in sorted(os.listdir(workspace)):
            path = os.path.join(workspace, entry)
            if entry in skip or not os.path.isfile(path):
                continue
            with open(path, "rb") as f:
                generated[entry] = f.read()

        u = r.usage
        raw = {
            "backend": "agy",
            "model": model,
            "thinking_depth": thinking_depth,
            "exit_status": r.exit_status,
            # Recorded per attempt so "which images did this turn actually get?"
            # is answerable from the output alone — the question that uncovered
            # the cross-task image mix-up in the first place.
            "prompt": prompt,
            "images": {dest: str(src) for dest, src in image_map.items()},
            "text": r.text,
            "transcript": r.transcript,
            "usage": None
            if u is None
            else {
                "prompt_tokens": u.prompt_tokens,
                "candidates_tokens": u.candidates_tokens,
                "total_tokens": u.total_tokens,
                "thoughts_tokens": (u.raw or {}).get("thoughtsTokenCount"),
            },
            "generated_files": sorted(generated),
        }
        if not r.text and not generated:
            raise RuntimeError(
                "agy --print returned no output "
                f"(exit_status={r.exit_status}). Ensure agy is logged in "
                "(~/.gemini/antigravity-cli/) and reachable. "
                f"Transcript head:\n{r.transcript[:500]}"
            )
        return r.text or "", raw, generated
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
        shutil.rmtree(data_dir, ignore_errors=True)


def parse_python_script(text):
    """Extract a Python script from a model reply. Preference order:
    ```python fenced block, any fenced block, then everything from the first
    import/from/def/class line. Returns None if nothing looks like Python."""
    fenced = re.findall(
        r"```(?:python|py)\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE
    )
    if fenced:
        return max(fenced, key=len).strip()
    fenced = re.findall(r"```\s*\n(.*?)```", text, re.DOTALL)
    if fenced:
        return max(fenced, key=len).strip()
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^(import\s|from\s|def\s|class\s)", line):
            return "\n".join(lines[i:]).strip()
    return None


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


def _blender_env():
    """The bundled Blender needs this pixi env's shared libs (libXfixes.so.3 and
    other X11 libs live in $CONDA_PREFIX/lib) on LD_LIBRARY_PATH; the activated
    env does not include them, so Blender fails to even launch (ERR_NOLOG)."""
    env = os.environ.copy()
    conda = env.get("CONDA_PREFIX")
    if conda:
        libdir = os.path.join(conda, "lib")
        prev = env.get("LD_LIBRARY_PATH", "")
        if libdir not in prev.split(os.pathsep):
            env["LD_LIBRARY_PATH"] = libdir + ((os.pathsep + prev) if prev else "")
    return env


def render_script(script_path: Path, renders_dir: Path, args):
    """Render via the project-local evaluation renderer subprocess. Returns the render_log.json record."""
    renders_dir.mkdir(parents=True, exist_ok=True)
    log_path = renders_dir / "render_log.json"
    cmd = [
        args.blender,
        "--background",
        "--python",
        str(RENDER_PY),
        "--",
        "--blender-render",
        "--script",
        str(script_path),
        "--output-dir",
        str(renders_dir),
        "--samples",
        str(args.render_samples),
        "--resolution",
        str(args.render_resolution),
        "--engine",
        args.render_engine,
        "--light-power",
        "1.0" if args.texture_renders else "0.5",
        "--light-ref-extent",
        "2.5",
    ]
    t0 = time.time()
    try:
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=args.render_timeout,
            env=_blender_env(),
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "ERR_TIMEOUT",
            "error": f"Blender subprocess exceeded {args.render_timeout}s",
            "latency_s": round(time.time() - t0, 2),
        }
    if log_path.exists():
        return json.loads(log_path.read_text())
    return {
        "status": "ERR_NOLOG",
        "error": "Blender exited without writing render_log.json (likely segfault)",
        "latency_s": round(time.time() - t0, 2),
    }


def rendered_views(renders_dir: Path):
    """The candidate view PNGs render.py actually produced, in view order."""
    return [renders_dir / n for n in RENDER_VIEW_NAMES if (renders_dir / n).is_file()]


def role_views(scope, role, paths):
    """[(role, dest_name, src_path), ...] for one image role, order-preserving.

    dest_name carries both `scope` (task + iteration, e.g.
    "single_view-fish-gt.iter2") and the role, so the staged copies are unique
    against each other — which is what lets the verifier see target/left/right
    side by side — and also against every identically named view elsewhere on
    the machine (input/<other task>/images/Image_005.png, other agents' render
    dirs, agy's own scratch dir). The absolute path in the manifest is what
    makes the read correct; these names are what make a stray `find` harmless."""
    return [
        (role, f"{slugify(scope)}__{slugify(role)}{i}{p.suffix.lower()}", p)
        for i, p in enumerate(paths, 1)
    ]


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
        "returned no output",
    ]
    return any(ind in msg for ind in indicators)
