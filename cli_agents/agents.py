"""CLI adapters for ActiveVisual: codex, qwen code, claude code, kimi code, agy.

Each adapter does the same three things for one task:

  1. register the run's two Blender MCP servers (whose ports change per task)
     in whatever form its CLI reads,
  2. open a NEW session and send it one message: the port instruction with
     prompt.txt appended (the driver composes it; see ActiveVisual.py),
  3. export that session with the CLI's own mechanism -- codex `exec --json`
     event stream + its rollout file, qwen `-o stream-json`, claude
     `--output-format stream-json` + its session transcript, kimi `-p
     --output-format stream-json`, agy's conversation store via
     `Session.history()` -- and normalise the tool calls out of it.

and 4. report what the session cost -- tokens and API requests, read back out
     of the CLI's own accounting (`collect_usage`; see the USAGE section below).

codex, qwen, claude and kimi are one-shot print-mode CLIs, so "one session"
there is one process. agy has no usable print mode for this, so `AgyAgent` holds
a live conversational session (`agy --prompt-interactive`) and types each turn
into it; see that class for the details and for the three things its MCP wiring
needs that no other CLI does.

Raw exports are always kept verbatim; `tool_calls.json` is a convenience view.

RESUME (`run(..., resume=...)`, and `classify` below)
--------------------------------------------------------------------------
A run can end because the account behind the CLI ran out of quota, not because
the task is done. `classify()` recognises that from the CLI's own output
(`LIMIT_PATTERNS`, plus whatever `[resume] extra_limit_patterns` adds) and a
timeout is recognised from the killed process, so the driver can checkpoint
instead of reporting a plain failure -- see `checkpoint.py`.

`resume` (the checkpoint's `session` block) makes the adapter continue THAT
conversation instead of opening a new one. Each CLI has its own mechanism, and
each keeps its own store, which is what makes resuming possible at all:

  * codex -- `codex exec resume <session_id> … -`; rollouts live in the
    task-owned `session/codex_home`, while `auth.json` points at the currently
    selected account. Note `resume` rejects `-C`, so the working directory
    comes from the process cwd (which is the task dir either way).
  * claude -- `--resume <session_id>` in place of `--session-id`; each turn's
    active-login transcript is copied into the task-local `claude_sessions/`
    store, then restored below the next active `CLAUDE_CONFIG_DIR`. This keeps
    one conversation across subscription-account rotation.
  * kimi -- `-S <session_id>`; the session store lives under this run's own
    `KIMI_CODE_HOME`, i.e. inside `session/kimi_home/`, which persists with
    the task dir. The id survives the resume, so the checkpoint keeps
    pointing at one conversation.
  * qwen -- `-r <session_id>`, with `--chat-recording` on every run so the
    transcript exists to resume; it is stored under this run's own `QWEN_HOME`,
    i.e. inside `session/qwen_home/`, which persists with the task dir.
  * agy -- `pyagy.Session(conversation_id=…)`, resolved inside the task's
    private HOME (`session/agy_home/`), where agy's conversation store lives.

The prompt a resumed run receives is the driver's continuation message (new
ports, what was restored), not the task from the top.

MEMORY ISOLATION (`no_memory`, on by default; `--no-memory` / `--memory` on
both drivers, `[agent].no_memory` in their config.toml)
--------------------------------------------------------------------------
Every one of these CLIs ships a cross-session memory of its own, keyed by the
directory it runs in -- so without this a task would answer partly from what
the same CLI learned in an earlier task, or worse from an interactive session
of the same repo, and it would leave notes behind for the next task to read.
With `no_memory` each adapter turns its own mechanism off, for reads AND
writes, and points whatever remains at this run's `session/` directory:

  * claude -- `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` (the hard switch: it
    short-circuits before any settings lookup) plus a run-local settings file
    with `autoMemoryEnabled=false`, `autoDreamEnabled=false`, an
    `autoMemoryDirectory` inside `session/`, and `claudeMdExcludes` so no
    CLAUDE.md above the task dir is loaded either. Note the default store is
    `~/.claude/projects/<sanitized-cwd>/memory/`, i.e. plain cwd scoping, which
    is exactly how a repo-root run would share an interactive session's notes.
  * codex -- task-local `CODEX_HOME` for the rollout store, plus
    `features.memories=false` (the memory tool itself),
    `memories.generate_memories=false` / `memories.use_memories=false` (write
    and read), and `project_doc_max_bytes=0` so no AGENTS.md is read either;
    `--ignore-user-config` was already keeping `$CODEX_HOME/config.toml` out.
  * qwen -- `memory.enableManagedAutoMemory/enableManagedAutoDream/
    enableAutoSkill/enableTeamMemory*=false`, `save_memory` excluded from the
    tool surface, `context.fileName` pointed at a name that never exists (no
    QWEN.md discovery) and `QWEN_CODE_MEMORY_BASE_DIR` inside `session/`.
  * kimi -- a per-task `KIMI_CODE_HOME` (`session/kimi_home/`), which is the
    CLI's entire state directory: user-level AGENTS.md, skills, sessions and
    config all start empty and die with the task. Plus `--skills-dir` at an
    empty directory (it replaces user AND project skill discovery) and
    `KIMI_DISABLE_CRON=1`. See `KimiAgent` for the two AGENTS.md roots this
    CLI offers no switch for.
  * agy -- nothing to add: `AgyAgent` already gives each task a private HOME,
    so its whole `~/.gemini` store (brain, knowledge, conversations) is
    per-task and starts empty.

`no_memory=False` restores each CLI's stock behaviour, which is only useful for
reproducing what a normal interactive run of that CLI would do.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import traceback
import time
import uuid
import urllib.error
import urllib.request
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Entries inside a run's `session/` directory that are a CLI's own persistent
# store rather than this attempt's export. The driver must not archive these
# away when it rotates a finished attempt: they are what `resume` reads.
PERSISTENT_SESSION_ENTRIES = ("codex_home", "qwen_home", "qwen_memory", "agy_home",
                              "claude_memory", "claude_sessions",
                              "kimi_home", "mmx_home")

# "The agent stopped because the account is out of quota, not because the work
# is finished." Matched case-insensitively against every turn's log, its final
# text, and (for the CLIs that write one) the copied transcript. Kept
# deliberately narrow -- a bare "usage limit" also appears in ordinary status
# text, so a hit needs the reached/exceeded/hit wording next to it.
LIMIT_PATTERNS = (
    r"(usage|rate|quota|token)[ _-]?limits?[^\n]{0,40}?(reach|exceed|hit|exhaust)",
    r"(reach|exceed|hit|exhaust)\w*[^\n]{0,40}?(usage|rate|quota|token)[ _-]?limits?",
    r"usage limit reached",
    r"rate[ _-]?limit(ed|_exceeded)?\b",
    r"resource[ _-]?exhausted",
    r"insufficient[ _-]?quota",
    r"quota[ _-]?exceeded",
    r"too many requests",
    r"\b429\b",
    r"upgrade to (a )?(higher|paid) (plan|tier)",
    r"try again (later|in \d)",
)


def usage_limit_evidence(text: str, extra_patterns=()) -> str:
    """Return provider-limit evidence from diagnostic text, if present."""
    patterns = [re.compile(p, re.I) for p in (*LIMIT_PATTERNS, *extra_patterns)]
    return _first_match(patterns, text)


def _usage_limit_retryable(text: str) -> bool:
    """Whether a usage/429/limit signal may resolve after a delay."""
    if not text:
        return False
    # Weekly/plan exhaustion is terminal even when the provider wraps it in a
    # 429 response. Retrying those responses only burns the continuation pool.
    if re.search(r"weekly|1-week|quota.{0,40}exhaust|upgrade to", text, re.I):
        return False
    if re.search(r"\b429\b|request burst|too many requests|request.*retry", text,
                 re.I):
        return True
    # Plan-upgrade prompts are terminal for this code path.
    return False


# --------------------------------------------------------------------------
# MCP server definitions (identical wiring for every CLI, only the port varies)
# --------------------------------------------------------------------------
def mcp_server_specs(repo: Path, full_access_port: int, viewport_only_port: int,
                     full_name: str, viewport_name: str,
                     uv_bin: str | None = None,
                     official_source: Path | None = None) -> dict[str, dict]:
    """Official reconstruction server plus the restricted reference server."""
    uv = uv_bin or shutil.which("uv") or "/usr/local/bin/uv"
    source = official_source or (
        repo / "scripts" / "ActiveVisual" / ".cache" / "blender_mcp-v1.0.0")
    return {
        full_name: {
            "command": uv,
            "args": ["run", "--directory", str(source / "mcp"), "blender-mcp"],
            "env": {"BLENDER_MCP_HOST": "127.0.0.1",
                    "BLENDER_MCP_PORT": str(full_access_port)},
        },
        viewport_name: {
            "command": uv,
            "args": ["run", "--directory", str(repo / "BlenderMCP" / "viewport_only"),
                     "blender-mcp"],
            "env": {"BLENDER_HOST": "127.0.0.1", "BLENDER_PORT": str(viewport_only_port)},
        },
    }


@dataclass
class TurnResult:
    label: str
    returncode: int
    seconds: float
    log: Path | None = None
    text: str = ""
    # The harness killed this turn at [run].timeout_sec. Not a CLI failure, and
    # (like a usage limit) something a later run can pick up from -- so it is
    # recorded rather than raised. Blender is still up at that point, which is
    # what lets the driver save a checkpoint.
    timed_out: bool = False
    # Qwen may serialize a provider failure into a normal-looking result and
    # exit 0. Keep the provider diagnostic separate from the process status.
    api_error: str = ""
    # A failed turn which was successfully continued in the same session is
    # evidence, not the final status of the aggregate run.
    recovered: bool = False


@dataclass
class AgentRun:
    kind: str
    model: str
    turns: list[TurnResult] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    session_ids: list[str] = field(default_factory=list)
    exports: list[str] = field(default_factory=list)
    # What the session cost: tokens and API requests, read out of the CLI's own
    # accounting by `BaseAgent.collect_usage`. See the USAGE section below.
    usage: dict = field(default_factory=dict)
    # Why the run stopped, when it was not "the agent finished". `limit_hit` is
    # set by classify(); `resumed_from` is the session id this run continued.
    limit_hit: bool = False
    limit_reason: str = ""
    limit_evidence: str = ""
    resumed_from: str = ""
    # Set when the live Blender scene is missing its required deliverable, or a
    # bounded Qwen recovery loop exhausts its transient failures/deadline. A
    # zero CLI exit is not completion in those cases, and the session remains
    # resumable.
    completion_error: str = ""

    @property
    def ok(self) -> bool:
        return not self.completion_error and bool(self.turns) and all(
            t.recovered or
            (t.returncode == 0 and not t.timed_out and not t.api_error)
            for t in self.turns
        )

    @property
    def timed_out(self) -> bool:
        return any(t.timed_out and not t.recovered for t in self.turns)

    @property
    def resume_target(self) -> str:
        """The session id a later run should resume. The newest one this run
        learned, else the one it resumed (a CLI that keeps the id across a
        resume reports nothing new)."""
        return self.session_ids[-1] if self.session_ids else self.resumed_from

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "model": self.model,
            "ok": self.ok,
            "session_ids": self.session_ids,
            "exports": self.exports,
            "tool_call_count": len(self.tool_calls),
            "usage": self.usage or None,
            "timed_out": self.timed_out,
            "limit_hit": self.limit_hit,
            "limit_reason": self.limit_reason,
            "limit_evidence": self.limit_evidence[:400],
            "completion_error": self.completion_error,
            "resumed_from": self.resumed_from,
            "turns": [
                {"label": t.label, "returncode": t.returncode,
                 "seconds": round(t.seconds, 1),
                 "timed_out": t.timed_out,
                 "api_error": t.api_error,
                 "recovered": t.recovered,
                 "log": str(t.log) if t.log else None,
                 "final_text": str(t.text)[-4000:]}
                for t in self.turns
            ],
        }


# --------------------------------------------------------------------------
# SESSION USAGE: what the session cost in tokens, and how many API requests it
# took (`AgentRun.usage`, `run.usage` in each harness's run record)
# --------------------------------------------------------------------------
# Every number comes from the CLI's OWN accounting -- nothing here estimates or
# re-tokenises anything, and a field the CLI does not report stays None rather
# than being guessed at.
#
#   scope         "session" -- the whole conversation, INCLUDING what earlier
#                 attempts of a resumed task already spent, because every store
#                 read below grows in place across a resume. "attempt" is used
#                 only where the fallback source is this one invocation.
#   source        the file(s) and field the numbers were read from.
#   api_requests  requests to the model API, i.e. one per assistant response.
#   tokens        normalised so the CLIs are comparable:
#                   input        every prompt token sent, cached ones included
#                   input_cached the part of `input` served from a prompt cache
#                   cache_write  tokens billed for WRITING a cache entry
#                                (Anthropic counts these outside `input`)
#                   output       completion tokens, reasoning included
#                   reasoning    the reasoning part of `output`, where reported
#                   total        input + cache_write + output, unless the CLI
#                                states its own total
#   by_model      the same split per model, where the CLI names one
#   cost_usd      only where the CLI computes money itself (claude code does)
#   rate_limit    the CLI's last quota snapshot -- account-level API usage,
#                 i.e. how much of the plan this session consumed
#   notes         anything a reader needs in order to read the numbers right
#
# Where each CLI keeps it:
#   codex   the rollout's `token_count` events: one per API response, and their
#           `last_token_usage` sums exactly to `total_token_usage` (verified on
#           single- and multi-attempt sessions).
#   claude  the copied transcript, one usage block per `requestId`. Several
#           stream events share a requestId, so the largest `output_tokens` for
#           it is that response's final figure; those sum exactly to what the
#           terminal `result` event reports. `result` also carries claude's own
#           cost and per-model split -- for the LAST attempt only.
#   kimi    the session store's `wire.jsonl`: an `llm.request` per API call and
#           a `usage.record` per completed step.
#   qwen    `usage_record.jsonl`, which qwen writes once per PROCESS with
#           per-model request counts, so a resumed task has one record per
#           attempt and they are summed.
#   agy     nothing. Antigravity records no token counts anywhere in its store
#           (transcript, conversations DB, cli.log all carry only auth tokens),
#           so only its planner-response count is knowable.
USAGE_TOKEN_KEYS = ("input", "input_cached", "cache_write", "output",
                    "reasoning", "total")


def _usage(source: str, *, scope: str = "session", api_requests=None,
           tokens: dict | None = None, by_model: dict | None = None,
           cost_usd=None, rate_limit=None, notes=()) -> dict:
    record = {
        "scope": scope,
        "source": source,
        "api_requests": api_requests,
        "tokens": {key: (tokens or {}).get(key) for key in USAGE_TOKEN_KEYS},
        "notes": [str(n) for n in notes],
    }
    if by_model:
        record["by_model"] = by_model
    if cost_usd is not None:
        record["cost_usd"] = round(float(cost_usd), 6)
    if rate_limit:
        record["rate_limit"] = rate_limit
    return record


def _tokens(*, input_total=None, input_cached=None, cache_write=None,
            output=None, reasoning=None, total=None) -> dict:
    if total is None:
        parts = [v for v in (input_total, cache_write, output) if v is not None]
        total = sum(parts) if parts else None
    return {"input": input_total, "input_cached": input_cached,
            "cache_write": cache_write, "output": output,
            "reasoning": reasoning, "total": total}


def usage_line(usage: dict | None) -> str:
    """The one-line console form both drivers print."""
    if not usage:
        return "usage: not recorded"
    tokens = usage.get("tokens") or {}

    def count(key) -> str:
        value = tokens.get(key)
        return "?" if value is None else f"{value:,}"

    requests = usage.get("api_requests")
    parts = [f"{count('total')} tokens "
             f"(in {count('input')}, cached {count('input_cached')}, "
             f"out {count('output')})",
             f"{'?' if requests is None else requests} API requests"]
    if usage.get("cost_usd") is not None:
        parts.append(f"${usage['cost_usd']:.2f}")
    return f"{usage.get('scope', '?')} usage: " + ", ".join(parts)


def _json_lines(path: Path):
    """Every JSON object in a .jsonl file. Tool output is interleaved with the
    events in some of these logs, so non-JSON lines are skipped."""
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


def _exception_trace(exc: BaseException, max_chars: int = 30000) -> str:
    """Render full traceback text for both plain exceptions and ExceptionGroup."""
    try:
        if isinstance(exc, BaseExceptionGroup):
            parts: list[str] = [f"{type(exc).__name__}: {exc}\n"]
            for index, child in enumerate(exc.exceptions, 1):
                parts.append(f"\n--- exception {index}/{len(exc.exceptions)} ---\n")
                parts.extend(traceback.format_exception(
                    type(child), child, child.__traceback__))
            text = "".join(parts)
        else:
            text = "".join(traceback.format_exception(
                type(exc), exc, exc.__traceback__))
    except Exception:
        text = f"{type(exc).__name__}: {exc}"
    return _clip(text, max_chars)


def _accumulate(totals: dict, values: dict) -> None:
    """Add the numeric fields of `values` into `totals`, in place."""
    for key, value in (values or {}).items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        totals[key] = totals.get(key, 0) + value


class BaseAgent:
    kind = "base"
    THINKING_DEPTHS = ("low", "medium", "high", "xhigh", "max")

    def __init__(self, cfg: dict, repo: Path, timeout: int):
        self.cfg = cfg
        self.repo = repo
        self.timeout = timeout
        self.model = cfg.get("model", "")
        self.thinking_depth = str(cfg.get("thinking_depth", "high")).lower()
        if self.thinking_depth not in self.THINKING_DEPTHS:
            raise SystemExit(
                f"invalid thinking_depth {self.thinking_depth!r} for {self.kind}; "
                f"choose from {self.THINKING_DEPTHS}")
        self.full_name = cfg.get("full_access_server", "official-blender-mcp")
        self.viewport_name = cfg.get("viewport_only_server", "blender-viewport-only")
        # Memory off unless a caller explicitly asks for the CLI's stock
        # behaviour -- see the module docstring for what each adapter disables.
        self.no_memory = bool(cfg.get("no_memory", True))

    # -- helpers ---------------------------------------------------------
    def servers(self, ports: dict[str, int]) -> dict[str, dict]:
        return mcp_server_specs(self.repo, ports["full_access"],
                                ports["viewport_only"], self.full_name,
                                self.viewport_name)

    @staticmethod
    def _stop_process_group(proc: subprocess.Popen,
                            grace: float = 5.0) -> None:
        """Stop a CLI and every MCP/helper process it started."""
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            proc.poll()  # reap the group leader so a zombie cannot hold PGID
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def _spawn(self, argv: list[str], *, cwd: Path, log: Path, label: str,
               stdin_text: str | None = None, env: dict | None = None,
               timeout: int | float | None = None,
               stop_check=None) -> TurnResult:
        """Run one CLI turn, streaming its stdout+stderr into `log`.

        A timeout is returned, not raised: the CLI is killed, but Blender is
        still up and the CLI's session store still holds the conversation, so
        the driver can save a checkpoint and let a later run continue. (Raising
        used to abort the task before even the .blend was saved.)

        ``stop_check`` is an optional non-blocking callback for diagnostics a
        CLI writes outside stdout.  Returning a non-empty string terminates the
        complete process group immediately and records that string as the
        turn's API error.  Qwen needs this because quota failures are written to
        its private chat telemetry while its print-mode process remains alive.
        """
        started = time.monotonic()
        stop_reason = ""
        log.parent.mkdir(parents=True, exist_ok=True)
        turn_timeout = max(1, float(self.timeout if timeout is None else timeout))
        with log.open("w") as sink:
            proc = subprocess.Popen(
                argv, cwd=str(cwd), stdout=sink, stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE if stdin_text is not None else None,
                text=True, env={**os.environ, **(env or {})},
                # The CLI often starts a second Node process plus MCP servers.
                # Owning a process group lets a timeout reap the whole turn.
                start_new_session=True,
            )
            try:
                if stop_check is None:
                    proc.communicate(input=stdin_text, timeout=turn_timeout)
                else:
                    if stdin_text is not None and proc.stdin is not None:
                        proc.stdin.write(stdin_text)
                        proc.stdin.close()
                        proc.stdin = None
                    deadline = time.monotonic() + turn_timeout
                    while proc.poll() is None:
                        try:
                            stop_reason = str(stop_check() or "")
                        except Exception:                     # noqa: BLE001
                            # Monitoring is a fast-exit optimisation. A broken
                            # observer must not replace the normal hard timeout.
                            stop_reason = ""
                        if stop_reason:
                            self._stop_process_group(proc)
                            proc.wait()
                            sink.write(
                                "\n[ActiveVisual] killed process group after "
                                f"provider usage limit: {stop_reason}\n")
                            break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(argv, turn_timeout)
                        try:
                            proc.wait(timeout=min(0.25, remaining))
                        except subprocess.TimeoutExpired:
                            pass
                returncode, timed_out = proc.returncode, False
                # A well-behaved CLI closes its MCP children before exiting.
                # Reap any that survived the wrapper's successful return.
                self._stop_process_group(proc, grace=1.0)
            except subprocess.TimeoutExpired:
                self._stop_process_group(proc)
                try:
                    proc.communicate(timeout=1)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.communicate()
                # 124 is what `timeout(1)` reports, for the same event.
                returncode, timed_out = 124, True
                sink.write(f"\n[ActiveVisual] killed process group after "
                           f"{turn_timeout:g}s ([run].timeout_sec)\n")
            except BaseException:
                self._stop_process_group(proc)
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                raise
        return TurnResult(label=label, returncode=returncode,
                          seconds=time.monotonic() - started, log=log,
                          timed_out=timed_out, api_error=stop_reason)

    def run(self, prompt: str, ports: dict[str, int], workdir: Path,
            session_dir: Path, resume: dict | None = None) -> AgentRun:
        """`prompt` is the composed input: port instruction + task prompt (or,
        when `resume` is given, the driver's continuation message).

        `resume` is a checkpoint's `session` block -- see `checkpoint.py`. Its
        `resume_target` is the session id to continue instead of opening a new
        one.
        """
        raise NotImplementedError

    # -- usage accounting -------------------------------------------------
    @staticmethod
    def _session_keys(run: AgentRun) -> list[str]:
        """The session ids whose usage belongs to THIS run: the one it opened,
        plus the one it resumed (a CLI that keeps the id across a resume reports
        only one).

        This is what keeps the accounting honest when a task has been run more
        than once: every CLI store below lives in (or is copied into) the task's
        `session/` directory and is named after its session id, so a re-run of
        the same task leaves the earlier, ABANDONED conversation's records right
        next to this one's. Counting those would attribute someone else's tokens
        to this run -- while a resumed attempt, which is the same conversation
        growing, must still be counted in full.
        """
        ids = list(run.session_ids) + ([run.resumed_from] if run.resumed_from else [])
        return [str(i) for i in dict.fromkeys(ids) if i]

    @classmethod
    def _own_files(cls, paths: list[Path], run: AgentRun) -> tuple[list[Path], list[Path]]:
        """(this run's session files, other conversations' files). Both are
        returned so the caller can say what it left out. With no session id
        known, nothing can be attributed and every file is treated as ours."""
        keys = cls._session_keys(run)
        if not keys:
            return list(paths), []
        mine = [p for p in paths if any(key in str(p) for key in keys)]
        return mine, [p for p in paths if p not in mine]

    @staticmethod
    def _foreign_note(other: list[Path], what: str, session_dir: Path) -> list[str]:
        """"...and here is what was NOT counted." A skipped file is not an error
        -- it is another conversation's record -- but silently dropping it would
        leave a total nobody can reconcile against the directory."""
        if not other:
            return []
        # Named relative to session/, which for kimi is what identifies the
        # conversation (every one of its files is called `wire.jsonl`) and for
        # codex/claude is just the file name.
        names = []
        for path in other:
            try:
                names.append(str(path.relative_to(session_dir)))
            except ValueError:
                names.append(path.name)
        return [f"excludes {len(other)} {what} from other conversation(s) in "
                f"this task dir (earlier run(s) of the same task): "
                f"{', '.join(names[:4])}{' ...' if len(other) > 4 else ''}"]

    def collect_usage(self, session_dir: Path, run: AgentRun) -> dict:
        """Tokens and API requests for the session `run` just finished, read out
        of the CLI's own records. See the USAGE section above for the shape."""
        return _usage("", notes=[f"{self.kind} reports no usage accounting"])

    def _record_usage(self, run: AgentRun, session_dir: Path) -> AgentRun:
        """Fill `run.usage`. Every adapter's run() ends with this. Accounting is
        a report about the run, so a failure to read it is recorded in the
        record itself and never fails the run."""
        try:
            run.usage = self.collect_usage(session_dir, run)
        except Exception as exc:                              # noqa: BLE001
            run.usage = _usage("", notes=[f"usage accounting failed: "
                                          f"{type(exc).__name__}: {exc}"])
        return run

    def resume_state(self, run: AgentRun) -> dict:
        """What a checkpoint has to record for a later run to continue `run`."""
        return {"kind": self.kind, "model": self.model,
                "resume_target": run.resume_target,
                "session_ids": list(run.session_ids)}

    @classmethod
    def _is_api_timeout(cls, text: str) -> bool:
        """Whether a provider diagnostic means the connection timed out.

        QwenAgent overrides this with provider-specific patterns; other agents
        rely on the harness's process-level timeout detection by default.
        """
        return False

    @classmethod
    def _recoverable_api_error(cls, text: str) -> bool:
        """Whether another request in the same chat may make progress.

        QwenAgent overrides this to retry transient provider errors.  Other
        agents default to False: their adapters already surface hard failures
        as non-zero exits, and timeout recovery is handled separately.
        """
        return False


# --------------------------------------------------------------------------
# codex (default: gpt-5.6-sol)
# --------------------------------------------------------------------------
class CodexAgent(BaseAgent):
    kind = "codex"

    @staticmethod
    def _active_home() -> Path:
        """The current login's Codex state root (credentials, not session)."""
        return Path(
            os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
        ).expanduser().resolve()

    @staticmethod
    def _stored_rollouts(home: Path, session_id: str) -> list[Path]:
        root = home / "sessions"
        if not root.is_dir() or not session_id:
            return []
        return [
            path for path in root.rglob(f"*{session_id}*.jsonl")
            if path.is_file() and path.stat().st_size > 0
        ]

    @classmethod
    def _prepare_home(cls, session_dir: Path,
                      resume_id: str = "") -> Path:
        """Use a task-owned session store with the currently selected login.

        ``CODEX_HOME`` normally combines credentials and rollouts. Account
        rotation therefore used to make ``codex exec resume`` look in the new
        account's empty session tree. The task home below owns ``sessions/``;
        only ``auth.json`` is linked from the active account on each attempt.
        """
        active = cls._active_home()
        home = (session_dir / "codex_home").resolve()
        home.mkdir(parents=True, exist_ok=True)

        if active != home:
            source_auth = active / "auth.json"
            task_auth = home / "auth.json"
            if task_auth.is_symlink() or task_auth.exists():
                task_auth.unlink()
            if source_auth.is_file():
                task_auth.symlink_to(source_auth)
            elif not (os.environ.get("OPENAI_API_KEY") or
                      os.environ.get("CODEX_API_KEY")):
                raise RuntimeError(
                    f"Codex login not found in active account {active}; "
                    "refusing to reuse credentials from a previous account")

        if not resume_id or cls._stored_rollouts(home, resume_id):
            return home

        # Backward compatibility for checkpoints written before codex_home was
        # task-owned: their copied rollout may already have been rotated into
        # attempts/, or may still live under the account that made the turn.
        task_dir = session_dir.parent
        candidates = [
            path for path in task_dir.rglob(f"*{resume_id}*.jsonl")
            if path.is_file() and path.stat().st_size > 0
            and (path.name.startswith("rollout-") or
                 path.name.startswith("codex.rollout."))
        ]
        candidates.extend(cls._stored_rollouts(active, resume_id))
        if os.environ.get("CODEX_HOME"):
            for sibling in active.parent.iterdir():
                if sibling == active or not (sibling / "auth.json").is_file():
                    continue
                candidates.extend(cls._stored_rollouts(sibling, resume_id))
        if not candidates:
            raise RuntimeError(
                f"cannot resume Codex session {resume_id}: its task-owned "
                f"rollout is missing from {home / 'sessions'}")

        source = max(
            candidates,
            key=lambda path: (path.stat().st_size, path.stat().st_mtime_ns),
        )
        filename = source.name.removeprefix("codex.rollout.")
        destination = home / "sessions" / "imported" / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return home

    @staticmethod
    def _toml_value(value) -> str:
        if isinstance(value, str):
            return json.dumps(value)          # JSON strings are valid TOML strings
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, list):
            return "[" + ", ".join(CodexAgent._toml_value(v) for v in value) + "]"
        if isinstance(value, dict):
            return "{" + ", ".join(f"{k} = {CodexAgent._toml_value(v)}"
                                   for k, v in value.items()) + "}"
        raise TypeError(f"cannot render {type(value)!r} as TOML")

    def _config_flags(self, ports: dict[str, int]) -> list[str]:
        # One -c per server; the value is an inline TOML table. Server names use
        # underscores so the dotted key needs no quoting.
        flags: list[str] = []
        for name, spec in self.servers(ports).items():
            flags += ["-c", f"mcp_servers.{name}={self._toml_value(spec)}"]
        return flags

    def _memory_flags(self) -> list[str]:
        """Codex's memories feature: the tool, its writes and its reads, plus
        AGENTS.md. `features.memories` defaults to off in this build, but a
        `[features]` table could turn it on, and it is on for some channels --
        so state all four rather than relying on the default."""
        if not self.no_memory:
            return []
        return ["-c", "features.memories=false",
                "-c", "memories.generate_memories=false",
                "-c", "memories.use_memories=false",
                "-c", "project_doc_max_bytes=0"]

    def run(self, prompt, ports, workdir, session_dir, resume=None) -> AgentRun:
        run = AgentRun(kind=self.kind, model=self.model)
        resume_id = (resume or {}).get("resume_target") or ""
        home = self._prepare_home(session_dir, resume_id)
        if resume_id:
            # `codex exec resume` has no -C: the session is found by id, and the
            # working directory is this process's cwd (the task dir) anyway.
            argv = ["codex", "exec", "resume"]
        else:
            argv = ["codex", "exec", "-C", str(workdir)]
        argv += ["--json", "--ignore-user-config", "--skip-git-repo-check",
                 "--dangerously-bypass-approvals-and-sandbox"]
        if self.model:
            argv += ["-m", self.model]
        argv += ["-c", f'model_reasoning_effort="{self.thinking_depth}"']
        argv += self._memory_flags()
        argv += self._config_flags(ports)
        if resume_id:
            argv.append(resume_id)
            run.resumed_from = resume_id

        # One session, one message. It goes through stdin ("-") so neither
        # quoting nor argv length limits apply -- the composed input is the port
        # instruction plus the whole task prompt.
        turn = self._spawn(argv + ["-"], cwd=workdir, label="prompt",
                           log=session_dir / "codex.session.jsonl",
                           stdin_text=prompt,
                           env={"CODEX_HOME": str(home)})
        run.turns.append(turn)
        session_id = self._session_id(turn.log)
        if session_id:
            run.session_ids.append(session_id)

        for turn in run.turns:
            run.tool_calls += self._tool_calls(turn)
            turn.text = self._final_text(turn.log)
            run.exports.append(str(turn.log))
        # A resumed session may report no new id (codex continues the same
        # thread), so copy the rollout for the resumed one too.
        wanted = list(dict.fromkeys(run.session_ids +
                                    ([resume_id] if resume_id else [])))
        run.exports += [str(p) for p in self._copy_rollouts(
            wanted, session_dir, home)]
        return self._record_usage(run, session_dir)

    # -- usage ------------------------------------------------------------
    def collect_usage(self, session_dir: Path, run: AgentRun) -> dict:
        """codex emits a `token_count` event per API response, carrying both that
        response's usage (`last_token_usage`) and the thread's running total
        (`total_token_usage`). A resumed thread appends to the same rollout and
        the running total continues, so its LAST value is the session total; it is
        summed across files only for the case where a thread ever lands in a
        second rollout, since each file carries its own accumulator.

        The running total is used rather than the sum of the per-response figures,
        because the two can disagree: a response codex does not count (a retry, a
        duplicated event) still arrives with a `last_token_usage`, and summing
        those double-counts it. Observed once in 6 codex sessions -- one event
        reporting 30,489 input tokens while the running total did not move (1.1%
        of that session). `total_token_usage` is codex's own accounting, the same
        number `turn.completed` reports, so it wins -- and the difference is put
        in `notes` rather than silently dropped, because those tokens were
        probably still sent."""
        totals: dict = {}
        per_response: dict = {}
        requests, rate_limit = 0, None
        rollouts, other = self._own_files(
            sorted(session_dir.glob("codex.rollout.*.jsonl")), run)
        for path in rollouts:
            running: dict = {}
            for event in _json_lines(path):
                payload = event.get("payload") or {}
                if payload.get("type") != "token_count":
                    continue
                info = payload.get("info") or {}
                last = info.get("last_token_usage") or {}
                if not last:
                    continue
                requests += 1
                _accumulate(per_response, last)
                running = info.get("total_token_usage") or running
                rate_limit = payload.get("rate_limits") or rate_limit
            _accumulate(totals, running)
        if requests:
            uncounted = (per_response.get("total_tokens", 0)
                         - totals.get("total_tokens", 0))
            notes = ["codex counts cached prompt tokens inside input_tokens, "
                     "and reasoning tokens inside output_tokens"]
            if uncounted:
                notes.append(
                    f"codex's running total excludes {uncounted:,} token(s) that "
                    f"arrived with a response it did not count (a retry or a "
                    f"repeated event); the per-response figures sum to "
                    f"{per_response.get('total_tokens', 0):,}")
            return _usage(
                f"{', '.join(p.name for p in rollouts)} "
                f"(token_count.total_token_usage)",
                api_requests=requests,
                tokens=_tokens(input_total=totals.get("input_tokens"),
                               input_cached=totals.get("cached_input_tokens"),
                               cache_write=totals.get("cache_write_input_tokens"),
                               output=totals.get("output_tokens"),
                               reasoning=totals.get("reasoning_output_tokens"),
                               total=totals.get("total_tokens")),
                by_model={self.model: {"api_requests": requests}} if self.model else None,
                rate_limit=rate_limit,
                notes=notes
                + self._foreign_note(other, "rollout(s)", session_dir))
        # No rollout to read (it is copied out of ~/.codex after the turn, so a
        # missing one means the copy found nothing): the exec stream's own
        # `turn.completed` carries the same running total for the thread, but
        # nothing per response.
        totals = {}
        for turn in run.turns:
            for event in self._events(turn.log):
                if event.get("type") == "turn.completed":
                    totals = event.get("usage") or totals
        if not totals:
            return _usage("", notes=["no codex rollout and no turn.completed "
                                     "event: nothing reported the usage"]
                          + self._foreign_note(other, "rollout(s)", session_dir))
        return _usage("codex.session.jsonl (turn.completed.usage)",
                      tokens=_tokens(
                          input_total=totals.get("input_tokens"),
                          input_cached=totals.get("cached_input_tokens"),
                          cache_write=totals.get("cache_write_input_tokens"),
                          output=totals.get("output_tokens"),
                          reasoning=totals.get("reasoning_output_tokens")),
                      notes=["this run's own rollout was not available, so the "
                             "request count is unknown"]
                      + self._foreign_note(other, "rollout(s)", session_dir))

    # -- codex event-stream parsing --------------------------------------
    @staticmethod
    def _events(log: Path | None):
        if not log or not log.exists():
            return
        for line in log.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue

    @classmethod
    def _session_id(cls, log: Path | None) -> str | None:
        for event in cls._events(log):
            for key in ("session_id", "thread_id", "conversation_id", "id"):
                found = _deep_get(event, key)
                if isinstance(found, str) and len(found) >= 32:
                    return found
        return None

    def _tool_calls(self, turn: TurnResult) -> list[dict]:
        calls = []
        for event in self._events(turn.log):
            for node in _walk_dicts(event):
                kind = str(node.get("type", ""))
                name = node.get("tool") or node.get("name")
                if not name or "call" not in kind:
                    continue
                if kind.endswith("_call_output") or kind.endswith("_end"):
                    continue
                calls.append(_normalise_call(
                    turn.label, node.get("server"), str(name),
                    node.get("arguments") or node.get("input") or node.get("args"),
                    kind))
        return _dedupe(calls)

    @classmethod
    def _final_text(cls, log: Path | None) -> str:
        text = ""
        for event in cls._events(log):
            for node in _walk_dicts(event):
                if node.get("type") in ("agent_message", "assistant_message") and \
                        isinstance(node.get("message"), str):
                    text = node["message"]
                elif node.get("type") == "item.completed":
                    item = node.get("item") or {}
                    if item.get("type") in ("assistant_message", "agent_message"):
                        text = item.get("text") or item.get("message") or text
        return text

    @classmethod
    def _copy_rollouts(cls, session_ids: list[str], session_dir: Path,
                       home: Path | None = None) -> list[Path]:
        """Copy task-owned Codex rollouts into the attempt's evidence files."""
        copied: list[Path] = []
        root = (home or cls._active_home()) / "sessions"
        if not root.is_dir():
            return copied
        for sid in session_ids:
            for src in root.rglob(f"*{sid}*.jsonl"):
                dst = session_dir / f"codex.rollout.{src.name}"
                shutil.copy2(src, dst)
                copied.append(dst)
        return copied


