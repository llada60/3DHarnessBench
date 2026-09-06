#!/usr/bin/env python3
"""Prepare graded outputs and run usage, shape, image, and Uni3D metrics.

The graded runner writes results to::

    outputs/<setting>/<w_texture|wo_texture>/<agent>/<instance>/

This entry point renders missing four-view predictions, builds prediction
GLBs, reads ``data/benchmark`` directly in its native layout, and runs usage,
Chamfer, image similarity, and Uni3D.

Run from this project root, for example::

    pixi run python evaluation/evaluate.py --data-path data/benchmark \
        --agent gpt-5-6-sol --setting Multi-view
"""

from __future__ import annotations

import argparse
import re
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


EVAL_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = EVAL_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from raw_agents.project_env import default_blender, load_project_env  # noqa: E402

load_project_env()

from raw_agents.agent_registry import SETTING_CHOICES  # noqa: E402
from raw_agents.common_cli import (  # noqa: E402
    add_agent,
    add_data_path,
    add_output_dir,
    add_tasks,
    add_texture_renders,
    positive_int,
)

DEFAULT_GT_PATH = PROJECT_ROOT / "data" / "benchmark"
DEFAULT_OUTPUTS_ROOT = PROJECT_ROOT / "outputs"
DEFAULT_GREY_SHADED_OUTPUTS_ROOT = (
    PROJECT_ROOT / "grey_shaded_outputs")
DEFAULT_UNI3D_REPO = EVAL_ROOT / "external" / "Uni3D"
PREPROCESSOR = EVAL_ROOT / "core" / "export_glb.py"
DEFAULT_BLENDER = Path(default_blender())
VIEWS = ("Image_005.png", "Image_015.png", "Image_025.png", "Image_035.png")
ENCODERS = ("siglip2", "dinov2", "dinov3")
SCENE_RIG_VERSION = "benchmark_extent_squared_v1"
GEOMETRY_PREP_VERSION = "realize_gn_instances_v1"
GREY_MATERIAL_VALUE = 0.55
FINAL_METRIC_FILES = {
    "siglip2": "image_similarity_siglip2.json",
    "dinov2": "image_similarity_dinov2.json",
    "dinov3": "image_similarity_dinov3.json",
    "grey_shaded_siglip2": "image_similarity_siglip2_grey_shaded.json",
    "grey_shaded_dinov2": "image_similarity_dinov2_grey_shaded.json",
    "grey_shaded_dinov3": "image_similarity_dinov3_grey_shaded.json",
    "chamfer": "shape_chamfer.json",
    "uni3d": "shape_uni3d.json",
    "usage": "usage.json",
}
# API-equivalent list prices in USD per million tokens. These estimate what
# the recorded usage would cost under API billing; they are not ChatGPT-plan
# charges. Keep model-specific so an unknown agent never gets a made-up cost.
API_TOKEN_PRICES = {
    "gpt-5-6-sol": {"input": 5.0, "cached_input": 0.5, "output": 30.0},
}


def resolve_input_path(path: Path) -> Path:
    if path.is_absolute():
        return path.resolve()
    from_cwd = (Path.cwd() / path).resolve()
    if from_cwd.exists():
        return from_cwd
    return (PROJECT_ROOT / path).resolve()


def resolve_blender(path: Path) -> Path:
    """Resolve a Blender path consistently with the four experiment runners."""
    value = str(path)
    if os.sep not in value:
        found = shutil.which(value)
        return Path(found).resolve() if found else Path(value)
    return resolve_input_path(path)


