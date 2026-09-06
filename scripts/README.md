# Experiment runners

The four public entry points use the same benchmark, output layout, agent
names, and common CLI flags. Run each command from the experiment root
(`1graded_exp/` in the parent checkout), after following [INSTALL.md](../INSTALL.md).
All four runners automatically load the root `.env`; their child processes
inherit its variables. ActiveVisual and Full3DInteraction require Linux/Xvfb.

```bash
pixi run single-view --agent gpt-5-6-sol
pixi run multi-view --agent gemini3-1-pro
pixi run active-visual --agent opus-5
pixi run full-3d-interaction --agent minimax-m3
```

Common options:

| Option | Meaning |
| --- | --- |
| `--data-path PATH` | benchmark root or one instance directory |
| `--output-dir PATH` | output base; setting/texture/agent are appended |
| `--agent NAME` | one of the seven names listed in the root README |
| `--texture-renders [BOOL]` | use color renders/textured GLBs |
| `--no-texture-renders` | use grey renders/geometry-only GLBs |
| `--tasks NAME ...` | select benchmark instances |
| `--limit N` | keep the first N selected instances |
| `--num-parallel N` | concurrent instances |
| `--blender PATH` | Blender executable |
| `--dry-run` | resolve inputs and outputs without model/Blender work |

Single-view and Multi-view perform one initial generation followed by three
editing iterations by default. Use `--iterations` to change the edit count and
`--max-render-retries` to control repairs after invalid Python or failed
renders.

ActiveVisual and Full3DInteraction persist task-local CLI sessions and Blender
checkpoints. Repeating the same command resumes interrupted compatible work;
`--fresh` archives the old attempt and restarts it. `--block-incompatible`
keeps an incompatible checkpoint untouched instead of restarting it.

Outputs are always written as:

```text
outputs/<setting>/<w_texture|wo_texture>/<agent>/<instance>/
```

Agent time and token/API usage are recorded per instance and aggregated by
`evaluation/evaluate.py`. The supporting `_runner.py`, bootstrap, shell,
template and scene-setup files are runtime dependencies of the four entry
points.
