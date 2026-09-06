#!/usr/bin/env python3
"""Report what finished sessions cost: tokens and API requests, per task and
per agent, from run-record trees supplied with ``--root``.

    python cli_agents/session_usage.py --root PATH/TO/OUTPUT

    # one harness, one agent, as JSON
    python .../session_usage.py --root PATH/TO/OUTPUT --agent opus --json

    # fill in `usage` on run records written before the harness recorded it
    python .../session_usage.py --write

Both drivers already write this into their run record (`usage`, and
`run`/`agent_run` → `usage`) as each session ends; this reads the CLIs' own
stores again, so it also works on runs made before that existed (`--write`
backfills those records) and lets a whole sweep be totalled after the fact.

Reading is always safe: the numbers come out of each CLI's session store inside
`<task>/session/`, and nothing is recomputed or estimated -- see the USAGE
section of agents.py for what each CLI reports and what it does not (agy, for
one, reports no token counts at all).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The adapter's repository argument is intentionally local.  Usage collection
# never needs MCP source trees; keeping this here prevents accidental coupling
# to the repository layout containing this vendored copy.
REPO = HERE
sys.path.insert(0, str(HERE))
import agents                                                   # noqa: E402

# Each harness's run record, and the key its agent block lives under.
RECORDS = {"ActiveVisual.run.json": "run",
           "Full3DInteraction.run.json": "agent_run"}
# The turn log each adapter's usage fallbacks read when its own store is gone.
TURN_LOGS = {"codex": "codex.session.jsonl",
             "claude": "claude.session.jsonl",
             "opus": "claude.session.jsonl",
             "qwen-tokenplan": "qwen.session.jsonl",
             "minimax-m3": "mmx.session.jsonl",
             "kimi": "kimi.session.jsonl",
             "agy": "agy.prompt.response.json"}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", action="append", default=[],
                        help="an output root to scan (repeatable); default is "
                             "cli_agents/output")
    parser.add_argument("--agent", action="append", default=[],
                        help="only this agent kind (repeatable)")
    parser.add_argument("--task", action="append", default=[],
                        help="only this task (repeatable)")
    parser.add_argument("--json", action="store_true",
                        help="emit the collected records as JSON")
    parser.add_argument("--write", action="store_true",
                        help="also store the usage block back into each run "
                             "record (`usage`, and the agent block's `usage`)")
    return parser.parse_args(argv)


def find_records(roots: list[Path]):
    for root in roots:
        if not root.is_dir():
            continue
        for name in RECORDS:
            yield from sorted(root.glob(f"*/*/{name}"))


def collect(path: Path) -> dict | None:
    """Read one run record and re-derive its session usage from the CLI's store."""
    try:
        record = json.loads(path.read_text(errors="replace"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    block = record.get(RECORDS[path.name])
    block = block if isinstance(block, dict) else {}
    agent_block = record.get("agent")
    agent_block = agent_block if isinstance(agent_block, dict) else {}
    kind = block.get("kind") or agent_block.get("kind") or ""
    if kind not in agents.AGENTS:
        return None
    model = block.get("model") or agent_block.get("model") or ""
    session_dir = path.parent / "session"

    agent = agents.build(kind, {"model": model}, REPO, 60)
    run = agents.AgentRun(kind=kind, model=model)
    log = session_dir / TURN_LOGS.get(kind, "")
    run.turns.append(agents.TurnResult(label="prompt", returncode=0, seconds=0.0,
                                       log=log if log.is_file() else None))
    # The session ids matter: a task that was run more than once keeps every
    # conversation's records in the same session/ dir, and the adapters use these
    # to count only the one this record describes.
    run.session_ids = [str(s) for s in (block.get("session_ids") or []) if s]
    run.resumed_from = str(block.get("resumed_from") or "")
    agent._record_usage(run, session_dir)
    # The directory is the task's identity on disk; `record["task"]` can be a
    # stale name from an earlier run of that directory, and two directories can
    # carry the same one -- which would silently merge two rows.
    return {"record": str(path), "harness": path.name.split(".")[0],
            "agent": kind, "model": model,
            "task": path.parent.name,
            "task_recorded": record.get("task") or "",
            "attempt": (record.get("resume") or {}).get("attempt"),
            "session_ids": run.session_ids,
            "seconds": record.get("seconds"),
            "tool_calls": block.get("tool_call_count"),
            "usage": run.usage}


def write_back(path: Path, usage: dict) -> None:
    record = json.loads(path.read_text(errors="replace"))
    record["usage"] = usage or None
    block = record.get(RECORDS[path.name])
    if isinstance(block, dict):
        block["usage"] = usage or None
    path.write_text(json.dumps(record, indent=2))


def number(value) -> str:
    return "?" if value is None else f"{value:,}"


def main(argv=None) -> int:
    args = parse_args(argv)
    roots = [Path(r).resolve() for r in args.root] or [HERE / "output"]

    rows = []
    for path in find_records(roots):
        row = collect(path)
        if row is None:
            continue
        if args.agent and row["agent"] not in args.agent:
            continue
        if args.task and row["task"] not in args.task:
            continue
        rows.append(row)
        if args.write:
            write_back(path, row["usage"])
    if not rows:
        print("no run records found", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    # One row per task run -- i.e. per <agent, task> in one harness, which is one
    # CLI session (a resumed task is still one session; see agents.py).
    header = (f"{'harness':<22} {'agent':<16} {'task':<14} {'total':>12} "
              f"{'input':>12} {'cached':>12} {'output':>9} {'api':>5} "
              f"{'cost':>7} {'att':>3}")
    print(header)
    print("-" * len(header))
    per_agent: dict = {}
    for row in rows:
        usage = row["usage"] or {}
        tokens = usage.get("tokens") or {}
        cost = usage.get("cost_usd")
        attempt = row.get("attempt")
        print(f"{row['harness']:<22} {row['agent']:<16} {row['task'][:14]:<14} "
              f"{number(tokens.get('total')):>12} {number(tokens.get('input')):>12} "
              f"{number(tokens.get('input_cached')):>12} "
              f"{number(tokens.get('output')):>9} "
              f"{number(usage.get('api_requests')):>5} "
              f"{'-' if cost is None else format(cost, '.2f'):>7} "
              f"{'-' if attempt is None else attempt:>3}")
        key = (row["harness"], row["agent"])
        entry = per_agent.setdefault(key, {"runs": 0, "total": 0, "output": 0,
                                           "api": 0, "cost": 0.0, "unknown": 0})
        entry["runs"] += 1
        if tokens.get("total") is None:
            entry["unknown"] += 1
        for name, value in (("total", tokens.get("total")),
                            ("output", tokens.get("output")),
                            ("api", usage.get("api_requests"))):
            entry[name] += value or 0
        entry["cost"] += cost or 0.0

    # Each row above is one session; this adds them up over an agent's tasks.
    print("\n=== per agent (summed over its tasks)")
    for (harness, kind), entry in sorted(per_agent.items()):
        cost = f", ${entry['cost']:.2f}" if entry["cost"] else ""
        if entry["unknown"] == entry["runs"]:
            # agy: the CLI reports no token counts, so a "0" would be a lie.
            tokens = "no token counts reported"
        else:
            tokens = f"{entry['total']:,} tokens ({entry['output']:,} output)"
            if entry["unknown"]:
                tokens += (f" over {entry['runs'] - entry['unknown']} of "
                           f"{entry['runs']} task(s)")
        print(f"  {harness} / {kind}: {entry['runs']} task(s), "
              f"{tokens}, {entry['api']} API requests{cost}")
    notes = [(row, note) for row in rows
             for note in ((row["usage"] or {}).get("notes") or [])
             if "excludes" in note]
    if notes:
        print("\n=== what was left out")
        for row, note in notes:
            print(f"  {row['harness']} / {row['agent']} / {row['task']}: {note}")
    if args.write:
        print(f"\nwrote the usage block into {len(rows)} run record(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
