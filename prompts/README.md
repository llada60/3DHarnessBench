# Reconstruction prompts

All benchmark task instructions live here, including the MCP reconstruction
skill and its access-mode references.

```text
prompts/
├── image_only.py                 # Initial image-to-code task instructions
├── image_iterations.py           # Generation wrapper, editing and repair feedback
├── active_visual/
│   ├── instruction.tmpl          # Two-Blender connection instructions
│   └── resume_instruction.tmpl   # Same-session continuation instructions
├── full_3d_interaction/
│   ├── instruction.tmpl          # One-Blender connection instructions
│   └── resume_instruction.tmpl   # Same-session continuation instructions
└── blender-gt-reconstruction/
    ├── SKILL.md                  # Shared MCP reconstruction task
    └── references/
        ├── two-instance.md       # ActiveVisual access rules
        ├── same-scene.md         # Full3DInteraction access rules
        └── textures.md           # Texture/material reconstruction stage
```

Single-view and Multi-view share `image_only.py`, whose
`build_prompt(texture_renders)` provides the initial task instructions.
`--prompt-path` overrides this base prompt module. `image_iterations.py`
provides `initial_prompt`, `editing_prompt`, `parse_feedback`, and
`render_feedback`: the initial image-count wrapper, the subsequent editing
rounds, and retry feedback for an unparseable reply or a Blender render failure.
The runner supplies the previous script, image counts, reply and error details.

For ActiveVisual and Full3DInteraction, the reconstruction task is defined by
[`blender-gt-reconstruction/SKILL.md`](blender-gt-reconstruction/SKILL.md).
The setting-specific `.tmpl` files supply connection and resumption context.
Each runner copies the complete skill directory, including `references/`, into
`<task-workspace>/.agents/skills/blender-gt-reconstruction/` and invokes it by
name. This preserves CLI skill discovery and the skill's relative references;
the repository source is maintained only under `prompts/`.

The [Single-view](../core/README.md#single-view) and
[ActiveVisual](../core/README.md#activevisual) code reading paths show where
each prompt is loaded and how it reaches the agent.
