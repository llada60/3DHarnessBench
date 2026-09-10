# Installation

This guide sets up **3DHarnessBench on Linux x86-64 with an NVIDIA GPU**.
Commands use Debian/Ubuntu and run from the repository root
(`3DHarnessBench/`). The Linux installation and end-to-end checklist in step 6
have been validated on Ubuntu with an NVIDIA GPU.

## 1. Install the environment

Clone the repository, then install the host tools. An NVIDIA driver compatible
with **CUDA 13** must be installed separately; check it with `nvidia-smi`.

```bash
git clone https://github.com/llada60/3DHarnessBench.git
cd 3DHarnessBench

sudo apt-get update
sudo apt-get install -y git curl xz-utils xvfb xauth x11-utils
nvidia-smi
```

Install [Pixi](https://pixi.sh/) **0.79+**, then the project dependencies:

```bash
curl -fsSL https://pixi.sh/install.sh | bash
export PATH="$HOME/.pixi/bin:$HOME/.local/bin:$PATH"
pixi install --locked
```

Pixi provides Python, Node.js, ripgrep, TaskSolver, evaluation libraries, D-Bus,
VirtualGL and Xpra. Agent CLIs are installed separately in step 3. Keep
`$HOME/.local/bin` on PATH in subsequent shells. Native builds can take time;
reduce the build-job settings in `pyproject.toml` on smaller machines. Recreate
`.pixi/` on Linux when migrating from another OS.

## 2. Install Blender and configure .env

Download **Blender 5.1.2** from the [official archive](https://download.blender.org/release/Blender5.1/):

```bash
mkdir -p tools
curl -fL https://download.blender.org/release/Blender5.1/blender-5.1.2-linux-x64.tar.xz \
  -o tools/blender.tar.xz
tar -xJf tools/blender.tar.xz -C tools
test -f .env || cp .env.example .env
```

Set the executable path in `.env`, then add the API keys needed in step 3:

```dotenv
BLENDER="tools/blender-5.1.2-linux-x64/blender"
```

All four runners and `evaluation/evaluate.py` load `.env` on every launch, and
child processes inherit it. Exported shell variables take precedence; no manual
`source .env` is needed. See [.env.example](.env.example) for optional GPU settings.

## 3. Install and log in to your agent

Install only the agents you use:

| Agent | Installation | Authentication |
| --- | --- | --- |
| `gpt-6-astra`, `gpt-5-6-sol` | `pixi run npm install -g --prefix "$HOME/.local" @openai/codex@0.153.4` | `pixi run codex login` |
| `opus-5`, `fable-5` | [Claude Code](https://code.claude.com/docs/en/setup): `curl -fsSL https://claude.ai/install.sh \| bash` | Run `claude` and log in |
| `kimi-k3` | `pixi run npm install -g --prefix "$HOME/.local" --omit=optional @moonshot-ai/kimi-code@0.29.2` | `.env`: `MOONSHOT_API_KEY="..."` for the Kimi Code coding endpoint |
| `qwen3-8-max-preview` | `pixi run npm install -g --prefix "$HOME/.local" @qwen-code/qwen-code@0.21.0` | `.env`: `QWEN_API_KEY="..."` for Alibaba Cloud's international Coding Plan |
| `gemini3-1-pro` | [Antigravity CLI](https://antigravity.google/docs/cli/getting-started): `curl -fsSL https://antigravity.google/cli/install.sh \| bash` | Run `agy` and log in |
| `minimax-m3` | Official MiniMax API; no CLI needed | `.env`: `MINIMAX_API_KEY="..."` |

The npm commands use Pixi's pinned Node.js but install the CLIs under
`$HOME/.local`, outside the Pixi environment. Run the benchmark through
`pixi run` so these Node.js launchers use the project's compatible runtime.
Kimi's optional native TUI modules are omitted because benchmark runs use its
non-interactive print mode. Qwen's optional Linux prebuilds are installed.

Kimi/Qwen adapters require the listed API keys; interactive OAuth alone is
insufficient. Gemini uses TaskSolver's `pyagy` runtime and your Antigravity
login. Claude/Qwen image backends also use TaskSolver. Verify installed CLIs
with `pixi run kimi --version` and `pixi run qwen --version`.

For Codex API billing, put `OPENAI_API_KEY` in `.env` and run:

```bash
pixi run env-run sh -c 'printenv OPENAI_API_KEY | codex login --with-api-key'
```

Claude API billing uses `ANTHROPIC_API_KEY` in `.env`.

For ActiveVisual/Full3DInteraction, prepare the local MCP service:

```bash
pixi run uv sync --directory BlenderMCP/viewport_only --locked
```

The official Blender MCP source is downloaded automatically on first use.

## 4. Add data and evaluation assets

Download the [benchmark](https://huggingface.co/datasets/lingada/3DHarnessBench)
and place the instance directories under `data/benchmark/`, following the
[data layout](data/benchmark/README.md).

Clone the Uni3D source (skip if already present):

```bash
mkdir -p evaluation/external
git clone --depth 1 https://github.com/baaivision/Uni3D.git evaluation/external/Uni3D
test -f "${UNI3D_REPO:-evaluation/external/Uni3D}/models/point_encoder.py"
```

`UNI3D_REPO` may point to another local checkout. Evaluation checks for the
required `models/point_encoder.py` module and reports a direct setup error when
it is absent. The harness does not enforce a Uni3D commit or tag.

Evaluation downloads **SigLIP2, DINOv2, DINOv3, Uni3D-Giant and EVA02/OpenCLIP**
weights on first use. Allow roughly **20 GB** for weights in addition to the
Pixi environment and benchmark data.

For DINOv3, request access on its [model page](https://huggingface.co/facebook/dinov3-vitl16-pretrain-lvd1689m),
then log in:

```bash
pixi run hf auth login
```

Alternatively set `HF_TOKEN="..."` in `.env`. Existing HF CLI logins are reused;
weights stay in the project's `.cache/`. If a download fails with a CAS/Xet error,
set `HF_HUB_DISABLE_XET="1"` in `.env` and rerun to use the HTTP downloader.

## 5. Run a small check

After setup, generate one instance and evaluate it:

```bash
pixi run codex login status

pixi run single-view --agent gpt-5-6-sol \
  --tasks VaseFactory --iterations 0 --num-parallel 1 \
  --output-dir outputs/install-check

pixi run python evaluation/evaluate.py --setting Single-view --agent gpt-5-6-sol \
  --tasks VaseFactory --output-dir outputs/install-check \
  --num-parallel 1 --device cuda --batch-size 1
```

Generation should produce `VaseFactory.py` and four images in `renders/`.
Evaluation writes `_metrics/evaluation_run.json` and `_metrics/final_metrics.json`
under `outputs/install-check/Single-view/w_texture/gpt-5-6-sol/`.
Check the evaluation status and instance count; this check covers **one instance**
and **no editing rounds**.

Without DINOv3 access, add `--image-encoders siglip2 dinov2` to evaluation;
Uni3D still runs, but this omits one image encoder. A `--dry-run` checks inputs
without agent/model execution and does not establish runtime success.

See [README.md](README.md#running-3dharnessbench) for all four settings, benchmark
parameters and resumption, and [evaluation/README.md](evaluation/README.md) for
metric details and GLB export limitations.

## 6. Linux release-validation checklist

Use a fresh clone and complete every item before treating the environment as
validated:

- [ ] `nvidia-smi` detects the intended GPU and driver.
- [ ] `pixi install --locked` completes without changing `pixi.lock`.
- [ ] The selected agent CLI version and authentication checks succeed.
- [ ] `pixi run uv sync --directory BlenderMCP/viewport_only --locked`
      completes.
- [ ] The Uni3D `models/point_encoder.py` check in step 4 succeeds.
- [ ] All five public entry points accept `--help`.
- [ ] The single `VaseFactory` generation in step 5 writes its Python script
      and four renders.
- [ ] The CUDA evaluation in step 5 writes successful `evaluation_run.json`
      and `final_metrics.json` records for one instance.

If any item fails, keep the release untagged and include the Linux distribution,
GPU, driver, Pixi and Blender versions with the failing command when reporting
the problem.
