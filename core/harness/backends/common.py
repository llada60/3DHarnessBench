"""Provider-independent image loading, script parsing and rendering helpers."""

import json
import re
import subprocess
import time
from pathlib import Path

from core.blender_runtime import blender_environment
from core.paths import CORE_ROOT

RENDER_PY = CORE_ROOT / "render.py"
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
    """Configure imports and shared libraries for the Blender subprocess."""
    return blender_environment()


def render_script(script_path: Path, renders_dir: Path, args):
    """Render via the project-local evaluation renderer subprocess. Returns the render_log.json record."""
    renders_dir.mkdir(parents=True, exist_ok=True)
    log_path = renders_dir / "render_log.json"
    cmd = [
        args.blender,
        "--background",
        "--python-use-system-env",
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
