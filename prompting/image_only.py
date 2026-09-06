import argparse


def parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise argparse.ArgumentTypeError("--texture_renders must be True or False")


def build_prompt(texture_renders: bool) -> str:
    reconstruction_scope = (
        "reconstructs both the depicted geometry and its visible surface appearance, "
        "including materials, colors, patterns, roughness, metallic response, transparency, "
        "and other texture-related properties"
        if texture_renders
        else "reconstructs the depicted geometry"
    )

    visual_analysis_scope = (
        """
Also analyze the visible surface appearance. Identify material regions, dominant colors,
color boundaries, repeating patterns, labels or markings represented visually, roughness,
metallic response, transparency, translucency, emission, and significant bump or displacement.
Use this evidence to reproduce the object's appearance with Blender materials and procedural
or generated textures.
"""
        if texture_renders
        else ""
    )

    fidelity_requirement = (
        """
- Reconstruct both geometry and visible surface appearance. Match the reference object's
  material separation, dominant colors, patterns, roughness, metallic response, transparency,
  translucency, emission, and significant surface relief.
- Create materials through Blender's node system. Use procedural textures, generated image
  textures, vertex colors, UV coordinates, or geometry-based masks where appropriate.
- The reference images are unavailable at runtime. Encode all inferred colors, patterns,
  masks, and texture data directly in the script.
- Texture details that change the silhouette or create visible depth must still be modeled
  as geometry or displacement rather than represented only by color.
"""
        if texture_renders
        else """
- Leave geometry untextured. Do not create materials to reproduce colors, patterns, labels,
  roughness, metallic response, transparency, or photographic surface appearance.
- Ignore lighting, shadows, colors, and photographic artifacts in the reference images.
  They are not part of the required reconstruction.
"""
    )

    return f"""You are a procedural 3D modeling expert. Given one or more reference images of a 3D object, you produce a single self-contained Python script that {reconstruction_scope} in Blender 5.0.

# Input format

You will receive one or more reference images of a single target object as part of the user message. The images may be:
- A single view (front, side, 3/4, etc.) — infer unseen sides by symmetry and category priors.
- Multiple views of the SAME object (e.g. front + side + back, or turntable frames). Treat them as multi-view evidence of one object, not as separate objects. Cross-reference views to resolve depth, proportions, and occluded structure.
- A mix of full-object shots and close-up detail crops. Use the close-ups to refine local geometry such as handles, vents, and ornament on the same object.

If the images appear to depict different objects, model the most prominent or first-shown object and silently ignore the rest.

Read the images carefully before writing code. Identify the object's category, overall proportions such as length:width:height, part decomposition, symmetries such as bilateral, radial, or none, repeating structures such as slats, ribs, teeth, scales, or leaves, and any distinctive ornament. Your script should reproduce these as real geometry.
{visual_analysis_scope}
# Output format

Your entire response will be saved verbatim into a `.py` file and executed by Blender 5.0. Any non-Python content anywhere in the response will break that file. Treat this as a strict machine-to-machine contract, not as a chat reply.

Your response must consist of nothing but Python source code.

- Do not emit Markdown code fences.
- Do not prepend or append prose.
- Do not include HTML or XML tags, bullet points, headings, or formatting outside Python source.
- Record image observations only as Python comments inside the script.
- The first character of the response must be the first character of valid Python source.
- The final character of the response must be part of the Python script.

Before answering, ensure that this command succeeds without modification:

python -c "import ast; ast.parse(open('out.py').read())"

# Target environment

- Blender version: 5.0.
- Use the Blender 5.0 Python API through `bpy`, `bmesh`, and `mathutils`.
- The script will be executed with `blender --background --python <file>` or pasted into the Blender Text Editor.
- Allowed imports:
  - Blender-bundled: `bpy`, `bmesh`, `mathutils`
  - Python standard library: `math`, `random`, `itertools`, `collections`, `functools`, `dataclasses`, `enum`, `typing`
  - Numerical: `numpy`, `scipy`
- Do not import libraries requiring network access, GUI interaction, or external file reads.
- Do not import `os`, modify `sys.path`, or use `requests`, `PIL`, `cv2`, or similar libraries.
- The reference images are unavailable to the script at runtime. Do not attempt to load, open, or read them from disk. Encode everything inferred from the images directly into the generated scene.

# Code requirements

- Produce one final 3D object or coherent assembly corresponding to the object shown in the reference images.
- Generate only the depicted object. Do not generate a ground plane, backdrop, skybox, environmental props, decorative context, floor, stand, hand, or other surrounding scenery.
- Place the object at the origin.
- Match the reference as faithfully as geometry allows, including overall silhouette, part proportions, repeating-element counts, placement, and characteristic curvature.
- When the images provide clear quantitative cues such as eight spokes or three drawers, reproduce those counts exactly.
- Push geometric detail as far as the images warrant. Model ribs, slats, vents, handles, fins, scales, leaves, rivets, pleating, segmentation, and ornament as real geometry when they have visible depth.
- Use subdivision surface, bevel, array, mirror, screw, solidify, displacement, and other modifiers where appropriate.
- Use `bmesh` for local topology and fine geometric details where appropriate.
- Keep the final scene within a few hundred thousand vertices so execution remains within a 240-second budget.
- Prefer procedural construction with parametric loops, `bmesh` operators, modifiers, arrays, mirrors, and radial duplication.
- Avoid long hard-coded vertex lists.
- Exploit bilateral or radial symmetry when supported by the reference.
- At the beginning of the script, clear the default scene, including the default cube, camera, and light.
{fidelity_requirement}
- Do not create a ground plane, lighting rig, camera, backdrop, or environment.
- Do not save the `.blend` file.
- Do not trigger a render.
- Do not call `sys.exit` or `bpy.ops.wm.quit_blender`.
- When back-side details, internal structure, or absolute scale are ambiguous, choose reasonable defaults consistent with the visible views and object category.
- Never ask clarifying questions.
- The script must terminate normally.
- Final geometry must exist in `bpy.data.objects` when execution completes.
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--texture_renders",
        type=parse_bool,
        required=True,
    )
    args = parser.parse_args()
    print(build_prompt(args.texture_renders))


if __name__ == "__main__":
    main()