# Restricted reference-view MCP

This service is the reference-model interface for ActiveVisual. The local
add-on and stdio server expose two tools:

- `get_viewport_screenshot`: capture the reference viewport.
- `execute_blender_code`: execute camera and viewport adjustments validated
  by the AST allowlist in `src/blender_mcp/validator.py`.

Object, material and geometry editing are rejected by the restricted tool.
The reconstruction workspace uses a separate official Blender MCP server.

## Installation and execution

This bundled service depends on the sibling `../core` socket and screenshot
package; `../scripts` contains the Blender add-on bootstrap. Keep these
directories together: this package's `pyproject.toml` and `uv.lock` reference
`../core`.

From the repository root, after following [INSTALL.md](../../../INSTALL.md):

```bash
pixi run uv sync --directory core/blender_mcp/viewport_only --locked
pixi run active-visual --agent gpt-5-6-sol
```

The experiment runner starts the add-on, assigns its port and configures the
agent's stdio server. Use the [ActiveVisual entry point](../../../tasks/active_visual/run.py)
through the command above. The default reference port is 8888; concurrent
experiments allocate ports dynamically. See the
[setting guide](../../../tasks/README.md#activevisual) for execution and recovery.

## Official full-access source

The official Blender MCP source is fetched on demand for the full-access
workspace in ActiveVisual and the single Blender in Full3DInteraction.
Public commands cache it under `tasks/active_visual/.cache/` or
`tasks/full_3d_interaction/.cache/`, as set by `core/harness/mcp_entry.py`.
Internal runners without that public configuration layer use the TOML defaults
`.cache/active_visual/` and `.cache/full_3d_interaction/` instead.
These checkouts are separate from the bundled restricted service; source
versions are configured under `configs/`. See
[configuration](../../../tasks/README.md#configuration) for precedence.

## License

The bundled BlenderMCP-derived packages retain their MIT licenses:
[shared core](../core/LICENSE) and [viewport service](LICENSE).
