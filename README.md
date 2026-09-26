# 3DHarnessBench: Probing Agentic 3D-to-Code Capabilities of Frontier Vision-Language Models


<p align="center">
  🌐 <a href="https://llada60.github.io/3DHarnessBench/">Project Page</a> |
  📄 <a href="https://arxiv.org/abs/2609.06535">arXiv</a> |
  🤗 <a href="https://huggingface.co/datasets/lingada/3DHarnessBench">Benchmark</a>
</p>


![3DHarnessBench: visual comparisons, agentic Blender reconstruction, and benchmark scores](assets/teaser.png)


> **Abstract:** We introduce 3DHarnessBench, a benchmark that evaluates the agentic ability of frontier vision-language models (VLMs) to recover 3D geometry as Blender Python code from a variety of inputs. Unlike previous frameworks that prompt the VLMs with a fixed input (e.g., a single rendering or a text description), 3DHarnessBench evaluates four separate harness settings that progressively enable active agentic exploration, facilitated by recent Blender MCP functionality. Our hierarchy from Single-view, Multi-view, Active Visual (arbitrary viewpoint access), and Full 3D Interaction (complete access to the target object through Blender function calls) probes the models' abilities in both visual perception and active inference, tool calling, and self-correction. We observe that the ability of all frontier models to recover 3D geometry improves significantly with richer function call access, although the improvements are strongly model-dependent, revealing highly uneven agentic 3D-to-code capabilities. We release the benchmark, code, outputs, and agent trajectories for reproducible 3D evaluation.

---

## Repository layout

```text
.
├── .github/
│   ├── ISSUE_TEMPLATE/bug_report.yml
│   └── pull_request_template.md
├── assets/
│   ├── logo1.png
│   ├── logo2.png
│   └── teaser.png
├── benchmark/
│   └── README.md                # Dataset layout and download instructions
├── configs/
│   ├── active_visual.toml
│   ├── full_3d_interaction.toml
│   └── image_agents.toml
├── core/
│   ├── agents/                  # MCP adapters, checkpoints and usage
│   ├── blender_mcp/             # Bundled MCP packages and their licenses
│   │   └── viewport_only/
│   │       └── README.md        # Restricted reference-view service
│   ├── harness/                 # Shared runners and orchestration
│   │   └── backends/            # Provider modules and shared image/render helpers
│   ├── README.md
│   ├── blender_render.py
│   ├── blender_runtime.py
│   ├── blender_scene_setup.py
│   ├── export_glb.py
│   ├── paths.py
│   └── render.py
├── metrics/
│   ├── README.md
│   ├── WORKERS.md
│   ├── evaluate.py
│   ├── image_similarity.py
│   ├── shape_betti.py
│   ├── shape_chamfer.py
│   ├── shape_uni3d.py
│   └── usage.py
├── packages/
│   └── virtualgl/
│       ├── pixi.toml
│       └── recipe.yaml
├── prompts/
│   ├── active_visual/
│   │   ├── instruction.tmpl
│   │   └── resume_instruction.tmpl
│   ├── blender-gt-reconstruction/
│   │   ├── SKILL.md
│   │   └── references/
│   ├── full_3d_interaction/
│   │   ├── instruction.tmpl
│   │   └── resume_instruction.tmpl
│   ├── README.md
│   ├── image_iterations.py
│   └── image_only.py
├── scripts/
│   └── with_env.py
├── tasks/
│   ├── active_visual/           # Two-Blender MCP setting
│   ├── full_3d_interaction/      # Full-access MCP setting
│   ├── multi_view/              # Multi-view image setting
│   ├── single_view/             # Single-view image setting
│   └── README.md
├── .env.example
├── .gitignore
├── INSTALL.md
├── LICENSE                     # Apache-2.0 for original code and content
├── NOTICE
├── README.md
├── pixi.lock
└── pyproject.toml
```

