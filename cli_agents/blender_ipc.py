"""Direct talk to a BlenderMCP add-on socket (no MCP server in between).

The harness needs a few things the agent-facing MCP layer deliberately cannot
do: inspect the viewport_only scene (its MCP server AST-blocks every read), and
save the full_access .blend at the end. Both add-ons speak the same one-shot
JSON protocol on their TCP port -- {"type": ..., "params": {...}} in, a single
{"status": ..., "result"/"message": ...} object back -- so we speak it directly.
"""

from __future__ import annotations

import json
import socket


class BlenderIPCError(RuntimeError):
    pass


def send(port: int, kind: str, host: str = "127.0.0.1", timeout: float = 120.0,
         **params) -> dict:
    """One command, one reply. Raises BlenderIPCError on a non-success status."""
    payload = json.dumps({"type": kind, "params": params}).encode()
    reply = None
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(payload)
            buf = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                try:
                    reply = json.loads(buf.decode())
                except json.JSONDecodeError:
                    continue  # partial frame; keep reading
                break
            else:
                raise BlenderIPCError(f"{kind}: connection closed early")
    except OSError as exc:
        raise BlenderIPCError(f"{kind} on {host}:{port}: {exc}") from exc
    if reply is None:
        raise BlenderIPCError(f"{kind} on {host}:{port}: empty response")
    if reply.get("status") != "success":
        raise BlenderIPCError(
            f"{kind} on {host}:{port} failed: {reply.get('message') or reply}")
    return reply.get("result") or {}


def run_code(port: int, code: str, **kw) -> str:
    """`execute_code`, returning whatever the snippet printed.

    Both add-ons accept this; the viewport_only *add-on* is unrestricted (the
    camera-only allowlist lives in its MCP server, which we bypass here).
    """
    result = send(port, "execute_code", code=code, **kw)
    return str(result.get("result", ""))


def send_official_code(port: int, code: str, host: str = "127.0.0.1",
                       timeout: float = 120.0) -> dict:
    """Execute code through Blender's official null-delimited bridge."""
    payload = json.dumps({
        "type": "execute", "code": code, "strict_json": True,
    }).encode() + b"\0"
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(payload)
            buf = bytearray()
            while b"\0" not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf.extend(chunk)
    except OSError as exc:
        raise BlenderIPCError(
            f"official execute on {host}:{port}: {exc}") from exc
    if not buf:
        raise BlenderIPCError(
            f"official execute on {host}:{port}: empty response")
    try:
        reply = json.loads(bytes(buf).partition(b"\0")[0])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BlenderIPCError(
            f"official execute on {host}:{port}: invalid response") from exc
    if reply.get("status") != "ok":
        raise BlenderIPCError(
            f"official execute on {host}:{port} failed: "
            f"{reply.get('message') or reply}")
    return reply


def official_scene_summary(port: int, **kw) -> dict:
    """Scene summary through the official Blender MCP bridge."""
    reply = send_official_code(port, (
        "import bpy\n"
        "result = {\n"
        "    'scene': bpy.context.scene.name,\n"
        "    'objects': sorted(o.name for o in bpy.data.objects),\n"
        "    'meshes': sorted(o.name for o in bpy.data.objects if o.type == 'MESH'),\n"
        "    'filepath': bpy.data.filepath,\n"
        "}\n"), **kw)
    result = reply.get("result")
    if not isinstance(result, dict):
        raise BlenderIPCError(f"official scene summary is not a dict: {result!r}")
    return result


def save_official_blend(port: int, path: str, **kw) -> str:
    """Save an artifact/checkpoint through the official bridge."""
    reply = send_official_code(port, (
        "import bpy, os\n"
        f"target = {path!r}\n"
        "os.makedirs(os.path.dirname(target), exist_ok=True)\n"
        "bpy.ops.wm.save_as_mainfile(filepath=target, copy=True)\n"
        "result = {'path': target, 'bytes': os.path.getsize(target)}\n"), **kw)
    return json.dumps(reply.get("result") or {}, sort_keys=True)


