"""Resume state for one task: what survives a CLI agent that cannot continue.

Both drivers (`ActiveVisual.py` and `Full3DInteraction.py`) use this.
The problem it solves: a CLI agent hits its account's usage limit -- or the
harness timeout -- half way through a task. The agent's *conversation* is still
alive in its own CLI's session store, and Blender still holds everything it
built, but both die with the run. Re-running the same command would then start
the task from zero.

So a run that ends that way writes a checkpoint next to its other artifacts:

    <task_dir>/checkpoint/
        checkpoint.json         state below: reason, attempt count, session ids
        full_access.blend       every Blender in the run, saved as it stood
        viewport_only.blend     (official harness: one `scene.blend` instead)

and the next invocation of the same command picks it up: each Blender OPENS its
checkpoint .blend instead of starting from the factory scene/reference import,
and the CLI is resumed into the SAME session (`codex exec resume <id>`,
`claude --resume <id>`, `qwen -r <id>`, `kimi -S <id>`,
`pyagy.Session(conversation_id=...)`)
rather than being handed the task from the top. See `agents.py` for the
per-CLI resume wiring and for how a usage limit is recognised.

A checkpoint is only reused when the run being resumed is the same piece of
work: same task, same agent kind, same model, and byte-identical inputs
(`inputs_digest`). Change the prompt or switch models and the checkpoint is
stale by definition -- the driver says so and starts fresh instead of silently
continuing a conversation about a different task.

Nothing is ever overwritten in place: before a resume attempt starts, the
previous attempt's session export, logs, run record and .blend are moved to
`<task_dir>/attempts/attempt-<N>/`, so a task interrupted three times leaves
three complete, separately inspectable runs plus the live one.

The mirror image of a resume is `already_done` at the bottom of this file: a
task whose output is already there AND complete AND not waiting to be resumed is
skipped by the next run instead of being redone from the start. A sweep over
every task/agent can then simply be re-issued after any interruption -- it picks
up the checkpointed tasks, leaves the finished ones alone, and runs the rest.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

VERSION = 1
DIR_NAME = "checkpoint"
STATE_NAME = "checkpoint.json"
ATTEMPTS_DIR = "attempts"

# States. Only RESUMABLE is picked up by the next run; the others exist so the
# file is a readable record of what happened rather than being deleted.
RESUMABLE = "resumable"
COMPLETED = "completed"
EXHAUSTED = "exhausted"
OUT_OF_LIMIT = "out_of_limit"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def checkpoint_dir(task_dir: Path) -> Path:
    return Path(task_dir) / DIR_NAME


def state_path(task_dir: Path) -> Path:
    return checkpoint_dir(task_dir) / STATE_NAME


# --------------------------------------------------------------------------
# input identity
# --------------------------------------------------------------------------
def inputs_digest(paths) -> str:
    """A digest of the task's actual inputs (prompt, reference, ...).

    Content, not mtime: staging copies inputs into the task dir on every run,
    so timestamps always differ while the work is unchanged.
    """
    digest = hashlib.sha256()
    for path in paths:
        if path is None:
            continue
        path = Path(path)
        digest.update(path.name.encode())
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def resume_input_digest(task_dir: Path, current_digest: str, *,
                        allow_prompt_changes: bool,
                        reference_matches: bool) -> str:
    """Choose the digest used only for checkpoint compatibility checks.

    A graded pool may explicitly allow an interrupted conversation to receive
    an updated prompt/skill on resume.  Reuse the checkpoint's saved digest
    only after the caller independently verified that the staged reference GLB
    still matches the current one.  Commits continue to store the new full
    digest, so this does not hide the update from subsequent attempts.
    """
    if not allow_prompt_changes or not reference_matches:
        return current_digest
    state = load(task_dir)
    if not state or state.get("state") != RESUMABLE:
        return current_digest
    saved_digest = state.get("inputs_digest")
    return saved_digest if isinstance(saved_digest, str) and saved_digest \
        else current_digest


# --------------------------------------------------------------------------
# read / write
# --------------------------------------------------------------------------
def load(task_dir: Path) -> dict | None:
    """The task's checkpoint, or None when there is none / it is unreadable.

    A corrupt checkpoint must not be able to abort a run: the caller then just
    starts the task fresh, which is the same thing that happens with no
    checkpoint at all.
    """
    path = state_path(task_dir)
    if not path.is_file():
        return None
    try:
        state = json.loads(path.read_text())
    except (ValueError, OSError):
        return None
    return state if isinstance(state, dict) else None


def save(task_dir: Path, state: dict) -> Path:
    directory = checkpoint_dir(task_dir)
    directory.mkdir(parents=True, exist_ok=True)
    state = {**state, "version": VERSION, "updated_utc": now()}
    path = directory / STATE_NAME
    path.write_text(json.dumps(state, indent=2))
    return path


def mark(task_dir: Path, new_state: str, **extra) -> Path | None:
    """Move an existing checkpoint to `new_state` (e.g. COMPLETED). No-op
    without one -- a task that never needed resuming leaves no file behind."""
    state = load(task_dir)
    if state is None:
        return None
    state.update(extra)
    state["state"] = new_state
    return save(task_dir, state)


def mark_out_of_limit(task_dir: Path, *, reason: str, evidence: str) -> Path:
    """Persist a terminal token-limit outcome, with or without a checkpoint.

    Unlike ``mark()``, this also records a first-attempt task that never had a
    checkpoint.  It is intentionally non-resumable: the next sweep skips the
    matching run record unless the caller explicitly passes ``--rerun`` or
    ``--fresh``.
    """
    state = load(task_dir) or {}
    state.update({"state": OUT_OF_LIMIT, "reason": reason,
                  "evidence": evidence[:400], "finished_utc": now()})
    return save(task_dir, state)


def out_of_limit_skip_reason(task_dir: Path, *, record: str) -> str:
    """Return the terminal token-limit reason recorded by a task, if any."""
    path = Path(task_dir) / record
    try:
        outcome = json.loads(path.read_text())
    except (ValueError, OSError):
        return ""
    if isinstance(outcome, dict) and outcome.get("out_of_limit"):
        return f"{record} says out_of_limit=true ({outcome.get('out_of_limit_reason', 'token total limit reached')})"
    return ""


# --------------------------------------------------------------------------
# is this checkpoint usable for the run we are about to do?
# --------------------------------------------------------------------------
def usable(state: dict | None, *, kind: str, model: str, digest: str,
           max_attempts: int) -> tuple[bool, str]:
    """(usable, reason). `reason` explains a refusal, in one line, for printing.

    `attempts` is a hard stop rather than an advisory: an agent whose account
    limit is genuinely exhausted would otherwise be re-launched forever by a
    scripted caller, one full Blender stack per round.
    """
    if state is None:
        return False, "no checkpoint"
    if state.get("state") != RESUMABLE:
        return False, f"checkpoint state is {state.get('state')!r}"
    if int(state.get("version", 0)) != VERSION:
        return False, (f"checkpoint version {state.get('version')} != {VERSION}"
                       " (written by an older harness)")
    session = state.get("session") or {}
    if session.get("kind") != kind:
        return False, (f"checkpoint belongs to agent {session.get('kind')!r}, "
                       f"this run is {kind!r}")
    if model and session.get("model") and session["model"] != model:
        return False, (f"checkpoint used model {session['model']!r}, "
                       f"this run uses {model!r}")
    if not session.get("resume_target"):
        return False, "checkpoint has no resumable session id"
    if state.get("inputs_digest") and digest and state["inputs_digest"] != digest:
        return False, "task inputs changed since the checkpoint was written"
    attempts = int(state.get("attempts", 0))
    if max_attempts and attempts >= max_attempts:
        return False, (f"already resumed {attempts} time(s), at the "
                       f"max_attempts limit of {max_attempts}")
    return True, f"resuming attempt {attempts + 1}"


def blend_paths(task_dir: Path, state: dict) -> dict[str, Path]:
    """Checkpoint .blend files that still exist, by role, resolved absolutely."""
    found: dict[str, Path] = {}
    for role, value in (state.get("blends") or {}).items():
        path = Path(value)
        if not path.is_absolute():
            path = Path(task_dir) / path
        if path.is_file():
            found[role] = path
    return found


# --------------------------------------------------------------------------
# per-attempt archiving
# --------------------------------------------------------------------------
def rotate(task_dir: Path, attempt: int, *, keep: tuple[str, ...] = (),
           subdirs: tuple[str, ...] = ("session", "logs"),
           files: tuple[str, ...] = ()) -> Path:
    """Move the finished attempt's artifacts into `attempts/attempt-<N>/`.

    Called at the START of a resume attempt, because the file names a CLI
    adapter writes are fixed (`claude.session.jsonl`, ...) and the new attempt
    would otherwise overwrite the evidence of the one that hit the limit.

    `keep` names entries inside those subdirs that must stay where they are:
    the CLI's own persistent session store lives there (`codex_home`,
    `qwen_home`, `kimi_home`, `agy_home`), and moving it away is exactly what
    would make the resume impossible.
    """
    task_dir = Path(task_dir)
    destination = task_dir / ATTEMPTS_DIR / f"attempt-{attempt}"
    destination.mkdir(parents=True, exist_ok=True)
    for name in subdirs:
        source = task_dir / name
        if not source.is_dir():
            continue
        target = destination / name
        target.mkdir(parents=True, exist_ok=True)
        for entry in sorted(source.iterdir()):
            if entry.name in keep:
                continue
            moved = target / entry.name
            if moved.exists():
                if moved.is_dir():
                    shutil.rmtree(moved)
                else:
                    moved.unlink()
            shutil.move(str(entry), str(moved))
    for name in files:
        source = task_dir / name
        if source.is_file():
            shutil.move(str(source), str(destination / name))
    return destination


def settings(cfg: dict, *, enabled: bool | None = None,
             max_attempts: int | None = None) -> dict:
    """The `[resume]` section with CLI overrides applied. No section = on."""
    section = cfg.get("resume") or {}
    resolved = {
        "enabled": bool(section.get("enabled", True)),
        # A killed CLI is as resumable as an out-of-quota one: Blender is still
        # up and the conversation is still in the CLI's own store.
        "on_timeout": bool(section.get("on_timeout", True)),
        # Any other non-zero exit is usually a real failure, and resuming it
        # would just repeat it -- so it is not checkpointed unless asked for.
        "on_error": bool(section.get("on_error", False)),
        "max_attempts": int(section.get("max_attempts", 10)),
        "repeat_prompt": bool(section.get("repeat_prompt", True)),
        # Graded runners intentionally end the current invocation after writing
        # a checkpoint. A later identical command resumes the stored session.
        "defer_resume_to_next_run": bool(
            section.get("defer_resume_to_next_run", False)),
        "extra_limit_patterns": [str(p) for p in
                                 (section.get("extra_limit_patterns") or [])],
    }
    if enabled is not None:
        resolved["enabled"] = enabled
    if max_attempts:
        resolved["max_attempts"] = max_attempts
    return resolved


def decide(task_dir: Path, *, kind: str, model: str, digest: str,
           settings: dict, fresh: bool) -> dict:
    """`{mode: fresh|resume|blocked, state, attempt, note, archive}`.

    "blocked" is a deliberate stop rather than a silent restart: a checkpoint is
    waiting, and something about this invocation does not match it (different
    model, edited prompt, attempts used up). Starting the task over instead
    would quietly discard the work the checkpoint holds, so the driver reports
    what is wrong and leaves the decision to the caller (`--fresh`).

    `archive` is the attempt whose artifacts are still lying in the task dir and
    have to be moved aside before this run overwrites them.
    """
    state = load(task_dir)
    if state is None:
        return {"mode": "fresh", "state": None, "attempt": 1, "note": ""}
    if state.get("state") != RESUMABLE:
        return {"mode": "fresh", "state": None, "attempt": 1,
                "note": f"previous checkpoint is {state.get('state')!r}"}
    archive = int(state.get("attempts", 1))
    if fresh:
        mark(task_dir, EXHAUSTED, closed_reason="discarded by --fresh")
        return {"mode": "fresh", "state": state, "attempt": 1, "archive": archive,
                "note": "--fresh: the resumable checkpoint was discarded"}
    if not settings["enabled"]:
        # Archive anyway: this run overwrites the same file names, and the
        # attempt that produced the checkpoint should stay inspectable.
        return {"mode": "fresh", "state": state, "attempt": 1, "archive": archive,
                "note": "resume is off, so this run starts the task over; the "
                        "checkpoint itself is left untouched"}
    ok, why = usable(state, kind=kind, model=model, digest=digest,
                     max_attempts=settings["max_attempts"])
    if not ok:
        return {"mode": "blocked", "state": state, "attempt": 0, "note": why}
    return {"mode": "resume", "state": state, "attempt": archive + 1,
            "archive": archive, "note": why}


def commit(task_dir: Path, run, *, harness: str, task: str, kind: str,
           model: str, digest: str, decision: dict, settings: dict,
           roles: tuple[str, ...], save_scene) -> dict:
    """Write, keep or close this task's checkpoint after a run.

    Call this with Blender still running: `save_scene(role, path)` saves one
    live instance, and those files are what a later run reopens. `run` is an
    `agents.AgentRun` that `agents.classify` has already looked at.
    """
    interrupted = (run.limit_hit
                   or (run.timed_out and settings["on_timeout"])
                   or bool(run.completion_error)
                   or (not run.ok and not run.timed_out and settings["on_error"]))
    if not interrupted:
        if run.ok:
            closed = mark(task_dir, COMPLETED, finished_utc=now()) is not None
            if closed:
                print("    checkpoint closed: the task finished")
            return {"checkpoint": "completed" if closed else "none"}
        print(f"    not checkpointed ({run.limit_reason or 'failed'}): "
              "see [resume].on_error")
        return {"checkpoint": "none", "reason": run.limit_reason}

    if not settings["enabled"]:
        print(f"    {run.limit_reason}: resume is off, so nothing was checkpointed")
        return {"checkpoint": "disabled", "reason": run.limit_reason}
    if not run.resume_target:
        print(f"    warning: {run.limit_reason}, but no session id was captured "
              "— this task cannot be resumed automatically")
        return {"checkpoint": "unresumable", "reason": run.limit_reason}

    blends: dict[str, str] = {}
    for role in roles:
        path = checkpoint_dir(task_dir) / f"{role}.blend"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            save_scene(role, path)
            blends[role] = str(path.relative_to(task_dir))
        except Exception as exc:                              # noqa: BLE001
            print(f"    warning: could not checkpoint the {role} scene: {exc}")

    previous = decision.get("state") or {}
    history = list(previous.get("history") or [])
    history.append({
        "attempt": decision["attempt"],
        "finished_utc": now(),
        "reason": run.limit_reason,
        "evidence": run.limit_evidence[:400],
        "session_ids": list(run.session_ids),
        "resumed_from": run.resumed_from,
        "tool_calls": len(run.tool_calls),
        # What the session had cost by the end of this attempt. The stores these
        # come from are cumulative (see the USAGE section in agents.py), so the
        # step between two entries is what that attempt alone spent.
        "tokens": ((run.usage or {}).get("tokens") or {}).get("total"),
        "api_requests": (run.usage or {}).get("api_requests"),
    })
    path = save(task_dir, {
        "harness": harness,
        "task": task,
        "state": RESUMABLE,
        "reason": run.limit_reason,
        "evidence": run.limit_evidence[:400],
        "attempts": decision["attempt"],
        "inputs_digest": digest,
        "session": {"kind": kind, "model": model,
                    "resume_target": run.resume_target,
                    "session_ids": list(run.session_ids)},
        "blends": blends,
        "history": history,
    })
    print(f"    CHECKPOINT ({run.limit_reason}): {path}")
    print(f"    saved scenes: {', '.join(sorted(blends)) or '(none)'}; "
          f"session to resume: {run.resume_target}")
    if (kind == "qwen-tokenplan" and
            not run.limit_hit and
            not settings.get("defer_resume_to_next_run", False)):
        print("    the harness will automatically start the next attempt")
    else:
        print("    re-run the same command to continue this task")
    return {"checkpoint": "written", "reason": run.limit_reason,
            "path": str(path), "resume_target": run.resume_target,
            "blends": blends}


# --------------------------------------------------------------------------
# has this task already been done?
# --------------------------------------------------------------------------
def already_done(task_dir: Path, *, record: str, artifacts=(),
                 digest: str = "", require_blend_python: bool = False) -> str:
    """Why this task needs no run at all, or "" when it does need one.

    A task counts as done only when all of this holds:

      * its own run record (`ActiveVisual.run.json` /
        `Full3DInteraction.run.json`) is there, readable, and says the agent
        finished -- `ok` is true, so a failed or errored run is retried;
      * every artifact that run should have produced is on disk, so a record
        left behind by a run whose .blend never got saved is not mistaken for a
        finished one;
      * no checkpoint is waiting: an interrupted task has a RESUMABLE
        checkpoint, and that is `decide`'s business -- it is continued, never
        skipped;
      * the inputs still hash to what the record was written from, when the
        record carries a digest. Editing prompt.txt or swapping ref.glb is a
        different piece of work and re-runs.

    Records written before `inputs_digest` was recorded hold none, and are taken
    at face value rather than re-running every finished task in the tree once.
    """
    task_dir = Path(task_dir)
    path = task_dir / record
    if not path.is_file():
        return ""
    try:
        state = json.loads(path.read_text())
    except (ValueError, OSError):
        return ""                       # unreadable record: treat it as no run
    if not isinstance(state, dict) or not state.get("ok"):
        return ""
    if require_blend_python:
        reconstruction = state.get("reconstruction") or {}
        name = str(reconstruction.get("name") or "")
        if (not reconstruction.get("matched") or not name.lower().endswith(".py")
                or int(reconstruction.get("bytes") or 0) <= 0):
            return ""
    if (load(task_dir) or {}).get("state") == RESUMABLE:
        return ""
    missing = [name for name in artifacts if not (task_dir / name).exists()]
    if missing:
        return ""
    recorded = state.get("inputs_digest")
    if recorded and digest and recorded != digest:
        return ""
    when = (state.get("finished_at") or state.get("finished_utc")
            or state.get("started_utc") or state.get("started_at") or "?")
    return f"{record} says ok=true ({when})"


def stage_restore(task_dir: Path, blends: dict[str, Path]) -> dict[str, Path]:
    """Copy the checkpoint .blend files to the paths Blender is launched from.

    Blender is opened ON the file it is given, so the running instance would
    save over it (`bpy.ops.wm.save_mainfile`, which agents do use). Launching
    from a copy under `.resume/` keeps `checkpoint/*.blend` pristine as the
    record of where this attempt started.
    """
    staged: dict[str, Path] = {}
    directory = Path(task_dir) / ".resume"
    directory.mkdir(parents=True, exist_ok=True)
    for role, source in blends.items():
        target = directory / f"{role}.blend"
        shutil.copy2(source, target)
        staged[role] = target
    return staged
