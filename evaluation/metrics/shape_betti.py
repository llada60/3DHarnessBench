#!/usr/bin/env python3
"""Normalized L1 error between generated and reference GLB Betti vectors.

For each instance, compute the Z2 Betti vector ``(beta_0, beta_1, beta_2)``
for the reference and reconstructed triangle meshes.  The per-instance score
is::

    sum(abs(beta_gt - beta_reconstruction)) / sum(beta_gt)

Coincident vertices are merged first because GLB export may duplicate vertices
at material, normal, or UV seams. Betti numbers are computed from simplicial
boundary maps over Z2, including for non-manifold triangle complexes.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import statistics
import time
from pathlib import Path
from queue import Empty

import numpy as np
import trimesh
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components


EVAL_ROOT = Path(os.environ.get("EVAL_ROOT")
                 or Path(__file__).resolve().parent.parent)
DEFAULT_DATA_ROOT = EVAL_ROOT / "data"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", required=True)
    parser.add_argument("--results-root", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument(
        "--gt-glb-relpath", default="glb/{instance}.glb",
        help="Reference GLB path relative to <data-root>/<instance>; "
             "supports {instance} (default: glb/{instance}.glb).",
    )
    parser.add_argument("--instances", nargs="*", default=None)
    parser.add_argument("--limit", type=int, default=None)
    return parser.parse_args()


def load_triangle_mesh(path: Path) -> trimesh.Trimesh | None:
    """Load and weld a GLB triangle mesh, returning None on invalid input."""
    try:
        # Topology is geometry-only; avoid decoding large embedded textures.
        mesh = trimesh.load(
            path, force="mesh", process=False, skip_materials=True)
    except Exception:
        return None
    if (not isinstance(mesh, trimesh.Trimesh) or mesh.is_empty
            or mesh.faces is None or len(mesh.faces) == 0):
        return None
    try:
        mesh = mesh.copy()
        # Weld positional duplicates introduced at UV and normal seams.
        mesh.merge_vertices(merge_tex=True, merge_norm=True)
        mesh.update_faces(mesh.nondegenerate_faces())
        mesh.update_faces(mesh.unique_faces())
        mesh.remove_unreferenced_vertices()
    except Exception:
        return None
    if len(mesh.vertices) == 0 or len(mesh.faces) == 0:
        return None
    return mesh


def _beta_2_from_edge_faces(
    n_faces: int,
    edge_inverse: np.ndarray,
    edge_incidence: np.ndarray,
) -> int:
    """Return nullity of the edge-face boundary matrix over Z2.

    Degree-two edge equations first merge equal face coefficients. Boundary
    edges pin a merged coefficient to zero. Only remaining non-manifold XOR
    equations need sparse Gaussian elimination.
    """
    face_indices = np.tile(np.arange(n_faces, dtype=np.int64), 3)
    order = np.argsort(edge_inverse, kind="stable")
    offsets = np.concatenate((
        np.array([0], dtype=np.int64),
        np.cumsum(edge_incidence, dtype=np.int64),
    ))

    degree_two = np.flatnonzero(edge_incidence == 2)
    first = face_indices[order[offsets[degree_two]]]
    second = face_indices[order[offsets[degree_two] + 1]]
    rows = np.concatenate((first, second))
    cols = np.concatenate((second, first))
    face_adjacency = coo_matrix(
        (np.ones(len(rows), dtype=np.uint8), (rows, cols)),
        shape=(n_faces, n_faces),
    ).tocsr()
    n_groups, face_groups = connected_components(
        face_adjacency, directed=False, return_labels=True)

    boundary_edges = np.flatnonzero(edge_incidence == 1)
    fixed_groups = set(face_groups[
        face_indices[order[offsets[boundary_edges]]]
    ].tolist())

    # Sets encode rows over Z2; symmetric difference is row addition.
    pivots: dict[int, set[int]] = {}
    rank = 0
    for edge_index in np.flatnonzero(edge_incidence > 2):
        groups: set[int] = set()
        for face_index in face_indices[
                order[offsets[edge_index]:offsets[edge_index + 1]]]:
            group = int(face_groups[face_index])
            if group in fixed_groups:
                continue
            if group in groups:
                groups.remove(group)
            else:
                groups.add(group)
        while groups:
            pivot = max(groups)
            previous = pivots.get(pivot)
            if previous is None:
                pivots[pivot] = groups
                rank += 1
                break
            groups = groups.symmetric_difference(previous)

    return int(n_groups - len(fixed_groups) - rank)


def betti_vector(mesh: trimesh.Trimesh) -> list[int]:
    """Return exact simplicial ``[beta_0, beta_1, beta_2]`` over Z2."""
    faces = np.asarray(mesh.faces, dtype=np.int64)
    vertices = np.asarray(mesh.vertices)
    if faces.ndim != 2 or faces.shape[1] != 3:
        raise ValueError("mesh is not triangulated")

    face_edges = np.concatenate(
        (faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]), axis=0)
    face_edges.sort(axis=1)
    edges, edge_inverse, incidence = np.unique(
        face_edges, axis=0, return_inverse=True, return_counts=True)

    n_vertices = len(vertices)
    rows = np.concatenate((edges[:, 0], edges[:, 1]))
    cols = np.concatenate((edges[:, 1], edges[:, 0]))
    adjacency = coo_matrix(
        (np.ones(len(rows), dtype=np.uint8), (rows, cols)),
        shape=(n_vertices, n_vertices),
    ).tocsr()
    beta_0 = connected_components(
        adjacency, directed=False, return_labels=False)

    beta_2 = _beta_2_from_edge_faces(
        len(faces), edge_inverse, incidence)
    euler_characteristic = n_vertices - len(edges) + len(faces)
    beta_1 = int(beta_0 + beta_2 - euler_characteristic)
    if beta_1 < 0:
        raise ValueError("invalid simplicial homology dimensions")
    return [int(beta_0), beta_1, beta_2]


def relative_l1_error(gt: list[int], reconstruction: list[int]) -> float:
    """Return the reconstruction's GT-normalized Betti-vector L1 error."""
    gt_array = np.asarray(gt, dtype=np.int64)
    reconstruction_array = np.asarray(reconstruction, dtype=np.int64)
    denominator = int(gt_array.sum())
    if denominator <= 0:
        raise ValueError("GT Betti vector has a zero L1 norm")
    return float(np.abs(gt_array - reconstruction_array).sum() / denominator)


