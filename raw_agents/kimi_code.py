"""Drive the `kimi` CLI (Kimi Code) as a raw-run backend, one turn per call.

The `kimi_k3_raw.backend` module exports these CLI calls to the shared
iterative runner and reuses image/render helpers from the Claude backend.

Why not go through TaskSolver like the claude wrapper does: tasksolver's
`KimiModel` talks to the Moonshot HTTP endpoint directly, which is a different
agent from the CLI-based benchmark. This drives the same Kimi CLI as the
project's shared `KimiAgent`, and it drives it the way the agy wrapper drives
agy: a throwaway workspace per turn, images staged into it, the CLI run with
that workspace as its cwd, everything deleted afterwards.

IMAGES. Kimi Code takes no image argument; like Claude Code it reads them as
files, with its `ReadMediaFile` tool (verified: the tool result comes back as
an inline `image_url` data URI and K3 read a word that existed only in the
pixels). So each turn's PIL views are written into the workspace and the prompt
opens with an absolute-path manifest telling the model to read them first.

MODEL/KEY. `KIMI_MODEL_*` synthesizes one provider plus one model alias in
memory and makes it the default, so the key is never written to the CLI's
config; `k3` is served by Kimi for Coding over the Anthropic Messages API. The
CLI reads no shell variable or `.env` of its own, so the key is looked up here
(`$MOONSHOT_API_KEY`, loaded from the checkout root `.env` when needed).

NO MEMORY (unconditional here, as everywhere in this project -- see no_memory.py):

  * a per-turn `KIMI_CODE_HOME` (`no_memory.kimi_home`), which is the CLI's
    entire state directory -- user AGENTS.md, skills, sessions, config -- so it
    is created empty and deleted with the turn;
  * `--skills-dir` at an empty directory, which replaces user AND project skill
    discovery;
  * `KIMI_DISABLE_CRON=1`, so a turn cannot leave scheduled work behind;
  * the workspace lives under /tmp, not in the repo. That is not only hygiene:
    kimi code walks from its cwd up to the nearest `.git` and reads every
    `AGENTS.md` on the way, with no switch to turn it off, so a turn run from
    inside the repo would be handed the repo's own instructions (verified: it
    quotes them back). Under /tmp the walk finds no repo and stops at the
    workspace.

`~/.agents/AGENTS.md` -- resolved against the real home, not `KIMI_CODE_HOME` --
is the one root left, and there is no way to scope it. It does not exist on this
machine; if it ever does, every kimi turn here would read it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import no_memory
from usage_accounting import collect_kimi_home_usage


# k3's own think_efforts, as the endpoint advertises them in /v1/models. The
# shared runner's --thinking-depth choices are narrowed to these.
THINKING_DEPTHS = ("low", "high", "max")

DEFAULT_MODEL = "k3"
DEFAULT_BASE_URL = "https://api.kimi.com/coding"       # Kimi for Coding
DEFAULT_CONTEXT_WINDOW = 1048576
API_KEY_ENV = "MOONSHOT_API_KEY"
BIN_ENV = "KIMI_BIN"

# The id handed to the CLI, as opposed to the alias that names the output
# folder. `kimi_k3_raw` overrides it from $KIMI_RAW_MODEL; module-level so
# `build_agent` can keep the shared runner's signature.
MODEL_NAME = DEFAULT_MODEL

# Turn workspaces and per-turn CLI homes, next to the agy wrapper's. Outside the
# repo on purpose (see the module docstring).
CLI_WORKSPACE_ROOT = "/tmp/agent_generation"

# Hard cap per CLI turn. A hung turn is raised as a failed attempt, which the
# runner's retry loop handles like any other backend error, rather than stalling
# a batch overnight.
TURN_TIMEOUT_S = int(os.environ.get("KIMI_RAW_TURN_TIMEOUT") or 1800)

# Set by memory_off(), read by build_agent(): where the run records what it
# turned off. Mirrors no_memory.apply_claude, which likewise runs for its side
# effects before the first turn.
_STATE_DIR: Path | None = None


def resolve_kimi_bin(spec: str | None = None) -> str:
    """Resolve `$KIMI_BIN` or `kimi` on PATH to an absolute path.

    Kimi Code is installed in the user's executable prefix rather than the
    project Pixi environment. In `_env`, the resolved launcher's directory is
    kept on PATH while Node.js comes from the activated Pixi environment.

    Resolved once before the first turn so a missing CLI is one clear error at
    startup, rather than an
    ERR_BACKEND per candidate with a bare "No such file or directory: 'kimi'".
    """
    spec = os.path.expanduser(spec or os.environ.get(BIN_ENV) or "kimi")
    path = spec if os.sep in spec else shutil.which(spec)
    if not path or not (os.path.isfile(path) and os.access(path, os.X_OK)):
        raise SystemExit(
            f"kimi binary {spec!r} not found or not executable. Install Kimi "
            "Code as described in INSTALL.md, ensure ~/.local/bin is on PATH, "
            f"or point ${BIN_ENV} at the CLI.")
    return os.path.abspath(path)


def _api_key() -> str:
    """Read the key from exported variables or the checkout's .env."""
    from project_env import PROJECT_ROOT, load_project_env

    load_project_env()
    if key := os.environ.get(API_KEY_ENV):
        return key
    raise RuntimeError(
        f"kimi code: no API key -- set {API_KEY_ENV} in the shell or "
        f"{PROJECT_ROOT / '.env'}")


