#!/usr/bin/env python3
"""Render generated Blender scripts at the benchmark's four camera angles.

This internal worker is called by the experiment runners and evaluate.py.
Its normal-Python mode schedules Blender processes; --blender-render executes
inside Blender, creates the shared camera/light rig and writes four PNGs plus
render_log.json. Views Image_005/015/025/035 correspond to azimuths
45/135/225/315 degrees.

Use tasks/<setting>/run.py for generation and metrics/evaluate.py for
evaluation. Rendering failures are recorded with status, error and latency.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from core.blender_runtime import blender_environment

EVAL_ROOT = Path(os.environ.get("EVAL_ROOT")
                 or Path(__file__).resolve().parents[1] / "metrics")
DEFAULT_RESULTS_ROOT = EVAL_ROOT / "results"


def _default_blender() -> str:
    """$BLENDER, else the newest tools/blender-*/blender at or above EVAL_ROOT."""
    if os.environ.get("BLENDER"):
        return os.environ["BLENDER"]
    for base in (EVAL_ROOT, *EVAL_ROOT.parents):
        hits = sorted((base / "tools").glob("blender-*/blender"))
        if hits:
            return str(hits[-1])
    return "blender"


def parse_cli(argv=None):
    p = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__.split("\n\n", 1)[0],
    )
    p.add_argument("--model",         required=True,
                   help="Sub-folder name under `results/`.")
    p.add_argument("--results-root",  type=Path, default=DEFAULT_RESULTS_ROOT)
    p.add_argument("--output-results-root", type=Path, default=None,
                   help="Optional separate root for rendered outputs while "
                        "scripts are still read from --results-root.")
    p.add_argument("--blender",       default=_default_blender())
    p.add_argument("--samples",       type=int, default=64)
    p.add_argument("--resolution",    type=int, default=512)
    p.add_argument("--engine",        default="CYCLES",
                   choices=["CYCLES", "BLENDER_EEVEE"])
    p.add_argument("--light-power", type=float, default=1.0,
                   help="1.0 for benchmark colour; 0.5 for benchmark grey")
    p.add_argument("--light-ref-extent", type=float, default=2.5)
    p.add_argument("--renders-subdir", default="renders",
                   choices=["renders", "grey_renders"],
                   help="Per-instance output directory (default: renders).")
    p.add_argument("--grey-shaded", action="store_true",
                   help="Override all object materials with benchmark grey.")
    p.add_argument("--grey-value", type=float, default=0.55)
    p.add_argument("--instances",     nargs="*", default=None,
                   help="Limit to these instance folder names.")
    p.add_argument("--workers",       type=int, default=1,
                   help="Parallel Blender subprocesses (1 is safest on single GPU).")
    p.add_argument("--timeout",       type=int, default=240,
                   help="Per-instance Blender timeout in seconds.")
    p.add_argument("--overwrite",     action="store_true")
    return p.parse_args(argv)


def render_one(args, this_script: Path, instance_dir: Path):
    name = instance_dir.name
    gen_script = instance_dir / f"{name}.py"
    output_instance_dir = instance_dir
    if args.output_results_root is not None:
        output_instance_dir = args.output_results_root / args.model / name
    renders_dir = output_instance_dir / args.renders_subdir
    log_path = renders_dir / "render_log.json"

    if not gen_script.exists():
        return name, "MISSING", None, 0.0

    if log_path.exists() and not args.overwrite:
        try:
            existing = json.loads(log_path.read_text())
            status = existing.get("status")
            same_mode = bool(existing.get("grey_shaded", False)) == args.grey_shaded
            same_value = (not args.grey_shaded
                          or existing.get("grey_value") == args.grey_value)
            if status == "OK" and same_mode and same_value:
                return name, status, "skip-existing", 0.0
        except Exception:
            pass  # fall through and re-render

    renders_dir.mkdir(parents=True, exist_ok=True)
    log_path.unlink(missing_ok=True)
    cmd = [args.blender, "--background", "--python-use-system-env",
           "--python", str(this_script), "--",
           "--blender-render",
           "--script",     str(gen_script),
           "--output-dir", str(renders_dir),
           "--samples",    str(args.samples),
           "--resolution", str(args.resolution),
           "--engine",     args.engine,
           "--light-power", str(args.light_power),
           "--light-ref-extent", str(args.light_ref_extent)]
    if args.grey_shaded:
        cmd.extend(["--grey-shaded", "--grey-value", str(args.grey_value)])
    t0 = time.time()
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=args.timeout,
                       env=blender_environment())
    except subprocess.TimeoutExpired:
        log_path.write_text(json.dumps({
            "script":   str(gen_script),
            "status":   "ERR_TIMEOUT",
            "error":    f"Blender subprocess exceeded {args.timeout}s",
            "n_meshes": 0,
            "n_views_rendered": 0,
            "grey_shaded": args.grey_shaded,
            "grey_value": args.grey_value if args.grey_shaded else None,
            "latency_s": round(time.time() - t0, 2),
        }, indent=2))
        return name, "ERR_TIMEOUT", None, time.time() - t0

    dt = time.time() - t0
    if log_path.exists():
        rec = json.loads(log_path.read_text())
        return name, rec.get("status", "?"), rec.get("error"), dt
    log_path.write_text(json.dumps({
        "script": str(gen_script),
        "status": "ERR_NOLOG",
        "error":  "Blender exited without writing render_log.json (likely segfault)",
        "n_meshes": 0,
        "n_views_rendered": 0,
        "grey_shaded": args.grey_shaded,
        "grey_value": args.grey_value if args.grey_shaded else None,
        "latency_s": round(dt, 2),
    }, indent=2))
    return name, "ERR_NOLOG", None, dt


def sanitize_script(gen_script: Path):
    if not gen_script.is_file():
        return
    import re
    content = gen_script.read_text(encoding="utf-8")
    updated = re.sub(
        r"""['"][^'"]*?(reconstruct_gt|local_recon_full|local_recon)\.py['"]""",
        f"'{gen_script.name}'",
        content
    )
    updated = re.sub(
        r"""\b(reconstruct_gt|local_recon_full|local_recon)\.py\b""",
        f"{gen_script.name}",
        updated
    )
    if updated != content:
        gen_script.write_text(updated, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    argsv = sys.argv[1:] if argv is None else argv
    if "--" in argsv:
        argsv = argsv[argsv.index("--") + 1:]
    if "--blender-render" in argsv:
        from core.blender_render import main as render_in_blender

        return render_in_blender(argsv)
    args = parse_cli(argsv)
    model_dir = args.results_root / args.model
    if not model_dir.exists():
        raise SystemExit(f"No such model dir: {model_dir}")

    instance_dirs = sorted(d for d in model_dir.iterdir() if d.is_dir())
    if args.instances:
        keep = set(args.instances)
        instance_dirs = [d for d in instance_dirs if d.name in keep]
        if not instance_dirs:
            raise SystemExit(f"No matches for {args.instances}")

    for d in instance_dirs:
        gen_script = d / f"{d.name}.py"
        sanitize_script(gen_script)

    this_script = Path(__file__).resolve()

    print(f"Model:       {args.model}")
    print(f"Instances:   {len(instance_dirs)}")
    print(f"Engine:      {args.engine}, samples={args.samples}, res={args.resolution}")
    output_root = args.output_results_root or args.results_root
    print(f"Output:      {output_root}/{args.model}/<instance>/"
          f"{args.renders_subdir}")
    print(f"Grey shaded: {args.grey_shaded}")
    print(f"Workers:     {args.workers}")
    print(f"Timeout:     {args.timeout}s per instance")
    print(f"Overwrite:   {args.overwrite}")
    print()

    counts = {}
    t_run = time.time()
    if args.workers <= 1:
        for d in instance_dirs:
            name, status, err, dt = render_one(args, this_script, d)
            counts[status] = counts.get(status, 0) + 1
            tail = f" -- {err.splitlines()[0]}" if err else ""
            print(f"  [{status:<13}] {name}  ({dt:.1f}s){tail}")
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(render_one, args, this_script, d): d
                    for d in instance_dirs}
            for fut in as_completed(futs):
                name, status, err, dt = fut.result()
                counts[status] = counts.get(status, 0) + 1
                tail = f" -- {err.splitlines()[0]}" if err else ""
                print(f"  [{status:<13}] {name}  ({dt:.1f}s){tail}")

    print()
    print(f"Done in {time.time()-t_run:.1f}s")
    for k in sorted(counts):
        print(f"  {k:<13} {counts[k]:>4}")

    failed_counts = [k for k in counts if k != "OK"]
    return 1 if failed_counts else 0


if __name__ == "__main__":
    raise SystemExit(main())