def list_instances(model_dir: Path, override: list[str] | None) -> list[Path]:
    instances = sorted(
        path for path in model_dir.iterdir()
        if path.is_dir() and not path.name.startswith("_")
        and (path / f"{path.name}.py").is_file())
    if override:
        keep = set(override)
        instances = [path for path in instances if path.name in keep]
    return instances


def score_instance(
    instance_dir: Path,
    data_root: Path,
    gt_glb_relpath: str,
) -> dict:
    """Score one instance without retaining either loaded mesh."""
    gt_path = data_root / instance_dir.name / gt_glb_relpath
    reconstruction_path = instance_dir / "glb" / f"{instance_dir.name}.glb"
    record = {
        "instance": instance_dir.name,
        "gt_exists": gt_path.is_file(),
        "reconstruction_exists": reconstruction_path.is_file(),
        "gt_betti": None,
        "reconstruction_betti": None,
        "relative_l1_error": None,
        "status": None,
    }

    if not gt_path.is_file():
        record["status"] = "NO_GT_GLB"
        return record
    if not reconstruction_path.is_file():
        record["status"] = "NO_RECONSTRUCTION_GLB"
        return record

    gt_mesh = load_triangle_mesh(gt_path)
    reconstruction_mesh = load_triangle_mesh(reconstruction_path)
    if gt_mesh is None:
        record["status"] = "BAD_GT_MESH"
    elif reconstruction_mesh is None:
        record["status"] = "BAD_RECONSTRUCTION_MESH"
    else:
        try:
            gt_betti = betti_vector(gt_mesh)
            reconstruction_betti = betti_vector(reconstruction_mesh)
            record.update({
                "gt_betti": gt_betti,
                "reconstruction_betti": reconstruction_betti,
                "relative_l1_error": relative_l1_error(
                    gt_betti, reconstruction_betti),
                "status": "OK",
            })
        except ValueError as exc:
            record["status"] = "INVALID_TOPOLOGY"
            record["error"] = str(exc)
    return record


