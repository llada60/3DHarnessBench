#!/usr/bin/env python3
"""Chamfer-distance worker for evaluation/evaluate.py.

Loads generated and reference GLBs, surface-samples points with a fixed RNG,
normalizes each point cloud and computes bidirectional squared distance.
Also reports yaw-min and right-handed axis-aligned variants.

Conditional means use successfully loaded meshes. Failed instances receive
the run's fallback distance (1.5 times the worst observed valid distance).
The unified evaluator supplies benchmark paths, agent names and instances.
"""

import argparse
import itertools
import json
import os
import time
from pathlib import Path

import numpy as np
import trimesh
from scipy.spatial import cKDTree

EVAL_ROOT = Path(os.environ.get("EVAL_ROOT")
                 or Path(__file__).resolve().parent.parent)
DEFAULT_DATA_ROOT = EVAL_ROOT / "data"


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model",        required=True)
    p.add_argument("--results-root", type=Path, required=True,
                   help="e.g. results/text_to_3D")
    p.add_argument("--data-root",    type=Path, default=DEFAULT_DATA_ROOT)
    p.add_argument("--gt-glb-relpath", default="glb/{instance}.glb",
                   help="Reference GLB path relative to <data-root>/<instance>; "
                        "supports {instance} (default: glb/{instance}.glb).")
    p.add_argument("--n-points",     type=int, default=8192)
    p.add_argument("--seed",         type=int, default=0)
    p.add_argument("--instances",    nargs="*", default=None)
    p.add_argument("--limit",        type=int, default=None,
                   help="Cap number of instances (debug).")
    return p.parse_args()


def load_mesh_points(glb_path, n_points, rng):
    """Load a GLB and return n_points sampled on its surface, or None."""
    try:
        scene_or_mesh = trimesh.load(glb_path, force="mesh")
    except Exception:
        return None
    if scene_or_mesh is None or scene_or_mesh.is_empty:
        return None
    mesh = scene_or_mesh
    if mesh.faces is None or len(mesh.faces) == 0:
        return None
    # Surface-sample.
    try:
        pts, _ = trimesh.sample.sample_surface(mesh, n_points, seed=int(rng.integers(0, 2**31-1)))
    except Exception:
        return None
    pts = np.asarray(pts, dtype=np.float64)
    if pts.shape[0] < n_points // 4:
        return None
    return pts


def normalize_unit_sphere(pts):
    """Center at centroid, scale so max ||p|| = 1."""
    pts = pts - pts.mean(0, keepdims=True)
    r = np.linalg.norm(pts, axis=1).max()
    if r < 1e-9:
        return pts
    return pts / r


def chamfer_squared(a, b):
    """Symmetric mean squared Chamfer:
        CD = mean_i min_j ||a_i - b_j||^2 + mean_j min_i ||a_i - b_j||^2.
    """
    tree_b = cKDTree(b)
    da, _  = tree_b.query(a, k=1)
    tree_a = cKDTree(a)
    db, _  = tree_a.query(b, k=1)
    return float((da ** 2).mean() + (db ** 2).mean())


def yaw_rotated(pts, deg):
    """Rotate point cloud around Z axis by `deg` degrees."""
    if deg == 0:
        return pts
    th = np.deg2rad(deg)
    c, s = np.cos(th), np.sin(th)
    R = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=pts.dtype)
    return pts @ R.T


def yaw_rotation_matrix(deg, dtype=np.float64):
    """Return the row-vector-compatible matrix for a Z-axis rotation."""
    th = np.deg2rad(deg)
    c, s = np.rint([np.cos(th), np.sin(th)]).astype(dtype)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=dtype)


def axis_rotation_label(matrix):
    """Describe ``p @ matrix.T`` as signed source axes for x/y/z."""
    names = ("x", "y", "z")
    mapped = []
    for row in np.asarray(matrix):
        source = int(np.flatnonzero(row)[0])
        sign = "+" if row[source] > 0 else "-"
        mapped.append(f"{sign}{names[source]}")
    return f"x={mapped[0]},y={mapped[1]},z={mapped[2]}"


