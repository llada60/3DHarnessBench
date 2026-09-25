# Internal metric workers

These modules are invoked by `evaluate.py` in this directory:

- `usage.py`: aggregates recorded attempts, tokens, API calls, costs and time.
- `shape_chamfer.py`: bidirectional Chamfer distance, including yaw alignment.
- `shape_betti.py`: normalized L1 error between generated/reference Z2 Betti
  vectors; the headline aggregate is the median valid instance.
- `image_similarity.py`: SigLIP2, DINOv2 and DINOv3 feature similarity with
  view-paired and best-assignment aggregation.
- `shape_uni3d.py`: Uni3D-Giant/OpenCLIP image-to-3D and 3D-to-3D similarity.

Use `python -m metrics.evaluate --help` from the repository root for the supported
interface. See [evaluation setup](README.md) for model weights and output
semantics. There is no checked-in `tests/` suite.
