# Two-instance mode — the GT lives behind a separate read-only Blender

**You are in this mode only if** two Blender MCP servers are connected and one of them exposes only
viewport/camera changes and screenshots, with no code execution or scene editing. If instead a
single Blender holds both the GT and your reconstruction, stop and read
[same-scene.md](same-scene.md).

**Before doing anything else, list and read the tools each MCP server provides**, so you know
exactly what operations each instance supports. This is both a safety step and how you confirm the
server roles.

## The two servers

Address each server by its **name**, never by a hardcoded port — ports come from the MCP
configuration.

* **The read-only server** (typically `blender-viewport-only`) holds the **GT model**. You can only
  take viewport screenshots and change the viewport/camera. Use it to observe the GT from many
  angles. It exposes no scene-editing tools, and any attempt to run geometry-modifying code there
  is rejected.
* **The full-access server** (typically the official Blender MCP, `official-blender-mcp`) is your
  **workspace**. It exposes inspection, screenshot, viewport/render, and full-access
  `execute_blender_code` tools. Here you execute the reconstruction script.

## GT preservation in this mode

The GT server is read-only by design. Do not attempt to edit, delete, hide, move, rotate, scale,
rename, or otherwise alter anything there, and do not run scene-mutating code on it. All
construction happens in the workspace server.

## Inspecting the GT

The read-only server exposes **only viewport screenshots and view/camera changes** — precise numeric
measurement tools (scene info, bounding boxes) are **not** available there. Infer GT proportions
visually by comparing several angles, and switch the viewport/camera to cover the viewpoint list in
SKILL.md.

## Placement — no offset

Construct the model in a coordinate frame aligned with the GT as you observed it, and keep the
reconstruction **at that GT-aligned origin**. There is no GT object in the workspace server to sit
beside, so no comparison offset is needed — and it could not be computed anyway, since it depends on
the GT bounding box, which is unmeasurable from here. The two models live in separate Blender
instances and are compared by matching viewpoints across the two servers.

Keep the exact complete script in the workspace Blender's `reconstruction_gt.py` Text datablock and
execute the code stored in that datablock via the workspace server. Do not write an output `.py`
file yourself; the graded runner publishes the exact in-Blender Text datablock.

## Independence — satisfied by construction

The GT is in a different Blender instance and is not reachable from the workspace server, so the
reconstruction cannot depend on any GT object or mesh datablock. Build everything procedurally;
your only inputs from the GT are visual observation and estimated measurements.

## Comparing

After each run, frame the reconstruction in the workspace viewport from angles that **match** the
ones you used to observe the GT, then compare screenshot to screenshot from that same set of
viewpoints. Re-screenshot both instances after every revision.

## Final state for this mode

In addition to the SKILL.md checklist:

* the read-only server's GT model remains completely unchanged;
* the workspace server contains the `VLM_RECONSTRUCTION` collection, the reconstructed objects, and
  the `recon__root` Empty;
* no major geometric or proportional discrepancy remains when comparing the two instances'
  screenshots.

## Also report

* the tools discovered on each MCP server;
* the GT viewpoints inspected on the read-only server.