# --------------------------------------------------------------------------
# qwen code -- Model Studio token-plan backend.
# --------------------------------------------------------------------------
class QwenAgent(BaseAgent):
    kind = "qwen"

    # Qwen Code defaults to a 120s request timeout and a 240s stream-idle
    # timeout.  Long, tool-heavy Blender sessions routinely need more time to
    # emit their final reconstruction script, so use a larger bounded default
    # and make the provider SDK retry transient failures explicitly.
    DEFAULT_REQUEST_TIMEOUT_MS = 10 * 60 * 1000
    DEFAULT_STREAM_IDLE_TIMEOUT_MS = 10 * 60 * 1000
    DEFAULT_REQUEST_MAX_RETRIES = 5
    API_ERROR_RE = re.compile(r"\[API Error:\s*([^\]\r\n]+)", re.I)
    # Qwen emits both provider request timeouts and stream-idle timeouts.  The
    # latter is the form seen when a long Blender turn stops producing tokens:
    # ``No stream activity for 600000ms``.
    API_TIMEOUT_RE = re.compile(
        r"(?:Request timeout after\s+\d+(?:\.\d+)?s\b|"
        r"No stream activity for\s+\d+(?:\.\d+)?ms\b|"
        r"\b(?:request|connection|stream)\s+(?:timed?\s*out|timeout)\b)",
        re.I)
    INVALID_URL_RE = re.compile(
        r"provided URL does not appear to be valid", re.I)
    PERMANENT_API_ERROR_RE = re.compile(
        r"\b(400|401|403)\b|invalid[_ -]?(request|api key)|"
        r"authentication|unauthorized|forbidden|context length|"
        r"maximum context|content policy", re.I)

    @staticmethod
    def _chat_usage_limit_monitor(home: Path):
        """Watch only newly appended Qwen API-error telemetry for a quota hit.

        Qwen Code writes provider failures to
        ``qwen_home/projects/*/chats/*.jsonl`` but can leave print mode waiting
        indefinitely after a terminal 429.  Existing bytes are deliberately
        skipped so an old checkpoint's quota event cannot terminate a later
        attempt after the account has recovered.
        """
        offsets = {
            path: path.stat().st_size
            for path in home.glob("projects/*/chats/*.jsonl")
            if path.is_file()
        }

        def check() -> str:
            for path in home.glob("projects/*/chats/*.jsonl"):
                try:
                    size = path.stat().st_size
                    offset = offsets.get(path, 0)
                    if size < offset:
                        offset = 0
                    with path.open("rb") as handle:
                        handle.seek(offset)
                        while True:
                            line_start = handle.tell()
                            line = handle.readline()
                            if not line:
                                offsets[path] = handle.tell()
                                break
                            # Do not consume a partially flushed JSON line.
                            if not line.endswith(b"\n"):
                                offsets[path] = line_start
                                break
                            offsets[path] = handle.tell()
                            try:
                                event = json.loads(line)
                            except (UnicodeError, ValueError):
                                continue
                            ui_event = ((event.get("systemPayload") or {}).get(
                                "uiEvent") or {}) if isinstance(event, dict) else {}
                            if ui_event.get("event.name") != "qwen-code.api_error":
                                continue
                            message = str(ui_event.get("error_message") or "")
                            if usage_limit_evidence(message):
                                return message.strip()
                except OSError:
                    continue
            return ""

        return check

    def _write_home(self, ports: dict[str, int], session_dir: Path) -> Path:
        """A throwaway QWEN_HOME so the run cannot inherit ~/.qwen's ports/model."""
        home = session_dir / "qwen_home"
        home.mkdir(parents=True, exist_ok=True)
        servers = {name: {**spec, "trust": True, "timeout": 60000}
                   for name, spec in self.servers(ports).items()}
        provider = str(self.cfg.get("provider", "openai")).strip().lower()
        image_inputs = bool(self.cfg.get("image_inputs", True))
        if provider not in ("openai", "anthropic"):
            raise ValueError(
                f"unsupported Qwen provider protocol {provider!r}; expected "
                "'openai' or 'anthropic'")
        model_settings = {"name": self.model}
        # Some Anthropic-compatible gateways translate Qwen Code's manual
        # thinking budget back into `thinking_budget`. Sending the global
        # reasoning effort at the same time then produces two mutually
        # exclusive wire parameters. Let such gateways use Qwen Code's budget
        # ladder alone.
        if bool(self.cfg.get("send_reasoning_effort", True)):
            model_settings["reasoningEffort"] = self.thinking_depth
        image_payload_threshold = self.cfg.get("image_payload_threshold")
        max_recent_images = self.cfg.get("max_recent_images_to_retain")
        if image_payload_threshold is not None or max_recent_images is not None:
            # Qwen Code sends an MCP inline image once as the current tool
            # result.  On later turns it would normally replay that same large
            # data URL in every request.  Some OpenAI-compatible endpoints
            # intermittently reject those old, repeated image URLs.  Qwen's
            # built-in history compactor can retain the textual image marker
            # while keeping old base64 payloads off the wire.
            model_settings["chatCompression"] = {
                "imagePayloadThreshold": int(
                    image_payload_threshold if image_payload_threshold is not None
                    else 1),
                "maxRecentImagesToRetain": int(
                    max_recent_images if max_recent_images is not None else 0),
            }
        generation_config = {
            "contextWindowSize": int(self.cfg.get("context_window", 262144)),
            "modalities": {"image": image_inputs},
            "timeout": int(self.cfg.get(
                "request_timeout_ms", self.DEFAULT_REQUEST_TIMEOUT_MS)),
            "maxRetries": int(self.cfg.get(
                "request_max_retries", self.DEFAULT_REQUEST_MAX_RETRIES)),
        }
        if bool(self.cfg.get("disable_reasoning_controls", False)):
            # This disables Qwen Code's request-side effort/budget fields, not
            # the model's own default thinking behavior.
            generation_config["reasoning"] = False
        settings = {
            "security": {"auth": {"selectedType": provider}},
            "model": model_settings,
            "modelProviders": {provider: [{
                "id": self.model,
                "baseUrl": self.cfg.get("base_url", ""),
                "envKey": self.cfg.get("api_key_env", "VLLM_API_KEY"),
                "capabilities": {"vision": image_inputs},
                "generationConfig": generation_config,
            }]},
            "privacy": {"usageStatisticsEnabled": False},
            # MCP tools must be visible without a tool_search round trip.
            "tools": {"toolSearch": {"enabled": False}},
            "mcpServers": servers,
        }
        if self.no_memory:
            # Managed auto-memory (extraction + dream consolidation), the
            # skill/team tiers built on it, and the save_memory tool. The
            # context filename is set to something that never exists so no
            # QWEN.md above the task dir is discovered either.
            settings["memory"] = {"enableManagedAutoMemory": False,
                                  "enableManagedAutoDream": False,
                                  "enableAutoSkill": False,
                                  "enableTeamMemory": False,
                                  "enableTeamMemorySync": False}
            settings["context"] = {"fileName": ["__ActiveVisual_no_context__.md"],
                                   "loadFromIncludeDirectories": False}
            settings["tools"]["exclude"] = ["save_memory"]
        (home / "settings.json").write_text(json.dumps(settings, indent=2))
        return home

    def run(self, prompt, ports, workdir, session_dir, resume=None) -> AgentRun:
        run = AgentRun(kind=self.kind, model=self.model)
        home = self._write_home(ports, session_dir)
        env = {
            "QWEN_HOME": str(home),
            # Qwen Code treats a silent stream separately from the provider
            # request deadline.  Raise both limits together so an otherwise
            # healthy long generation is not aborted by the smaller one.
            "QWEN_STREAM_IDLE_TIMEOUT_MS": str(int(self.cfg.get(
                "stream_idle_timeout_ms",
                self.DEFAULT_STREAM_IDLE_TIMEOUT_MS))),
        }
        if self.no_memory:
            # Belt and braces: even if something still wants a memory root, it
            # lands in this run's session dir instead of ~/.qwen.
            env["QWEN_CODE_MEMORY_BASE_DIR"] = str(session_dir / "qwen_memory")
        # --chat-recording is what makes -r/--resume possible at all ("if false,
        # chat history is not saved and --continue/--resume will not work"). The
        # transcript lands under this run's own QWEN_HOME, so it is per-task and
        # is not the cross-session memory `no_memory` disables.
        argv = ["qwen", "-o", "stream-json", "--chat-recording"]
        resume_id = (resume or {}).get("resume_target") or ""
        if resume_id:
            chats = list((home / "projects").glob(
                f"*/chats/{resume_id}.jsonl"))
            if not any(path.is_file() and path.stat().st_size > 0
                       for path in chats):
                raise RuntimeError(
                    f"cannot resume Qwen session {resume_id}: task-owned chat "
                    f"is missing from {home / 'projects'}")
            argv += ["-r", resume_id]
            run.resumed_from = resume_id
        continuation = int((resume or {}).get("_continuation", 0))
        label = f"continue-{continuation}" if continuation else "prompt"
        log_name = (f"qwen.continue-{continuation}.session.jsonl"
                    if continuation else "qwen.session.jsonl")
        turn = self._spawn(
            argv + ["-p", prompt], cwd=workdir, env=env, label=label,
            log=session_dir / log_name,
            timeout=(resume or {}).get("_timeout_sec"),
            stop_check=self._chat_usage_limit_monitor(home))
        run.turns.append(turn)
        session_id = self._session_id(turn.log)
        if session_id:
            run.session_ids.append(session_id)

        for turn in run.turns:
            run.tool_calls += self._tool_calls(turn)
            turn.text = self._final_text(turn.log)
            # Qwen Code 0.21 can serialize a provider timeout into a successful
            # `result` event and exit 0.  Preserve that diagnostic, but do not
            # let the harness mark an exhausted retry sequence as successful.
            diagnostic = turn.api_error or self._api_error(
                f"{turn.text}\n{_tail(turn.log)}")
            if diagnostic:
                turn.api_error = diagnostic
                if self._is_api_timeout(diagnostic):
                    turn.timed_out = True
            run.exports.append(str(turn.log))
        # qwen's own session store lives under QWEN_HOME, which is inside
        # session_dir already -- nothing to copy.
        return self._record_usage(run, session_dir)

    # -- usage ------------------------------------------------------------
    def collect_usage(self, session_dir: Path, run: AgentRun) -> dict:
        """qwen appends one `usage_record.jsonl` line per PROCESS when it shuts
        down (killed at [run].timeout_sec included), holding per-model request
        counts and token totals for that process. Each line names its
        `sessionId`, so this run's lines are its own session's -- a resumed
        attempt and a same-session continuation each add one, and an earlier,
        abandoned run of the same task is left out (QWEN_HOME is per TASK, not
        per session)."""
        home = session_dir / "qwen_home"
        records: list[dict] = []
        seen: set = set()
        keys = self._session_keys(run)
        foreign = 0
        for path in sorted(home.rglob("usage_record.jsonl")):
            for record in _json_lines(path):
                if keys and str(record.get("sessionId") or "") not in keys:
                    foreign += 1
                    continue
                # One line per process, but the same line can be re-read if the
                # file is ever copied alongside itself.
                key = (record.get("sessionId"), record.get("startTime"),
                       record.get("timestamp"))
                if key in seen:
                    continue
                seen.add(key)
                records.append(record)
        foreign_note = ([f"excludes {foreign} usage record(s) belonging to other "
                         f"session(s) in this task dir (earlier run(s) of the "
                         f"same task)"] if foreign else [])
        if records:
            totals: dict = {}
            by_model: dict = {}
            requests = 0
            for record in records:
                for name, model in (record.get("models") or {}).items():
                    _accumulate(totals, model)
                    _accumulate(by_model.setdefault(name, {}), model)
                    requests += int(model.get("requests") or 0)
            for name, model in by_model.items():
                by_model[name] = {"api_requests": model.get("requests"),
                                  "input": model.get("inputTokens"),
                                  "input_cached": model.get("cachedTokens"),
                                  "output": model.get("outputTokens"),
                                  "reasoning": model.get("thoughtsTokens"),
                                  "total": model.get("totalTokens")}
            return _usage(f"qwen_home/usage_record.jsonl "
                          f"({len(records)} process record(s))",
                          api_requests=requests,
                          tokens=_tokens(
                              input_total=totals.get("inputTokens"),
                              input_cached=totals.get("cachedTokens"),
                              output=totals.get("outputTokens"),
                              reasoning=totals.get("thoughtsTokens"),
                              total=totals.get("totalTokens")),
                          by_model=by_model,
                          notes=["cached prompt tokens are counted inside "
                                 "input; qwen's own total is prompt + "
                                 "completion + thoughts"] + foreign_note)
        # The record is written at shutdown; if the process died before that, the
        # stream's terminal `result` event still holds this attempt's totals.
        result = _anthropic_result_event(run)
        totals = (result or {}).get("usage") or {}
        if not totals:
            return _usage("", notes=["qwen wrote no usage_record.jsonl for this "
                                     "session and no result event: nothing "
                                     "reported the usage"] + foreign_note)
        return _usage("qwen.session.jsonl (result.usage)", scope="attempt",
                      tokens=_tokens(
                          input_total=totals.get("input_tokens"),
                          input_cached=totals.get("cache_read_input_tokens"),
                          output=totals.get("output_tokens"),
                          total=totals.get("total_tokens")),
                      notes=["this session's usage_record.jsonl line was missing, "
                             "so this covers this attempt only and has no "
                             "request count"] + foreign_note)

    @classmethod
    def _is_api_timeout(cls, text: str) -> bool:
        return bool(cls.API_TIMEOUT_RE.search(text or ""))

    @classmethod
    def _api_error(cls, text: str) -> str:
        found = cls.API_ERROR_RE.search(text or "")
        return found.group(1).strip() if found else ""

    @classmethod
    def _recoverable_api_error(cls, text: str) -> bool:
        """Whether another request in the same chat may make progress.

        Qwen's provider SDK has already performed its HTTP-level retries by
        this point. Authentication, malformed requests and context exhaustion
        need intervention; timeout/network/429/5xx failures are resumable.
        Unknown provider errors are treated as transient, because the bounded
        continuation count and task deadline still prevent an infinite loop.
        """
        # Model Studio has occasionally rejected a replayed, otherwise valid
        # MCP image data URL after accepting the same conversation for several
        # turns.  A continuation is useful here: with image-history compaction
        # enabled, the resumed request no longer contains the stale payload.
        if cls.INVALID_URL_RE.search(text or ""):
            return True
        return bool(text) and not cls.PERMANENT_API_ERROR_RE.search(text)

    def _tool_calls(self, turn: TurnResult) -> list[dict]:
        return _anthropic_tool_calls(turn)

    @staticmethod
    def _final_text(log: Path | None) -> str:
        return _anthropic_final_text(log)

    @staticmethod
    def _session_id(log: Path | None) -> str:
        """qwen stamps every stream-json event with its session id; the `system`
        /`init` event is the first. That id names the chat file under
        `$QWEN_HOME/projects/<cwd slug>/chats/`, which is what `-r` resumes."""
        for line in (log.read_text(errors="replace").splitlines()
                     if log and log.exists() else []):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            found = event.get("session_id")
            if isinstance(found, str) and found:
                return found
        return ""


