# Experiment runners

The four public entry points use the same benchmark, output layout, agent
names, and common CLI flags. Run each command from the repository root
(`3DHarnessBench/`), after following [INSTALL.md](../INSTALL.md).
All four runners automatically load the root `.env`; their child processes
inherit its variables. ActiveVisual and Full3DInteraction require Linux/Xvfb.

```bash
pixi run single-view --agent gpt-5-6-sol
pixi run multi-view --agent gemini3-1-pro
pixi run active-visual --agent opus-5
pixi run full-3d-interaction --agent minimax-m3
```

## Common options

| Option | Meaning |
| --- | --- |
| `--data-path PATH` | benchmark root or one instance directory |
| `--output-dir PATH` | output base; setting/texture/agent are appended |
| `--agent NAME` | one of the eight names listed in the root README |
| `--texture-renders [BOOL]` | use color renders/textured GLBs |
| `--no-texture-renders` | use grey renders/geometry-only GLBs |
| `--tasks NAME ...` | select benchmark instances |
| `--limit N` | keep the first N selected instances |
| `--num-parallel N` | concurrent instances |
| `--blender PATH` | Blender executable |
| `--dry-run` | resolve inputs and outputs without model/Blender work |

Python commands use module execution from the root, for example
`python -m tasks.single_view.run --help`; Pixi task aliases use this same form.
Use module execution rather than invoking a nested `run.py` by filename.
Each entry point exposes `main(argv=None)` and loads `.env` only when called,
so importing it does not start a run or alter the environment.
Directory names use snake_case; setting values and output directory names
keep their original spelling. Legacy underscore CLI spellings remain supported.

## Settings

### Single-view

Generate Blender Python from one reference image, render it, then refine the
script using reference and generated images. The default reference is
`Image_005.png`. Although Blender renders four views, only the matching view
is returned to the agent as feedback.

```bash
pixi run single-view --agent gpt-5-6-sol --tasks VaseFactory --iterations 0
```

