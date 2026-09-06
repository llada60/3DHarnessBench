# Internal metric workers

These modules are invoked by `../evaluate.py`:

- `usage.py`: aggregates recorded attempts, tokens, API calls, costs and time.
- `shape_chamfer.py`: bidirectional Chamfer distance, including yaw alignment.
- `image_similarity.py`: SigLIP2, DINOv2 and DINOv3 feature similarity with
  view-paired and best-assignment aggregation.
- `shape_uni3d.py`: Uni3D-Giant/OpenCLIP image-to-3D and 3D-to-3D similarity.

Use `evaluation/evaluate.py --help` from the repository root for the supported
interface. See [evaluation setup](../README.md) for model weights and output
semantics. Tests next to the metric implementations cover geometry helpers.