def scene_summary(port: int, **kw) -> dict:
    """Scene name + object names/types, via execute_code so it works on both roles."""
    printed = run_code(port, (
        "import bpy, json\n"
        "print(json.dumps({\n"
        "    'scene': bpy.context.scene.name,\n"
        "    'objects': sorted(o.name for o in bpy.data.objects),\n"
        "    'meshes': sorted(o.name for o in bpy.data.objects if o.type == 'MESH'),\n"
        "    'filepath': bpy.data.filepath,\n"
        "}))\n"), **kw)
    try:
        return json.loads(printed.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise BlenderIPCError(f"unparseable scene summary: {printed!r}") from exc


def prepare_reference_scene(port: int, add_light: bool = True, **kw) -> dict:
    """Make the freshly-imported reference observable, and report its size.

    The bootstrap's GLB import removes *every* object first, so the factory
    camera and light go with it. That leaves the reference Blender in a state the
    agent cannot work with: `bpy.context.scene.camera` is None, so every
    camera-move the viewport_only MCP allowlist permits (`cam.location = ...`)
    raises, and the 3D viewport is still parked at the default position, framing
    empty space -- an agent that screenshots it first thing concludes there is no
    reference model at all (observed exactly that before this existed).

    So: ensure a camera exists, optionally add a fallback sun, and frame every
    3D viewport on the imported geometry. EvalCam from the shared grading rig
    keeps its exact transform; geometry itself is untouched by this helper.
    """
    printed = run_code(port, (
        "import bpy, json\n"
        "meshes = [o for o in bpy.data.objects if o.type == 'MESH']\n"
        "cam = next((o for o in bpy.data.objects if o.type == 'CAMERA'), None)\n"
        "if cam is None:\n"
        "    cam_data = bpy.data.cameras.new('Camera')\n"
        "    cam = bpy.data.objects.new('Camera', cam_data)\n"
        "    bpy.context.scene.collection.objects.link(cam)\n"
        "    cam.location = (7.36, -6.93, 4.96)\n"
        "    cam.rotation_euler = (1.109, 0.0, 0.815)\n"
        "bpy.context.scene.camera = cam\n"
        f"if {bool(add_light)} and not [o for o in bpy.data.objects if o.type == 'LIGHT']:\n"
        "    light = bpy.data.objects.new('Light', bpy.data.lights.new('Light', 'SUN'))\n"
        "    bpy.context.scene.collection.objects.link(light)\n"
        "    light.location = (4.08, 1.01, 5.9)\n"
        "for obj in bpy.data.objects:\n"
        "    obj.select_set(obj.type == 'MESH')\n"
        "if meshes:\n"
        "    bpy.context.view_layer.objects.active = meshes[0]\n"
        "window = bpy.context.window_manager.windows[0]\n"
        "framed = 0\n"
        "for area in window.screen.areas:\n"
        "    if area.type != 'VIEW_3D':\n"
        "        continue\n"
        "    region = next((r for r in area.regions if r.type == 'WINDOW'), None)\n"
        "    if region is None:\n"
        "        continue\n"
        "    with bpy.context.temp_override(window=window, area=area, region=region):\n"
        "        if meshes:\n"
        # view_selected fits ONLY the selection (the imported meshes, selected
        # just above). view_all would fit the camera and sun this function just
        # added ~7m out as well, zooming the viewport ~9x past a 1.5m model --
        # and since get_viewport_screenshot captures this 3D viewport rather
        # than the camera view, the agent then sees the reference as a speck.
        "            bpy.ops.view3d.view_selected()\n"
        # EvalCam is already placed with the exact grading transform. Keep it
        # there; this helper only needs to frame the interactive 3D viewport.
        "            if cam.name != 'EvalCam':\n"
        "                bpy.ops.view3d.camera_to_view_selected()\n"
        "        else:\n"
        "            bpy.ops.view3d.view_all(center=False)\n"
        "    framed += 1\n"
        "dims = [0.0, 0.0, 0.0]\n"
        "for obj in meshes:\n"
        "    dims = [max(a, b) for a, b in zip(dims, obj.dimensions)]\n"
        "print(json.dumps({\n"
        "    'meshes': [o.name for o in meshes],\n"
        "    'max_dimensions': [round(v, 3) for v in dims],\n"
        "    'camera': cam.name,\n"
        "    'camera_location': [round(v, 3) for v in cam.location],\n"
        "    'viewports_framed': framed,\n"
        "}))\n"), **kw)
    try:
        return json.loads(printed.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise BlenderIPCError(
            f"reference-scene prep returned no summary: {printed!r}") from exc


def save_blend(port: int, path: str, **kw) -> str:
    """Save the instance's current state to `path` without repointing its session.

    `copy=True` keeps the running Blender's own filepath untouched, so the agent's
    Blender is not silently rebound to our artifact file.
    """
    printed = run_code(port, (
        "import bpy, os\n"
        f"target = {path!r}\n"
        "os.makedirs(os.path.dirname(target), exist_ok=True)\n"
        "bpy.ops.wm.save_as_mainfile(filepath=target, copy=True)\n"
        "print('saved', target, os.path.getsize(target))\n"), **kw)
    return printed.strip()


# Legacy callers may still use this default. The graded Full3DInteraction and viewport
# runners pass their canonical block name explicitly.
RECONSTRUCTION_TEXT_BLOCK = "reconstruct_gt.py"


def configure_gpu_environment(blender_cfg: dict, env: dict[str, str]) -> str | None:
    """Apply ``[blender].gpu_device`` to a Blender process environment.

    ``auto``/``inherit`` leaves the caller's environment untouched. An integer,
    a comma-separated string, or a TOML list selects physical device numbers.
    CUDA/HIP use the same visibility convention; inside Blender the selected
    devices are renumbered from zero, as those runtimes normally do.
    """
    configured = blender_cfg.get("gpu_device", "auto")
    if configured is None:
        return None
    if isinstance(configured, bool):
        raise ValueError("[blender].gpu_device must be a GPU number, list, or 'auto'")
    if isinstance(configured, (list, tuple)):
        parts = [str(value).strip() for value in configured]
    else:
        text = str(configured).strip()
        if not text or text.lower() in ("auto", "inherit"):
            return None
        parts = [part.strip() for part in text.split(",")]
    if not parts or any(not part.isdigit() for part in parts):
        raise ValueError(
            "[blender].gpu_device must contain non-negative GPU numbers, "
            "for example 0, '1', or '0,1'")
    selected = ",".join(parts)
    env["CUDA_VISIBLE_DEVICES"] = selected
    env["HIP_VISIBLE_DEVICES"] = selected
    env["ROCR_VISIBLE_DEVICES"] = selected
    return selected


def text_block_export_code(target: str,
                           preferred: str = RECONSTRUCTION_TEXT_BLOCK,
                           allow_fallback: bool = True) -> str:
    """Blender-side source that writes an existing text datablock to `target`.

    Shared by both harnesses, which reach Blender over different transports, so
    it reports through BOTH channels: `result` for the official bridge's
    strict-JSON execute, and a printed JSON line for the add-on's execute_code.

    This function deliberately never creates or modifies a Blender Text
    datablock. The in-Blender ``preferred`` block is the authoritative artifact;
    the runner only publishes its exact contents under the instance filename.
    """
    return (
        "import bpy, json, os\n"
        f"target = {target!r}\n"
        f"preferred = {preferred!r}\n"
        "blocks = list(bpy.data.texts)\n"
        "chosen = bpy.data.texts.get(preferred)\n"
        "how = 'exact'\n"
        f"if chosen is None and {allow_fallback!r}:\n"
        "    lowered = [t for t in blocks if t.name.lower() == preferred.lower()]\n"
        "    chosen, how = (lowered[0], 'case-insensitive') if lowered else (None, how)\n"
        "if chosen is not None and not chosen.as_string().strip():\n"
        "    chosen = None\n"
        f"if chosen is None and {allow_fallback!r}:\n"
        # A renamed script is still the deliverable, but an arbitrary note is
        # not: require a non-empty .py Text datablock stored in the .blend.
        "    scripts = [t for t in blocks if t.name.lower().endswith('.py') "
        "and t.as_string().strip()]\n"
        "    if scripts:\n"
        "        chosen = max(scripts, key=lambda t: len(t.as_string()))\n"
        "        how = 'largest .py text block'\n"
        "summary = {'path': None, 'name': None, 'bytes': 0, 'matched': False,\n"
        "           'how': None, 'candidates': sorted(t.name for t in blocks)}\n"
        "if chosen is not None and chosen.as_string().strip():\n"
        "    body = chosen.as_string()\n"
        "    parent = os.path.dirname(target)\n"
        "    if parent:\n"
        "        os.makedirs(parent, exist_ok=True)\n"
        "    with open(target, 'w', encoding='utf-8') as handle:\n"
        "        handle.write(body)\n"
        "    summary.update({'path': target, 'name': chosen.name,\n"
        "                    'bytes': os.path.getsize(target), 'matched': True,\n"
        "                    'how': how})\n"
        "result = summary\n"
        "print(json.dumps(summary))\n")


def export_text_block(port: int, path: str,
                      preferred: str = RECONSTRUCTION_TEXT_BLOCK,
                      allow_fallback: bool = True, **kw) -> dict:
    """Write the agent's in-.blend build script to `path`. Never raises on a miss."""
    printed = run_code(port, text_block_export_code(
        path, preferred, allow_fallback), **kw)
    try:
        return json.loads(printed.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise BlenderIPCError(
            f"text-block export returned no summary: {printed!r}") from exc


def export_official_text_block(
        port: int, path: str,
        preferred: str = RECONSTRUCTION_TEXT_BLOCK,
        allow_fallback: bool = True, **kw) -> dict:
    """Export the reconstruction script through the official bridge."""
    reply = send_official_code(
        port, text_block_export_code(
            path, preferred, allow_fallback), **kw)
    result = reply.get("result")
    if not isinstance(result, dict):
        raise BlenderIPCError(
            f"official text-block export is not a dict: {result!r}")
    return result
