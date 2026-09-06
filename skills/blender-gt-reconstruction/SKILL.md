---
name: blender-gt-reconstruction
description: >-
  Reconstruct, rebuild, replicate, or visually match a ground-truth (GT) 3D model that already
  exists in a connected Blender MCP instance, producing a procedural, rerunnable Blender Python
  reconstruction. Use whenever the task says to reconstruct/rebuild/copy/match/reproduce a Blender
  model, scene object, or GT. Works in either access mode: (a) the GT sits in the same Blender
  scene as the reconstruction, compared side by side with exact measurements, or (b) the GT sits
  behind a separate read-only viewport-only Blender MCP server and is matched screenshot-to-
  screenshot from matching viewpoints. Covers GT inspection, geometry-first then optional
  texture/material stages, GT preservation, independence rules, the VLM_RECONSTRUCTION/recon__
  output contract, a rerun-safe self-contained script, and the final report. Not for
  general Blender modeling, creating scenes from text prompts, or importing assets when no
  existing GT model is present.
---

# Blender GT reconstruction

Inspect a ground-truth 3D model that already exists in a connected Blender instance, then build a
procedurally generated reconstruction that is geometrically as close to it as practical.

Do not stop at a rough approximation. Repeatedly inspect, modify, and rerun the Blender Python
script until the reconstruction closely matches the GT.

## Step 0 — determine access mode and scope, before touching Blender

Classify the connected Blender MCP servers from the tools available to you:

* **Exactly one** Blender server, which can both inspect the scene and execute Python (an
  `execute_blender_code`-style tool), with the GT model in that scene → **same-scene mode**.
  Read [references/same-scene.md](references/same-scene.md) now.
* **Two** Blender servers, where one exposes only viewport/camera changes and screenshots — no code
  execution, no scene editing (typically named `blender-viewport-only`) → **two-instance mode**.
  The restricted server holds the GT; the full-access server is your workspace.
  Read [references/two-instance.md](references/two-instance.md) now.

If the task statement names the mode or the servers, that wins over this inference. If neither rule
matches (for example two full-access servers), state your assumption in one line and proceed — and
never run scene-modifying code on a server whose role you have not confirmed.

**Scope.** Geometry is always stage 1. Also reconstruct textures and materials (stage 2 — read
[references/textures.md](references/textures.md) only once stage 1 passes its checks) unless the
task limits scope to geometry.

Do not start inspecting the GT until you have read your mode's reference: it contains required
parts of the output contract (measurement rules, model placement, final-state checks) that are not
repeated here. Read exactly one access-mode reference.

## GT preservation

Do not modify the GT in any way — not its objects, meshes, materials, modifiers, hierarchy,
collections, transforms, or visibility. Do not delete, hide, duplicate, rename, move, rotate,
scale, join, replace, or edit any part of it.

How this is enforced differs by mode; your mode reference states the specific rules.

## Inspect the GT

Inspect the GT before writing the final script, using your mode's inspection methods. Do not rely
only on the initial viewport image.

Use multiple viewpoints, including:

* front and rear;
* left and right;
* top and bottom;
* front and rear three-quarter views;
* close-ups of ambiguous or distinctive geometry.

Determine:

* overall dimensions, orientation, and aspect ratio;
* major parts and part count;
* proportions and relative scales;
* positions and rotations;
* symmetry and repeated components;
* attachment relationships;
* silhouettes;
* curvature and profile changes;
* visible materials and colors.

Your mode reference defines *how* to obtain these — whether exact measurement is available or the
values must be inferred visually.

## Procedural decomposition

Decompose the GT into coherent geometry units. For each unit, determine its:

* name and geometry type;
* dimensions and shape parameters;
* local position and orientation;
* attachment target;
* symmetry or repetition relationship.

Use suitable Blender procedures such as primitives, custom meshes, `bmesh`, curves, extrusions,
swept profiles, bevels, subdivision, solidify, mirror, array, booleans, or Geometry Nodes.

Use sufficiently detailed geometry for important shapes. Avoid crude primitives when a more
faithful procedural construction is practical.

## Reconstruction script

Write and execute **one self-contained Blender Python script**.

