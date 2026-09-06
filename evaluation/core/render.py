#!/usr/bin/env python3
"""Render generated Blender scripts at the benchmark's four camera angles.

This internal worker is called by the experiment runners and evaluate.py.
Its normal-Python mode schedules Blender processes; --blender-render executes
inside Blender, creates the shared camera/light rig and writes four PNGs plus
render_log.json. Views Image_005/015/025/035 correspond to azimuths
45/135/225/315 degrees.

Use scripts/<setting>/run.py for generation and evaluation/evaluate.py for
evaluation. Rendering failures are recorded with status, error and latency.
"""

import sys
import os
from pathlib import Path


SCENE_RIG_VERSION = "benchmark_extent_squared_v1"
GEOMETRY_PREP_VERSION = "realize_gn_instances_v1"

_BL_ARGV = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


# ╔══════════════════════════════════════════════════════════════════╗
# ║  Blender-side  (active when --blender-render is in -- argv)      ║
# ╚══════════════════════════════════════════════════════════════════╝
if "--blender-render" in _BL_ARGV:
    import argparse
    import json
    import math
    import runpy
    import time
    import traceback
    from pathlib import Path

    import bpy
    from mathutils import Vector

    _SCRIPTS_ROOT = Path(__file__).resolve().parents[2] / "scripts"
    if str(_SCRIPTS_ROOT) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS_ROOT))
    from blender_scene_setup import clear_scene, setup_scene

    # frame_idx → output filename (azimuth_deg = 9° × frame_idx)
    REF_VIEWS = [(5,  "Image_005.png"),
                 (15, "Image_015.png"),
                 (25, "Image_025.png"),
                 (35, "Image_035.png")]

    def _bl_parse():
        p = argparse.ArgumentParser()
        p.add_argument("--blender-render", action="store_true")
        p.add_argument("--script",      required=True)
        p.add_argument("--output-dir",  required=True)
        p.add_argument("--samples",     type=int, default=64)
        p.add_argument("--resolution",  type=int, default=512)
        p.add_argument("--engine",      default="CYCLES",
                       choices=["CYCLES", "BLENDER_EEVEE"])
        p.add_argument("--light-power", type=float, default=1.0,
                       help="1.0 for benchmark colour; 0.5 for benchmark grey")
        p.add_argument("--light-ref-extent", type=float, default=2.5)
        p.add_argument("--grey-shaded", action="store_true")
        p.add_argument("--grey-value", type=float, default=0.55)
        return p.parse_args(_BL_ARGV)

    def _clear_scene():
        clear_scene()

    def _realize_geometry_node_instances(modifier):
        """Realize a Geometry Nodes result before applying it to a mesh.

        Blender can display an instances component in the viewport, but
        ``modifier_apply`` only writes the mesh component back to the object.
        Put Realize Instances immediately before each linked group output so
        viewport-visible instances survive the evaluator's mesh conversion.
        """
        if modifier.type != "NODES" or modifier.node_group is None:
            return 0

        node_tree = modifier.node_group
        output_nodes = [
            node for node in node_tree.nodes
            if node.bl_idname == "NodeGroupOutput"
            and getattr(node, "is_active_output", True)
        ]
        inserted = 0
        for output_node in output_nodes:
            for output_socket in output_node.inputs:
                if (getattr(output_socket, "type", None) != "GEOMETRY"
                        or not output_socket.is_linked):
                    continue
                link = output_socket.links[0]
                if link.from_node.bl_idname == "GeometryNodeRealizeInstances":
                    continue
                source_socket = link.from_socket
                realize = node_tree.nodes.new("GeometryNodeRealizeInstances")
                realize.name = f"__eval_realize_instances_{inserted}"
                realize.location = (
                    output_node.location.x - 180,
                    output_node.location.y - 80 * inserted,
                )
                node_tree.links.remove(link)
                node_tree.links.new(source_socket, realize.inputs["Geometry"])
                node_tree.links.new(
                    realize.outputs["Geometry"], output_socket)
                inserted += 1
        return inserted

    def _run_user_script(path):
        """Execute the generated script and strip only camera/light objects.

        Keep authored Curve objects intact.  In particular, a path Curve may
        use another Curve as its bevel profile; converting objects one by one
        destroys that dependency and produces empty meshes.
        """
        gen_script = Path(path)
        if gen_script.is_file():
            import re
            content = gen_script.read_text(encoding="utf-8")
            updated = content
            correct_path = str(gen_script.resolve())
            updated = re.sub(
                r"""['"][^'"]*?/(reconstruct_gt|local_recon_full|local_recon)\.py['"]""",
                f"'{correct_path}'",
                updated
            )
            updated = re.sub(
                r"""\b(reconstruct_gt|local_recon_full|local_recon)\.py\b""",
                f"{gen_script.name}",
                updated
            )
            updated = re.sub(
                r"""open\(\s*(["'])(.*?\.py)\1""",
                lambda m: m.group(0)
                if m.group(2) == correct_path
                else f"open({m.group(1)}{correct_path}{m.group(1)}",
                updated,
            )

            # Keep unlink calls idempotent and stable across reruns.  Rewrite
            # user calls before adding the helper so the rewrite cannot turn
            # the helper body into a recursive call.  The helper deliberately
            # uses a local collection variable for the same reason on later
            # renders.
            updated, unlink_count = re.subn(
                r"""([ \t]*)bpy\.context\.scene\.collection\.objects\.unlink\((.*?)\)""",
                lambda m: f"{m.group(1)}_safe_unlink({m.group(2)})",
                updated,
            )
            if unlink_count and "def _safe_unlink(obj):" not in updated:
                updated = (
                    "def _safe_unlink(obj):\n"
                    "    scene_objects = bpy.context.scene.collection.objects\n"
                    "    if obj is not None and hasattr(obj, 'name') and "
                    "obj.name in scene_objects:\n"
                    "        scene_objects.unlink(obj)\n\n"
                    + updated
                )

            if updated != content:
                gen_script.write_text(updated, encoding="utf-8")

        runpy.run_path(path, run_name="__main__")
        for o in list(bpy.context.scene.objects):
            if o.type in ("CAMERA", "LIGHT"):
                bpy.data.objects.remove(o, do_unlink=True)
            elif o.type == "MESH":
                if any(m.type == 'ARRAY' and m.use_object_offset for m in o.modifiers):
                    try:
                        bpy.context.view_layer.objects.active = o
                        o.select_set(True)
                        bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
                        o.select_set(False)
                    except Exception:
                        pass
                for mod in list(o.modifiers):
                    realized = _realize_geometry_node_instances(mod)
                    if realized:
                        print(
                            f"[render] {o.name}: realized {realized} "
                            "Geometry Nodes output(s)"
                        )
                if len(o.data.polygons) == 0 and len(o.modifiers) > 0:
                    try:
                        bpy.context.view_layer.objects.active = o
                        o.select_set(True)
                        for mod in list(o.modifiers):
                            bpy.ops.object.modifier_apply(modifier=mod.name)
                        o.select_set(False)
                    except Exception:
                        pass
        renderable_types = {
            "MESH", "CURVE", "SURFACE", "FONT", "META",
            "VOLUME", "POINTCLOUD", "CURVES", "GREASEPENCIL",
        }
        geometry = [o for o in bpy.context.scene.objects
                    if o.type in renderable_types]
        if len(geometry) > 1:
            for o in geometry:
                if o.name == "Cube" and o.location == Vector((0, 0, 0)):
                    bpy.data.objects.remove(o, do_unlink=True)
        return [o for o in bpy.context.scene.objects
                if o.type in renderable_types]

    def _setup_scene(meshes, resolution, samples, engine, light_power,
                     light_ref_extent):
        cam, center, cam_r, cam_h, extent = setup_scene(
            meshes, resolution, samples, engine,
            light_power=light_power,
            light_ref_extent=light_ref_extent)
        sc = bpy.context.scene

        if engine == "CYCLES":
            gpu_ok = False
            try:
                pr = bpy.context.preferences.addons["cycles"].preferences
                for dev_type in ("OPTIX", "CUDA"):
                    try:
                        pr.compute_device_type = dev_type
                    except TypeError:
                        continue  # device type not in this Blender build
                    # Blender 5.x: refresh_devices(); older Blender: get_devices()
                    if hasattr(pr, "refresh_devices"):
                        pr.refresh_devices()
                    elif hasattr(pr, "get_devices"):
                        pr.get_devices()
                    gpus = [d for d in pr.devices if d.type == dev_type]
                    if gpus:
                        for d in pr.devices:
                            d.use = (d.type == dev_type)
                        sc.cycles.device = "GPU"
                        print(f"[render] GPU: {dev_type} × {len(gpus)}")
                        gpu_ok = True
                        break
                if not gpu_ok:
                    print("[render] No GPU available, falling back to CPU")
                    sc.cycles.device = "CPU"
            except Exception as e:
                print(f"[render] GPU init failed, using CPU: {type(e).__name__}: {e}")
                sc.cycles.device = "CPU"

        return cam, center, cam_r, cam_h, float(extent)

    def _render_views(cam, center, cam_r, cam_h, output_dir):
        sc = bpy.context.scene
        n_done = 0
        for frame_idx, fname in REF_VIEWS:
            angle = 2 * math.pi * frame_idx / 40
            cam.location = (cam_r * math.cos(angle),
                            cam_r * math.sin(angle),
                            cam_h)
            cam.rotation_euler = (center - cam.location).to_track_quat("-Z", "Y").to_euler()
            sc.render.filepath = os.path.join(output_dir, fname)
            bpy.ops.render.render(write_still=True)
            n_done += 1
        return n_done

    def _greyify_all_materials(value):
        """Replace every material with the benchmark's neutral grey shader."""
        grey = (value, value, value, 1.0)
        n_materials = 0
        for material in bpy.data.materials:
            try:
                material.use_nodes = True
                node_tree = material.node_tree
                for node in list(node_tree.nodes):
                    node_tree.nodes.remove(node)
                bsdf = node_tree.nodes.new("ShaderNodeBsdfPrincipled")
                output = node_tree.nodes.new("ShaderNodeOutputMaterial")
                output.location = (300, 0)
                bsdf.inputs["Base Color"].default_value = grey
                bsdf.inputs["Roughness"].default_value = 0.5
                bsdf.inputs["Metallic"].default_value = 0.0
                for key, socket_value in (
                    ("Specular IOR Level", 0.3),
                    ("Specular", 0.3),
                    ("Alpha", 1.0),
                    ("Transmission Weight", 0.0),
                    ("Emission Strength", 0.0),
                ):
                    socket = bsdf.inputs.get(key)
                    if socket is not None:
                        socket.default_value = socket_value
                node_tree.links.new(bsdf.outputs[0], output.inputs[0])
                material.diffuse_color = grey
                for attribute, attribute_value in (
                    ("blend_method", "OPAQUE"),
                    ("use_backface_culling", False),
                ):
                    if hasattr(material, attribute):
                        try:
                            setattr(material, attribute, attribute_value)
                        except (AttributeError, TypeError):
                            pass
                n_materials += 1
            except Exception as exc:
                print(f"[render] greyify failed on {material.name}: "
                      f"{type(exc).__name__}: {exc}")
        if bpy.data.materials:
            bpy.context.view_layer.material_override = bpy.data.materials[0]
        bpy.context.view_layer.update()
        return n_materials

    # ── main ──
    args = _bl_parse()
    os.makedirs(args.output_dir, exist_ok=True)
    log_path = os.path.join(args.output_dir, "render_log.json")
    record = {
        "script":           args.script,
        "samples":          args.samples,
        "resolution":       args.resolution,
        "engine":           args.engine,
        "rig_version":      SCENE_RIG_VERSION,
        "geometry_prep_version": GEOMETRY_PREP_VERSION,
        "light_power":      args.light_power,
        "light_ref_extent": args.light_ref_extent,
        "grey_shaded":      args.grey_shaded,
        "grey_value":       args.grey_value if args.grey_shaded else None,
        "status":           None,
        "n_meshes":         0,
        "n_materials_greyed": 0,
        "extent":           None,
        "n_views_rendered": 0,
        "latency_s":        None,
        "error":            None,
    }
    t0 = time.time()
    try:
        _clear_scene()
        try:
            meshes = _run_user_script(args.script)
        except Exception as e:
            record["status"] = "ERR_EXEC"
            record["error"] = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        else:
            record["n_meshes"] = len(meshes)
            if not meshes:
                record["status"] = "ERR_NO_MESH"
                record["error"] = (
                    "Script ran but produced no supported renderable geometry objects"
                )
            else:
                try:
                    cam, center, cam_r, cam_h, extent = _setup_scene(
                        meshes, args.resolution, args.samples, args.engine,
                        args.light_power, args.light_ref_extent)
                    record["extent"] = extent
                    if args.grey_shaded:
                        record["n_materials_greyed"] = _greyify_all_materials(
                            args.grey_value)
                    n = _render_views(cam, center, cam_r, cam_h, args.output_dir)
                    record["n_views_rendered"] = n
                    record["status"] = "OK" if n == 4 else "ERR_RENDER"
                except Exception as e:
                    record["status"] = "ERR_RENDER"
                    record["error"] = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
    finally:
        record["latency_s"] = round(time.time() - t0, 2)
        with open(log_path, "w") as f:
            json.dump(record, f, indent=2)

    sys.exit(0 if record["status"] == "OK" else 0)  # never bubble error to subprocess


# ╔══════════════════════════════════════════════════════════════════╗
# ║  Orchestrator (normal Python)                                    ║
# ╚══════════════════════════════════════════════════════════════════╝
import argparse
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

EVAL_ROOT = Path(os.environ.get("EVAL_ROOT")
                 or Path(__file__).resolve().parent.parent)
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


DEFAULT_BLENDER = _default_blender()


def parse_cli():
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
    p.add_argument("--blender",       default=DEFAULT_BLENDER)
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
    return p.parse_args()


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
    cmd = [args.blender, "--background", "--python", str(this_script), "--",
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
        subprocess.run(cmd, capture_output=True, text=True, timeout=args.timeout)
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


def main():
    args = parse_cli()
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
    if failed_counts:
        sys.exit(1)


if __name__ == "__main__":
    main()
