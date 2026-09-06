# Full3DInteraction benchmark

This runner starts an official Blender MCP stack: one Blender containing the reference model,
connected through the official full-access MCP server. `blender_bootstrap.py`
wraps the pinned official add-on startup for this benchmark.

On a fresh run, Blender uses the same world, three-point area lights, camera,
default material, Cycles samples, transparency and resolution as the grading
renderer. After GLB import, the rig is installed from the imported geometry's
extent. Area-light energy follows the benchmark's
`(extent / 2.5)^2` normalization; colour runs use multiplier 1.0 and grey runs
use 0.5. The interactive viewport starts in Rendered shading with scene lights
and world enabled. Resume preserves the checkpoint.

```bash
pixi run -e default python scripts/Full3DInteraction/run.py \
  --agent gpt-5-6-sol \
  --num-parallel 6 \
  --texture-renders True \
  --data-path data/benchmark
```

Use `--tasks NAME ...` or `--limit N` for a subset, and `--dry-run` to inspect
the resolved GLBs and output paths without starting Blender. `--data-path` is
optional and defaults to `data/benchmark`; legacy underscore spellings remain
supported. Textured runs load `<instance>.glb`, while
`--texture-renders False` loads `<instance>_grey.glb`.

Up to four instances run concurrently by default. Use `--num-parallel N` to
change the concurrency, or `--num-parallel 1` to run one at a time.

The reconstruction prompt is the shared
`skills/blender-gt-reconstruction` skill. The runner stages
that complete directory in each task workspace and explicitly invokes it; the
skill selects the same-scene reference for this mode.

Each completed instance saves its final reconstructed Blender scene as
`final.blend` in that instance's output directory.

After the agent exits, its inspection screenshots are moved to
`log_renders/<run-id>/` and its other workspace scratch files are moved to
`log_artifacts/<run-id>/`, preserving their relative paths. Inputs, final
deliverables, CLI/session logs, attempts, and checkpoints remain in their
stable locations. Artifact organization is deferred while a resumable
checkpoint is active so the continued agent can still use its helper files.

Every backend reconnects a captured same-session ID up to five times within
each timeout episode by default; a successful response resets that counter.
Qwen and MiniMax also continue after a clean exit without the required
artifact or a configured recoverable provider error. Provider usage-limit
responses are terminal for the current invocation: Qwen's private API-error
telemetry is monitored while it runs, its process group is stopped immediately,
and the live Blender scene and session ID are saved as a resumable checkpoint.
After the wrapper checkpoints, repeating the same command resumes the saved
CLI session and Blender scene under `checkpoint/`; cross-invocation
`max_attempts` is unlimited. Within the same output pool, changing the selected
GLB or another compatibility input causes the incompatible checkpoint to be
archived before that instance automatically starts over; pass
`--block-incompatible` to retain the old strict `BLOCKED` behavior. Instances
already marked successful are skipped before checkpoint conflict handling,
while compatible checkpoints still resume.