class KimiCodeCLI:
    """The `interface` the shared runner passes to `call_model`.

    Only `thinking_depth` (set by the runner) and `ask()` are used; it exists to
    hold the per-run settings that a tasksolver `visual_interface` would.
    """

    def __init__(self, model: str, thinking_depth: str = "high"):
        self.model = model
        self.thinking_depth = thinking_depth
        self.bin = resolve_kimi_bin()
        self.base_url = os.environ.get("KIMI_RAW_BASE_URL") or DEFAULT_BASE_URL
        self.context_window = DEFAULT_CONTEXT_WINDOW
        self.api_key = _api_key()
        self.turn = 0

    # -- one turn --------------------------------------------------------
    def ask(self, text: str, pil_images, max_tokens: int) -> tuple[str, dict]:
        """Stage `pil_images`, run one `kimi -p`, return (reply, raw record)."""
        self.turn += 1
        os.makedirs(CLI_WORKSPACE_ROOT, exist_ok=True)
        workspace = tempfile.mkdtemp(prefix=f"kimi_turn{self.turn:04d}_",
                                     dir=CLI_WORKSPACE_ROOT)
        home = no_memory.kimi_home(CLI_WORKSPACE_ROOT, f"turn{self.turn:04d}")
        skills = os.path.join(home, "empty-skills")
        os.makedirs(skills, exist_ok=True)
        try:
            paths = self._stage_images(pil_images, workspace)
            prompt = self._prompt(paths, text)
            argv = [self.bin, "-p", prompt, "--output-format", "stream-json",
                    "--skills-dir", skills]
            try:
                proc = subprocess.run(argv, cwd=workspace, text=True,
                                      capture_output=True,
                                      timeout=TURN_TIMEOUT_S,
                                      env={**os.environ,
                                           **self._env(home, max_tokens)})
            except subprocess.TimeoutExpired as expired:
                raise RuntimeError(
                    f"kimi code turn exceeded {TURN_TIMEOUT_S}s "
                    "($KIMI_RAW_TURN_TIMEOUT); killed"
                ) from expired
            events = list(self._events(proc.stdout))
            reply = self._final_text(events)
            usage = collect_kimi_home_usage(home, self.model)
            raw = {
                "backend": "kimi-code",
                "model": self.model,
                "thinking_depth": self.thinking_depth,
                "returncode": proc.returncode,
                # Recorded per attempt so "which images did this turn actually
                # get, and did it read them?" is answerable from the output
                # alone -- the question that uncovered agy's cross-task image
                # mix-up (see no_memory.py).
                "prompt": prompt,
                "images": [str(p) for p in paths],
                "tool_calls": self._tool_calls(events),
                "text": reply,
                "n_images": len(paths),
                "session_id": self._session_id(events),
                "usage": usage,
            }
            if not reply:
                raise RuntimeError(
                    "kimi code returned no output for "
                    f"model={self.model} (exit {proc.returncode}). Ensure "
                    f"${API_KEY_ENV} is valid and api.kimi.com is reachable. "
                    f"stderr head:\n{(proc.stderr or '')[:500]}")
            return reply, raw
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
            shutil.rmtree(home, ignore_errors=True)

    @staticmethod
    def _stage_images(pil_images, workspace: str) -> list[str]:
        paths = []
        for index, image in enumerate(pil_images, 1):
            path = os.path.join(workspace, f"view{index}.png")
            image.save(path)
            paths.append(path)
        return paths

    @staticmethod
    def _prompt(paths: list[str], text: str) -> str:
        """The image manifest plus the runner's prompt, verbatim below it.

        Assembled against the workspace that is about to run, so a re-issued
        body (render/parse feedback) can never quote a path from an
        already-deleted workspace.
        """
        if not paths:
            return text
        listing = "\n".join(f"  {index}. {path}"
                            for index, path in enumerate(paths, 1))
        manifest = (
            "Before answering, read these image files with your ReadMediaFile "
            "tool, in this order. They are the images the task below refers "
            "to, numbered the same way:\n" + listing)
        return "\n\n".join([manifest, text])

    def _env(self, home: str, max_tokens: int) -> dict[str, str]:
        # Keep the resolved user-level launcher visible to child processes;
        # `node` itself is resolved from the inherited (normally Pixi) PATH.
        bindir = os.path.dirname(self.bin)
        path = os.environ.get("PATH", "")
        if bindir not in path.split(os.pathsep):
            path = bindir + ((os.pathsep + path) if path else "")
        return {
            "PATH": path,
            "KIMI_CODE_HOME": home,
            "KIMI_MODEL_NAME": self.model,
            "KIMI_MODEL_API_KEY": self.api_key,
            "KIMI_MODEL_PROVIDER_TYPE": "anthropic",
            "KIMI_MODEL_BASE_URL": self.base_url,
            "KIMI_MODEL_MAX_CONTEXT_SIZE": str(self.context_window),
            "KIMI_MODEL_MAX_OUTPUT_SIZE": str(max_tokens),
            "KIMI_MODEL_CAPABILITIES": "thinking,tool_use,image_in,video_in",
            "KIMI_MODEL_THINKING_EFFORT": self.thinking_depth,
            "KIMI_DISABLE_CRON": "1",
            "KIMI_DISABLE_TELEMETRY": "1",
            # The pixi package pins the version; a self-upgrade mid-benchmark
            # would swap the agent under the run.
            "KIMI_CODE_NO_AUTO_UPDATE": "1",
        }

    # -- stream-json -----------------------------------------------------
    # One JSON object per line, OpenAI-shaped (`role`: assistant | tool | meta).
    # Tool output is also written to stdout as plain text, so non-JSON lines and
    # objects without a `role` are skipped.
    @staticmethod
    def _events(stdout: str):
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and "role" in event:
                yield event

    @staticmethod
    def _final_text(events) -> str:
        text = ""
        for event in events:
            if event.get("role") == "assistant" and \
                    isinstance(event.get("content"), str):
                text = event["content"]
        return text

    @staticmethod
    def _tool_calls(events) -> list[str]:
        names = []
        for event in events:
            for call in event.get("tool_calls") or []:
                function = (call or {}).get("function") or {}
                name = function.get("name") or call.get("name")
                if name:
                    names.append(str(name))
        return names

    @staticmethod
    def _session_id(events) -> str:
        found = ""
        for event in events:
            candidate = event.get("session_id")
            if isinstance(candidate, str) and candidate:
                found = candidate
        return found