def validate_uni3d_repo(path: Path | str | None = None) -> Path:
    """Return a Uni3D checkout containing the module used by the evaluator."""
    configured = path or os.environ.get("UNI3D_REPO") or DEFAULT_UNI3D_REPO
    repo = resolve_input_path(Path(configured).expanduser())
    required = repo / "models" / "point_encoder.py"
    if not required.is_file():
        raise SystemExit(
            "Missing Uni3D module: "
            f"{required}\n"
            "Clone https://github.com/baaivision/Uni3D.git to "
            "evaluation/external/Uni3D or set UNI3D_REPO to another local "
            "checkout. The harness checks module presence but does not "
            "enforce a Uni3D commit or tag."
        )
    return repo


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="All relative paths are resolved from this project root.",
    )
    add_data_path(
        parser, DEFAULT_GT_PATH, dest="data_path",
        help_text="GT root containing per-instance renders and GLBs "
        "(default: %(default)s)",
    )
    add_texture_renders(parser)
    parser.add_argument("--grey-shaded", action="store_true",
                        help="Additionally render generated objects with the "
                             "benchmark grey material and compare those views "
                             "against GT grey_renders.")
    add_agent(parser, required=True)
    parser.add_argument(
        "--setting", "--evaluate-setting", "--evaluate_setting",
        dest="setting", required=True, choices=SETTING_CHOICES,
        help="experiment setting to evaluate",
    )
    add_output_dir(
        parser, DEFAULT_OUTPUTS_ROOT, dest="output_dir",
        legacy_flags=("--outputs-root",),
        help_text="base experiment output directory (default: %(default)s)",
    )
    parser.add_argument("--grey-shaded-outputs-root", type=Path,
                        default=DEFAULT_GREY_SHADED_OUTPUTS_ROOT,
                        help="Root for --grey-shaded renders and image metrics "
                             "(default: grey_shaded_outputs).")
    parser.add_argument("--blender", type=Path, default=DEFAULT_BLENDER,
                        help="Blender binary used for render and bake "
                             "(default: %(default)s).")
    add_tasks(parser)
    parser.add_argument("--limit", type=positive_int, default=None)
    parser.add_argument("--image-encoders", nargs="+", choices=ENCODERS,
                        default=list(ENCODERS))
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--n-points", type=int, default=8192)
    parser.add_argument("--render-workers", type=int, default=1)
    parser.add_argument("--prepare-workers", type=int, default=4,
                        help="Concurrent Blender preprocessing workers.")
    parser.add_argument(
        "--num-parallel", "--num_parallel", type=positive_int, default=None,
        help="set both render and preprocessing worker counts",
    )
    parser.add_argument("--samples", type=int, default=64)
    parser.add_argument("--resolution", type=int, default=512)
    parser.add_argument("--light-ref-extent", type=float, default=2.5,
                        help="Benchmark light normalization extent.")
    # 900s is not enough for the worst bakes: ReedMonocotFactory's per-object
    # albedo bake measures ~1215s end-to-end.
    parser.add_argument("--timeout", type=int, default=2400)
    parser.add_argument("--overwrite-artifacts", action="store_true")
    parser.add_argument("--overwrite-metrics", action="store_true")
    parser.add_argument("--skip-prepare", action="store_true",
                        help="Do not render/export; score whatever exists.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.num_parallel is not None:
        args.render_workers = args.num_parallel
        args.prepare_workers = args.num_parallel
    return args


def _iterations_complete(instance_dir: Path) -> bool:
    """Return True if process.json shows all iterations finished.

    An instance is considered complete when its ``process.json`` exists and
    the number of recorded iterations equals ``settings.total_iterations``.
    Instances without a ``process.json`` are assumed complete (legacy layout).
    """
    process_json = instance_dir / "process.json"
    if not process_json.is_file():
        # No process metadata – assume the instance is usable.
        return True
    try:
        data = json.loads(process_json.read_text())
    except (OSError, json.JSONDecodeError):
        return True  # unreadable – let downstream stages decide
    total = data.get("settings", {}).get("total_iterations")
    if total is None:
        return True  # field absent – cannot judge
    actual = len(data.get("iterations", []))
    return actual >= total


def selected_instances(model_dir: Path, gt_root: Path,
                       requested: list[str] | None, limit: int | None) -> list[str]:
    available = {
        d.name for d in model_dir.iterdir()
        if d.is_dir() and not d.name.startswith("_")
        and (d / f"{d.name}.py").is_file()
    }
    gt_available = {
        d.name for d in gt_root.iterdir()
        if d.is_dir() and not d.name.startswith("_")
    }
    names = sorted(available & gt_available)
    if requested:
        wanted = set(requested)
        missing = wanted - set(names)
        if missing:
            raise SystemExit(
                "Requested instances missing from generated scripts or GT: "
                f"{sorted(missing)}"
            )
        names = [name for name in names if name in wanted]

    # ── skip instances that have not completed all iterations ──
    skipped = [n for n in names if not _iterations_complete(model_dir / n)]
    if skipped:
        print(f"Skipping {len(skipped)} incomplete instance(s) "
              f"(iterations not finished): {skipped}")
        names = [n for n in names if n not in set(skipped)]

    if limit is not None:
        names = names[:limit]
    if not names:
        raise SystemExit("No matching generated/GT instances to evaluate.")
    return names


def replace_symlink(link: Path, target: Path, overwrite: bool = False) -> None:
    """Create an absolute symlink while preserving unrelated real files."""
    if link.is_symlink():
        if link.resolve(strict=False) == target.resolve():
            return
        link.unlink()
    elif link.exists():
        if not overwrite:
            return
        if link.is_dir():
            raise RuntimeError(f"refusing to replace real directory: {link}")
        link.unlink()
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target.resolve(), target_is_directory=target.is_dir())


