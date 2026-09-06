"""Turn each CLI's own cross-session memory off for the raw runs.

Same policy as the shared CLI adapters (`--no-memory`), applied here
unconditionally: a raw tree run is a benchmark, so
every turn must answer from its own prompt and leave nothing behind for the next
turn, the next task, or an interactive session in this repo.

Why this is not cosmetic: these CLIs key their memory on the directory they run
in, and a raw wrapper runs from wherever it was launched -- normally the repo
root. `claude`'s auto-memory store is `~/.claude/projects/<sanitized-cwd>/memory/`,
so a run started in a repository reads and writes *the same notes an
interactive Claude Code session there uses*. agy is worse: without a
scoped HOME every turn shares the one global `~/.gemini` store (brain,
knowledge, conversations), which is also why its file tools, whose cwd is
`~/.gemini/antigravity-cli`, could reach another task's staged images.

What each wrapper gets:

  * claude_fable_5_raw / claude_opus_5_0_raw -- `apply_claude()`:
    `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` (checked before any settings lookup, so
    nothing can re-enable it) plus a run-local `--settings` file appended to
    tasksolver's argv with reads/writes and background consolidation off, the
    store redirected next to the run's outputs, and `claudeMdExcludes` so no
    CLAUDE.md above the cwd is loaded either.
  * codex_gpt_5_6_sol_raw -- `CODEX_FLAGS`: the memories feature, its writes,
    its reads, and AGENTS.md discovery.
  * agy_gemini_3_pro_raw -- `agy_home()`: a private HOME per turn, so agy's whole
    store is created empty and deleted with the turn. pyagy seeds it with
    symlinks to the real login token, so this costs nothing but a directory.
  * kimi_k3_raw -- `kimi_home()`: a private `KIMI_CODE_HOME` per turn, which is
    that CLI's whole state directory (user AGENTS.md, skills, sessions,
    config), created empty and deleted with the turn. The rest of its policy --
    an empty skills dir, `KIMI_DISABLE_CRON=1`, and running the turn from a
    workspace outside the repo so kimi's `AGENTS.md` walk finds nothing -- is
    in `kimi_code.py`, next to the CLI invocation it belongs to.
  * minimax_m3_raw -- nothing to do: it makes a stateless request to the
    official Messages endpoint and has no CLI store to read or write.
  * qwen3_8_max_preview_raw -- likewise, a plain HTTP completion (Model
    Studio's token-plan endpoint) through tasksolver's `VLLMModel`, no CLI
    and no store.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


# `codex exec` flags: the memory tool, memory writes, memory reads, AGENTS.md.
# `features.memories` is off by default in the current build, but a `[features]`
# table in ~/.codex/config.toml can turn it on, so all four are stated.
CODEX_FLAGS = ["-c", "features.memories=false",
               "-c", "memories.generate_memories=false",
               "-c", "memories.use_memories=false",
               "-c", "project_doc_max_bytes=0"]


def apply_claude(state_dir: Path) -> Path:
    """Disable claude's auto-memory for every CLI turn this process makes.

    Sets the env vars the spawned `claude` inherits and patches tasksolver's
    `ClaudeCodeModel._build_cli_command` -- the only seam available, since it
    builds the argv itself -- to append `--settings <file>`. Idempotent; returns
    the settings file, which doubles as the run's record of what was turned off.
    """
    # Absolute: the CLI resolves --settings against its own cwd, and a raw run
    # may well have been given a relative --output-dir.
    state_dir = Path(state_dir).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    settings_path = state_dir / "no-memory.settings.json"
    settings_path.write_text(json.dumps({
        "autoMemoryEnabled": False,
        "autoDreamEnabled": False,
        "autoMemoryDirectory": str(state_dir / "claude_memory"),
        "claudeMdExcludes": ["**/CLAUDE.md", "**/CLAUDE.local.md",
                             "**/AGENTS.md"],
    }, indent=2))

    os.environ["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    os.environ["CLAUDE_CODE_DISABLE_ORG_MEMORY"] = "1"

    from tasksolver.claude_code import ClaudeCodeModel

    if getattr(ClaudeCodeModel, "_no_memory_patched", False):
        return settings_path
    build = ClaudeCodeModel._build_cli_command

    def with_no_memory(prompt, tool_flag):
        return [*build(prompt, tool_flag), "--settings", str(settings_path)]

    ClaudeCodeModel._build_cli_command = staticmethod(with_no_memory)
    ClaudeCodeModel._no_memory_patched = True
    return settings_path


def agy_home(root: str, scope: str) -> str:
    """A fresh private HOME for one agy turn (pass as pyagy's `data_dir`).

    Named after the turn it serves, like the workspaces, so a leftover directory
    under `root` is traceable to the turn that failed to clean it up.
    """
    os.makedirs(root, exist_ok=True)
    return tempfile.mkdtemp(prefix=f"agyhome_{scope}_", dir=root)


def kimi_home(root: str, scope: str) -> str:
    """A fresh private `KIMI_CODE_HOME` for one kimi code turn.

    The same idea as `agy_home`, and it covers more: that variable relocates the
    CLI's *entire* state directory, so the user-level AGENTS.md, the skills
    directory, the session store and config.toml are all created empty here and
    deleted with the turn. Nothing in the developer's own `~/.kimi-code` is read
    or written. The model and key are passed per turn in `KIMI_MODEL_*`, so this
    home never has to hold a credential either.
    """
    os.makedirs(root, exist_ok=True)
    home = tempfile.mkdtemp(prefix=f"kimihome_{scope}_", dir=root)
    # Print mode forces the session into `auto` on its own; stating it (plus the
    # blanket allow rule) keeps the home from being silently interactive if it
    # is ever reused by hand, and keeps telemetry off at the config level too.
    with open(os.path.join(home, "config.toml"), "w") as handle:
        handle.write('# written per turn by raw_agents (no_memory.kimi_home)\n'
                     'default_permission_mode = "auto"\n'
                     'default_plan_mode = false\n'
                     'telemetry = false\n\n'
                     '[[permission.rules]]\n'
                     'decision = "allow"\n'
                     'pattern = "*"\n')
    return home
