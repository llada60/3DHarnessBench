# CLI agent adapters

This package provides the MCP-capable adapters, process cleanup, checkpointing,
and usage accounting used by ActiveVisual and Full3DInteraction.

The public experiment names are defined once in
`raw_agents/agent_registry.py`. Internally they route to these adapters:

| Public name | Adapter kind |
| --- | --- |
| `gpt-6-astra` | `codex` |
| `gpt-5-6-sol` | `codex` |
| `kimi-k3` | `kimi` |
| `opus-5` | `opus` |
| `fable-5` | `claude` |
| `qwen3-8-max-preview` | `qwen-tokenplan` |
| `gemini3-1-pro` | `agy` |
| `minimax-m3` | `minimax-m3` |

Third-party CLI executables and TaskSolver's Python adapters are environment
dependencies. They are not vendored into the public repository. Every run uses
a task-local session directory so resumable state does not depend on a global
conversation store.

Use the public wrappers rather than importing adapters directly:

```bash
pixi run python scripts/ActiveVisual/run.py --help
pixi run python scripts/Full3DInteraction/run.py --help
```
