# Same-scene mode — the GT lives in the Blender you are building in

**You are in this mode only if** a single Blender MCP server holds both the GT model and your
reconstruction, and you can execute Python in it. If instead the GT sits behind a separate
read-only viewport server, stop and read [two-instance.md](two-instance.md).

The final scene must contain **both** the unchanged GT model and the generated reconstruction.
They may be moved side by side temporarily while comparing, but the reconstruction must be
returned to its GT-aligned origin before the final save or export.

## GT preservation in this mode

The GT is reachable and editable here, so preservation is a discipline you must enforce.

**Before running generated code, identify and record all existing GT objects and collections.** The
reconstruction script may only create, remove, or modify its own generated objects, materials,
textures, images, node groups, and collection.

Preserve all GT materials, textures, images, UV data, and shader settings unchanged.

## Inspecting the GT

Exact measurement is available — use it rather than guessing:

* world-space bounding boxes and object dimensions;
* transforms, distances, angles, symmetry planes;
* mesh statistics;
* sampled colors and material observations.

Take these alongside the multi-viewpoint visual inspection in SKILL.md.

## Placement — origin-authored reconstruction and temporary comparison offset

Construct the model in the GT-aligned frame and make the complete reconstruction script leave
`recon__root.location` at `(0, 0, 0)`. Do not encode a side-by-side offset in the reconstruction
script.

For visual comparison only, after executing the complete script, translate **only `recon__root`**
with a separate MCP code execution. Compute the temporary distance from the GT world-space
bounding box:

```python
comparison_distance = 1.25 * max(gt_bbox_dimensions)
```

Prefer translation along world X. Use another axis only when X causes overlap or poor visibility.

This is viewport/comparison state, not model data. Each rerun of the complete script must recreate
the reconstruction at the origin. Apply the temporary offset again separately when another
side-by-side comparison is needed. Before the final save or export, restore:

```python
bpy.data.objects["recon__root"].location = (0.0, 0.0, 0.0)
```

Verify the restored origin after the assignment. Never move the GT to create comparison space.

## Independence — what is reachable here, and therefore banned

Because GT datablocks are reachable in this scene, the reconstruction must not:

* duplicate GT objects or mesh datablocks;
* import or append the source model;
* copy or serialize the complete GT vertex and face arrays;
* use GT objects as boolean, shrinkwrap, Geometry Nodes, constraint, driver, or instancing inputs;
* construct the reconstruction from evaluated GT meshes;
* duplicate or reuse GT materials, node trees, textures, images, or UV layers;
* extract or bake GT textures into reconstruction assets;
* reference GT materials, textures, images, or objects from generated shader nodes.

Inspection and numerical measurement remain allowed, as do manually observed colors, material
properties, texture frequencies, and pattern dimensions.

## Comparing

After each run, temporarily offset `recon__root`, frame both models in the viewport, and keep them
visible simultaneously from a useful comparison angle. Compare them directly, viewpoint by
viewpoint. Restore the reconstruction to the origin after the final comparison.

## Final state for this mode

In addition to the SKILL.md checklist:

* the GT model is present and unchanged;
* the complete reconstruction script contains no comparison offset;
* `recon__root` is restored to location `(0, 0, 0)` before the final save or export.

## Also report

* temporary comparison offset and axis used during inspection, and confirmation that it was
  removed before the final save or export.