Create a collection named:

`VLM_RECONSTRUCTION`

All generated objects must be placed in this collection.

Create a root Empty named:

`recon__root`

Parent every generated geometry object to this root.

Use deterministic names beginning with `recon__` for every generated object, material, texture,
image, and node group. Examples:

* `recon__body`
* `recon__head`
* `recon__leg_left`
* `recon__antenna_right`

Construct the model in a coordinate frame aligned with the GT, and keep all part coordinates in
that GT-aligned frame. The self-contained reconstruction script must finish with `recon__root` at the
GT-aligned origin: location `(0, 0, 0)`, with no comparison offset encoded in the script. A mode
reference may allow a temporary display transform for comparison, but that transform is not part
of the reconstruction and must be removed before the final save or export.

## Independence from the GT

The reconstruction must be independently generated. Only observations and measurements may flow
from the GT into your script — never GT data itself.

Inspection and numerical measurement are allowed. Small manually defined vertex sets for individual
procedural parts are allowed.

Your mode reference states which specific dependencies are possible and therefore banned.

## Safe reruns

The script must be safe to rerun. At the beginning, remove only the previous generated collection:

```python
collection_name = "VLM_RECONSTRUCTION"

existing = bpy.data.collections.get(collection_name)
if existing is not None:
    for obj in list(existing.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    bpy.data.collections.remove(existing)
```

Also remove only reconstruction-owned materials, textures, images, and node groups whose names
begin with `recon__`, once they are no longer used.

Never clear the full Blender scene, and never remove objects, materials, textures, images, node
groups, or collections outside reconstruction-owned data.

Keep the exact latest complete script in a non-empty Blender Text datablock named
`reconstruction_gt.py`. Before finishing, update that datablock and execute the code stored in it,
so the visible reconstruction and the saved script agree. Do not write an instance-named `.py`
artifact yourself; the graded runner exports the exact contents of `reconstruction_gt.py` under
the instance name without modifying the Blender Text datablock.

## Iterative reconstruction

After every script execution, compare the GT and the reconstruction from multiple viewpoints, using
your mode's comparison method.

Check:

* total dimensions and aspect ratio;
* part count;
* symmetry and repeated-part spacing;
* part size, position, and orientation;
* attachment and intersections;
* front, rear, side, top, and bottom silhouettes;
* important curvature and distinctive local geometry.

Then:

1. identify the largest geometric discrepancy;
2. modify the self-contained reconstruction script;
3. rerun the complete script;
4. compare the GT and the reconstruction again, using your mode's comparison method;
5. repeat until no major visible geometric discrepancy remains.

Prioritize geometry corrections in this order:

1. missing or extra parts;
2. global dimensions and proportions;
3. part position, rotation, and scale;
4. attachment and floating geometry;
5. major silhouettes;
6. distinctive local shapes;
7. minor geometric details.

Do not declare completion, and do not begin detailed texture reconstruction, while major parts,
proportions, placements, repeated structures, attachments, or silhouettes are clearly incorrect.

Use a viewport shading mode that allows the comparison you are making: solid or studio lighting
during geometry validation.

## Final scene requirements

At completion there must be:

* the unchanged GT model;
* the `VLM_RECONSTRUCTION` collection containing the reconstructed objects;
* the `recon__root` root Empty.

Verify that:

* the GT remains unchanged;
* the script runs without errors and can be safely rerun;
* only generated reconstruction data is removed during reruns;
* no major geometric or proportional discrepancy remains;
* the generated geometry does not depend on GT datablocks.
* `recon__root` is at location `(0, 0, 0)`, and rerunning the complete script leaves it there.
* the non-empty `reconstruction_gt.py` Text datablock contains that exact complete script.

Additionally complete the final-state checklist in the reference(s) you used.

## Final response

Report:

* reconstruction collection name;
* generated object count;
* main geometry construction techniques;
* inspected viewpoints;
* number of geometry revision cycles;
* main geometry corrections made;
* major remaining geometric differences.

Append the "Also report" items from every reference you used.

Do not claim completion unless the script has executed successfully and the reconstruction has been
compared against the unchanged GT.
