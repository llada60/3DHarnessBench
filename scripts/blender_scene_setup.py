"""Shared Blender scene rig used by grading and interactive MCP sessions.

This module is imported only inside Blender.  Keeping the camera, lighting,
world and render settings here prevents the interactive reconstruction scene
from drifting away from the scene used by ``evaluation/core/render.py``.
"""

from __future__ import annotations

import bpy
from mathutils import Vector


GEOMETRY_TYPES = ("MESH", "CURVE", "SURFACE", "FONT", "META")
LIGHT_REFERENCE_EXTENT = 2.5
LIGHT_ENERGIES = {"Key": 1200.0, "Fill": 400.0, "Rim": 600.0}
SCENE_RIG_VERSION = "benchmark_extent_squared_v1"


def configure_cycles_gpu() -> str:
    """Use the best CUDA-capable Cycles backend, falling back to CPU.

    CUDA_VISIBLE_DEVICES/HIP_VISIBLE_DEVICES are applied by the harness before
    Blender starts.  This function performs the separate Blender-side step:
    enabling the visible device in Cycles and selecting GPU rendering.
    """
    scene = bpy.context.scene
    try:
        preferences = bpy.context.preferences.addons["cycles"].preferences
        for backend in ("OPTIX", "CUDA"):
            try:
                preferences.compute_device_type = backend
                if hasattr(preferences, "refresh_devices"):
                    preferences.refresh_devices()
                else:
                    preferences.get_devices()
            except Exception:
                continue

            devices = [
                device for device in preferences.devices
                if device.type == backend
            ]
            if not devices:
                continue

            for device in preferences.devices:
                device.use = device.type == backend
            scene.cycles.device = "GPU"
            print(
                f"[cycles] GPU enabled: {backend} "
                f"({', '.join(device.name for device in devices)})",
                flush=True,
            )
            return backend
    except Exception as exc:
        print(
            f"[cycles] GPU initialization failed: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )

    scene.cycles.device = "CPU"
    print("[cycles] no CUDA/OptiX device available; using CPU", flush=True)
    return "CPU"


def clear_scene() -> None:
    """Remove the factory scene and its reusable object data."""
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for collection in (
        bpy.data.meshes,
        bpy.data.curves,
        bpy.data.cameras,
        bpy.data.lights,
        bpy.data.materials,
        bpy.data.node_groups,
    ):
        for item in list(collection):
            collection.remove(item)
    bpy.context.scene.cursor.location = (0, 0, 0)


def geometry_objects() -> list:
    return [
        obj for obj in bpy.context.scene.objects
        if obj.type in GEOMETRY_TYPES
    ]


def _bounds(objects: list) -> tuple[Vector, float]:
    """Return the robust bounding-box center and scalar extent."""
    if not objects:
        return Vector((0, 0, 0)), 1.0

    sizes = []
    for obj in objects:
        corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
        size = max(
            max(v.x for v in corners) - min(v.x for v in corners),
            max(v.y for v in corners) - min(v.y for v in corners),
            max(v.z for v in corners) - min(v.z for v in corners),
        )
        sizes.append((size, obj))

    sorted_sizes = sorted(size for size, _obj in sizes)
    median_size = sorted_sizes[len(sorted_sizes) // 2]
    max_allowed = max(median_size * 30.0, 30.0) if median_size > 0 else 30.0
    bounded = [obj for size, obj in sizes if size <= max_allowed] or objects
    corners = [
        obj.matrix_world @ Vector(corner)
        for obj in bounded
        for corner in obj.bound_box
    ]
    xs, ys, zs = zip(*[(v.x, v.y, v.z) for v in corners])
    center = Vector((
        (min(xs) + max(xs)) / 2,
        (min(ys) + max(ys)) / 2,
        (min(zs) + max(zs)) / 2,
    ))
    extent = max(
        max(xs) - min(xs),
        max(ys) - min(ys),
        max(zs) - min(zs),
    ) or 1.0
    return center, float(extent)


def _remove_camera_and_lights() -> None:
    for obj in list(bpy.data.objects):
        if obj.type in ("CAMERA", "LIGHT"):
            bpy.data.objects.remove(obj, do_unlink=True)
    for collection in (bpy.data.cameras, bpy.data.lights):
        for item in list(collection):
            if item.users == 0:
                collection.remove(item)


def _frame_viewports(center: Vector, extent: float) -> int:
    framed = 0
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
            space = area.spaces.active
            region_3d = space.region_3d
            region_3d.view_location = center
            region_3d.view_distance = max(extent * 1.8, 0.1)
            # MCP screenshots capture this viewport. Use the actual benchmark
            # world and lights so imported GLB materials are visible instead
            # of the default solid-shading studio light.
            space.shading.type = "RENDERED"
            space.shading.use_scene_lights_render = True
            space.shading.use_scene_world_render = True
            area.tag_redraw()
            framed += 1
    return framed


def setup_scene(
    objects: list | None = None,
    resolution: int = 512,
    samples: int = 64,
    engine: str = "CYCLES",
    *,
    center_geometry: bool = True,
    frame_viewports: bool = False,
    light_power: float = 1.0,
    light_ref_extent: float = LIGHT_REFERENCE_EXTENT,
) -> tuple[object, Vector, float, float, float]:
    """Install the benchmark world, lights, camera and render settings.

    With no geometry, the rig uses an extent of 1.0.  Calling this again after
    a GLB import replaces that provisional rig with one scaled to the model.

    The benchmark scales area-light energy by
    ``(extent / light_ref_extent) ** 2``. ``light_power`` is 1.0 for its colour
    pass and 0.5 for its grey-material pass.
    """
    objects = list(geometry_objects() if objects is None else objects)
    bpy.context.view_layer.update()
    center, extent = _bounds(objects)

    if center_geometry and objects:
        roots = set()
        for obj in objects:
            root = obj
            while root.parent is not None:
                root = root.parent
            roots.add(root)
        for root in roots:
            root.location = root.location - center
        bpy.context.view_layer.update()
        center = Vector((0, 0, 0))

    cam_r, cam_h = extent * 1.8, extent * 0.6
    if light_ref_extent <= 0:
        raise ValueError("light_ref_extent must be positive")
    if light_power < 0:
        raise ValueError("light_power must be non-negative")
    light_energy_scale = (extent / light_ref_extent) ** 2

    world = bpy.context.scene.world or bpy.data.worlds.new("World")
    bpy.context.scene.world = world
    world.use_nodes = True
    for node in list(world.node_tree.nodes):
        world.node_tree.nodes.remove(node)
    background = world.node_tree.nodes.new("ShaderNodeBackground")
    output = world.node_tree.nodes.new("ShaderNodeOutputWorld")
    background.inputs[0].default_value = (0.012, 0.013, 0.021, 1)
    background.inputs[1].default_value = 1.0
    world.node_tree.links.new(background.outputs[0], output.inputs[0])

    _remove_camera_and_lights()

    def add_light(name: str, location: tuple, energy: float, size: float) -> None:
        bpy.ops.object.light_add(type="AREA", location=location)
        light = bpy.context.object
        light.name = name
        light.data.energy = energy * light_energy_scale * light_power
        light.data.size = extent * size
        light.data.color = (1.0, 1.0, 1.0)
        light.rotation_euler = (
            center - Vector(location)
        ).to_track_quat("-Z", "Y").to_euler()

    add_light("Key", (cam_r * 0.9, -cam_r * 0.7, cam_h * 1.7),
              LIGHT_ENERGIES["Key"], 0.9)
    add_light("Fill", (-cam_r * 0.6, -cam_r * 0.4, cam_h),
              LIGHT_ENERGIES["Fill"], 1.2)
    add_light("Rim", (0, cam_r * 0.9, cam_h * 1.3),
              LIGHT_ENERGIES["Rim"], 0.7)

    material = bpy.data.materials.get("eval_default")
    if material is None:
        material = bpy.data.materials.new("eval_default")
    material.use_nodes = True
    bsdf = material.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = (0.72, 0.72, 0.75, 1)
        bsdf.inputs["Roughness"].default_value = 0.65
    for obj in objects:
        if hasattr(obj.data, "materials") and not obj.data.materials:
            obj.data.materials.append(material)

    bpy.ops.object.camera_add(location=(cam_r, 0, cam_h))
    camera = bpy.context.object
    camera.name = "EvalCam"
    bpy.context.scene.camera = camera
    camera.data.lens = 50
    camera.data.clip_end = extent * 25
    camera.rotation_euler = (
        center - camera.location
    ).to_track_quat("-Z", "Y").to_euler()

    scene = bpy.context.scene
    scene.render.resolution_x = scene.render.resolution_y = resolution
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.film_transparent = True
    scene.render.engine = engine
    if engine == "CYCLES":
        scene.cycles.samples = samples
        scene.cycles.use_denoising = True
        configure_cycles_gpu()
    else:
        for attr in ("taa_render_samples", "samples"):
            try:
                setattr(scene.eevee, attr, samples)
                break
            except AttributeError:
                pass

    if frame_viewports:
        _frame_viewports(center, extent)
    return camera, center, cam_r, cam_h, extent


def scene_setup_summary(
    extent: float,
    light_power: float = 1.0,
    light_ref_extent: float = LIGHT_REFERENCE_EXTENT,
) -> dict:
    scene = bpy.context.scene
    return {
        "rig_version": SCENE_RIG_VERSION,
        "engine": scene.render.engine,
        "cycles_device": (
            scene.cycles.device if scene.render.engine == "CYCLES" else ""
        ),
        "resolution": [scene.render.resolution_x, scene.render.resolution_y],
        "film_transparent": scene.render.film_transparent,
        "camera": scene.camera.name if scene.camera else "",
        "lights": sorted(
            obj.name for obj in scene.objects if obj.type == "LIGHT"
        ),
        "extent": round(float(extent), 6),
        "light_power": float(light_power),
        "light_ref_extent": float(light_ref_extent),
        "light_energy_scale": round(
            (float(extent) / float(light_ref_extent)) ** 2 * float(light_power),
            8,
        ),
        "light_energies": {
            name: round(float(scene.objects[name].data.energy), 6)
            for name in LIGHT_ENERGIES
            if name in scene.objects and scene.objects[name].type == "LIGHT"
        },
        "viewport_shading": sorted({
            area.spaces.active.shading.type
            for window in bpy.context.window_manager.windows
            for area in window.screen.areas
            if area.type == "VIEW_3D"
        }),
    }