class QwenTokenPlanAgent(QwenAgent):
    """The same qwen code CLI, pointed at Alibaba Model Studio's token-plan
    OpenAI-compatible endpoint (ap-southeast-1) instead of the local vLLM.

    The adapter uses the endpoint, model and credential variable configured in
    `[agent.qwen-tokenplan]`. Results use the public
    `qwen3-8-max-preview` directory name.

    The key comes from the `api_key_env` configured for this backend
    (`QWEN_API_KEY` in the full-MCP runner). qwen
    code resolves `envKey` against its own process environment and additionally
    loads `.env` from the working directory upward, so the repo-root `.env`
    covers a shell that has not exported it. Note the wire model id is
    `qwen3.8-max-preview` (dotted); the dashed spelling 404s.
    """

    kind = "qwen-tokenplan"


class _AnthropicStreamAccumulator:
    """Rebuild one Messages response while its SSE events are forwarded.

    mmx's streaming renderer currently retains only ``text_delta`` events.  In
    particular it drops ``tool_use`` blocks and their ``input_json_delta``
    payloads, which this adapter needs for its MCP loop.  The proxy therefore
    keeps an authoritative response beside the byte-for-byte downstream SSE.
    """

    def __init__(self):
        self.message: dict = {}
        self.blocks: dict[int, dict] = {}
        self.partial_json: dict[int, str] = {}
        self.error: dict | None = None

    def feed(self, data: str) -> None:
        if not data or data == "[DONE]":
            return
        event = json.loads(data)
        event_type = event.get("type")
        if event_type == "error":
            self.error = event
            return
        if event_type == "message_start":
            self.message = dict(event.get("message") or {})
            content = self.message.pop("content", []) or []
            self.blocks = {
                index: dict(block) for index, block in enumerate(content)
                if isinstance(block, dict)
            }
            return
        if event_type == "content_block_start":
            index = int(event.get("index", len(self.blocks)))
            self.blocks[index] = dict(event.get("content_block") or {})
            return
        if event_type == "content_block_delta":
            index = int(event.get("index", 0))
            block = self.blocks.setdefault(index, {})
            delta = event.get("delta") or {}
            delta_type = delta.get("type")
            string_fields = {
                "text_delta": ("text", "text"),
                "thinking_delta": ("thinking", "thinking"),
                "signature_delta": ("signature", "signature"),
            }
            if delta_type == "input_json_delta":
                self.partial_json[index] = (
                    self.partial_json.get(index, "") +
                    str(delta.get("partial_json") or "")
                )
            elif delta_type in string_fields:
                source, target = string_fields[delta_type]
                block[target] = (str(block.get(target) or "") +
                                 str(delta.get(source) or ""))
            else:
                # Preserve future Anthropic delta fields rather than silently
                # dropping them. String fragments are concatenated.
                for key, value in delta.items():
                    if key == "type":
                        continue
                    if isinstance(value, str) and isinstance(block.get(key), str):
                        block[key] += value
                    else:
                        block[key] = value
            return
        if event_type == "content_block_stop":
            self._finish_block(int(event.get("index", 0)))
            return
        if event_type == "message_delta":
            self.message.update(event.get("delta") or {})
            usage = event.get("usage") or {}
            if usage:
                self.message["usage"] = {
                    **(self.message.get("usage") or {}), **usage}

    def _finish_block(self, index: int) -> None:
        if index not in self.partial_json:
            return
        partial = self.partial_json.pop(index)
        try:
            self.blocks.setdefault(index, {})["input"] = (
                json.loads(partial) if partial else {})
        except ValueError as exc:
            self.error = {
                "type": "error",
                "error": {
                    "type": "invalid_tool_input",
                    "message": f"could not decode streamed tool input: {exc}",
                },
            }

    def response(self) -> dict:
        for index in list(self.partial_json):
            self._finish_block(index)
        if self.error:
            return self.error
        if not self.message:
            return {
                "type": "error",
                "error": {
                    "type": "proxy_error",
                    "message": "upstream stream ended without message_start",
                },
            }
        response = dict(self.message)
        response["content"] = [self.blocks[index]
                               for index in sorted(self.blocks)]
        return response