# ------------------------------------------------- shared-runner seams -------

def memory_off(state_dir):
    """`MEMORY_OFF`: record what is disabled, and remember where to write.

    Nothing has to be switched off process-wide the way claude's auto-memory
    does -- every switch here is per turn (see the module docstring) -- so this
    only writes the run's own record of them, which is what the runner prints.
    """
    global _STATE_DIR
    _STATE_DIR = Path(state_dir).resolve()
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    record = _STATE_DIR / "no-memory.kimi.json"
    record.write_text(json.dumps({
        "kimi_code_home": f"{CLI_WORKSPACE_ROOT}/kimihome_turn<N>_* (per turn, "
                          "deleted with the turn)",
        "workspace": f"{CLI_WORKSPACE_ROOT}/kimi_turn<N>_* (per turn, outside "
                     "the repo so no AGENTS.md above it is discovered)",
        "skills_dir": "empty (replaces user and project skill discovery)",
        "env": {"KIMI_DISABLE_CRON": "1", "KIMI_DISABLE_TELEMETRY": "1",
                "KIMI_CODE_NO_AUTO_UPDATE": "1"},
        "not_scoped": "~/.agents/AGENTS.md (no switch exists; absent here)",
    }, indent=2) + "\n")
    return record


class _Agent:
    """Stands in for the tasksolver Agent the shared runner builds."""

    def __init__(self, interface):
        self.visual_interface = interface


def build_agent(vision_model, credentials):
    """`build_agent`: no tasksolver dispatch, just the CLI wrapper.

    `vision_model` is the output-folder alias (`kimi-code-k3`), not a model id;
    the model the CLI is pointed at is `DEFAULT_MODEL`, overridable with
    `--model-name` on the wrapper.
    """
    return _Agent(KimiCodeCLI(MODEL_NAME))


def call_model(interface, text, pil_images, max_tokens, model):
    """`call_model`: one CLI turn. Returns (reply_text, raw_dict)."""
    return interface.ask(text, list(pil_images), max_tokens)