def validate_gt_inputs(gt_root: Path, names: list[str], texture: bool,
                       grey_shaded: bool = False) -> None:
    """Validate the benchmark-native GT paths without staging or copying."""
    render_dir = "color_renders" if texture else "grey_renders"
    glb_suffix = ".glb" if texture else "_grey.glb"
    for name in names:
        source = gt_root / name
        images = source / render_dir
        glb = source / f"{name}{glb_suffix}"
        if not images.is_dir():
            raise SystemExit(f"Missing GT renders: {images}")
        if not glb.is_file():
            raise SystemExit(f"Missing GT GLB: {glb}")
        grey_images = source / "grey_renders"
        if grey_shaded and not grey_images.is_dir():
            raise SystemExit(f"Missing GT renders: {grey_images}")


def run_command(label: str, command: list[str], dry_run: bool,
                env: dict[str, str] | None = None) -> dict:
    printable = " ".join(command)
    print(f"\n[{label}]\n  {printable}", flush=True)
    if dry_run:
        return {"label": label, "command": command, "returncode": None,
                "status": "DRY_RUN", "elapsed_s": 0.0}
    started = time.time()
    proc = subprocess.run(command, env=env)
    elapsed = round(time.time() - started, 2)
    status = "OK" if proc.returncode == 0 else "FAILED"
    print(f"[{label}] {status} ({elapsed:.1f}s)", flush=True)
    return {"label": label, "command": command,
            "returncode": proc.returncode, "status": status,
            "elapsed_s": elapsed}


def add_instances(command: list[str], names: list[str]) -> None:
    if names:
        command.extend(["--instances", *names])


def read_metric_json(metrics_dir: Path, key: str) -> dict:
    path = metrics_dir / FINAL_METRIC_FILES[key]
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise ValueError(f"missing required metric file: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read required metric file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"metric file is not a JSON object: {path}")
    return value


def nested_number(data: dict, path: tuple[str, ...], source: str,
                  allow_none: bool = False) -> int | float | None:
    value: object = data
    try:
        for key in path:
            if not isinstance(value, dict):
                raise KeyError(key)
            value = value[key]
    except KeyError as exc:
        dotted = ".".join(path)
        raise ValueError(f"missing {dotted} in {source}") from exc
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        dotted = ".".join(path)
        raise ValueError(f"expected numeric {dotted} in {source}, got {value!r}")
    return value


def average_estimated_api_cost(usage: dict) -> float | None:
    prices = API_TOKEN_PRICES.get(str(usage.get("model")))
    if prices is None:
        return None
    n_instances = nested_number(usage, ("n_instances",), "usage")
    input_tokens = nested_number(usage, ("tokens", "input", "total"), "usage")
    cached_tokens = nested_number(
        usage, ("tokens", "input_cached", "total"), "usage")
    output_tokens = nested_number(
        usage, ("tokens", "output", "total"), "usage")
    if not n_instances or cached_tokens > input_tokens:
        return None
    total = ((input_tokens - cached_tokens) * prices["input"]
             + cached_tokens * prices["cached_input"]
             + output_tokens * prices["output"]) / 1_000_000
    return total / n_instances


def rounded_integer(value: int | float | None) -> int | None:
    """Round an optional metric value to the nearest integer."""
    return None if value is None else round(value)


def formatted_integer(value: int | float | None) -> str | None:
    """Format an optional metric as a comma-separated integer string."""
    return None if value is None else f"{value:,.0f}"


def average_agent_latency(metrics_dir: Path, usage: dict) -> float | None:
    """Return active agent seconds per instance, excluding render/pause time.

    Legacy runs may have a separately backfilled latency metric because their
    per-instance usage files predate ``agent_seconds``.  Prefer that auditable
    reconstruction; otherwise use fully covered native usage accounting.
    """
    path = metrics_dir / "latency.json"
    if path.is_file():
        try:
            latency = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"could not read latency metric {path}: {exc}") from exc
        latency_n = nested_number(
            latency, ("n_instances",), "latency", allow_none=True)
        usage_n = nested_number(
            usage, ("n_instances",), "usage", allow_none=True)
        if latency_n == usage_n:
            average = nested_number(
                latency, ("agent_seconds", "average_per_instance"),
                "latency", allow_none=True)
            if average is not None:
                return average
    summary = usage.get("agent_seconds")
    if not isinstance(summary, dict):
        return None
    return nested_number(
        usage, ("agent_seconds", "average_per_evaluated_instance"),
        "usage", allow_none=True)