class _MmxMessagesProxyHandler(BaseHTTPRequestHandler):
    """Relay mmx's fixed MiniMax URL while retaining streamed tool calls.

    mmx always posts to ``<base>/anthropic/v1/messages`` with ``x-api-key``.
    The configured MiniMax-compatible endpoint may expose that payload at a
    different URL, so the local proxy forwards it using the same authentication
    header.  The upstream request is forced to stream so reverse proxies keep
    seeing activity during long thinking phases.  SSE is relayed immediately
    while a full response is reconstructed for the MCP adapter, because mmx's
    stream renderer drops tool-use events.
    """

    @staticmethod
    def _json_response(payload: bytes) -> dict:
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, ValueError):
            value = {"error": payload.decode(errors="replace")}
        return value if isinstance(value, dict) else {"error": value}

    @staticmethod
    def _proxy_error(exc: Exception) -> dict:
        return {
            "type": "error",
            "error": {
                "type": "proxy_error",
                "message": f"{type(exc).__name__}: {exc}",
            },
        }

    @staticmethod
    def _upstream_headers(api_key: str, content_type: str,
                          anthropic_version: str) -> dict[str, str]:
        return {
            "X-Api-Key": api_key,
            "Content-Type": content_type,
            "Anthropic-Version": anthropic_version,
        }

    def _publish(self, response: dict) -> None:
        self.server.completed_responses.put(response)

    def _send_buffered(self, status: int, content_type: str,
                       payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            size = int(self.headers.get("content-length", "0"))
            body = self.rfile.read(size)
            request_body = json.loads(body)
            if not isinstance(request_body, dict):
                raise ValueError("Messages request body must be a JSON object")
            request_body["stream"] = True
            body = json.dumps(request_body, separators=(",", ":")).encode()
            request = urllib.request.Request(
                self.server.upstream_url, data=body, method="POST",
                headers=self._upstream_headers(
                    self.server.upstream_key,
                    self.headers.get("content-type", "application/json"),
                    self.headers.get("anthropic-version", "2023-06-01")))
            try:
                with urllib.request.urlopen(
                        request, timeout=self.server.upstream_timeout) as response:
                    status = response.status
                    content_type = response.headers.get(
                        "content-type", "application/json")
                    if "text/event-stream" not in content_type.lower():
                        payload = response.read()
                        self._publish(self._json_response(payload))
                        self._send_buffered(status, content_type, payload)
                        return

                    accumulator = _AnthropicStreamAccumulator()
                    self.send_response(status)
                    self.send_header("Content-Type", content_type)
                    # BaseHTTPRequestHandler speaks HTTP/1.0 by default. With
                    # no Content-Length, closing this local connection marks
                    # the end while every SSE line can be flushed immediately.
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    try:
                        for line in response:
                            if line.startswith(b"data:"):
                                data = line[5:].decode(errors="replace").lstrip()
                                accumulator.feed(data.rstrip("\r\n"))
                            self.wfile.write(line)
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError) as exc:
                        self._publish(self._proxy_error(exc))
                        return
                    except Exception as exc:  # noqa: BLE001 - stream failure
                        self._publish(self._proxy_error(exc))
                        return
                    self._publish(accumulator.response())
                    return
            except urllib.error.HTTPError as exc:
                status = exc.code
                content_type = exc.headers.get(
                    "content-type", "application/json")
                payload = exc.read()
            self._publish(self._json_response(payload))
            try:
                self._send_buffered(status, content_type, payload)
            except (BrokenPipeError, ConnectionResetError):
                return
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001 - return proxy failures to mmx
            error = self._proxy_error(exc)
            self._publish(error)
            payload = json.dumps(error).encode()
            try:
                self._send_buffered(502, "application/json", payload)
            except (BrokenPipeError, ConnectionResetError):
                return

    def log_message(self, _format, *args):
        # Do not put request metadata (or, in future versions, auth headers) in
        # the experiment console. mmx's own per-round response log is retained.
        return


