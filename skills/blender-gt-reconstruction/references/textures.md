# Stage 2 — texture and material reconstruction

**Read this only once stage 1 has passed its checks.** Geometry comes first: do not spend
significant effort matching textures or materials while major parts, proportions, placements,
repeated structures, attachments, or silhouettes are clearly incorrect. Material work must never be
used to conceal incorrect geometry.

## What to match

Inspect and reconstruct the visible surface characteristics:

* base colors;
* color separation between parts;
* roughness and gloss;
* metallic or dielectric appearance;
* transparency or emission;
* visible texture patterns;
* normal or bump details;
* material assignments and boundaries.

When decomposing parts, also record each unit's visible material category and its texture or
surface characteristics.

Add close-ups of important texture, material, and color boundaries to your inspection viewpoints
now that geometry is validated.

## Independence

Texture reconstruction must remain independent from the GT. Recreate the visible appearance
procedurally or with newly generated reconstruction assets — never copy, duplicate, extract, bake,
serialize, or directly reuse GT texture images, material node trees, UV data, or material
datablocks. (In same-scene mode those datablocks are reachable, so the enforceable ban list in
[same-scene.md](same-scene.md) applies in full.)

Manually observed colors, material properties, texture frequencies, and pattern dimensions may be
used to create independent reconstruction materials and textures.

Name every generated material, texture, image, and node group with the `recon__` prefix, and purge
unused ones on rerun.

## How to build them

Prefer procedural shader nodes and deterministic generated textures where practical. Newly
generated image textures are allowed when a procedural shader cannot adequately reproduce an
important visible pattern.

Preserve, in the reconstruction: visible color regions, material boundaries, approximate roughness,
approximate metallic response, transparency or emission, prominent texture scale and direction, and
major bump or normal characteristics.

Switch to material preview or rendered shading for this stage (solid/studio lighting was for
geometry validation).

## Mode-conditional notes

* **Observing color and material.** In same-scene mode you can sample colors and read material
  properties directly. In two-instance mode you are estimating from screenshots of two different
  Blender instances whose lighting may differ, so treat hue and value matches as approximate and
  compare under several lighting angles before concluding a color is wrong.
* **Deliberate distinguishability.** In same-scene mode, reconstruction materials should closely
  resemble the GT after geometry validation while remaining *slightly* distinguishable where that
  helps side-by-side comparison. This does not apply in two-instance mode, where the models are
  never in one viewport.

## Iterating

Compare the GT and the reconstruction using material preview or rendered shading, from multiple
viewpoints and lighting angles.

Check:

* base colors;
* color boundaries;
* material assignments;
* roughness and gloss;
* metallic response;
* transparency and emission;
* texture pattern scale;
* texture direction and alignment;
* bump, normal, and displacement appearance;
* consistency across repeated or symmetric components.

Then:

1. identify the largest material or texture discrepancy;
2. modify the self-contained reconstruction script;
3. rerun the complete script;
4. compare again using your mode's comparison method;
5. repeat until no major visible surface discrepancy remains.

Prioritize surface corrections in this order:

1. missing or incorrect material regions;
2. dominant base colors;
3. roughness, metallic, transparency, and emission;
4. prominent texture patterns;
5. material boundary placement;
6. texture scale and orientation;
7. bump, normal, and minor surface details.

The final reconstruction should be nearly indistinguishable from the GT at a normal comparison
distance, except for minor details that are impractical to reproduce procedurally and any slight
intentional material difference used for comparison.

## Final state for this stage

* the independent reconstruction materials and textures exist and are `recon__`-named;
* GT materials, textures, images, and UV data are unchanged;
* no major visible material or texture discrepancy remains;
* generated materials and textures do not depend on GT datablocks.

## Also report

* generated material and texture counts;
* main material and texture construction techniques;
* number of texture and material revision cycles;
* main texture and material corrections made;
* major remaining material or texture differences.