def write_final_metrics(
    metrics_dir: Path,
    grey_shaded_encoders: list[str] | None = None,
    grey_shaded_metrics_dir: Path | None = None,
    image_encoders: list[str] | tuple[str, ...] = ENCODERS,
) -> Path:
    """Write the paper-facing aggregate metrics."""
    image_metrics = {
        encoder: nested_number(
            read_metric_json(metrics_dir, encoder),
            ("best_assignment", "penalized_mean"), encoder)
        for encoder in image_encoders
    }
    chamfer = read_metric_json(metrics_dir, "chamfer")
    uni3d = read_metric_json(metrics_dir, "uni3d")
    usage = read_metric_json(metrics_dir, "usage")

    task = uni3d.get("task")
    ti_key = "cos_text_3d" if task == "text_to_3d" else "cos_image_3d"
    chamfer_alignment_key = (
        "cd_axismin" if isinstance(chamfer.get("cd_axismin"), dict)
        else "cd_yawmin"
    )
    final = {
        **image_metrics,
        "chamfer": nested_number(
            chamfer, (chamfer_alignment_key, "penalized_mean"), "chamfer"),
        "uni3d": nested_number(
            uni3d, ("cos_3d_3d", "penalized_mean"), "uni3d"),
        "uni3d_t_i_3d": nested_number(
            uni3d, (ti_key, "penalized_mean"), "uni3d"),
        "avg_attempts": nested_number(
            usage, ("attempt_count", "average_per_evaluated_instance"),
            "usage", allow_none=True),
        "avg_api_calls": rounded_integer(nested_number(
            usage, ("api_calls", "average_per_evaluated_instance"),
            "usage", allow_none=True)),
        "avg_tokens": formatted_integer(nested_number(
            usage, ("tokens", "total", "average_per_evaluated_instance"),
            "usage", allow_none=True)),
        "avg_latency_s": average_agent_latency(metrics_dir, usage),
        "avg_cost_usd": nested_number(
            usage, ("cost_usd", "average_per_evaluated_instance"),
            "usage", allow_none=True),
        "avg_estimated_api_cost_usd": average_estimated_api_cost(usage),
    }
    grey_metrics_dir = grey_shaded_metrics_dir or metrics_dir
    for encoder in grey_shaded_encoders or []:
        metric_key = f"grey_shaded_{encoder}"
        metric = read_metric_json(grey_metrics_dir, metric_key)
        final[metric_key] = nested_number(
            metric, ("best_assignment", "penalized_mean"), metric_key)
    output = metrics_dir / "final_metrics.json"
    output.write_text(json.dumps(final, indent=2) + "\n")
    return output


def sanitize_instance_scripts(model_dir: Path, names: list[str]) -> None:
    """Sanitize generated Blender Python scripts for all evaluate settings."""
    for name in names:
        script_path = model_dir / name / f"{name}.py"
        if not script_path.is_file():
            continue
        content = script_path.read_text(encoding="utf-8")
        updated = re.sub(
            r"""['"][^'"]*?(reconstruct_gt|local_recon_full|local_recon)\.py['"]""",
            f"'{script_path.name}'",
            content
        )
        updated = re.sub(
            r"""\b(reconstruct_gt|local_recon_full|local_recon)\.py\b""",
            f"{script_path.name}",
            updated
        )
        if updated != content:
            script_path.write_text(updated, encoding="utf-8")


