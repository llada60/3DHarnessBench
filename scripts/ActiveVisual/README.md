# ActiveVisual benchmark

This runner starts a two-Blender stack: a read-only reference Blender and a full-access
workspace Blender, each exposed through its own MCP server. The three shell
launcher files in this directory adapt the source launchers for the graded
two-instance setup.

Fresh instances use the grading renderer's world, three-point area lights,
camera, default material, Cycles samples, transparency and resolution. The
reference rig is installed after GLB import; the initially empty full-access rig
uses extent 1. Area-light energy follows the benchmark's `(extent / 2.5)^2`
normalization; colour runs use multiplier 1.0 and grey runs use 0.5. Resume
preserves both checkpoint scenes unchanged. Fresh VIEW_3D areas start in
Rendered shading with scene lights and world enabled.

```bash
pixi run -e default python scripts/ActiveVisual/run.py \
  --agent kimi-k3 --texture-renders True
```

Use `--tasks NAME ...` or `--limit N` for a subset, and `--dry-run` to inspect
the resolved GLBs and output paths without starting Blender. The default input
is `data/benchmark`; textured runs load `<instance>.glb`, while
`--texture-renders False` loads `<instance>_grey.glb`.

Up to four instances run concurrently by default. Use `--num-parallel N` to
change the concurrency, or `--num-parallel 1` to run one at a time.

The reconstruction prompt is the shared
`skills/blender-gt-reconstruction` skill. The runner stages
that complete directory in each task workspace and explicitly invokes it; the
skill selects the two-instance reference for this mode.

Each completed instance saves the reconstructed full-access Blender scene as
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
and the live Blender scenes and session ID are saved as a resumable checkpoint.
After the wrapper checkpoints, repeating the same command resumes the saved
CLI session and both Blender scenes under `checkpoint/`; cross-invocation
`max_attempts` is unlimited. Within the same output pool, changing the selected
GLB or another compatibility input causes the checkpoint to be
archived before the instance starts over; pass `--block-incompatible` to keep
it and report `BLOCKED` instead.
