# Shared runtime

`core/` contains reusable execution code; public experiments start in `tasks/`
and scoring starts in `metrics/evaluate.py`.

| Component | Responsibility |
| --- | --- |
| `paths.py` | Resolve bundled data, configs, prompts and services from this checkout |
| [harness/](harness/) | Agent registry, common CLI, iterative/MCP orchestration, environment and usage |
| [harness/backends/](harness/backends/) | Image-agent adapters grouped by provider, with common parsing and rendering helpers |
| [agents/](agents/) | MCP-capable provider adapters, session accounting and checkpoints |
| `blender_scene_setup.py` | Shared grading camera, lighting and material setup |
| `render.py` | Host-side rendering CLI and subprocess scheduling |
| `blender_render.py` | Four-view rendering executed inside Blender |
| `blender_runtime.py` | Child-interpreter import paths and native library environment |
| `export_glb.py` | GLB preparation worker |
| [blender_mcp/](blender_mcp/viewport_only/README.md) | Bundled viewport-only service and common MCP transport |

## Agent backends

Public agent names live in [harness/agent_registry.py](harness/agent_registry.py),
the shared source of truth for runners and evaluation. Provider model IDs and
internal adapter names may differ from public output-directory names.

Image backends for Single-view and Multi-view are loaded on demand from
`harness/backends/`. `iterative_runner.py::AGENT_SPECS` maps public agent names
to backend modules, model IDs and configuration keys. Models using the same
provider share one implementation. All adapters use `backends/common.py` for
shared image loading, script parsing and rendering, without importing another
provider's implementation.

| Public agent | Image backend | MCP adapter kind |
| --- | --- | --- |
| `gpt-6-astra`, `gpt-5-6-sol` | `harness/backends/codex.py` | `codex` |
| `opus-5` | `harness/backends/claude.py` | `opus` |
| `fable-5` | `harness/backends/claude.py` | `claude` |
| `kimi-k3` | `harness/backends/kimi.py` | `kimi` |
| `qwen3-8-max-preview` | `harness/backends/qwen.py` | `qwen-tokenplan` |
| `gemini3-1-pro` | `harness/backends/gemini.py` | `agy` |
| `minimax-m3` | `harness/backends/minimax.py` | `minimax-m3` |

`agents/` provides the MCP-capable adapters, checkpoints and session usage
accounting used by ActiveVisual and Full3DInteraction. Each task uses a local
session directory so resumable state does not depend on a global conversation
store. Whole-run process cleanup lives in `harness/process_janitor.py`.

Third-party CLIs and TaskSolver adapters are environment dependencies, not
vendored code. TaskSolver and its `pyagy` adapter are included only in the Linux
environment. Use the public task entry points rather than calling adapters
directly; see [running the settings](../tasks/README.md) and
[agent installation and authentication](../INSTALL.md#3-install-and-log-in-to-your-agent).

## Code reading paths

### Single-view

1. [tasks/single_view/run.py](../tasks/single_view/run.py) selects the single-view
   mode and calls [harness/iterative_runner.py](harness/iterative_runner.py).
2. `main()` loads configuration, discovers inputs and schedules instances;
   `run_task()` owns initial generation, rendering, editing and repair.
3. [prompts/image_only.py](../prompts/image_only.py) supplies the base task;
   [prompts/image_iterations.py](../prompts/image_iterations.py) supplies the
   initial wrapper, editing instructions and parse/render feedback.
4. `Backend` resolves a provider module from `AGENT_SPECS` and sends each turn.
   Shared rendering helpers launch [render.py](render.py), which dispatches
   Blender execution to [blender_render.py](blender_render.py).
5. `feedback_views()` selects only the render matching the single reference,
   even though Blender produces four views. `run_task()` saves the script,
   renders and iteration records under the instance's output directory.

[tasks/multi_view/run.py](../tasks/multi_view/run.py) follows the same path with
`view_mode="multi"`: discovery loads all reference images, and feedback includes
all generated views.

### ActiveVisual

1. [tasks/active_visual/run.py](../tasks/active_visual/run.py) enters
   [harness/mcp_entry.py](harness/mcp_entry.py), which configures and schedules
   instances and performs final rendering.
2. [tasks/active_visual/_runner.py](../tasks/active_visual/_runner.py)::`run_task()`
   stages the reconstruction skill, starts both Blenders, runs the MCP agent
   session, exports the reconstruction and saves checkpoints.
3. [agents/agents.py](agents/agents.py) defines `mcp_server_specs`, `build` and
   `run_with_continuations`: read these before individual provider classes.
   [agents/checkpoint.py](agents/checkpoint.py) manages persisted recovery state.
4. The runner loads connection/resumption templates from `prompts/active_visual/`
   and stages [the shared skill](../prompts/blender-gt-reconstruction/SKILL.md)
   with its two-instance reference. The agent uses the
   [restricted reference service](blender_mcp/viewport_only/README.md) alongside
   the official full-access workspace service.

Full3DInteraction shares the MCP entry layer and adapters, but uses
[tasks/full_3d_interaction/_runner.py](../tasks/full_3d_interaction/_runner.py)
with one full-access Blender and the skill's same-scene reference. Neither MCP
setting has a fixed image-editing iteration loop. See
[MCP execution and recovery](../tasks/README.md#mcp-execution-and-recovery)
for resumption and output behavior.

## Imports and Blender execution

Import shared modules by package name, such as `core.harness.agent_registry`.
Run Python entry points as modules from the checkout root, for example
`python -m tasks.single_view.run`. Entry points expose `main(argv=None)`;
loading `.env` and starting work happen only when `main()` is called.
Repository modules do not modify `sys.path` or suppress import-order checks.
Blender-only imports stay inside workers/bootstraps so public `--help` and
offline checks do not require Blender or model weights.

Blender uses a separate Python interpreter. Its launchers supply a child-only
`PYTHONPATH` for the checkout and requested add-on, disable user site-packages,
and enable Blender's documented
[`--python-use-system-env`](https://docs.blender.org/manual/en/5.1/advanced/command_line/arguments.html#python-options)
option. Python launchers use `blender_runtime.blender_environment()`; the shell
launcher applies the same settings inside its Blender subshells. No import-path
setup is needed in task modules or in the user's shell.