def _is_render_failed_or_missing(
    instance_dir: Path,
    *,
    light_power: float,
    light_ref_extent: float,
    renders_subdir: str = "renders",
    grey_shaded: bool = False,
    grey_value: float = GREY_MATERIAL_VALUE,
) -> bool:
    """Return whether renders are absent, failed, or use an obsolete rig."""
    renders_dir = instance_dir / renders_subdir
    log_path = renders_dir / "render_log.json"
    if not log_path.is_file():
        return True
    try:
        data = json.loads(log_path.read_text(encoding="utf-8"))
        if data.get("status") != "OK":
            return True
        if data.get("rig_version") != SCENE_RIG_VERSION:
            return True
        if data.get("geometry_prep_version") != GEOMETRY_PREP_VERSION:
            return True
        if float(data.get("light_power")) != light_power:
            return True
        if float(data.get("light_ref_extent")) != light_ref_extent:
            return True
        if bool(data.get("grey_shaded", False)) != grey_shaded:
            return True
        if grey_shaded and float(data.get("grey_value")) != grey_value:
            return True
    except Exception:
        return True
    for view in VIEWS:
        if not (renders_dir / view).is_file():
            return True
    return False


def prepare_predictions(args: argparse.Namespace, model_dir: Path,
                        results_root: Path, grey_results_root: Path,
                        grey_model_dir: Path, agent: str,
                        names: list[str], records: list[dict]
                        ) -> tuple[bool, bool, bool]:
    if not args.dry_run:
        sanitize_instance_scripts(model_dir, names)

    render_light_power = 1.0 if args.texture_renders else 0.5
    missing_renders = [
        name for name in names
        if args.overwrite_artifacts
        or _is_render_failed_or_missing(
            model_dir / name,
            light_power=render_light_power,
            light_ref_extent=args.light_ref_extent)
    ]
    if missing_renders:
        command = [
            sys.executable, str(EVAL_ROOT / "core" / "render.py"),
            "--model", agent, "--results-root", str(results_root),
            "--blender", str(args.blender),
            "--workers", str(args.render_workers),
            "--samples", str(args.samples),
            "--resolution", str(args.resolution),
            "--light-power", str(render_light_power),
            "--light-ref-extent", str(args.light_ref_extent),
            "--timeout", str(args.timeout), "--overwrite",
        ]
        add_instances(command, missing_renders)
        records.append(run_command("prepare:renders", command, args.dry_run))
    else:
        print("\n[prepare:renders] all selected four-view renders exist; skip")

    missing_grey_renders: list[str] = []
    if args.grey_shaded:
        missing_grey_renders = [
            name for name in names
            if args.overwrite_artifacts
            or _is_render_failed_or_missing(
                grey_model_dir / name,
                light_power=0.5,
                light_ref_extent=args.light_ref_extent,
                renders_subdir="grey_renders",
                grey_shaded=True)
        ]
        if missing_grey_renders:
            command = [
                sys.executable, str(EVAL_ROOT / "core" / "render.py"),
                "--model", agent, "--results-root", str(results_root),
                "--output-results-root", str(grey_results_root),
                "--blender", str(args.blender),
                "--workers", str(args.render_workers),
                "--samples", str(args.samples),
                "--resolution", str(args.resolution),
                "--light-power", "0.5",
                "--light-ref-extent", str(args.light_ref_extent),
                "--renders-subdir", "grey_renders",
                "--grey-shaded", "--grey-value", str(GREY_MATERIAL_VALUE),
                "--timeout", str(args.timeout), "--overwrite",
            ]
            add_instances(command, missing_grey_renders)
            records.append(run_command(
                "prepare:grey-renders", command, args.dry_run))
        else:
            print("\n[prepare:grey-renders] all selected grey-shaded "
                  "four-view renders exist; skip")

    artifact_root = model_dir / "_evaluation_artifacts"
    suffix = ".glb" if args.texture_renders else "_grey.glb"
    fallback_glbs: set[str] = set()
    artifact_geometry_current = False
    preprocess_summary = artifact_root / "preprocess_summary.json"
    if preprocess_summary.is_file():
        try:
            previous = json.loads(preprocess_summary.read_text())
            artifact_geometry_current = (
                previous.get("geometry_prep_version")
                == GEOMETRY_PREP_VERSION
            )
            if args.texture_renders:
                fallback_glbs = {
                    row["instance"]
                    for row in previous.get("instances", [])
                    if row.get("bake_fallback") is True
                    and row.get("instance") in names
                }
        except (OSError, json.JSONDecodeError, TypeError, KeyError):
            pass
    if fallback_glbs:
        print(f"\n[prepare:baked-glb] rebuilding {len(fallback_glbs)} "
              "previous no-bake fallback(s)")
    missing_glbs = [
        name for name in names
        if args.overwrite_artifacts
        or not artifact_geometry_current
        or name in fallback_glbs
        or not (artifact_root / name / f"{name}{suffix}").is_file()
    ]
    if missing_glbs:
        command = [
            sys.executable, str(PREPROCESSOR),
            "--input_data_path", str(model_dir),
            "--output_data_path", str(artifact_root),
            "--blender", str(args.blender),
            "--instances", *missing_glbs,
            "--workers", str(args.prepare_workers),
            "--samples", str(args.samples),
            "--resolution", str(args.resolution),
            "--light-ref-extent", str(args.light_ref_extent),
            "--timeout", str(args.timeout),
            "--failed-list", str(artifact_root / "_failed_instances.json"),
            "--summary-json", str(artifact_root / "preprocess_summary.json"),
            "--artifact-mode", ("color-glb-only" if args.texture_renders
                                else "grey-glb-only"),
        ]
        if args.overwrite_artifacts or fallback_glbs:
            command.append("--overwrite")
        if not args.texture_renders:
            # Only the grey GLB is consumed in this setting; avoid the costly
            # albedo bake performed solely for the unused colour GLB.
            command.append("--no-bake-texture")
        records.append(run_command("prepare:baked-glb", command, args.dry_run))
    else:
        print("\n[prepare:baked-glb] all selected baked GLBs exist; skip")

    if not args.dry_run:
        linked = 0
        for name in names:
            source = artifact_root / name / f"{name}{suffix}"
            if not source.is_file():
                continue
            target = model_dir / name / "glb" / f"{name}.glb"
            replace_symlink(target, source, overwrite=args.overwrite_artifacts)
            linked += 1
        print(f"[prepare:baked-glb] prepared metric links for {linked}/{len(names)} instances")
    return (bool(missing_renders), bool(missing_grey_renders),
            bool(missing_glbs))