Start reading at [single_view/run.py](single_view/run.py), then follow the
[Single-view call chain](../core/README.md#single-view).

### Multi-view

Generate and refine Blender Python using all available reference images in
the selected render directory (four views in the released benchmark).
All generated views are returned as feedback for subsequent edits.

```bash
pixi run multi-view --agent gpt-5-6-sol --tasks VaseFactory --iterations 0
```

[multi_view/run.py](multi_view/run.py) uses the same iterative runtime as
Single-view with `view_mode="multi"`. Outputs retain the `Multi-view` name.

### ActiveVisual

Run two Blender instances: a restricted reference scene for camera/viewport
inspection and a separate full-access reconstruction workspace, each exposed
through its own MCP server. The shell launchers in
[active_visual/](active_visual/) manage this two-instance setup.

```bash
pixi run active-visual --agent kimi-k3 --texture-renders True
```

The reference lighting rig is installed after GLB import; the initially empty
workspace rig uses extent 1. The reconstruction skill uses its two-instance
access rules. Connection and resumption templates live in
`prompts/active_visual/`. The completed workspace is saved as `final.blend`.
See the [ActiveVisual call chain](../core/README.md#activevisual).

### Full3DInteraction

Run one Blender containing the reference model through the official full-access
MCP server. [full_3d_interaction/blender_bootstrap.py](full_3d_interaction/blender_bootstrap.py)
wraps the pinned official add-on startup. The lighting rig uses the imported
geometry's extent.

```bash
pixi run full-3d-interaction --agent gpt-5-6-sol --texture-renders True
```

The reconstruction skill uses its same-scene access rules. Connection and
resumption templates live in `prompts/full_3d_interaction/`. The completed
reconstruction is saved as `final.blend`.

## Generation and editing

Single-view and Multi-view perform one initial generation followed by three
editing iterations by default. `--iterations 0` performs generation only;
`--max-render-retries` controls repairs after invalid Python or failed renders.
The default concurrency is eight instances, adjustable with `--num-parallel`.
Repeat the same command to resume; use a new output directory or `--overwrite`
when changing checkpoint settings such as the iteration count.

Initial instructions are defined in `prompts/image_only.py`; subsequent editing
instructions and parse/render retry feedback live in `prompts/image_iterations.py`.
See [prompts](../prompts/README.md) for the override interface and prompt flow.

## MCP execution and recovery

ActiveVisual and Full3DInteraction have no fixed `--iterations` loop. Both load
`<instance>.glb` for textured runs and `<instance>_grey.glb` for geometry-only
runs. Their default concurrency is four instances; use `--num-parallel 1` to
run sequentially. `--dry-run` checks resolved GLBs and output paths without
starting Blender.

Fresh scenes use the grading renderer's world, three-point area lights,
camera, default material, Cycles samples, transparency and resolution.
Area-light energy follows `(extent / 2.5)^2`, with multiplier 1.0 for colour
runs and 0.5 for grey runs. Fresh VIEW_3D areas use Rendered shading with scene
lights and world enabled. Resume preserves checkpoint scenes unchanged.

Each runner stages the complete shared
[reconstruction skill](../prompts/blender-gt-reconstruction/SKILL.md), including
its references, under the task workspace's `.agents/skills/` and invokes it.

Both settings persist task-local CLI sessions and Blender checkpoints:

- `--timeout` defaults to 9600 seconds per CLI attempt, not per whole run.
- `--max-retries` defaults to five same-session reconnections per timeout
  episode; a successful response resets the counter. Qwen and MiniMax also
  continue after a clean exit without the required artifact or a configured
  recoverable provider error.
- Provider usage-limit responses stop the current invocation. Qwen's private
  API-error telemetry is monitored while it runs, and its process group is
  stopped immediately on a limit. Live Blender scenes and the captured session
  ID are saved under `checkpoint/` for resumption.
- Repeating the same command resumes compatible checkpoints and skips
  completed instances. The public runners impose no cross-invocation attempt
  limit (`max_attempts = 0`). `--fresh` archives the previous attempt and starts
  over; use `--fresh --rerun` to restart completed work too.
- For unfinished work, changing the GLB or another compatibility input archives
  the incompatible checkpoint before restarting. `--block-incompatible` instead
  preserves it and reports `BLOCKED`. Completed instances are skipped before
  compatibility checks unless a restart is requested.

## Outputs

Outputs are always written as:

```text
outputs/<setting>/<w_texture|wo_texture>/<agent>/<instance>/
```

Agent time and token/API usage are recorded per instance and aggregated by
[evaluation](../metrics/README.md).

After an MCP agent exits, inspection screenshots move to `log_renders/<run-id>/`
and other workspace scratch files move to `log_artifacts/<run-id>/`, preserving
relative paths. Inputs, final deliverables, CLI/session logs, attempts and
checkpoints retain stable locations. Organization is deferred while a resumable
checkpoint is active so the continued agent can still use its helper files.

## Configuration

| Setting | Python entry point | Defaults |
| --- | --- | --- |
| Single-view | `tasks/single_view/run.py` | `configs/image_agents.toml` |
| Multi-view | `tasks/multi_view/run.py` | `configs/image_agents.toml` |
| ActiveVisual | `tasks/active_visual/run.py` | `configs/active_visual.toml` |
| Full3DInteraction | `tasks/full_3d_interaction/run.py` | `configs/full_3d_interaction.toml` |

`image_agents.toml` supplies reasoning-effort defaults per provider through
`core.harness.run_config`. The MCP TOML files configure execution, providers
and resumption; their runners expose the file path through `CONFIG_PATH`.

Public CLI flags override task defaults. Relative resource/cache paths in MCP
configs resolve against the repository root. After loading TOML,
`core/harness/mcp_entry.py::configure` applies CLI defaults and runtime values
for the agent/model, input/output roots, timeout/resumption and Blender binary.
It also sets the official-source cache to `tasks/<setting>/.cache/`.
The TOML cache values `.cache/active_visual/` and `.cache/full_3d_interaction/`
apply when internal runners are used without that public configuration layer.

Store credentials in the root `.env` or exported environment variables;
see [.env.example](../.env.example) and [authentication setup](../INSTALL.md#3-install-and-log-in-to-your-agent).
Task-specific `_runner.py`, Blender bootstraps and shell launchers live beside
their entry point. See [shared runtime and code reading paths](../core/README.md)
for how the settings call providers, load prompts and render outputs.
