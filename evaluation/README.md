# Evaluation

The public entry point is `evaluation/evaluate.py`. It evaluates outputs from
Single-view, Multi-view, ActiveVisual, and Full3DInteraction for the seven
agents registered in `raw_agents/agent_registry.py`. Complete the
[evaluation installation steps](../INSTALL.md#4-add-data-and-evaluation-assets)
first. This entry point automatically loads the checkout root `.env`, including
`BLENDER`, `HF_TOKEN` and `UNI3D_REPO`; exported shell variables take precedence.

```bash
pixi run python evaluation/evaluate.py \
  --data-path data/benchmark \
  --output-dir outputs \
  --setting Full3DInteraction \
  --agent gpt-5-6-sol \
  --tasks VaseFactory \
  --num-parallel 2
```

Use `--dry-run` to inspect commands without running Blender or metric models.
A dry-run still requires the selected generated scripts and reference assets.
Use `--no-texture-renders` for the geometry-only track.

## Pipeline and outputs

The evaluator selects instances under
`outputs/<setting>/<w_texture|wo_texture>/<agent>/`, prepares missing four-view
renders and GLBs, and invokes these internal metric workers:

| Worker | Measures |
| --- | --- |
| `metrics/usage.py` | attempts, tokens, API calls, provider cost and agent time |
| `metrics/shape_chamfer.py` | generated/reference geometry distance |
| `metrics/image_similarity.py` | SigLIP2, DINOv2 and DINOv3 image similarity |
| `metrics/shape_uni3d.py` | Uni3D image/3D and 3D/3D similarity |

`core/render.py` and `core/export_glb.py` are internal Blender workers for
rendering and GLB export respectively. The exporter uses Python's standard
library to schedule Blender; `bpy` is provided by Blender itself. The current
exporter preserves GLB-compatible materials but does not bake arbitrary
procedural textures (`texture_baked` is recorded as false). The legacy
`prepare:baked-glb` log label does not indicate that baking occurred.
The metric workers are implementation details; use the unified entry point.

Per-metric JSON, `evaluation_run.json` and `final_metrics.json` are written in
the agent's `_metrics/` directory. Missing provider accounting remains `null`.
Reported API price estimates are separate from provider-reported costs.
Use `--image-encoders siglip2 dinov2` for a partial run without gated DINOv3;
the final metrics include only the selected image encoders.

`--grey-shaded` adds an image-only pass with neutral-grey materials, writing to
`grey_shaded_outputs/<setting>/<track>/<agent>/`. Its metrics compare against
the benchmark's `grey_renders/`; the regular textured pass is preserved.

## Environment

Install the repository's Pixi environment. Blender is resolved from `--blender`,
`BLENDER`, `tools/blender-*/blender`, or PATH. GPU metrics use `--device cuda`
by default; use `--device cpu` on macOS. Model weights are downloaded into the configured Hugging Face and
Torch caches; DINOv3 requires access to its gated model repository.

Uni3D additionally requires its source checkout:

```bash
mkdir -p evaluation/external
git clone https://github.com/baaivision/Uni3D.git evaluation/external/Uni3D
```

Alternatively set `UNI3D_REPO` to an existing checkout. Uni3D-Giant and OpenCLIP
weights are loaded by `metrics/shape_uni3d.py`.

## Re-running evaluation

Use `--skip-prepare` when render/GLB artifacts are already available.
`--overwrite-artifacts --overwrite-metrics` rebuilds stale results after the
generated scripts or selected instances change. Existing metric files may
cover a different selection, so inspect their instance counts before reporting.

Outside dry-run, preparation can sanitize generated scripts in place and
replace metric-input links. Evaluate a copy if original outputs must be kept
byte-for-byte. Reference assets are read directly from `--data-path`.