def right_handed_axis_rotations(dtype=np.float64):
    """Return the 24 proper rotations that map principal axes onto axes.

    These are all signed permutation matrices with determinant +1.  Reflections
    are deliberately excluded because they change object handedness.
    """
    rotations = []
    for permutation in itertools.permutations(range(3)):
        for signs in itertools.product((-1, 1), repeat=3):
            matrix = np.zeros((3, 3), dtype=dtype)
            matrix[np.arange(3), permutation] = signs
            if np.linalg.det(matrix) > 0:
                rotations.append((axis_rotation_label(matrix), matrix))

    identity = np.eye(3, dtype=dtype)
    rotations.sort(key=lambda item: item[0])
    identity_index = next(
        i for i, (_, matrix) in enumerate(rotations)
        if np.array_equal(matrix, identity)
    )
    rotations.insert(0, rotations.pop(identity_index))
    return rotations


def chamfer_with_axis_alignment(ref_pts, gen_pts):
    """Compute raw, 4-yaw-min, and 24-axis-min Chamfer distances.

    KD trees are built once.  For an orthogonal rotation ``R``, querying the
    unrotated generated tree with ``ref @ R`` is equivalent to querying the
    rotated generated cloud with ``ref``.
    """
    rotations = right_handed_axis_rotations(dtype=gen_pts.dtype)
    tree_ref = cKDTree(ref_pts)
    tree_gen = cKDTree(gen_pts)
    cd_per_axis = []
    for _, matrix in rotations:
        ref_to_gen, _ = tree_gen.query(ref_pts @ matrix, k=1)
        gen_to_ref, _ = tree_ref.query(gen_pts @ matrix.T, k=1)
        cd_per_axis.append(float(
            (ref_to_gen ** 2).mean() + (gen_to_ref ** 2).mean()))

    matrices = [matrix for _, matrix in rotations]
    yaw_indices = []
    for deg in (0, 90, 180, 270):
        yaw_matrix = yaw_rotation_matrix(deg, dtype=gen_pts.dtype)
        yaw_indices.append(next(
            i for i, matrix in enumerate(matrices)
            if np.array_equal(matrix, yaw_matrix)
        ))
    cd_per_yaw = [cd_per_axis[i] for i in yaw_indices]
    yaw_best = int(np.argmin(cd_per_yaw))
    axis_best = int(np.argmin(cd_per_axis))

    return {
        "cd": cd_per_axis[0],
        "cd_yawmin": cd_per_yaw[yaw_best],
        "yaw_argmin": (0, 90, 180, 270)[yaw_best],
        "cd_per_yaw": cd_per_yaw,
        "cd_axismin": cd_per_axis[axis_best],
        "axis_argmin": rotations[axis_best][0],
        "axis_matrix": rotations[axis_best][1].astype(int).tolist(),
        "cd_per_axis": cd_per_axis,
    }


def list_instances(model_dir, override=None):
    insts = sorted(d for d in model_dir.iterdir()
                   if d.is_dir() and not d.name.startswith("_"))
    if override:
        keep = set(override)
        insts = [d for d in insts if d.name in keep]
    return insts