def _score_worker(
    output: multiprocessing.Queue,
    instance_dir: Path,
    data_root: Path,
    gt_glb_relpath: str,
) -> None:
    """Child-process entry point for one memory-isolated instance."""
    try:
        output.put(score_instance(instance_dir, data_root, gt_glb_relpath))
    except Exception as exc:
        output.put({
            "instance": instance_dir.name,
            "gt_betti": None,
            "reconstruction_betti": None,
            "relative_l1_error": None,
            "status": "WORKER_EXCEPTION",
            "error": f"{type(exc).__name__}: {exc}",
        })


def score_instance_isolated(
    instance_dir: Path,
    data_root: Path,
    gt_glb_relpath: str,
) -> dict:
    """Score one instance in a fresh process so mesh memory is reclaimed."""
    context = multiprocessing.get_context("spawn")
    output = context.Queue(maxsize=1)
    process = context.Process(
        target=_score_worker,
        args=(output, instance_dir, data_root, gt_glb_relpath),
    )
    process.start()
    process.join()
    try:
        if process.exitcode == 0:
            return output.get(timeout=1.0)
    except Empty:
        pass
    finally:
        output.close()
        output.join_thread()
    return {
        "instance": instance_dir.name,
        "gt_betti": None,
        "reconstruction_betti": None,
        "relative_l1_error": None,
        "status": "WORKER_FAILED",
        "error": f"isolated scorer exited with code {process.exitcode}",
    }


def main() -> None:
    args = parse_args()
    model_dir = args.results_root / args.model
    if not model_dir.is_dir():
        raise SystemExit(f"No such model dir: {model_dir}")

    instances = list_instances(model_dir, args.instances)
    if args.limit is not None:
        instances = instances[:args.limit]
    if not instances:
        raise SystemExit("No instances.")

    print(f"Model: {args.model}  ({len(instances)} instances)")
    per_instance: list[dict] = []
    started = time.time()
    for index, instance_dir in enumerate(instances):
        try:
            gt_relpath = args.gt_glb_relpath.format(
                instance=instance_dir.name)
        except (KeyError, ValueError) as exc:
            raise SystemExit(f"Invalid --gt-glb-relpath: {exc}") from exc
        record = score_instance_isolated(
            instance_dir, args.data_root, gt_relpath)
        per_instance.append(record)
        if (index + 1) % 25 == 0 or index == len(instances) - 1:
            n_ok = sum(row["status"] == "OK" for row in per_instance)
            print(f"  [{index + 1}/{len(instances)}] ok={n_ok} "
                  f"elapsed={time.time() - started:.1f}s")

    valid_errors = [
        row["relative_l1_error"] for row in per_instance
        if row["status"] == "OK"
    ]
    summary = {
        "model": args.model,
        "results_root": str(args.results_root),
        "definition": "sum(abs(gt_betti - reconstruction_betti)) / "
                      "sum(gt_betti)",
        "coefficients": "Z2",
        "n_instances": len(per_instance),
        "n_ok": len(valid_errors),
        "relative_l1_error": {
            "median": (float(statistics.median(valid_errors))
                       if valid_errors else None),
            "mean": (float(np.mean(valid_errors))
                     if valid_errors else None),
        },
        "per_instance": per_instance,
    }

    output_dir = model_dir / "_metrics"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "shape_betti.json"
    output_path.write_text(json.dumps(summary, indent=2) + "\n")
    median = summary["relative_l1_error"]["median"]
    median_text = f"{median:.6f}" if isinstance(median, float) else "--"
    print(f"Betti normalized L1 median: {median_text}")
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
