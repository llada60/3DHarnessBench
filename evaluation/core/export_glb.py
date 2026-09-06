#!/usr/bin/env python3
"""Build evaluation GLBs from generated Blender Python scripts.

This internal worker is called by evaluation/evaluate.py. The normal Python
process schedules one Blender subprocess per instance; Blender
executes the published script and exports its renderable scene objects to the
evaluation artifact directory.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


WORKER_ARGV = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
GEOMETRY_PREP_VERSION = "realize_gn_instances_v1"


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


if "--blender-worker" in WORKER_ARGV:
    import runpy
    import traceback

    import bpy

    parser = argparse.ArgumentParser()
    parser.add_argument("--blender-worker", action="store_true")
    parser.add_argument("--script", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--strip-materials", action="store_true")
    worker = parser.parse_args(WORKER_ARGV)

    def realize_geometry_node_instances(modifier) -> int:
        if modifier.type != "NODES" or modifier.node_group is None:
            return 0
        inserted = 0
        tree = modifier.node_group
        for output in [
            node for node in tree.nodes
            if node.bl_idname == "NodeGroupOutput"
            and getattr(node, "is_active_output", True)
        ]:
            for socket in output.inputs:
                if (getattr(socket, "type", None) != "GEOMETRY"
                        or not socket.is_linked):
                    continue
                link = socket.links[0]
                if link.from_node.bl_idname == "GeometryNodeRealizeInstances":
                    continue
                source = link.from_socket
                realize = tree.nodes.new("GeometryNodeRealizeInstances")
                realize.name = f"__graded_realize_instances_{inserted}"
                realize.location = (output.location.x - 180,
                                    output.location.y - 80 * inserted)
                tree.links.remove(link)
                tree.links.new(source, realize.inputs["Geometry"])
                tree.links.new(realize.outputs["Geometry"], socket)
                inserted += 1
        return inserted

    started = time.monotonic()
    record: dict[str, object] = {
        "status": None,
        "script": str(Path(worker.script).resolve()),
        "output": str(Path(worker.output).resolve()),
    }
    try:
        bpy.ops.wm.read_factory_settings(use_empty=True)
        runpy.run_path(worker.script, run_name="__main__")
        renderable = []
        for obj in list(bpy.context.scene.objects):
            if obj.type in {"CAMERA", "LIGHT"}:
                bpy.data.objects.remove(obj, do_unlink=True)
                continue
            if obj.type in {
                "MESH", "CURVE", "SURFACE", "FONT", "META", "VOLUME",
                "POINTCLOUD", "CURVES", "GREASEPENCIL",
            }:
                renderable.append(obj)
                for modifier in list(obj.modifiers):
                    realize_geometry_node_instances(modifier)
                if worker.strip_materials and hasattr(obj.data, "materials"):
                    obj.data.materials.clear()
        if not renderable:
            raise RuntimeError("script produced no renderable objects")

        bpy.ops.object.select_all(action="DESELECT")
        for obj in renderable:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = renderable[0]
        output = Path(worker.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        bpy.ops.export_scene.gltf(
            filepath=str(output),
            export_format="GLB",
            use_selection=True,
            export_apply=True,
            export_yup=True,
            export_materials="NONE" if worker.strip_materials else "EXPORT",
        )
        record.update({
            "status": "OK",
            "objects": len(renderable),
            "size_bytes": output.stat().st_size,
        })
    except Exception as exc:  # noqa: BLE001 - persisted for batch diagnosis
        record.update({
            "status": "ERROR",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        })
    finally:
        record["elapsed_s"] = round(time.monotonic() - started, 3)
        log = Path(worker.log)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps(record, indent=2) + "\n")
    raise SystemExit(0 if record["status"] == "OK" else 1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input_data_path", type=Path, required=True)
    parser.add_argument("--output_data_path", type=Path, required=True)
    parser.add_argument("--blender", required=True)
    parser.add_argument("--instances", nargs="*", default=None)
    parser.add_argument("--workers", type=_positive_int, default=4)
    parser.add_argument("--timeout", type=_positive_int, default=600)
    parser.add_argument("--failed-list", type=Path)
    parser.add_argument("--summary-json", type=Path)
    parser.add_argument(
        "--artifact-mode",
        choices=("color-glb-only", "grey-glb-only"),
        default="color-glb-only",
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--no-bake-texture", action="store_true")
    # Accepted for compatibility with the evaluation entry point. GLB export
    # itself does not render samples or use the grading camera.
    parser.add_argument("--samples", type=_positive_int, default=64)
    parser.add_argument("--resolution", type=_positive_int, default=512)
    parser.add_argument("--light-ref-extent", type=float, default=2.5)
    return parser.parse_args()


def discover(root: Path, requested: list[str] | None) -> list[Path]:
    available = {
        path.name: path
        for path in root.iterdir()
        if path.is_dir() and (path / f"{path.name}.py").is_file()
    }
    if requested:
        missing = sorted(set(requested) - set(available))
        if missing:
            raise SystemExit(f"unknown generated instance(s): {missing}")
        return [available[name] for name in requested]
    return [available[name] for name in sorted(available)]


def run_instance(args: argparse.Namespace, directory: Path) -> dict:
    name = directory.name
    grey = args.artifact_mode == "grey-glb-only"
    suffix = "_grey.glb" if grey else ".glb"
    output = args.output_data_path / name / f"{name}{suffix}"
    log = args.output_data_path / name / "preprocess_log.json"
    if output.is_file() and not args.overwrite:
        return {"instance": name, "status": "SKIPPED", "output": str(output)}
    command = [
        args.blender, "--background", "--factory-startup",
        "--python", str(Path(__file__).resolve()), "--",
        "--blender-worker", "--script", str(directory / f"{name}.py"),
        "--output", str(output), "--log", str(log),
    ]
    if grey or args.no_bake_texture:
        command.append("--strip-materials")
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=args.timeout,
            env=os.environ.copy(),
        )
        if log.is_file():
            record = json.loads(log.read_text())
        else:
            record = {"status": "ERROR", "error": "Blender wrote no log"}
        record.update({
            "instance": name,
            "returncode": completed.returncode,
            "stdout_tail": completed.stdout[-4000:],
            "stderr_tail": completed.stderr[-4000:],
            "host_elapsed_s": round(time.monotonic() - started, 3),
        })
        return record
    except subprocess.TimeoutExpired:
        return {
            "instance": name,
            "status": "TIMEOUT",
            "error": f"Blender exceeded {args.timeout}s",
        }


def main() -> int:
    args = parse_args()
    args.input_data_path = args.input_data_path.resolve()
    args.output_data_path = args.output_data_path.resolve()
    if not args.input_data_path.is_dir():
        raise SystemExit(f"input directory not found: {args.input_data_path}")
    if not Path(args.blender).is_file():
        raise SystemExit(f"Blender executable not found: {args.blender}")
    instances = discover(args.input_data_path, args.instances)
    args.output_data_path.mkdir(parents=True, exist_ok=True)

    records: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run_instance, args, item) for item in instances]
        for future in as_completed(futures):
            record = future.result()
            records.append(record)
            print(f"[{record.get('status', '?'):7}] {record['instance']}", flush=True)
    records.sort(key=lambda item: item["instance"])
    failures = [
        row["instance"] for row in records
        if row.get("status") not in {"OK", "SKIPPED"}
    ]
    summary = {
        "geometry_prep_version": GEOMETRY_PREP_VERSION,
        "artifact_mode": args.artifact_mode,
        "instances": [
            {**row, "bake_fallback": False, "texture_baked": False}
            for row in records
        ],
        "failed_instances": failures,
    }
    summary_path = args.summary_json or args.output_data_path / "preprocess_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    if args.failed_list:
        args.failed_list.parent.mkdir(parents=True, exist_ok=True)
        args.failed_list.write_text(json.dumps(failures, indent=2) + "\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
