# Raw agent backends

This package implements the image-conditioned backends used by Single-view and
Multi-view, plus shared registration and orchestration used by all four
settings.

Public agent names live in `agent_registry.py`; runners and evaluation import
that single source of truth. Backend module and provider model names are
internal and may differ from output-directory names.

See [INSTALL.md](../INSTALL.md#3-install-and-log-in-to-your-agent)
for official CLI installation and login steps. All public runners load the
checkout root `.env` before initializing a backend; exported shell values win.
Credentials must never be committed:

| Public agent | Credential source |
| --- | --- |
| `gpt-6-astra`, `gpt-5-6-sol` | Codex CLI login or Codex API environment |
| `kimi-k3` | `MOONSHOT_API_KEY` |
| `opus-5`, `fable-5` | Claude Code login |
| `qwen3-8-max-preview` | `QWEN_API_KEY` |
| `gemini3-1-pro` | authenticated `agy`/Antigravity CLI |
| `minimax-m3` | `MINIMAX_API_KEY` |

Python dependencies are installed from `pyproject.toml`. TaskSolver and its
`pyagy` adapter are included only in the Linux environment. Codex is invoked
directly as `codex` from PATH. Kimi uses the configured
API key in an isolated CLI home; Qwen uses the configured Coding Plan key.
Their interactive OAuth logins do not replace these experiment keys. MiniMax
is a direct API adapter and does not require a CLI.
Rendering uses `evaluation/core/render.py` directly.

The public image runners are:

```bash
pixi run python scripts/Single-view/run.py --help
pixi run python scripts/Multi-view/run.py --help
```

Each agent's `backend.py` exposes only the functions needed by the shared
iterative runner. Opus, Fable and Kimi adapters do not mutate each other's
module state. The four experiment wrappers and `evaluation/evaluate.py` are
the public entry points.