class MmxAgent(BaseAgent):
    """MiniMax-M3 through the installed ``mmx`` API CLI plus an MCP loop.

    mmx is deliberately a thin API client: it sends tool definitions and emits
    ``tool_use`` blocks, but it is not an MCP host.  This adapter owns the MCP
    stdio sessions, executes those tool calls, and feeds ``tool_result`` blocks
    back through mmx until MiniMax returns a final assistant response.
    """

    kind = "mmx"
    API_TIMEOUT_RE = re.compile(
        r"request timed out|request timeout|timed?\s*out|\"code\"\s*:\s*5",
        re.I)
    PERMANENT_API_ERROR_RE = re.compile(
        r"authentication|unauthorized|forbidden|invalid api key|"
        r"maximum context|context length|at most 0 image|\b40[013]\b",
        re.I)

    @classmethod
    def _is_api_timeout(cls, text: str) -> bool:
        return bool(cls.API_TIMEOUT_RE.search(text or ""))

    @classmethod
    def _recoverable_api_error(cls, text: str) -> bool:
        return bool(text) and not cls.PERMANENT_API_ERROR_RE.search(text)

    @staticmethod
    def _messages_url(base_url: str) -> str:
        base = base_url.rstrip("/")
        return base + ("/messages" if base.endswith("/v1") else "/v1/messages")

    def _cli(self) -> str:
        configured = str(self.cfg.get("cli", "")).strip()
        if configured:
            return configured
        found = shutil.which("mmx")
        if found and (shutil.which("node") or shutil.which("nodejs")):
            return found
        fallback = self.repo / "cli_agents" / "mmx_text_chat.py"
        if fallback.is_file():
            return str(fallback)
        if found:
            return found
        bundled = self.repo / ".pixi" / "envs" / "default" / "bin" / "mmx"
        return str(bundled) if bundled.is_file() else "mmx"

    @staticmethod
    def _assert_under(path: Path, root: Path) -> None:
        root = root.resolve()
        candidate = path.resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise RuntimeError(
                f"mmx path safety check failed: {candidate} not under {root}"
            ) from exc

    def _mmx_env(self, session_root: Path) -> dict[str, str]:
        mmx_home = session_root / "mmx_home"
        mmx_home.mkdir(parents=True, exist_ok=True)
        config_dir = mmx_home / "config"
        xdg_cache = mmx_home / ".cache"
        xdg_data = mmx_home / ".local" / "share"
        xdg_config = mmx_home / ".config"
        for directory in (config_dir, xdg_cache, xdg_data, xdg_config):
            directory.mkdir(parents=True, exist_ok=True)
        return {
            "HOME": str(mmx_home),
            "MMX_CONFIG_DIR": str(config_dir),
            "XDG_CONFIG_HOME": str(xdg_config),
            "XDG_CACHE_HOME": str(xdg_cache),
            "XDG_DATA_HOME": str(xdg_data),
        }

    @staticmethod
    def _response_json(log: Path | None) -> dict:
        """Find mmx's JSON response even if it prefixes a one-line diagnostic."""
        text = _tail(log) if log else ""
        decoder = json.JSONDecoder()
        candidates: list[dict] = []
        for match in re.finditer(r"\{", text):
            try:
                value, _ = decoder.raw_decode(text[match.start():])
            except ValueError:
                continue
            if isinstance(value, dict):
                candidates.append(value)
        for value in reversed(candidates):
            if isinstance(value.get("content"), list) or "error" in value:
                return value
        return candidates[-1] if candidates else {}

    @staticmethod
    def _assistant_content(response: dict) -> list[dict]:
        # Thinking blocks returned by this vLLM endpoint have no Anthropic
        # signature and need not be replayed. Text and tool_use are the actual
        # conversational state required by the next request.
        return [block for block in (response.get("content") or [])
                if isinstance(block, dict) and
                block.get("type") in ("text", "tool_use")]

    @staticmethod
    def _final_text(response: dict) -> str:
        return "\n".join(
            str(block.get("text") or "")
            for block in response.get("content", []) or []
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()

    @staticmethod
    def _tool_recovery_prompt() -> str:
        return (
            "The previous assistant reply had no tool call. "
            "You are in a tool-enabled MCP session and must continue by "
            "calling at least one tool in this turn. "
            "Do not send plain text conclusions."
        )

    def _tool_result_content(self, result) -> list[dict]:
        blocks: list[dict] = []
        text_limit = max(1000, int(self.cfg.get(
            "max_tool_result_chars", 24000)))
        for item in getattr(result, "content", []) or []:
            kind = getattr(item, "type", "")
            if kind == "text":
                text = str(item.text)
                if len(text) > text_limit:
                    text = (text[:text_limit] +
                            f"\n...<truncated {len(text) - text_limit} chars by "
                            "mmx adapter>")
                blocks.append({"type": "text", "text": text})
            elif kind == "image":
                if bool(self.cfg.get("image_inputs", False)):
                    blocks.append({
                        "type": "image",
                        "source": {"type": "base64",
                                   "media_type": getattr(
                                       item, "mimeType", None) or getattr(
                                           item, "mime_type", "image/png"),
                                   "data": str(item.data)},
                    })
                else:
                    blocks.append({
                        "type": "text",
                        "text": ("[Screenshot image omitted: this self-hosted "
                                 "MiniMax-M3 Messages endpoint accepts 0 image "
                                 "inputs. Do not request more screenshots. "
                                 "Inspect the reference and reconstruction with "
                                 "get_scene_info or execute_blender_code queries "
                                 "for geometry, bounds, materials, and objects.]"),
                    })
            else:
                try:
                    value = item.model_dump(by_alias=True, exclude_none=True)
                except AttributeError:
                    value = str(item)
                blocks.append({"type": "text", "text":
                               json.dumps(value, default=str)})
        structured = getattr(result, "structuredContent", None)
        if structured is None:
            structured = getattr(result, "structured_content", None)
        if not blocks and structured is not None:
            blocks.append({"type": "text", "text":
                           json.dumps(structured, default=str)})
        return blocks or [{"type": "text", "text": "Tool completed."}]

    @staticmethod
    async def _call_mcp_tool(session, name: str, arguments: dict,
                             timeout: float):
        return await asyncio.wait_for(
            session.call_tool(
                name, arguments,
                read_timeout_seconds=timedelta(seconds=timeout)),
            timeout=timeout + 1)

    def _tool_round_limit(self) -> int | None:
        """Optional model/tool round cap; absent or zero means unlimited."""
        raw = self.cfg.get("max_tool_rounds", 0)
        try:
            value = int(raw or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("max_tool_rounds must be an integer") from exc
        if value < 0:
            raise ValueError("max_tool_rounds must be non-negative")
        return value or None

    @staticmethod
    def _append_prompt_if_ready(messages: list[dict], prompt: str) -> bool:
        """Append a new user turn unless the stored one is still pending.

        A timeout can leave either the original prompt or a tool_result user
        message without an assistant response. Re-sending that pending turn is
        the exact same-session retry; appending a continuation beside it would
        create consecutive user messages and lose the request boundary.
        """
        if (messages and isinstance(messages[-1], dict)
                and messages[-1].get("role") == "user"):
            return False
        messages.append({"role": "user", "content": prompt})
        return True

    @staticmethod
    def _response_token_total(response: dict) -> int:
        """Normalised tokens spent by one Messages response.

        MiniMax reports cache reads separately from uncached input, so both are
        part of the prompt-token total. Cache creation is an additional charged
        component, matching ``collect_usage`` and the shared normalisation.
        """
        usage = response.get("usage") or {}
        total = 0
        for key in ("input_tokens", "cache_read_input_tokens",
                    "cache_creation_input_tokens", "output_tokens"):
            try:
                total += int(usage.get(key) or 0)
            except (TypeError, ValueError):
                continue
        return total

    def _local_tool(self, workdir: Path, name: str, arguments: dict):
        """The minimal read-only workspace surface a coding CLI normally has."""
        relative = str(arguments.get("path") or ".")
        root = workdir.resolve()
        target = (root / relative).resolve()
        if not target.is_relative_to(root):
            raise ValueError("path must stay inside the task working directory")
        if name == "read_local_file":
            if not target.is_file():
                raise FileNotFoundError(relative)
            limit = max(1000, int(self.cfg.get("max_local_file_chars", 60000)))
            text = target.read_text(errors="replace")
            if len(text) > limit:
                text = (text[:limit] +
                        f"\n...<truncated {len(text) - limit} chars>")
            return [{"type": "text", "text": text}]
        if name == "list_local_files":
            if not target.is_dir():
                raise NotADirectoryError(relative)
            entries = sorted(str(path.relative_to(root))
                             for path in target.iterdir())
            limit = max(20, int(self.cfg.get("max_local_list_entries", 200)))
            if len(entries) > limit:
                entries = entries[:limit] + [
                    f"...<{len(entries) - limit} more entries>"]
            return [{"type": "text", "text": "\n".join(entries)}]
        raise ValueError(f"unknown local tool {name}")

    @staticmethod
    def _write_event(path: Path, event: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            handle.write(json.dumps(event, default=str) + "\n")

    async def _open_mcp(self, ports, stack: AsyncExitStack, session_dir: Path):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        tools: dict[str, tuple[str, str, object]] = {}
        definitions: list[dict] = []
        for server_name, spec in self.servers(ports).items():
            errlog = stack.enter_context(
                (session_dir / f"mmx.mcp.{server_name}.log").open("a"))
            params = StdioServerParameters(
                command=spec["command"], args=spec.get("args", []),
                env={**os.environ, **spec.get("env", {})})
            read, write = await stack.enter_async_context(
                stdio_client(params, errlog=errlog))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            listed = await session.list_tools()
            for index, tool in enumerate(listed.tools):
                safe_server = re.sub(r"[^A-Za-z0-9_-]", "_", server_name)
                safe_tool = re.sub(r"[^A-Za-z0-9_-]", "_", tool.name)
                alias = f"mcp__{safe_server}__{safe_tool}"
                if alias in tools:
                    alias = f"{alias}_{index}"
                schema = (getattr(tool, "inputSchema", None) or
                          getattr(tool, "input_schema", None) or
                          {"type": "object", "properties": {}})
                definitions.append({
                    "name": alias,
                    "description": (f"MCP server {server_name}: " +
                                    (tool.description or tool.name)),
                    "input_schema": schema,
                })
                tools[alias] = (server_name, tool.name, session)
        return definitions, tools

    async def _conversation(self, run: AgentRun, prompt: str, ports: dict,
                            workdir: Path, session_dir: Path, resume: dict,
                            proxy_url: str, proxy_server, session_root: Path,
                            messages: list[dict], wire: Path) -> None:
        timeout = max(1.0, float(resume.get("_timeout_sec", self.timeout)))
        deadline = time.monotonic() + timeout
        max_rounds = self._tool_round_limit()
        non_tool_turns = 0

        # The outer continuation driver cannot inspect cumulative usage until
        # this conversation returns.  With no round cap, enforce the configured
        # per-instance token ceiling here before issuing each additional model
        # request.  Read the durable session once to include prior resume
        # attempts, then update the running total from each new response.
        token_limit = 0
        try:
            token_limit = max(
                0, int(self.cfg.get("token_total_limit", 0) or 0))
        except (TypeError, ValueError):
            token_limit = 0
        token_total = 0
        if token_limit:
            self._record_usage(run, session_dir)
            try:
                token_total = int(
                    ((run.usage or {}).get("tokens") or {}).get("total") or 0)
            except (TypeError, ValueError):
                token_total = 0
            if token_total_limit_reached(self, run):
                # Keep the normal AgentRun completion contract: when the outer
                # runner accepts an already-existing reconstruction at the
                # token ceiling, clearing completion_error must leave a
                # successful, non-empty turn record.
                run.turns.append(TurnResult(
                    label="mmx-token-limit", returncode=0, seconds=0.0))
                return

        async with AsyncExitStack() as stack:
            definitions, tools = await self._open_mcp(ports, stack, session_dir)
            definitions.extend([
                {
                    "name": "read_local_file",
                    "description": ("Read a UTF-8 text file inside the current "
                                    "task workspace. Use this for staged SKILL.md "
                                    "and its referenced instruction files."),
                    "input_schema": {"type": "object", "properties": {
                        "path": {"type": "string"}}, "required": ["path"]},
                },
                {
                    "name": "list_local_files",
                    "description": ("List the direct children of a directory "
                                    "inside the current task workspace."),
                    "input_schema": {"type": "object", "properties": {
                        "path": {"type": "string"}}, "required": ["path"]},
                },
            ])
            tools["read_local_file"] = (None, "read_local_file", None)
            tools["list_local_files"] = (None, "list_local_files", None)
            messages_path = session_root / "messages.json"
            self._assert_under(messages_path, session_root)
            tool_dir = session_root / "tools"
            tool_dir.mkdir(parents=True, exist_ok=True)
            tool_paths: list[Path] = []
            for index, definition in enumerate(definitions):
                path = tool_dir / f"{index:03d}.json"
                path.write_text(json.dumps(definition, indent=2))
                tool_paths.append(path)

            # Persist the pending user turn before its request. If the provider
            # or harness times out, a same-session retry resends this exact turn.
            messages_path.write_text(json.dumps(messages, indent=2))

            round_no = 0
            while max_rounds is None or round_no < max_rounds:
                round_no += 1
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    run.turns.append(TurnResult(
                        label=f"mmx-{round_no}", returncode=124,
                        seconds=timeout, timed_out=True,
                        log=session_dir / f"mmx.request-{round_no}.log"))
                    return
                label = f"mmx-{round_no}"
                log = session_dir / f"mmx.request-{round_no}.log"
                argv = [
                    self._cli(), "text", "chat", "--model", self.model,
                    "--messages-file", str(messages_path),
                    "--max-tokens", str(int(self.cfg.get("max_tokens", 4096))),
                    "--base-url", proxy_url,
                    # This is a local placeholder. The real self-hosted key is
                    # held only by the proxy and never appears in argv.
                    "--api-key", "local-self-hosted-proxy",
                    # A placeholder API key cannot be auto-classified as a
                    # MiniMax global/CN credential. The local proxy target is
                    # region-independent, so disable that discovery explicitly.
                    "--region", str(self.cfg.get("region", "global")),
                    "--timeout", str(max(1, int(remaining))),
                    "--stream",
                    "--output", "json", "--non-interactive",
                    "--no-color",
                ]
                for path in tool_paths:
                    self._assert_under(path, session_root)
                    argv += ["--tool", str(path)]
                turn = await asyncio.to_thread(
                    self._spawn, argv, cwd=workdir, log=log, label=label,
                    env=self._mmx_env(session_root),
                    timeout=remaining)
                run.turns.append(turn)
                run.exports.append(str(log))
                try:
                    response = proxy_server.completed_responses.get_nowait()
                except queue.Empty:
                    response = self._response_json(log)
                if turn.returncode != 0 or turn.timed_out:
                    if turn.returncode != 0:
                        error = response.get("error")
                        if error:
                            turn.api_error = _clip(error, 1000)
                    return

                if not isinstance(response.get("content"), list):
                    error = response.get("error") or "mmx returned no Messages response"
                    turn.api_error = _clip(error, 1000)
                    return
                self._write_event(wire, {"type": "response", "response": response})
                token_total += self._response_token_total(response)
                token_limit_hit = bool(
                    token_limit and token_total >= token_limit)
                assistant = self._assistant_content(response)
                messages.append({"role": "assistant", "content": assistant})
                turn.text = self._final_text(response)
                calls = [block for block in assistant
                         if block.get("type") == "tool_use"]
                if not calls:
                    if token_limit_hit:
                        messages_path.write_text(json.dumps(messages, indent=2))
                        self._record_usage(run, session_dir)
                        if token_total_limit_reached(self, run):
                            return
                    non_tool_turns += 1
                    if non_tool_turns > 2:
                        messages_path.write_text(json.dumps(messages, indent=2))
                        run.limit_hit = True
                        run.limit_reason = "assistant_reply_without_tool_calls"
                        run.limit_evidence = turn.text or self._final_text(response)
                        return
                    messages.append({
                        "role": "user",
                        "content": [{"type": "text",
                                     "text": self._tool_recovery_prompt()}],
                    })
                    messages_path.write_text(json.dumps(messages, indent=2))
                    continue

                results = []
                non_tool_turns = 0
                for call in calls:
                    alias = str(call.get("name") or "")
                    arguments = call.get("input") or {}
                    target = tools.get(alias)
                    if not target:
                        blocks = [{"type": "text", "text":
                                   f"Unknown MCP tool: {alias}"}]
                        is_error = True
                    else:
                        server_name, tool_name, mcp_session = target
                        run.tool_calls.append(_normalise_call(
                            label, server_name, tool_name, arguments,
                            "tool_use", mcp=server_name is not None))
                        try:
                            if server_name is None:
                                blocks = self._local_tool(
                                    workdir, tool_name, arguments)
                                is_error = False
                            else:
                                tool_timeout = min(
                                    max(1.0, deadline - time.monotonic()),
                                    float(self.cfg.get(
                                        "mcp_tool_timeout_sec", 600)))
                                result = await self._call_mcp_tool(
                                    mcp_session, tool_name, arguments,
                                    tool_timeout)
                                blocks = self._tool_result_content(result)
                                is_error = bool(
                                    getattr(result, "isError", False) or
                                    getattr(result, "is_error", False))
                        except asyncio.CancelledError:
                            raise
                        except (KeyboardInterrupt, SystemExit):
                            raise
                        except BaseException as exc:  # noqa: BLE001 - model sees tool error
                            blocks = [{"type": "text", "text":
                                       f"MCP tool failed:\n{_exception_trace(exc)}"}]
                            is_error = True
                    result_block = {
                        "type": "tool_result",
                        "tool_use_id": str(call.get("id") or ""),
                        "content": blocks,
                        "is_error": is_error,
                    }
                    results.append(result_block)
                    self._write_event(wire, {"type": "tool_result",
                                             "result": result_block})
                messages.append({"role": "user", "content": results})
                # The tool side effects have happened; durably pair their
                # results with the assistant tool_use blocks before any exit.
                messages_path.write_text(json.dumps(messages, indent=2))

                # Execute tool calls from the response that crossed the token
                # ceiling so a final reconstruction write/save is not lost, but
                # do not spend tokens on another model request.
                if token_limit_hit:
                    self._record_usage(run, session_dir)
                    if token_total_limit_reached(self, run):
                        return

            # Positive values remain supported for explicitly bounded tests or
            # ad-hoc runs.  Normal graded configs omit the setting and therefore
            # continue until completion, timeout, provider failure, or token cap.
            run.completion_error = (
                f"mmx MCP loop reached {max_rounds} model/tool rounds")

    def run(self, prompt, ports, workdir, session_dir, resume=None) -> AgentRun:
        run = AgentRun(kind=self.kind, model=self.model)
        resume = dict(resume or {})
        resume_id = str(resume.get("resume_target") or "")
        session_id = resume_id or str(uuid.uuid4())
        if resume_id:
            run.resumed_from = resume_id
        else:
            run.session_ids.append(session_id)

        session_root = session_dir / "mmx_home" / "sessions" / session_id
        messages_path = session_root / "messages.json"
        wire = session_root / "wire.jsonl"
        session_root.mkdir(parents=True, exist_ok=True)
        if resume_id and not messages_path.is_file():
            raise RuntimeError(
                f"cannot resume MiniMax session {resume_id}: task-owned "
                f"messages are missing from {messages_path}")
        if resume_id:
            try:
                messages = json.loads(messages_path.read_text())
            except (OSError, ValueError) as exc:
                raise RuntimeError(
                    f"cannot resume MiniMax session {resume_id}: invalid "
                    f"task-owned messages file {messages_path}") from exc
        else:
            messages = []
        if not isinstance(messages, list) or (resume_id and not messages):
            raise RuntimeError(
                f"cannot resume MiniMax session {resume_id}: task-owned "
                "conversation is empty or invalid")
        self._append_prompt_if_ready(messages, prompt)

        key_name = str(self.cfg.get("api_key_env", "VLLM_API_KEY"))
        api_key = os.environ.get(key_name) or _dotenv_value(
            self.repo / ".env", key_name)
        base_url = str(self.cfg.get("base_url", "")).strip()
        if not api_key or not base_url:
            log = session_dir / "mmx.setup.log"
            missing = key_name if not api_key else "[agent.minimax-m3].base_url"
            log.write_text(f"missing self-hosted MiniMax setting: {missing}\n")
            run.turns.append(TurnResult("mmx-setup", 1, 0.0, log=log))
            run.exports.append(str(log))
            return self._record_usage(run, session_dir)

        server = ThreadingHTTPServer(("127.0.0.1", 0),
                                     _MmxMessagesProxyHandler)
        server.upstream_url = self._messages_url(base_url)
        server.upstream_key = api_key
        server.upstream_timeout = max(
            1.0, float(self.cfg.get("request_timeout_sec", self.timeout)))
        server.completed_responses = queue.Queue()
        thread = threading.Thread(target=server.serve_forever,
                                  name="mmx-messages-proxy", daemon=True)
        thread.start()
        proxy_url = f"http://127.0.0.1:{server.server_port}"
        try:
            asyncio.run(self._conversation(
                run, prompt, ports, workdir, session_dir, resume, proxy_url,
                server, session_root, messages, wire))
        except asyncio.CancelledError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:  # noqa: BLE001 - preserve a resumable failure
            log = session_dir / "mmx.adapter.log"
            log.write_text(f"{_exception_trace(exc)}\n")
            run.turns.append(TurnResult("mmx-adapter", 1, 0.0, log=log))
            run.exports.append(str(log))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        if wire.is_file():
            run.exports.append(str(wire))
        return self._record_usage(run, session_dir)

    def collect_usage(self, session_dir: Path, run: AgentRun) -> dict:
        session_ids = self._session_keys(run)
        wires = [session_dir / "mmx_home" / "sessions" / sid / "wire.jsonl"
                 for sid in session_ids]
        responses = []
        for wire in wires:
            for event in _json_lines(wire):
                if event.get("type") == "response":
                    responses.append(event.get("response") or {})
        totals: dict = {}
        for response in responses:
            _accumulate(totals, response.get("usage") or {})
        uncached_input = totals.get("input_tokens")
        cached_input = totals.get("cache_read_input_tokens")
        input_total = (
            int(uncached_input or 0) + int(cached_input or 0)
            if uncached_input is not None or cached_input is not None
            else None
        )
        return _usage(
            f"mmx_home/sessions/<session>/wire.jsonl ({len(responses)} response(s))",
            api_requests=len(responses),
            tokens=_tokens(
                input_total=input_total,
                input_cached=cached_input,
                cache_write=totals.get("cache_creation_input_tokens"),
                output=totals.get("output_tokens")),
            by_model={self.model: {
                "api_requests": len(responses),
                "input": input_total,
                "input_cached": cached_input,
                "output": totals.get("output_tokens"),
            }} if responses else None,
            notes=["mmx is used as an API CLI; the adapter persists its "
                   "Messages responses and sums server-reported usage"])


def token_total_limit_reached(agent: BaseAgent, run: AgentRun) -> bool:
    """Stop a session once its configured cumulative token ceiling is hit.

    Qwen writes cumulative per-session usage when each CLI process exits.  This
    check therefore runs at the end of every initial/continued CLI turn, before
    another same-session continuation can spend more tokens.  A zero or absent
    setting leaves the ceiling disabled.
    """
    try:
        limit = max(0, int(agent.cfg.get("token_total_limit", 0) or 0))
    except (TypeError, ValueError):
        limit = 0
    total = ((run.usage or {}).get("tokens") or {}).get("total")
    try:
        total = int(total) if total is not None else None
    except (TypeError, ValueError):
        total = None
    if not limit or total is None or total < limit:
        return False
    run.completion_error = "configured token total limit reached"
    run.limit_reason = "token_total_limit"
    run.limit_evidence = f"tokens.total={total:,}; limit={limit:,}"
    return True


def accept_token_limited_completion(run: AgentRun, reconstruction: dict) -> bool:
    """Accept the latest exported reconstruction when the token cap stopped it.

    A timeout/API failure that caused the last Qwen process to stop is marked as
    recovered only after the harness found and exported a non-empty
    reconstruction_gt.py Blender Text datablock. That makes ``run.ok`` true,
    closes any checkpoint, and leaves the exported ``<instance>.py`` as the
    final script. Without that artifact the caller keeps the normal checkpoint
    path instead.
    """
    if run.limit_reason != "token_total_limit" or not reconstruction.get("matched"):
        return False
    for turn in run.turns:
        if turn.returncode != 0 or turn.timed_out or turn.api_error:
            turn.recovered = True
    run.completion_error = ""
    run.limit_evidence = (
        f"{run.limit_evidence}; accepted latest non-empty "
        f"{reconstruction.get('name') or 'reconstruction_gt.py'} "
        f"({int(reconstruction.get('bytes') or 0):,} bytes)")
    return True


def run_with_continuations(agent: BaseAgent, prompt: str, ports: dict[str, int],
                           workdir: Path, session_dir: Path,
                           resume: dict | None, completion_check):
    """Run an agent, resuming the same live chat until the task is complete.

    For Qwen, this also continues after transient provider errors or a clean
    exit before Blender holds the required reconstruction_gt.py Text datablock. For
    every other agent, only *timeouts* are retried: a killed CLI process is
    restarted only when its same session id was captured; a retry is never
    allowed to silently create a fresh conversation. A timed-out connection gets a fresh
    per-turn timeout on each reconnect; the timeout budget is not consumed by
    the killed turn.

    Returns ``(AgentRun, last_completion_summary)``.
    """
    # Qwen keeps the historic behaviour of continuing the same chat for missing
    # artifacts and transient provider errors.  Other agents only reconnect on
    # timeout, so their default normal-continuation budget is zero.
    default_continue = 5 if isinstance(agent, (QwenAgent, MmxAgent)) else 0
    max_continuations = max(
        0, int(agent.cfg.get("session_continue_max", default_continue)))
    max_usage_limit_continuations = max(
        0, int(agent.cfg.get(
            "usage_limit_continue_max", min(max_continuations, 2))))
    # A provider/stream timeout means the CLI process is no longer usable, so
    # each retry below starts a fresh CLI connection to the SAME stored chat.
    # Keep this separate from the normal clean-exit/missing-artifact budget,
    # but never reconnect unless the interrupted turn emitted a resumable id.
    max_timeout_reconnects = max(0, int(agent.cfg.get(
        "timeout_reconnect_max", 5)))
    # Count retries only within the current timeout episode.  A successful
    # reconnect resets this counter, so a later timeout gets its own five
    # retries instead of consuming a sample-wide quota.
    timeout_reconnects = 0
    delay = max(0.0, float(agent.cfg.get("session_retry_delay_sec", 10)))
    delay_cap = max(delay, float(agent.cfg.get(
        "session_retry_delay_max_sec", 120)))
    aggregate: AgentRun | None = None
    summary = {}

    continuation = 0
    while True:
        if continuation == 0:
            turn_prompt = prompt
            turn_resume = dict(resume or {})
        else:
            turn_prompt = (
                "Continue the same Blender reconstruction task from the current "
                "scene and conversation. Do not restart or repeat completed work. "
                "Inspect what is already in Blender, finish the remaining work, "
                "and before ending update the Blender Text datablock named "
                "reconstruction_gt.py with the latest complete, self-contained "
                "reconstruction script, then execute the code stored in that "
                "datablock. Do not write an instance-named .py file; the harness "
                "exports the exact in-Blender text. Use the already "
                "configured MCP server and save the Blender file."
            )
            turn_resume = agent.resume_state(aggregate)
            turn_resume["_continuation"] = continuation
        # Each reconnect is a new CLI process and must receive the full
        # per-turn timeout.  The previous implementation passed the remaining
        # time from one shared deadline, so a hard kill at 3600s left no time
        # for the promised timeout reconnects.
        turn_resume["_timeout_sec"] = agent.timeout
        current = agent.run(turn_prompt, ports, workdir, session_dir,
                            resume=turn_resume)
        requested_resume = str(turn_resume.get("resume_target") or "")
        if requested_resume:
            foreign_ids = [sid for sid in current.session_ids
                           if sid != requested_resume]
            if (current.resumed_from != requested_resume or foreign_ids or
                    current.resume_target != requested_resume):
                raise RuntimeError(
                    f"{agent.kind} was required to resume session "
                    f"{requested_resume}, but reported resumed_from="
                    f"{current.resumed_from!r}, session_ids="
                    f"{current.session_ids!r}; refusing a new conversation")
        summary = completion_check(current) or {}

        if aggregate is None:
            aggregate = current
        else:
            aggregate.turns.extend(current.turns)
            aggregate.tool_calls.extend(current.tool_calls)
            aggregate.session_ids.extend(
                sid for sid in current.session_ids
                if sid not in aggregate.session_ids)
            aggregate.exports.extend(current.exports)
            aggregate.usage = current.usage
            aggregate.resumed_from = aggregate.resumed_from or current.resumed_from

        artifact_complete = bool(summary.get("matched"))
        api_error = current.turns[-1].api_error if current.turns else ""
        clean_turn = current.ok
        if clean_turn and artifact_complete:
            return aggregate, summary

        quota_evidence = usage_limit_evidence(api_error)
        if quota_evidence and not _usage_limit_retryable(api_error):
            aggregate.limit_hit = True
            aggregate.limit_reason = "usage_limit"
            aggregate.limit_evidence = api_error or quota_evidence
            return aggregate, summary
        if quota_evidence and isinstance(agent, QwenAgent):
            # 429-style spikes are commonly transient, so keep retrying the
            # same session with longer waits before marking the invocation as
            # terminal quota exhaustion.
            if continuation < max_usage_limit_continuations:
                pass
            else:
                aggregate.limit_hit = True
                aggregate.limit_reason = "usage_limit"
                aggregate.limit_evidence = api_error or quota_evidence
                return aggregate, summary

        if token_total_limit_reached(agent, aggregate):
            print(f"    token total limit reached — {aggregate.limit_evidence}")
            return aggregate, summary

        last_turn = current.turns[-1] if current.turns else None
        # A harness timeout is normally represented by rc=124/timed_out=True
        # and has no API diagnostic.  Treat it exactly like Qwen's explicit
        # stream/request timeout so the sample reconnects instead of stopping
        # after the first killed process.
        is_timeout = bool(
            last_turn and (
                last_turn.timed_out or last_turn.returncode == 124 or
                (api_error and agent._is_api_timeout(api_error))))
        if not is_timeout:
            # The connection returned a response, so a later timeout starts a
            # fresh five-retry window for this sample.
            timeout_reconnects = 0
        can_continue = bool(aggregate.resume_target) and (
            (is_timeout and timeout_reconnects < max_timeout_reconnects) or
            (api_error and agent._recoverable_api_error(api_error)) or
            (clean_turn and not artifact_complete))
        normal_budget_exhausted = (
            not is_timeout and continuation >= max_continuations)
        timeout_budget_exhausted = (
            is_timeout and timeout_reconnects >= max_timeout_reconnects)
        if not can_continue or normal_budget_exhausted or timeout_budget_exhausted:
            if is_timeout:
                aggregate.completion_error = (
                    f"{agent.kind} timeout after {timeout_reconnects} reconnect(s)"
                )
                aggregate.limit_reason = (
                    "api_timeout" if api_error and agent._is_api_timeout(api_error)
                    else "timeout")
                aggregate.limit_evidence = (
                    api_error or
                    f"timed out after {last_turn.seconds:.1f}s; "
                    f"reconnects={timeout_reconnects}/"
                    f"{max_timeout_reconnects}")
            elif api_error:
                if agent._recoverable_api_error(api_error):
                    aggregate.completion_error = (
                        f"{agent.kind} API failure after {continuation} "
                        f"continuation(s): {api_error}")
                aggregate.limit_reason = (
                    "api_timeout" if agent._is_api_timeout(api_error)
                    else "api_error")
                aggregate.limit_evidence = api_error
            elif not artifact_complete:
                aggregate.completion_error = (
                    "missing complete reconstruction script")
                aggregate.limit_reason = "incomplete_artifact"
                candidates = summary.get("candidates") or []
                aggregate.limit_evidence = (
                    "no non-empty reconstruction_gt.py Blender Text "
                    f"datablocks: {', '.join(candidates) or 'none'}")
            return aggregate, summary

        # This failure is superseded if the next same-session request works.
        for turn in current.turns:
            if turn.returncode != 0 or turn.timed_out or turn.api_error:
                turn.recovered = True
        if is_timeout:
            timeout_reconnects += 1
        reason = (f"provider error: {api_error}" if api_error else
                  "no non-empty reconstruction_gt.py Text datablock")
        if is_timeout:
            print(f"    {agent.kind} timeout reconnect {timeout_reconnects}/"
                  f"{max_timeout_reconnects} ({reason})")
        else:
            print(f"    {agent.kind} continuation {continuation + 1}/"
                  f"{max_continuations} in the same session ({reason})")
        if (is_timeout or api_error) and delay:
            sleep_for = min(delay * (2 ** continuation), delay_cap)
            if sleep_for:
                print(f"    waiting {sleep_for:g}s before session retry")
                time.sleep(sleep_for)
        continuation += 1

    # The bounded continuation loop ended without a complete artifact.
    assert aggregate is not None
    if not aggregate.completion_error:
        aggregate.completion_error = f"{agent.kind} continuation budget exhausted"
        aggregate.limit_reason = "incomplete_artifact"
        aggregate.limit_evidence = (
            "no non-empty reconstruction_gt.py Blender Text datablock")
    return aggregate, summary


# --------------------------------------------------------------------------
# Anthropic-style `stream-json` logs (qwen code and claude code emit the same
# envelope: one JSON event per line, assistant turns carrying `message.content`
# blocks, MCP tools named `mcp__<server>__<tool>`).
# --------------------------------------------------------------------------
def _anthropic_messages(log: Path | None):
    if not log or not log.exists():
        return
    for line in log.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        message = event.get("message")
        if isinstance(message, dict):
            yield message


def _anthropic_tool_calls(turn: TurnResult) -> list[dict]:
    calls = []
    for message in _anthropic_messages(turn.log):
        for block in message.get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = str(block.get("name", ""))
                server = None
                if name.startswith("mcp__") and "__" in name[5:]:
                    server, _, name = name[5:].partition("__")
                calls.append(_normalise_call(turn.label, server, name,
                                             block.get("input"), "tool_use"))
    return calls


def _anthropic_result_event(run: AgentRun) -> dict:
    """The terminal `result` event of the last turn that produced one. It carries
    that INVOCATION's totals (usage, per-model split, cost, turn count) and is
    absent when the CLI was killed before finishing."""
    result: dict = {}
    for turn in run.turns:
        if not turn.log:
            continue
        for event in _json_lines(turn.log):
            if event.get("type") == "result":
                result = event
    return result


def _anthropic_final_text(log: Path | None) -> str:
    text = ""
    for message in _anthropic_messages(log):
        if message.get("role") != "assistant":
            continue
        for block in message.get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", text)
    return text


# --------------------------------------------------------------------------
# claude code (Claude Fable 5 / Claude Opus 5.0)
# --------------------------------------------------------------------------
class ClaudeAgent(BaseAgent):
    """Drives the local `claude` CLI in print mode.

    Auth is the machine's own Claude Code login (`claude /login`) — no API key,
    matching how the bundled raw Claude backend reaches Fable 5 (that wrapper
    goes through tasksolver's ClaudeCodeModel, which shells out to the same CLI;
    its `claude-code-fable-5` alias maps to `--model claude-fable-5`).
    """

    kind = "claude"

    @staticmethod
    def _config_dir() -> Path:
        """The state root used by the Claude CLI for the active login."""
        return Path(
            os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
        ).expanduser().resolve()

    @classmethod
    def _project_dir(cls, workdir: Path) -> Path:
        """Claude's cwd-scoped transcript directory for this task."""
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(workdir.resolve()))
        return cls._config_dir() / "projects" / slug

    @classmethod
    def _restore_transcript(cls, session_id: str, workdir: Path,
                            session_dir: Path) -> Path | None:
        """Make the task-owned conversation visible to the active login.

        Claude stores resumable conversations below ``CLAUDE_CONFIG_DIR``.
        Account rotation changes that directory, so the durable copy belongs
        to the task and is restored into whichever login is active now.
        """
        destination = cls._project_dir(workdir) / f"{session_id}.jsonl"
        store = session_dir / "claude_sessions" / f"{session_id}.jsonl"
        legacy_exports = list(
            session_dir.glob(f"claude.transcript.*{session_id}*.jsonl"))
        legacy_exports.extend(
            session_dir.parent.glob(
                f"attempts/*/session/claude.transcript.*{session_id}*.jsonl"))
        candidates = [
            path for path in (store, destination, *legacy_exports)
            if path.is_file() and path.stat().st_size > 0
        ]
        # A scheduler process started before task-owned transcript staging was
        # added may still rotate to another named login.  Named logins are
        # sibling directories; recover the prior account's copy here as a
        # backward-compatible last line of defence.
        if os.environ.get("CLAUDE_CONFIG_DIR"):
            config_dir = cls._config_dir()
            for sibling in config_dir.parent.iterdir():
                if (sibling == config_dir or not sibling.is_dir()
                        or not (sibling / ".credentials.json").is_file()):
                    continue
                projects = sibling / "projects"
                if projects.is_dir():
                    candidates.extend(projects.glob(f"*/{session_id}.jsonl"))
        candidates = [
            path for path in candidates
            if path.is_file() and path.stat().st_size > 0
        ]
        if not candidates:
            return None

        source = max(
            candidates,
            key=lambda path: (path.stat().st_size, path.stat().st_mtime_ns),
        )
        store.parent.mkdir(parents=True, exist_ok=True)
        if source != store and (
                not store.is_file()
                or (store.stat().st_size, store.stat().st_mtime_ns)
                < (source.stat().st_size, source.stat().st_mtime_ns)):
            temporary = store.with_name(f".{store.name}.stage-{os.getpid()}")
            shutil.copy2(source, temporary)
            temporary.replace(store)
        source = store if store.is_file() else source
        destination.parent.mkdir(parents=True, exist_ok=True)
        if (not destination.is_file()
                or (destination.stat().st_size,
                    destination.stat().st_mtime_ns)
                < (source.stat().st_size, source.stat().st_mtime_ns)):
            temporary = destination.with_name(
                f".{destination.name}.restore-{os.getpid()}")
            shutil.copy2(source, temporary)
            temporary.replace(destination)
        return destination

    def _memory_off(self, session_dir: Path, argv: list[str]) -> dict[str, str]:
        """Turn auto-memory off for this run; returns the env overlay (and
        appends `--settings` to `argv`).

        The env var is the one that matters -- it is checked before any settings
        lookup, so nothing can re-enable memory behind it. The settings file
        covers the same ground declaratively (and is worth keeping as the
        run's own record of what was disabled): reads and writes off, no
        background consolidation, the store itself moved inside `session/`, and
        `claudeMdExcludes` so a CLAUDE.md anywhere above the task dir is not
        loaded as project memory either.
        """
        if not self.no_memory:
            return {}
        settings = {
            "autoMemoryEnabled": False,
            "autoDreamEnabled": False,
            "autoMemoryDirectory": str(session_dir / "claude_memory"),
            "claudeMdExcludes": ["**/CLAUDE.md", "**/CLAUDE.local.md",
                                 "**/AGENTS.md"],
        }
        path = session_dir / "claude-settings.json"
        path.write_text(json.dumps(settings, indent=2))
        argv += ["--settings", str(path)]
        return {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                "CLAUDE_CODE_DISABLE_ORG_MEMORY": "1"}

    def run(self, prompt, ports, workdir, session_dir, resume=None) -> AgentRun:
        run = AgentRun(kind=self.kind, model=self.model)
        session_dir.mkdir(parents=True, exist_ok=True)

        # Same server specs as every other adapter, in claude's own file format.
        # --strict-mcp-config keeps the run from also inheriting whatever is in
        # the user's ~/.claude.json or a project .mcp.json, so the task only ever
        # sees this run's two Blenders.
        mcp_config = session_dir / "mcp-servers.json"
        mcp_config.write_text(json.dumps({"mcpServers": self.servers(ports)}, indent=2))

        # Choosing the session id up front is what makes the transcript findable
        # afterwards (claude names the file after it). On resume the id already
        # exists, and --resume replaces --session-id (they cannot both be given);
        # without --fork-session the resumed session keeps that same id, so the
        # transcript keeps growing in place across attempts.
        resume_id = (resume or {}).get("resume_target") or ""
        session_id = resume_id or str(uuid.uuid4())
        if resume_id and self._restore_transcript(
                resume_id, workdir, session_dir) is None:
            raise RuntimeError(
                f"cannot resume Claude session {resume_id}: task-owned "
                f"transcript is missing from {session_dir / 'claude_sessions'}"
            )
        argv = ["claude", "-p",
                "--output-format", "stream-json", "--verbose",
                "--mcp-config", str(mcp_config), "--strict-mcp-config",
                "--permission-mode", "bypassPermissions"]
        argv += ["--resume", resume_id] if resume_id else ["--session-id", session_id]
        if resume_id:
            run.resumed_from = resume_id
        if self.model:
            argv += ["--model", self.model]
        argv += ["--effort", self.thinking_depth]
        env = self._memory_off(session_dir, argv)

        turn = self._spawn(argv, cwd=workdir, env=env, label="prompt",
                           log=session_dir / "claude.session.jsonl",
                           stdin_text=prompt)
        run.turns.append(turn)
        run.session_ids.append(session_id)
        run.tool_calls += _anthropic_tool_calls(turn)
        turn.text = self._final_text(turn.log)
        run.exports.append(str(turn.log))
        run.exports += [str(p) for p in self._copy_transcripts(session_id, session_dir)]
        return self._record_usage(run, session_dir)

    # -- usage ------------------------------------------------------------
    def collect_usage(self, session_dir: Path, run: AgentRun) -> dict:
        """The transcript is the session total: a resumed run keeps its session
        id, so claude keeps appending to the same file, and the copy taken after
        the turn holds every attempt.

        One response streams as several `assistant` events sharing a `requestId`,
        each carrying the usage AS OF that event -- so the largest
        `output_tokens` per requestId is that response's final figure. Summed
        that way, the transcript reproduces the `result` event's totals exactly.
        """
        per_request: dict = {}
        transcripts, other = self._own_files(
            sorted(session_dir.glob("claude.transcript.*.jsonl")), run)
        for path in transcripts:
            for event in _json_lines(path):
                if event.get("type") != "assistant":
                    continue
                message = event.get("message") or {}
                usage = message.get("usage") or {}
                # requestId is per API call; message.id as a fallback.
                key = (path.name, event.get("requestId") or message.get("id"))
                if key[1] is None:
                    continue
                model = str(message.get("model") or "")
                if model == "<synthetic>":
                    # A message claude generated locally (a usage-limit notice,
                    # an API error) -- no request was made and its usage is 0.
                    continue
                previous = per_request.get(key)
                if previous is None or (usage.get("output_tokens") or 0) > \
                        (previous[1].get("output_tokens") or 0):
                    per_request[key] = (model, usage)

        result = _anthropic_result_event(run)
        # claude's own money and per-model figures -- for this invocation only.
        cost = result.get("total_cost_usd")
        notes = []
        if per_request:
            totals: dict = {}
            by_model: dict = {}
            for model, usage in per_request.values():
                _accumulate(totals, usage)
                entry = by_model.setdefault(model or "(unnamed)", {})
                entry["api_requests"] = entry.get("api_requests", 0) + 1
                _accumulate(entry, usage)
            for model, entry in by_model.items():
                by_model[model] = {
                    "api_requests": entry.get("api_requests"),
                    "input": (entry.get("input_tokens", 0)
                              + entry.get("cache_read_input_tokens", 0)),
                    "input_cached": entry.get("cache_read_input_tokens"),
                    "cache_write": entry.get("cache_creation_input_tokens"),
                    "output": entry.get("output_tokens"),
                }
            if result:
                notes.append("cost_usd is this attempt's, from the result event; "
                             "the token totals cover the whole session")
                side = sorted(set(result.get("modelUsage") or {})
                              - {m for m in by_model})
                if side:
                    notes.append("excludes claude's own side-model calls "
                                 f"(title generation etc.): {', '.join(side)}")
            else:
                notes.append("no result event (the CLI was cut short), so no "
                             "cost figure -- the transcript totals are complete")
            return _usage(f"{', '.join(p.name for p in transcripts)} "
                          f"(per-requestId usage)",
                          api_requests=len(per_request),
                          tokens=_tokens(
                              input_total=(totals.get("input_tokens", 0)
                                           + totals.get("cache_read_input_tokens", 0)),
                              input_cached=totals.get("cache_read_input_tokens"),
                              cache_write=totals.get("cache_creation_input_tokens"),
                              output=totals.get("output_tokens")),
                          by_model=by_model, cost_usd=cost,
                          rate_limit=self._rate_limit(run),
                          notes=notes + self._foreign_note(
                              other, "transcript(s)", session_dir))
        # No transcript copied (claude names it after the session id under
        # ~/.claude/projects/<cwd slug>): the stream's own result event holds
        # this invocation's totals.
        usage = result.get("usage") or {}
        if not usage:
            return _usage("", notes=["no claude transcript and no result event: "
                                     "nothing reported the usage"]
                          + self._foreign_note(other, "transcript(s)", session_dir))
        return _usage("claude.session.jsonl (result.usage)", scope="attempt",
                      api_requests=result.get("num_turns"),
                      tokens=_tokens(
                          input_total=(usage.get("input_tokens", 0)
                                       + usage.get("cache_read_input_tokens", 0)),
                          input_cached=usage.get("cache_read_input_tokens"),
                          cache_write=usage.get("cache_creation_input_tokens"),
                          output=usage.get("output_tokens")),
                      by_model=result.get("modelUsage"), cost_usd=cost,
                      rate_limit=self._rate_limit(run),
                      notes=["this run's transcript was not available: this "
                             "covers this attempt only, and api_requests is "
                             "claude's turn count rather than a request count"]
                      + self._foreign_note(other, "transcript(s)", session_dir))

    @staticmethod
    def _rate_limit(run: AgentRun) -> dict:
        """claude's last `rate_limit_event`: how much of the account's window
        this session had consumed when it ended."""
        found: dict = {}
        for turn in run.turns:
            if not turn.log:
                continue
            for event in _json_lines(turn.log):
                if event.get("type") == "rate_limit_event":
                    found = event.get("rate_limit_info") or found
        return found

    @staticmethod
    def _final_text(log: Path | None) -> str:
        # The terminal `result` event carries the final answer verbatim; fall
        # back to the last assistant text block if the run was cut short.
        result = ""
        for line in (log.read_text(errors="replace").splitlines()
                     if log and log.exists() else []):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "result" and isinstance(event.get("result"), str):
                result = event["result"]
        return result or _anthropic_final_text(log)

    @classmethod
    def _copy_transcripts(cls, session_id: str,
                          session_dir: Path) -> list[Path]:
        """Export and durably preserve Claude's active-login transcript."""
        copied: list[Path] = []
        root = cls._config_dir() / "projects"
        if not root.is_dir():
            return copied
        for src in root.rglob(f"{session_id}*.jsonl"):
            dst = session_dir / f"claude.transcript.{src.name}"
            shutil.copy2(src, dst)
            copied.append(dst)
            store = session_dir / "claude_sessions" / src.name
            store.parent.mkdir(parents=True, exist_ok=True)
            temporary = store.with_name(f".{store.name}.copy-{os.getpid()}")
            shutil.copy2(src, temporary)
            temporary.replace(store)
        return copied


class OpusAgent(ClaudeAgent):
    """Claude Code adapter exposed separately for the Opus 5.0 config."""

    kind = "opus"


# --------------------------------------------------------------------------
# kimi code (Kimi K3 on Moonshot's Kimi-for-Coding endpoint)
# --------------------------------------------------------------------------
class KimiAgent(BaseAgent):
    """Drives MoonshotAI's `kimi` CLI (Kimi Code) in its one-prompt mode.

    Everything a run needs lives in a per-task `KIMI_CODE_HOME`
    (`session/kimi_home/`), which is the CLI's whole state directory: config,
    MCP declarations, session store, skills. Nothing in `~/.kimi-code` is read.

    MODEL. `KIMI_MODEL_NAME` makes the CLI synthesize one provider plus one
    model alias from the environment and use it as the default model, so
    `[agent.kimi]`'s endpoint and key never touch config.toml -- the file the
    CLI would otherwise need the key written into (it does not read
    `MOONSHOT_API_KEY`, or any other shell variable, on its own; see
    `_api_key`). The endpoint is Kimi for Coding
    (https://api.kimi.com/coding), which speaks the Anthropic Messages API --
    hence `KIMI_MODEL_PROVIDER_TYPE=anthropic` -- and serves `k3` with a 1M
    context, images and video in, and low/high/max thinking efforts (which is
    why `THINKING_DEPTHS` is narrower here than the base class's).

    PERMISSIONS. `-p` and `--yolo`/`--auto` are mutually exclusive on this CLI,
    because prompt mode already forces the session into `auto` ("fully
    autonomous, the agent will not ask questions") for the duration of the turn
    and restores the previous mode afterwards. The generated config states the
    same thing anyway -- `default_permission_mode = "auto"` plus a blanket
    allow rule -- so the home is not silently interactive if the CLI is ever
    pointed at it by hand, and so a future prompt-mode change cannot leave MCP
    calls waiting on an approval no one is there to give.

    RESUME. `-S <session id>` (`-r` is the same option) continues the stored
    conversation and keeps its id, so a resumed attempt appends to the same
    session. The store is inside `session/kimi_home/`, i.e. it persists with the
    task dir -- that is what makes the resume possible at all, and why
    `kimi_home` is in `PERSISTENT_SESSION_ENTRIES`. The id itself is announced
    only in a final `session.resume_hint` event, which a killed turn never
    reaches, so `_session_id_from_store` reads it out of the CLI's own session
    index instead -- without that, a timed-out task could not be continued.

    MEMORY (`no_memory`). The scoped `KIMI_CODE_HOME` already covers most of it:
    the user-level `AGENTS.md`, the skills dir and the session store are all
    per-task and start empty. On top of that `--skills-dir` is pointed at an
    empty directory, which replaces user AND project skill discovery, and
    `KIMI_DISABLE_CRON=1` keeps a task from leaving scheduled work running past
    its own run.

    The one that needs a trick is AGENTS.md: this CLI walks from its cwd up to
    the nearest `.git` and reads every `AGENTS.md` on the way, with no config
    key, env var or flag to disable it -- so a task dir inside this repo would
    be handed the repo's own instructions (verified: it quotes them back).
    `_stop_agents_md_discovery` therefore drops an inert `.git` FILE in the task
    dir, which is where the walk then stops. Git itself is unbothered (a
    directory holding an unparsable `.git` file is still added by `git add -A`;
    only that file is skipped), and the task dir is run output either way.

    `~/.agents/AGENTS.md` is the one root left: it resolves against the real
    home, not `KIMI_CODE_HOME`, and there is no way to scope it. It does not
    exist on this machine; if it ever does, every kimi run would read it.
    """

    kind = "kimi"
    # `k3`'s own think_efforts, as the endpoint advertises them in /v1/models.
    THINKING_DEPTHS = ("low", "high", "max")

    def _api_key(self) -> str:
        """The Moonshot key, from the env var `[agent.kimi].api_key_env` names.

        Unlike qwen code, this CLI reads no `.env` and no shell variable of its
        own ("the CLI reads credentials only from here"), so the harness has to
        hand it the value. The repo-root `.env` is the fallback, which is where
        this key lives for the other project entry points.
        """
        name = self.cfg.get("api_key_env", "MOONSHOT_API_KEY")
        key = os.environ.get(name, "")
        if not key:
            key = _dotenv_value(self.repo / ".env", name)
        if not key:
            raise SystemExit(
                f"kimi: no API key -- ${name} is unset and {self.repo / '.env'} "
                f"does not define it")
        return key

    @staticmethod
    def _stop_agents_md_discovery(workdir: Path) -> None:
        """Make the task dir the end of kimi's `AGENTS.md` walk (see above).

        The walk stops at the first ancestor containing a `.git` entry, and it
        only tests for existence -- a plain file is enough, so nothing here has
        to look like a repository. Never overwrites a real one.
        """
        marker = workdir / ".git"
        if marker.exists():
            return
        marker.write_text(
            "# Not a git repository.\n"
            "# ActiveVisual writes this file for `--agent kimi` with memory\n"
            "# off: kimi code reads every AGENTS.md between its working\n"
            "# directory and the nearest .git above it, and has no switch to\n"
            "# turn that off, so the walk is stopped here instead.\n")

    def _write_home(self, ports: dict[str, int], session_dir: Path) -> Path:
        """The run's private KIMI_CODE_HOME: config.toml + mcp.json."""
        home = session_dir / "kimi_home"
        home.mkdir(parents=True, exist_ok=True)
        (home / "mcp.json").write_text(
            json.dumps({"mcpServers": self.servers(ports)}, indent=2))
        # MCP timeouts are set high on purpose: `uv run blender-mcp` may build
        # its environment on the first call of a fresh checkout, and a single
        # execute_blender_code can legitimately run for minutes.
        config = [
            '# written per task by ActiveVisual (agents.py KimiAgent)',
            'default_permission_mode = "auto"',
            'default_plan_mode = false',
            'telemetry = false',
            '',
            '[mcp]',
            'startup_timeout_ms = 120000',
            'tool_timeout_ms = 900000',
            '',
            '[[permission.rules]]',
            'decision = "allow"',
            'pattern = "*"',
        ]
        (home / "config.toml").write_text("\n".join(config) + "\n")
        return home

    def _env(self, home: Path) -> dict[str, str]:
        env = {
            "KIMI_CODE_HOME": str(home),
            # One provider + one model alias, synthesized from the environment
            # and made the default -- no key on disk. See the class docstring.
            "KIMI_MODEL_NAME": self.model,
            "KIMI_MODEL_API_KEY": self._api_key(),
            "KIMI_MODEL_PROVIDER_TYPE": self.cfg.get("provider_type", "anthropic"),
            "KIMI_MODEL_BASE_URL": self.cfg.get("base_url",
                                                "https://api.kimi.com/coding"),
            "KIMI_MODEL_MAX_CONTEXT_SIZE": str(self.cfg.get("context_window",
                                                            1048576)),
            "KIMI_MODEL_CAPABILITIES": ",".join(
                self.cfg.get("capabilities",
                             ["thinking", "tool_use", "image_in", "video_in"])),
            "KIMI_MODEL_THINKING_EFFORT": self.thinking_depth,
            # The pixi package pins the version; a self-upgrade mid-harness
            # would silently swap the CLI under a resumable task.
            "KIMI_CODE_NO_AUTO_UPDATE": "1",
            "KIMI_DISABLE_TELEMETRY": "1",
        }
        if self.no_memory:
            env["KIMI_DISABLE_CRON"] = "1"
        return env

    def run(self, prompt, ports, workdir, session_dir, resume=None) -> AgentRun:
        run = AgentRun(kind=self.kind, model=self.model)
        session_dir.mkdir(parents=True, exist_ok=True)
        home = self._write_home(ports, session_dir)

        argv = ["kimi", "--output-format", "stream-json"]
        if self.no_memory:
            # Replaces user AND project skill discovery with a directory this
            # run owns and nothing ever writes to.
            skills = session_dir / "kimi_skills"
            skills.mkdir(parents=True, exist_ok=True)
            argv += ["--skills-dir", str(skills)]
            self._stop_agents_md_discovery(workdir)
        resume_id = (resume or {}).get("resume_target") or ""
        if resume_id:
            # Kimi's native id currently already includes ``session_``. Keep
            # accepting an unprefixed legacy id without ever selecting a
            # different conversation.
            dirname = (resume_id if resume_id.startswith("session_")
                       else f"session_{resume_id}")
            stored = list(home.rglob(dirname))
            if not any(path.is_dir() for path in stored):
                raise RuntimeError(
                    f"cannot resume Kimi session {resume_id}: task-owned "
                    f"session is missing from {home / 'sessions'}")
            argv += ["-S", resume_id]
            run.resumed_from = resume_id
        turn = self._spawn(argv + ["-p", prompt], cwd=workdir,
                           env=self._env(home), label="prompt",
                           log=session_dir / "kimi.session.jsonl")
        run.turns.append(turn)
        session_id = self._session_id(turn.log) or \
            self._session_id_from_store(home, workdir) or resume_id
        if session_id:
            run.session_ids.append(session_id)

        run.tool_calls += self._tool_calls(turn)
        turn.text = self._final_text(turn.log)
        run.exports.append(str(turn.log))
        # The CLI's own session record lives under KIMI_CODE_HOME, which is
        # inside session_dir already -- nothing to copy.
        return self._record_usage(run, session_dir)

    # -- usage ------------------------------------------------------------
    def collect_usage(self, session_dir: Path, run: AgentRun) -> dict:
        """kimi's stream-json carries no usage at all, but its session store
        does: `wire.jsonl` logs an `llm.request` per API call and a
        `usage.record` per completed step. A resumed run appends to the same
        session, so this is the session total.

        The store is laid out `sessions/<workdir>/<session id>/agents/<agent
        id>/wire.jsonl`, so the session id in the path is what scopes this to
        THIS conversation -- one KIMI_CODE_HOME holds every session ever opened
        in this task dir. Every agent under that session id is counted: a spawned
        sub-agent's requests are part of what the session cost."""
        totals: dict = {}
        by_model: dict = {}
        requests, steps, scopes = 0, 0, set()
        wires, other = self._own_files(
            sorted((session_dir / "kimi_home").rglob("wire.jsonl")), run)
        for path in wires:
            for event in _json_lines(path):
                kind = event.get("type")
                if kind == "llm.request":
                    requests += 1
                    continue
                if kind != "usage.record":
                    continue
                scope = event.get("usageScope")
                scopes.add(str(scope))
                if scope not in (None, "turn"):
                    continue          # a roll-up of records already counted
                steps += 1
                usage = event.get("usage") or {}
                _accumulate(totals, usage)
                # The model id is a placeholder when the endpoint comes from the
                # KIMI_MODEL_* env vars this adapter sets, so name the configured
                # model instead.
                name = str(event.get("model") or "")
                if name.startswith("__") or not name:
                    name = self.model or name or "(unnamed)"
                _accumulate(by_model.setdefault(name, {}), usage)
        if not wires or not steps:
            return _usage("", notes=["no kimi wire.jsonl with usage records for "
                                     "this session: nothing reported the usage"]
                          + self._foreign_note(other, "wire.jsonl file(s)", session_dir))
        for name, entry in by_model.items():
            by_model[name] = {
                "input": entry.get("inputOther", 0) + entry.get("inputCacheRead", 0),
                "input_cached": entry.get("inputCacheRead"),
                "cache_write": entry.get("inputCacheCreation"),
                "output": entry.get("output"),
            }
        notes = [f"{steps} usage record(s) against {requests} API request(s); "
                 "a request without a record is one that failed or was retried"]
        if scopes - {"turn", "None"}:
            notes.append(f"ignored non-turn usage scopes: "
                         f"{', '.join(sorted(scopes - {'turn', 'None'}))}")
        notes += self._foreign_note(other, "wire.jsonl file(s)", session_dir)
        return _usage(f"kimi_home/**/wire.jsonl (usage.record, llm.request) "
                      f"-- {len(wires)} agent(s)",
                      api_requests=requests,
                      tokens=_tokens(
                          input_total=(totals.get("inputOther", 0)
                                       + totals.get("inputCacheRead", 0)),
                          input_cached=totals.get("inputCacheRead"),
                          cache_write=totals.get("inputCacheCreation"),
                          output=totals.get("output")),
                      by_model=by_model, notes=notes)

    # -- kimi stream-json parsing ----------------------------------------
    # One JSON object per line, in the OpenAI chat shape rather than the
    # Anthropic one qwen/claude emit: assistant messages carry either `content`
    # or `tool_calls`, results come back as `role: "tool"`, and a final
    # `role: "meta"` event carries the session id. Tool output is ALSO written
    # to stdout as plain text, so lines that are not JSON objects are skipped.
    @staticmethod
    def _events(log: Path | None):
        if not log or not log.exists():
            return
        for line in log.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and "role" in event:
                yield event

    def _tool_calls(self, turn: TurnResult) -> list[dict]:
        calls = []
        for event in self._events(turn.log):
            for call in event.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") or {}
                name = str(function.get("name") or call.get("name") or "")
                if not name:
                    continue
                server = None
                if name.startswith("mcp__") and "__" in name[5:]:
                    server, _, name = name[5:].partition("__")
                calls.append(_normalise_call(
                    turn.label, server, name,
                    function.get("arguments") or call.get("arguments"),
                    str(call.get("type") or "function")))
        return calls

    @staticmethod
    def _events(log: Path | None):
        if not log or not log.exists():
            return
        for line in log.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and "role" in event:
                yield event

    @classmethod
    def _final_text(cls, log: Path | None) -> str:
        text = ""
        for event in cls._events(log):
            if event.get("role") == "assistant" and \
                    isinstance(event.get("content"), str):
                text = event["content"]
        return text

    @classmethod
    def _session_id(cls, log: Path | None) -> str:
        """From the terminal `session.resume_hint` event -- the id `-S` takes.
        A resumed run reports the same id it was given, which is what keeps the
        checkpoint pointing at one growing conversation."""
        found = ""
        for event in cls._events(log):
            candidate = event.get("session_id")
            if isinstance(candidate, str) and candidate:
                found = candidate
        return found

    @staticmethod
    def _session_id_from_store(home: Path, workdir: Path) -> str:
        """The id of the newest session this KIMI_CODE_HOME recorded for
        `workdir`, read from the CLI's own `session_index.jsonl`.

        The stream-json event above only arrives when the turn ENDS, so a run
        killed at [run].timeout_sec -- exactly the case a checkpoint exists
        for -- would otherwise leave nothing to resume, even though the session
        is on disk and complete up to the kill. The index is appended to when a
        session is created, so the last matching line is this attempt's; the
        home is per-task, so a match can only be this task's own.
        """
        index = home / "session_index.jsonl"
        try:
            lines = index.read_text(errors="replace").splitlines()
        except OSError:
            return ""
        found = ""
        for line in lines:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            session_id = entry.get("sessionId")
            if not isinstance(session_id, str) or not session_id:
                continue
            if entry.get("workDir") not in (None, str(workdir)):
                continue
            found = session_id
        return found


class MiniMaxM3Agent(MmxAgent):
    """Official MiniMax-M3 endpoint via the installed ``mmx`` adapter."""

    kind = "minimax-m3"


# --------------------------------------------------------------------------
# agy / antigravity (default: Gemini 3.1 Pro), driven through pyagy
# --------------------------------------------------------------------------
class AgyAgent(BaseAgent):
    """agy / antigravity, driven as a CONVERSATIONAL CLI session.

    This uses `pyagy.Session`, which launches ONE live `agy --prompt-interactive`
    process under a PTY and types each turn into its TUI -- not `agy --print`.
    So the run is a real dialogue: turn 1 is the composed task input and every
    entry in `[agent.agy] followups` is typed into the SAME live session, with
    agy's full in-process context (print mode would need a `--conversation=<id>`
    resume per turn and re-answer the terminal/trust prompts each time).

    agy DOES support MCP (verified 2026-07-27: both Blender servers spawned,
    tools discovered and listed by the model). Getting there needs three things
    that only bite under this adapter's setup, all handled below:

      * `/usr/bin/env -u PYTHONHOME` in front of every server command.
        pyagy runs agy with PYTHONHOME pointed at the pixi env (its shim's
        embedded interpreter needs it) and agy hands its own environment to the
        MCP servers it spawns. `uv run ... blender-mcp` then boots
        `.venv/bin/python` against the pixi stdlib and dies before speaking a
        word of JSON-RPC -- silently, since agy logs nothing about MCP at all.
        This was the whole "agy has no MCP tools" story.
      * a seeded `cache/onboarding.json` in the scoped home. A fresh
        `$HOME/.gemini` makes agy open its first-run wizard (colour scheme +
        ToS); pyagy answers the terminal-capability and folder-trust prompts
        but not that one, so the session hangs there and the task input is
        never delivered (agy's log has no HandleUserInput line at all).
      * NO `--effort` flag. The TaskSolver-provided pyagy build uses its pinned
        build-id-matched agy binary, whose supported version has no such flag; it
        prints its usage and exits. `thinking_depth` rides on the model label
        instead ("Gemini 3.1 Pro (High)"), which is how agy names models here.

    Also note 1.1.7 (`agy --print`) is unusable on this machine anyway: its
    print mode makes the account-eligibility check fatal and it fails with
    "not currently available in your location". 1.0.16 only warns."""

    kind = "agy"
    THINKING_DEPTHS = ("low", "medium", "high")

    # pyagy's PYTHONHOME must not reach the MCP servers agy spawns (see above).
    @staticmethod
    def _unset_pythonhome(servers: dict[str, dict]) -> dict[str, dict]:
        return {
            name: {**spec,
                   "command": "/usr/bin/env",
                   "args": ["-u", "PYTHONHOME", spec["command"], *spec["args"]]}
            for name, spec in servers.items()
        }

    # agy's first-run wizard is not one of the prompts pyagy auto-answers, so a
    # scoped home has to look already-onboarded before agy starts.
    @staticmethod
    def _seed_onboarding(private_home: Path) -> None:
        cache = private_home / ".gemini" / "antigravity-cli" / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        marker = cache / "onboarding.json"
        if not marker.exists():
            marker.write_text(json.dumps({"consumerOnboardingComplete": True,
                                          "enterpriseOnboardingComplete": False,
                                          "onboardingComplete": True}, indent=2))

    def run(self, prompt, ports, workdir, session_dir, resume=None) -> AgentRun:
        run = AgentRun(kind=self.kind, model=self.model)
        session_dir.mkdir(parents=True, exist_ok=True)

        # Give this process a private HOME. This is also what makes agy
        # memory-isolated for free (`no_memory` needs nothing extra here): its
        # whole store -- brain/, knowledge/, conversations/ -- hangs off
        # $HOME/.gemini, so it starts empty and dies with the task. agy hardcodes
        # $HOME/.gemini/config/mcp_config.json, while pyagy's data_dir scoping
        # seeds the private home with symlinks to the login credentials. Creating
        # config/ before Session starts prevents pyagy from linking the user's
        # global config into this run, so no ordinary agy process can observe
        # these task-local Blender servers.
        private_home = session_dir / "agy_home"
        config_path = private_home / ".gemini" / "config" / "mcp_config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        doc = {"mcpServers": self._unset_pythonhome(self.servers(ports))}
        config_path.write_text(json.dumps(doc, indent=2))
        self._seed_onboarding(private_home)
        (session_dir / "mcp_config.json").write_text(json.dumps(doc, indent=2))
        resume_id = (resume or {}).get("resume_target") or ""
        if resume_id:
            conversation = (private_home / ".gemini" / "antigravity-cli" /
                            "conversations" / f"{resume_id}.db")
            if not conversation.is_file() or conversation.stat().st_size == 0:
                raise RuntimeError(
                    f"cannot resume Agy session {resume_id}: task-owned "
                    f"conversation is missing from {conversation}")
        # Import the optional runtime only after validating the saved session.
        import pyagy

        return self._converse(pyagy, run, prompt, workdir, session_dir,
                              private_home,
                              resume_id=resume_id)

    def _converse(self, pyagy, run: AgentRun, prompt: str,
                  workdir: Path, session_dir: Path,
                  private_home: Path, resume_id: str = "") -> AgentRun:
        """One live interactive agy, driven turn by turn.

        `[agent.agy] followups` are extra messages typed into the same session
        after the task input -- e.g. ["continue", "are you done?"]. Empty by
        default, so a plain run is the single turn every other adapter sends.

        `resume_id` starts the live session on agy's own stored conversation
        instead of a new one. It resolves inside `private_home`, which is where
        the interrupted attempt's conversation was written -- that directory
        persisting with the task dir is what makes the resume possible.
        """
        followups = [str(f) for f in (self.cfg.get("followups") or [])]
        messages = [("prompt", prompt)]
        messages += [(f"followup{i}", text) for i, text in enumerate(followups, 1)]

        if resume_id:
            run.resumed_from = resume_id
        session = pyagy.Session(
            workspace=str(workdir), model=self.model or None,
            timeout=self.timeout, idle=float(self.cfg.get("idle_sec", 25)),
            skip_permissions=True, agy_bin=self.cfg.get("agy_bin") or None,
            data_dir=str(private_home), conversation_id=resume_id or None)
            # no --effort: pyagy's pinned agy 1.0.16 rejects it (see the class
            # docstring). thinking_depth rides on the model label instead.
        pty_text, conversation_id, transcript = "", None, []
        try:
            for label, text in messages:
                started = time.monotonic()
                log = session_dir / f"agy.{label}.response.json"
                timed_out = False
                try:
                    reply = session.ask(text)
                    # `primary` can be a structured payload, not a string.
                    rc, final = 0, _as_text(reply.primary or reply.app_text)
                    pty_text = reply.transcript or pty_text
                    log.write_text(json.dumps(_jsonable(reply.json), indent=2,
                                              default=str))
                except Exception as exc:                      # noqa: BLE001
                    rc, final = 1, f"{type(exc).__name__}: {exc}"
                    # pyagy signals "the TUI never finished this turn" as an
                    # exception; treat it the same as a killed print-mode CLI so
                    # the driver checkpoints instead of just failing.
                    timed_out = "timeout" in f"{type(exc).__name__} {exc}".lower()
                run.turns.append(TurnResult(label=label, returncode=rc,
                                            seconds=time.monotonic() - started,
                                            log=log, text=final,
                                            timed_out=timed_out))
                run.exports.append(str(log))
                if rc != 0:
                    break                 # the session is gone; later turns can't land

            conversation_id = session.conversation_id
            # Read the stored transcript while the session still knows its
            # scoped home (Session.history() resolves both for us).
            transcript = session.history() if conversation_id else []
        finally:
            # agy is a TUI: when a turn produces nothing decodable, the PTY
            # transcript is the only record of what it actually did. Nothing in
            # here may raise -- this runs while an exception may be in flight,
            # and masking it would hide the actual failure.
            if not pty_text:
                try:
                    live = getattr(session, "_agy", None)
                    pty_text = (live.transcript if live is not None else "") or ""
                except Exception:                             # noqa: BLE001
                    pty_text = ""
            if pty_text:
                try:
                    path = session_dir / "agy.pty.log"
                    path.write_text(pty_text)
                    run.exports.append(str(path))
                except OSError:
                    pass
            session.close()

        if conversation_id:
            run.session_ids.append(conversation_id)
            path = session_dir / "agy.transcript.json"
            path.write_text(json.dumps(_jsonable(transcript), indent=2, default=str))
            run.exports.append(str(path))
            run.tool_calls = self._tool_calls(transcript)
        return self._record_usage(run, session_dir)

    # -- usage ------------------------------------------------------------
    def collect_usage(self, session_dir: Path, run: AgentRun) -> dict:
        """antigravity reports no token counts: not in the conversation store it
        writes (`agy.transcript.json`, `conversations/<id>.db`), not in its
        cli.log -- the only "token" there is the auth token. What the store does
        show is one `PLANNER_RESPONSE` step per model response, and it is written
        per conversation, so a resumed session's steps are all in it."""
        path = session_dir / "agy.transcript.json"
        try:
            transcript = json.loads(path.read_text(errors="replace"))
        except (OSError, ValueError):
            transcript = None
        if not isinstance(transcript, list):
            return _usage("", notes=["antigravity reports no token counts, and "
                                     "no transcript was written to count "
                                     "planner responses in"])
        responses = sum(1 for step in transcript
                        if isinstance(step, dict)
                        and str(step.get("type")) == "PLANNER_RESPONSE")
        return _usage(f"{path.name} (PLANNER_RESPONSE steps)",
                      api_requests=responses,
                      notes=["antigravity reports no token counts anywhere in "
                             "its store, so the token fields stay unknown",
                             "api_requests is the number of planner responses "
                             "in agy's own transcript, not a counter it keeps"])

    # Narration, not actions. These must not be counted as calls at all --
    # CHECKPOINT especially, because agy's truncation summaries quote our own
    # instruction text back (server names included), which used to be attributed
    # as a phantom MCP call and silenced the driver's "no MCP tool calls" warning
    # on a run that in fact made none.
    NON_ACTION_STEPS = frozenset({
        "PLANNER_RESPONSE", "USER_INPUT", "GENERIC", "CHECKPOINT",
        "CONVERSATION_HISTORY", "EPHEMERAL_MESSAGE", "None", "",
    })

    def _tool_calls(self, transcript) -> list[dict]:
        """agy's transcript records one step per action, typed (RUN_COMMAND,
        VIEW_FILE, MCP_TOOL, ...) rather than as a nested tool-call object, so
        every non-conversational step counts as a call. Keeping RUN_COMMAND et al
        in the list is deliberate: an agy run that answers by shelling out
        instead of using the Blender MCP servers is exactly the failure this
        harness needs to make visible.

        A Blender server is only ever attributed to a step that actually invokes
        a tool -- matching the name anywhere in any step's text is what produced
        the phantom calls described above.

        An `MCP_TOOL` step stores only the call's *result* ("tool call
        completed", or the tool's output), never the server or tool name, so
        `server` usually stays None even for a real Blender call. It is still
        flagged `mcp`: this run's mcp_config.json holds nothing but the two
        Blender servers, so an MCP call is a Blender call by construction. That
        flag is what the driver counts -- without it every agy run looked like it
        had reached neither Blender."""
        calls = []
        for step in transcript or []:
            if not isinstance(step, dict):
                continue
            step_type = str(step.get("type") or "")
            if step_type in self.NON_ACTION_STEPS:
                continue
            content = str(step.get("content") or "")
            server, is_mcp = None, False
            if "MCP" in step_type or "TOOL" in step_type:
                is_mcp = True
                for name in (self.full_name, self.viewport_name):
                    if name in content:
                        server = name
                        break
            calls.append(_normalise_call(
                "session", server, step_type, content[:400], step_type,
                mcp=is_mcp))
        return calls


AGENTS = {
    a.kind: a
    for a in (CodexAgent, QwenTokenPlanAgent, MiniMaxM3Agent,
              ClaudeAgent, OpusAgent, AgyAgent, KimiAgent)
}


def build(kind: str, cfg: dict, repo: Path, timeout: int) -> BaseAgent:
    try:
        return AGENTS[kind](cfg, repo, timeout)
    except KeyError:
        raise SystemExit(
            f"unknown agent kind {kind!r}; choose from {sorted(AGENTS)}") from None


# --------------------------------------------------------------------------
# why did this run stop?
# --------------------------------------------------------------------------
def classify(run: AgentRun, extra_patterns=()) -> AgentRun:
    """Fill in `run.limit_reason` (and `limit_hit`) from the CLI's own output.

    `limit_reason` is one of "" (finished normally), "usage_limit", "timeout" or
    "nonzero_exit". Only "usage_limit" sets `limit_hit`.

    A normal run that exited 0 is never classified from broad transcript text.
    That is not just an optimisation: every one of these CLIs echoes the
    message it was sent back into its own event stream, so a phrase like
    "usage limit" in a prompt would otherwise match itself. Provider API-error
    diagnostics are handled separately because Qwen can report a terminal 429
    while its print-mode process remains alive until the harness stops it.
    """
    if run.ok or run.limit_reason:
        return run
    # Provider diagnostics are more precise than broad quota patterns in a
    # multi-turn transcript (which may contain old prompt/tool text mentioning
    # 429 or limits). Only the final unrecovered turn determines this run.
    final = next((turn for turn in reversed(run.turns)
                  if not turn.recovered), None)
    if final and final.api_error:
        evidence = usage_limit_evidence(final.api_error, extra_patterns)
        if evidence:
            run.limit_hit = True
            run.limit_reason = "usage_limit"
            run.limit_evidence = final.api_error
            return run
        run.limit_reason = ("api_timeout" if QwenAgent._is_api_timeout(
            final.api_error) else "api_error")
        run.limit_evidence = final.api_error
        return run
    for turn in run.turns:
        sources = [turn.text]
        if turn.log:
            sources.append(_tail(Path(turn.log)))
        for text in sources:
            evidence = usage_limit_evidence(text, extra_patterns)
            if evidence:
                run.limit_hit = True
                run.limit_reason = "usage_limit"
                run.limit_evidence = evidence
                return run
    run.limit_reason = "timeout" if run.timed_out else "nonzero_exit"
    return run


def _tail(path: Path, max_bytes: int = 8 << 20) -> str:
    """The end of a log, bounded -- a long agent run's event stream is large and
    the interesting last words are at the end of it."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
            return handle.read().decode(errors="replace")
    except OSError:
        return ""


def _first_match(patterns, text: str) -> str:
    """The first match with a little context around it, kept verbatim in the
    checkpoint so a misclassification can be traced to what actually matched.
    Sliced rather than line-based: these logs put a whole JSON event on one line."""
    if not text:
        return ""
    for pattern in patterns:
        found = pattern.search(text)
        if found:
            start = max(0, found.start() - 120)
            return text[start:found.end() + 120].strip()
    return ""


# --------------------------------------------------------------------------
# small shared helpers
# --------------------------------------------------------------------------
def _walk_dicts(node):
    """Every dict inside an arbitrarily nested JSON structure."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_dicts(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_dicts(value)


def _deep_get(node, key):
    for candidate in _walk_dicts(node):
        if key in candidate:
            return candidate[key]
    return None


def _normalise_call(turn: str, server, tool: str, arguments, raw_type: str,
                    mcp: bool | None = None) -> dict:
    """`server` is the MCP server a call went to, or None when it went to a
    built-in tool -- OR when the CLI records the call without naming its server,
    which is agy's case (see `AgyAgent._tool_calls`). `mcp` is what the driver
    counts, so it defaults to "we know the server" and is set explicitly where
    the call is known to be MCP but unattributable."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            pass
    call = {
        "turn": turn,
        "server": server,
        "mcp": bool(server) if mcp is None else mcp,
        "tool": tool,
        "type": raw_type,
        "arguments": _clip(arguments),
    }
    return call


def _clip(value, limit: int = 2000):
    text = json.dumps(value, default=str) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[:limit] + f"...<+{len(text) - limit}>"


def _dedupe(calls: list[dict]) -> list[dict]:
    seen, out = set(), []
    for call in calls:
        key = (call["turn"], call["server"], call["tool"], call["arguments"])
        if key not in seen:
            seen.add(key)
            out.append(call)
    return out


def _dotenv_value(path: Path, name: str) -> str:
    """`NAME=value` out of a .env file, or "". Only what KimiAgent needs: no
    interpolation, no `export ` prefix handling beyond stripping it, quotes
    removed."""
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if sep and key.strip() == name:
            return value.strip().strip("'\"")
    return ""


def _as_text(value) -> str:
    """Best-effort human text out of whatever a client returns."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "content", "message", "output"):
            if isinstance(value.get(key), str):
                return value[key]
    return json.dumps(_jsonable(value), default=str)[:8000]


def _jsonable(value):
    """json.dumps-safe view of pyagy's dataclass-ish payloads."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "__dict__"):
        return _jsonable(vars(value))
    return str(value)