def main():
    args = parse_args()
    model_dir = args.results_root / args.model
    if not model_dir.exists():
        raise SystemExit(f"No such model dir: {model_dir}")

    insts = list_instances(model_dir, args.instances)
    if args.limit:
        insts = insts[:args.limit]
    if not insts:
        raise SystemExit("No instances.")
    print(f"Model: {args.model}  ({len(insts)} instances)")
    print(f"Sampling {args.n_points} points per cloud, normalize unit-sphere, "
          f"24-axis alignment (proper rotations only).")

    rng = np.random.default_rng(args.seed)
    per_instance = []
    t0 = time.time()
    for i, inst in enumerate(insts):
        try:
            gt_relpath = args.gt_glb_relpath.format(instance=inst.name)
        except (KeyError, ValueError) as exc:
            raise SystemExit(f"Invalid --gt-glb-relpath: {exc}") from exc
        ref_glb = args.data_root / inst.name / gt_relpath
        gen_glb = inst / "glb" / f"{inst.name}.glb"

        rec = {"instance": inst.name, "ref_exists": ref_glb.exists(),
               "gen_exists": gen_glb.exists(),
               "cd": None, "cd_yawmin": None, "yaw_argmin": None,
               "cd_per_yaw": None, "cd_axismin": None,
               "axis_argmin": None, "axis_matrix": None,
               "cd_per_axis": None, "n_ref_pts": None, "n_gen_pts": None,
               "status": None}

        if not gen_glb.exists():
            rec["status"] = "NO_GEN_GLB"
        elif not ref_glb.exists():
            rec["status"] = "NO_REF_GLB"
        else:
            ref_pts = load_mesh_points(ref_glb, args.n_points, rng)
            gen_pts = load_mesh_points(gen_glb, args.n_points, rng)
            if ref_pts is None:
                rec["status"] = "BAD_REF_MESH"
            elif gen_pts is None:
                rec["status"] = "BAD_GEN_MESH"
            else:
                ref_pts = normalize_unit_sphere(ref_pts)
                gen_pts = normalize_unit_sphere(gen_pts)
                distances = chamfer_with_axis_alignment(ref_pts, gen_pts)
                rec.update({
                    **distances,
                    "n_ref_pts":   int(ref_pts.shape[0]),
                    "n_gen_pts":   int(gen_pts.shape[0]),
                    "status":      "OK",
                })
        per_instance.append(rec)
        if (i + 1) % 25 == 0 or i == len(insts) - 1:
            ok = sum(1 for r in per_instance if r["status"] == "OK")
            print(f"  [{i+1}/{len(insts)}]  ok={ok}  "
                  f"elapsed={time.time()-t0:.1f}s")

    n = len(per_instance)
    ok_rows = [r for r in per_instance if r["status"] == "OK"]
    n_ok = len(ok_rows)

    # Penalty CD for missing/failed: use 1.5x worst observed within this run.
    # (Bounded, run-relative; still strictly worse than any successful sample.)
    if ok_rows:
        worst_cd = max(r["cd"]        for r in ok_rows)
        worst_cd_yaw = max(r["cd_yawmin"] for r in ok_rows)
        worst_cd_axis = max(r["cd_axismin"] for r in ok_rows)
        penalty_cd = 1.5 * worst_cd
        penalty_cd_yaw = 1.5 * worst_cd_yaw
        penalty_cd_axis = 1.5 * worst_cd_axis
    else:
        worst_cd = worst_cd_yaw = worst_cd_axis = None
        penalty_cd = penalty_cd_yaw = penalty_cd_axis = None

    def agg(field, penalty):
        if not ok_rows:
            return {"conditional_mean": None, "penalized_mean": None}
        cond = float(np.mean([r[field] for r in ok_rows]))
        pen = []
        for r in per_instance:
            pen.append(r[field] if r["status"] == "OK" else penalty)
        return {"conditional_mean": cond, "penalized_mean": float(np.mean(pen))}

    axis_rotations = right_handed_axis_rotations()
    summary = {
        "model":         args.model,
        "results_root":  str(args.results_root),
        "n_instances":   n,
        "n_ok":          n_ok,
        "n_points":      args.n_points,
        "normalization": "unit_sphere",
        "yaw_alignment": [0, 90, 180, 270],
        "axis_alignment": {
            "name": "right_handed_signed_axis_permutations",
            "count": len(axis_rotations),
            "includes_reflections": False,
            "rotations": [
                {"label": label, "matrix": matrix.astype(int).tolist()}
                for label, matrix in axis_rotations
            ],
        },
        "penalty_cd":         penalty_cd,
        "penalty_cd_yawmin":  penalty_cd_yaw,
        "penalty_cd_axismin": penalty_cd_axis,
        "cd":         agg("cd",        penalty_cd),
        "cd_yawmin":  agg("cd_yawmin", penalty_cd_yaw),
        "cd_axismin": agg("cd_axismin", penalty_cd_axis),
        "per_instance": per_instance,
    }

    out_dir = model_dir / "_metrics"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "shape_chamfer.json"
    out_path.write_text(json.dumps(summary, indent=2))

    # Stdout
    def fmt(v): return f"{v:.4f}" if isinstance(v, float) else "  --  "
    print()
    print(f"=== shape Chamfer — {args.model} ===")
    print(f"  Instances:                {n}")
    print(f"  Generated GLBs loaded:    {n_ok}")
    print(f"  CD             cond={fmt(summary['cd']['conditional_mean']):>9}  "
          f"pen={fmt(summary['cd']['penalized_mean']):>9}")
    print(f"  CD (yaw-min)   cond={fmt(summary['cd_yawmin']['conditional_mean']):>9}  "
          f"pen={fmt(summary['cd_yawmin']['penalized_mean']):>9}")
    print(f"  CD (axis-min)  cond={fmt(summary['cd_axismin']['conditional_mean']):>9}  "
          f"pen={fmt(summary['cd_axismin']['penalized_mean']):>9}")
    print(f"  Wrote {out_path}")


if __name__ == "__main__":
    main()
