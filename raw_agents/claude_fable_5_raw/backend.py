"""Internal backend for claude_fable_5_raw; invoked by the shared iterative runner."""

import json
import os
import re
import subprocess
import time
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent

AGENTS_DIR = PKG_DIR.parent

import no_memory

RENDER_PY = AGENTS_DIR.parent / "evaluation" / "core" / "render.py"

BACKEND = "claude-code"

MEMORY_OFF = no_memory.apply_claude

RENDER_VIEW_NAMES = ("Image_005.png", "Image_015.png", "Image_025.png", "Image_035.png")


def _json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return str(value)


def load_image(path):
    """Load one view as an RGB PIL image (tagged with its source path for
    downstream logging), the form tasksolver Questions expect."""
    from PIL import Image

    with Image.open(path) as im:
        img = im.convert("RGB").copy()
    img.info["image_path"] = str(Path(path).resolve())
    return img


def build_agent(vision_model, credentials):
    """Build the bundled TaskSolver Claude Code interface.

    The small wrapper preserves the former ``Agent.visual_interface`` shape
    without importing TaskSolver's unrelated provider registry.
    """
    from types import SimpleNamespace

    from tasksolver.answer_types import TextAnswer
    from tasksolver.claude_code import ClaudeCodeModel
    from tasksolver.common import TaskSpec

    task = TaskSpec(
        name="",
        description="",
        answer_type=TextAnswer,
        followup_func=None,
        completed_func=None,
    )
    aliases = {
        "claude-code-fable-5": "claude-fable-5",
        "claude-code-opus-5": "claude-opus-5",
    }
    if vision_model in aliases:
        model = aliases[vision_model]
    else:
        raise ValueError(f"Unsupported Claude Code model alias: {vision_model}")
    return SimpleNamespace(visual_interface=ClaudeCodeModel(None, task, model=model))


def call_model(interface, text, pil_images, max_tokens, model):
    """One backend turn: a Question of (text + inline images) via rough_guess
    (which skips first_question, so `text` is delivered verbatim). Returns
    (reply_text, raw_dict)."""
    from tasksolver.common import Question

    question = Question([text] + list(pil_images))
    p_ans, response, meta, payload = interface.rough_guess(
        question, max_tokens=max_tokens, max_tries=1
    )
    reply = getattr(p_ans, "data", None) or getattr(p_ans, "raw", None) or ""
    raw = {
        "backend": BACKEND,
        "model": model,
        "thinking_depth": interface.thinking_depth,
        "text": reply,
        "n_images": len(pil_images),
        "response_metadata": _json_safe(meta),
    }
    if not reply:
        raise RuntimeError(
            f"{BACKEND} returned no output for model={model}. Ensure the "
            "backend is authenticated/reachable."
        )
    return reply, raw


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


def extract_script(text):
    """Parse a runnable Python script from the reply text (fenced or raw)."""
    return parse_python_script(text)


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
        "max tries",
        "maxtriesexceeded",
        "returned no output",
    ]
    return any(ind in msg for ind in indicators)
