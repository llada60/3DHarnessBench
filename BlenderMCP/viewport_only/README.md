# Restricted reference-view MCP

This service is the reference-model interface for ActiveVisual. The local
add-on and stdio server expose two tools:

- `get_viewport_screenshot`: capture the reference viewport.
- `execute_blender_code`: execute camera and viewport adjustments validated
  by the AST allowlist in `src/blender_mcp/validator.py`.

Object, material and geometry editing are rejected by the restricted tool.
The reconstruction workspace uses a separate official Blender MCP server.

The experiment runner starts the add-on, assigns its port and configures the
agent's stdio server. Start through `scripts/ActiveVisual/run.py` from the
repository root. The default reference port is 8888; concurrent experiments
allocate ports dynamically.