def metric_commands(args: argparse.Namespace, results_root: Path,
                    grey_results_root: Path, agent: str, gt_root: Path,
                    names: list[str]) -> list[tuple[str, list[str]]]:
    gt_renders_subdir = ("color_renders" if args.texture_renders
                         else "grey_renders")
    gt_glb_relpath = ("{instance}.glb" if args.texture_renders
                      else "{instance}_grey.glb")
    common = ["--model", agent, "--results-root", str(results_root),
              "--data-root", str(gt_root)]
    commands: list[tuple[str, list[str]]] = []

    usage = [sys.executable, str(EVAL_ROOT / "metrics" / "usage.py"),
             "--model", agent, "--results-root", str(results_root)]
    add_instances(usage, names)
    commands.append(("metric:usage", usage))

    chamfer = [sys.executable, str(EVAL_ROOT / "metrics" / "shape_chamfer.py"),
               *common, "--gt-glb-relpath", gt_glb_relpath,
               "--n-points", str(args.n_points)]
    add_instances(chamfer, names)
    commands.append(("metric:shape_chamfer", chamfer))

    for encoder in args.image_encoders:
        image = [
            sys.executable, str(EVAL_ROOT / "metrics" / "image_similarity.py"),
            *common, "--gt-renders-subdir", gt_renders_subdir,
            "--encoder", encoder, "--device", args.device,
            "--batch-size", str(args.batch_size),
        ]
        add_instances(image, names)
        commands.append((f"metric:image_similarity:{encoder}", image))
        if args.grey_shaded:
            grey_image = [
                sys.executable,
                str(EVAL_ROOT / "metrics" / "image_similarity.py"),
                "--model", agent,
                "--results-root", str(grey_results_root),
                "--data-root", str(gt_root),
                "--renders-subdir", "grey_renders",
                "--gt-renders-subdir", "grey_renders",
                "--output-suffix", "grey_shaded",
                "--encoder", encoder,
                "--device", args.device,
                "--batch-size", str(args.batch_size),
            ]
            add_instances(grey_image, names)
            commands.append((
                f"metric:grey_shaded_image_similarity:{encoder}",
                grey_image,
            ))

    uni3d = [
        sys.executable, str(EVAL_ROOT / "metrics" / "shape_uni3d.py"),
        *common, "--gt-renders-subdir", gt_renders_subdir,
        "--gt-glb-relpath", gt_glb_relpath,
        "--task", "image_to_3d", "--device", args.device,
        "--n-points", str(args.n_points), "--overwrite",
    ]
    add_instances(uni3d, names)
    commands.append(("metric:shape_uni3d", uni3d))
    return commands