Start with [tasks/README.md](tasks/README.md) for generation and
[metrics/README.md](metrics/README.md) for scoring. Shared code, configuration,
prompts and assets are separated following the layout of
[3DCodeBench](https://github.com/gaoypeng/3dcodebench).
The [setting guide](tasks/README.md#settings) describes all four experiments;
[core/README.md](core/README.md#code-reading-paths) traces the Single-view and
ActiveVisual implementations.
All generation, editing, repair and MCP task prompts are described in
[prompts/README.md](prompts/README.md).
Generated scripts, renders and evaluation results are saved under `outputs/`.

## Installation

Follow **[INSTALL.md](INSTALL.md)** to install Pixi, Blender **5.1.2** and agent
CLIs, configure `.env`, and prepare evaluation weights. The target platform is
**Linux x86-64 + NVIDIA GPU**; ActiveVisual and Full3DInteraction use Xvfb.

Run commands from the repository root (`3DHarnessBench/`). All entry points load
`.env` automatically; exported shell variables take precedence.

Project modules use package imports, and Python subprocesses use `python -m`.
Resource paths are relative to the repository root; Blender workers import their
entry modules through `--python-expr`. Custom prompts and add-ons are selected
by importable module name, rather than by Python source-file path.

## Download the Benchmark

Download [3DHarnessBench from Hugging Face](https://huggingface.co/datasets/lingada/3DHarnessBench)
and place the instance directories under `benchmark/`, following the
[data layout](benchmark/README.md). The complete benchmark contains
**100 instances** with textured/grey GLBs and reference renders.

## Running 3DHarnessBench

### Single-view and Multi-view

Single-view uses one reference image (`Image_005.png` by default); Multi-view uses
the four benchmark reference images. Both generate Python, render the result,
and then edit it for the specified number of rounds.

```bash
pixi run single-view \
  --agent gpt-5-6-sol \
  --data-path benchmark \
  --output-dir outputs \
  --texture-renders True \
  --iterations 3 \
  --num-parallel 8
```

For **Multi-view**, replace `single-view` with `multi-view`.

- `--agent`: agent to run. Supported names:
  `gpt-6-astra`, `gpt-5-6-sol`, `kimi-k3`, `opus-5`, `fable-5`,
  `qwen3-8-max-preview`, `gemini3-1-pro`, `minimax-m3`.
- `--data-path`: benchmark directory; default **`benchmark`**.
- `--output-dir`: output base directory; default **`outputs`**.
- `--texture-renders`: use color references (`True`, default) or grey references
  (`False`).
- `--iterations`: editing rounds **after one initial generation**; default **3**.
  Set **0** for generation only.
- `--num-parallel`: concurrent instances; default **8**.

By default, the command runs all discoverable instances. Add `--tasks VaseFactory`
to select an instance, or `--limit 5` for the first five. Add `--dry-run` to inspect
inputs without model calls. A complete dataset should show **`Tasks: 100`**.

Repeat the same command to resume. If changing checkpoint settings such as
`--iterations`, use a new `--output-dir` or add `--overwrite` to restart.

### ActiveVisual and Full3DInteraction

ActiveVisual lets the agent inspect a restricted reference Blender and build in
a separate editable Blender. Full3DInteraction provides full MCP access to one
Blender containing the reference.

Both tasks use a verified upstream Blender MCP commit without runtime source
patches. Local checkouts and forks can be selected explicitly; see
[MCP source configuration and screenshot compatibility](core/blender_mcp/README.md).

```bash
pixi run active-visual \
  --agent gpt-5-6-sol \
  --data-path benchmark \
  --output-dir outputs \
  --texture-renders True \
  --num-parallel 4 \
  --timeout 9600 \
  --max-retries 5
```

For **Full3DInteraction**, replace `active-visual` with `full-3d-interaction`.

- `--agent`, `--data-path`, `--output-dir`: same meanings and defaults as above.
- `--texture-renders`: load the textured reference GLB (`True`, default) or its
  grey version (`False`).
- `--num-parallel`: concurrent instances; default **4**.
- `--timeout`: seconds per CLI attempt; default **9600**, not a total run limit.
- `--max-retries`: same-session reconnections after timeout; default **5**.
  Qwen/MiniMax also use this budget for session continuations.

These modes are interactive and have no `--iterations` parameter. `--tasks`,
`--limit` and `--dry-run` work as above. Repeating a command resumes compatible
checkpoints and skips completed instances. To restart including completed work,
add `--fresh --rerun`.

## Evaluation

Use the same setting, agent, output directory and texture choice as generation:

```bash
pixi run python -m metrics.evaluate \
  --setting Single-view \
  --agent gpt-5-6-sol \
  --data-path benchmark \
  --output-dir outputs \
  --texture-renders True \
  --device cuda \
  --batch-size 16 \
  --num-parallel 4
```

- `--setting`: **required**; `Single-view`, `Multi-view`, `ActiveVisual` or
  `Full3DInteraction`.
- `--agent`: **required**; the agent whose results you want to evaluate.
- `--data-path`, `--output-dir`, `--texture-renders`: same defaults as generation.
- `--device`: model inference device; default **`cuda`** (NVIDIA GPU).
- `--batch-size`: image-encoder batch size; default **16**.
- `--num-parallel`: concurrent Blender preparation jobs, not metric-model
  concurrency. Unset by default: **1** render worker and **4** GLB workers;
  supplying this option sets both counts.

Evaluation prepares missing renders/GLBs and computes usage, Chamfer, Betti, image
similarity (**SigLIP2, DINOv2, DINOv3**) and **Uni3D** metrics.

Add `--tasks VaseFactory` to evaluate a subset. Existing metric files are reused;
add `--overwrite-metrics` after changing the evaluated selection or parameters.
After changing generated scripts, add `--overwrite-artifacts --overwrite-metrics`.
Without DINOv3 access, `--image-encoders siglip2 dinov2` runs a partial encoder
evaluation; Uni3D still runs.

### Results

Generated scripts and renders are saved under:

```text
outputs/<setting>/<w_texture|wo_texture>/<agent>/<instance>/
```

Evaluation writes to the agent's `_metrics/` directory:

- **`evaluation_run.json`**: stage status and evaluated instance names/count.
- **`final_metrics.json`**: aggregate scores.
- Per-metric JSON files: detailed scores and coverage.

Check `status` and `n_instances`: evaluation selects existing generated/reference
pairs and skips unfinished image iterations. Success does not automatically
mean all **100 instances** were evaluated.

For additional options, append `--help` to any command. See
[metrics/README.md](metrics/README.md) for evaluation details and GLB export
limitations.

## Citation

If you find our work useful, please consider citing:

```bibtex
@misc{liu20263dharnessbench,
  title         = {3DHarnessBench: Probing Agentic 3D-to-Code Capabilities of Frontier Vision-Language Models},
  author        = {Ling Liu and Bingchen Gong and Amal Dev Parakkat and Maks Ovsjanikov},
  year          = {2026},
  eprint        = {2609.06535},
  archivePrefix = {arXiv},
  primaryClass  = {cs.CV},
  url           = {https://arxiv.org/abs/2609.06535}
}
```


## License

Original source code, configuration, executable scripts, paper, documentation,
standalone prompt templates, skills, and project images in this repository are
licensed under the [Apache License 2.0](LICENSE); see [NOTICE](NOTICE) for the
attribution summary.

Bundled BlenderMCP-derived code remains under its bundled
[core](core/blender_mcp/core/LICENSE) and
[viewport service](core/blender_mcp/viewport_only/LICENSE) MIT licenses.

Third-party models, weights and downloaded dependencies retain their own terms.
