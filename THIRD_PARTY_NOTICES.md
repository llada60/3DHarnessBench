# Third-party notices

3DHarnessBench includes or interoperates with third-party projects. Their
licenses are not replaced by the repository's GPL or CC licenses.

## Bundled BlenderMCP-derived code

`BlenderMCP/core/` and `BlenderMCP/viewport_only/` contain modified and
reorganized code derived from
[BlenderMCP by Siddharth Ahuja](https://github.com/ahujasid/blender-mcp).
It is distributed under the MIT License:

> Copyright (c) 2025 Siddharth Ahuja

The original notice and license text are retained in both component
directories. Changes made for 3DHarnessBench include the restricted
reference-view server, shared connection/screenshot runtime and harness
integration.

## Downloaded at runtime

The benchmark can download or clone Blender, Blender MCP, Uni3D, model weights
and Python/Conda packages. These artifacts are not bundled in the Git tree and
remain subject to their respective upstream licenses and access terms. Consult
the upstream project or model page before redistribution.