def main() -> int:
    args = parse_args()
    gt_root = resolve_input_path(args.data_path)
    outputs_root = resolve_input_path(args.output_dir)
    grey_shaded_outputs_root = resolve_input_path(
        args.grey_shaded_outputs_root)
    agent = args.agent
    texture_folder = "w_texture" if args.texture_renders else "wo_texture"
    results_root = outputs_root / args.setting / texture_folder
    model_dir = results_root / agent
    grey_results_root = (
        grey_shaded_outputs_root / args.setting / texture_folder)
    grey_model_dir = grey_results_root / agent

    if not gt_root.is_dir():
        raise SystemExit(f"No such GT path: {gt_root}")
    if not model_dir.is_dir():
        raise SystemExit(f"No such evaluate_data_path: {model_dir}")
    if not PREPROCESSOR.is_file():
        raise SystemExit(f"Missing preprocessing script: {PREPROCESSOR}")
    # Normalize the path inherited by shape_uni3d.py, including when a
    # project-relative UNI3D_REPO was supplied from another working directory.
    os.environ["UNI3D_REPO"] = str(validate_uni3d_repo())
    args.blender = resolve_blender(args.blender)
    if not args.dry_run and not args.blender.is_file():
        raise SystemExit(f"Missing Blender binary: {args.blender}")

    names = selected_instances(model_dir, gt_root, args.tasks, args.limit)
    validate_gt_inputs(
        gt_root, names, args.texture_renders, grey_shaded=args.grey_shaded)
    if not args.dry_run:
        sanitize_instance_scripts(model_dir, names)
        if args.grey_shaded:
            for name in names:
                (grey_model_dir / name).mkdir(parents=True, exist_ok=True)

    print("Evaluation configuration")
    print(f"  GT path:              {gt_root}")
    print(f"  texture renders:      {args.texture_renders} ({texture_folder})")
    print(f"  grey-shaded images:   {args.grey_shaded}")
    print(f"  agent:                {args.agent} -> {agent}")
    print(f"  setting:              {args.setting}")
    print(f"  evaluate_data_path:   {model_dir}")
    if args.grey_shaded:
        print(f"  grey-shaded output:   {grey_model_dir}")
    print(f"  blender:              {args.blender}")
    print(f"  instances:            {len(names)}")
    print(f"  image encoders:       {', '.join(args.image_encoders)}")

    records: list[dict] = []
    renders_changed = False
    grey_renders_changed = False
    glbs_changed = False
    if not args.skip_prepare:
        (renders_changed,
         grey_renders_changed,
         glbs_changed) = prepare_predictions(
             args, model_dir, results_root, grey_results_root, grey_model_dir,
             agent, names, records)
    else:
        print("\n[prepare] skipped by --skip-prepare")

    metrics_dir = model_dir / "_metrics"
    grey_metrics_dir = grey_model_dir / "_metrics"
    for label, command in metric_commands(
            args, results_root, grey_results_root, agent, gt_root, names):
        output_name = {
            "metric:usage": "usage.json",
            "metric:shape_chamfer": "shape_chamfer.json",
            "metric:shape_uni3d": "shape_uni3d.json",
        }.get(label)
        if label.startswith("metric:image_similarity:"):
            output_name = f"image_similarity_{label.rsplit(':', 1)[1]}.json"
        elif label.startswith("metric:grey_shaded_image_similarity:"):
            encoder = label.rsplit(":", 1)[1]
            output_name = f"image_similarity_{encoder}_grey_shaded.json"
        output_dir = (grey_metrics_dir
                      if label.startswith(
                          "metric:grey_shaded_image_similarity:")
                      else metrics_dir)
        output_path = output_dir / output_name if output_name else None
        render_metric_invalidated = (
            renders_changed and label.startswith("metric:image_similarity:"))
        render_metric_invalidated = render_metric_invalidated or (
            grey_renders_changed
            and label.startswith("metric:grey_shaded_image_similarity:"))
        shape_metric_invalidated = (
            glbs_changed
            and label in ("metric:shape_chamfer", "metric:shape_uni3d")
        )
        shape_metric_invalidated = shape_metric_invalidated or (
            renders_changed and label == "metric:shape_uni3d"
        )
        chamfer_schema_invalidated = False
        if (label == "metric:shape_chamfer" and output_path
                and output_path.is_file()):
            try:
                existing_chamfer = json.loads(output_path.read_text())
                chamfer_schema_invalidated = not isinstance(
                    existing_chamfer.get("cd_axismin"), dict)
            except (OSError, json.JSONDecodeError):
                chamfer_schema_invalidated = True
        if (label != "metric:usage" and output_path and output_path.is_file()
                and not args.overwrite_metrics
                and not render_metric_invalidated
                and not shape_metric_invalidated
                and not chamfer_schema_invalidated):
            print(f"\n[{label}] {output_path.name} exists; skip "
                  "(use --overwrite-metrics to rerun)")
            records.append({"label": label, "command": command,
                            "returncode": None, "status": "SKIPPED_EXISTING",
                            "elapsed_s": 0.0, "output": str(output_path)})
            continue
        record = run_command(label, command, args.dry_run)
        if output_path:
            record["output"] = str(output_path)
        records.append(record)

    failed = [r for r in records if r["status"] == "FAILED"]
    if not args.dry_run and not failed:
        try:
            final_path = write_final_metrics(
                metrics_dir,
                args.image_encoders if args.grey_shaded else None,
                grey_metrics_dir if args.grey_shaded else None,
                image_encoders=args.image_encoders,
            )
            print(f"\nFinal paper metrics: {final_path}")
            records.append({"label": "finalize:final_metrics",
                            "returncode": 0, "status": "OK",
                            "elapsed_s": 0.0, "output": str(final_path)})
        except (OSError, ValueError) as exc:
            print(f"\n[finalize:final_metrics] FAILED: {exc}")
            records.append({"label": "finalize:final_metrics",
                            "returncode": 1, "status": "FAILED",
                            "elapsed_s": 0.0, "error": str(exc)})

    summary = {
        "gt_path": str(gt_root),
        "texture_renders": args.texture_renders,
        "grey_shaded": args.grey_shaded,
        "agent_input": args.agent,
        "agent": agent,
        "setting": args.setting,
        "evaluate_data_path": str(model_dir),
        "grey_shaded_evaluate_data_path": (
            str(grey_model_dir) if args.grey_shaded else None),
        "n_instances": len(names),
        "instances": names,
        "image_encoders": args.image_encoders,
        "runs": records,
    }
    failed = [r for r in records if r["status"] == "FAILED"]
    summary["status"] = "FAILED" if failed else ("DRY_RUN" if args.dry_run else "OK")
    if not args.dry_run:
        metrics_dir.mkdir(parents=True, exist_ok=True)
        summary_path = metrics_dir / "evaluation_run.json"
        summary_path.write_text(json.dumps(summary, indent=2) + "\n")
        print(f"\nEvaluation summary: {summary_path}")
        if args.grey_shaded:
            grey_runs = [
                record for record in records
                if record["label"] == "prepare:grey-renders"
                or record["label"].startswith(
                    "metric:grey_shaded_image_similarity:")
            ]
            grey_failed = [
                record for record in grey_runs
                if record["status"] == "FAILED"
            ]
            grey_summary = {
                "gt_path": str(gt_root),
                "texture_renders": args.texture_renders,
                "grey_shaded": True,
                "source_evaluate_data_path": str(model_dir),
                "evaluate_data_path": str(grey_model_dir),
                "n_instances": len(names),
                "instances": names,
                "image_encoders": args.image_encoders,
                "runs": grey_runs,
                "status": "FAILED" if grey_failed else "OK",
            }
            grey_metrics_dir.mkdir(parents=True, exist_ok=True)
            grey_summary_path = grey_metrics_dir / "evaluation_run.json"
            grey_summary_path.write_text(
                json.dumps(grey_summary, indent=2) + "\n")
            print(f"Grey-shaded evaluation summary: {grey_summary_path}")
    print(f"Final status: {summary['status']}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
