#!/usr/bin/env python3
"""Aggregate per-instance latency, token, API-call, and provider cost usage.

Reads ``<results-root>/<model>/<instance>/usage.json`` and writes
``<model>/_metrics/usage.json``. Unknown values remain unknown: totals sum all
reported values, while averages divide only by instances reporting that field.
Coverage counts make partial provider reporting explicit.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path


EVAL_ROOT = Path(os.environ.get("EVAL_ROOT")
                 or Path(__file__).resolve().parent.parent)
DEFAULT_RESULTS_ROOT = EVAL_ROOT / "results"
TOKEN_KEYS = ("input", "input_cached", "cache_write", "output",
              "reasoning", "total")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--results-root", type=Path,
                        default=DEFAULT_RESULTS_ROOT)
    parser.add_argument("--instances", nargs="*", default=None)
    return parser.parse_args()


def number(value):
    """Return a finite int/float, otherwise None (bool is not numeric here)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def aggregate(values: list[int | float | None], n_instances: int) -> dict:
    reported = [value for value in values if value is not None]
    total = sum(reported) if reported else None
    return {
        "total": total,
        "average_per_reported_instance": (
            total / len(reported) if reported else None
        ),
        # Only expose an all-evaluated-instance average when coverage is full;
        # missing provider accounting must not silently become zero usage.
        "average_per_evaluated_instance": (
            total / n_instances
            if reported and len(reported) == n_instances else None
        ),
        "n_reported": len(reported),
        "n_missing": n_instances - len(reported),
    }


def main() -> None:
    args = parse_args()
    model_dir = args.results_root / args.model
    if not model_dir.is_dir():
        raise SystemExit(f"No such model dir: {model_dir}")

    instance_dirs = sorted(
        directory for directory in model_dir.iterdir()
        if directory.is_dir() and not directory.name.startswith("_")
    )
    if args.instances:
        wanted = set(args.instances)
        instance_dirs = [d for d in instance_dirs if d.name in wanted]
        missing_dirs = wanted - {d.name for d in instance_dirs}
        if missing_dirs:
            raise SystemExit(f"No such instances: {sorted(missing_dirs)}")
    if not instance_dirs:
        raise SystemExit("No instances found.")

    rows = []
    for directory in instance_dirs:
        path = directory / "usage.json"
        record = None
        error = None
        if not path.is_file():
            error = "MISSING_USAGE"
        else:
            try:
                loaded = json.loads(path.read_text())
                if not isinstance(loaded, dict):
                    raise ValueError("top-level JSON must be an object")
                record = loaded
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                error = f"INVALID_USAGE: {type(exc).__name__}: {exc}"

        tokens = record.get("tokens") if record else None
        if not isinstance(tokens, dict):
            tokens = {}
        rows.append({
            "instance": directory.name,
            "usage_path": str(path),
            "status": "OK" if record is not None else error,
            "agent_seconds": number(record.get("agent_seconds")) if record else None,
            "attempt_count": number(record.get("attempt_count")) if record else None,
            "api_calls": number(record.get("api_calls")) if record else None,
            "tokens": {key: number(tokens.get(key)) for key in TOKEN_KEYS},
            "cost_usd": number(record.get("cost_usd")) if record else None,
        })

    n = len(rows)
    token_summary = {
        key: aggregate([row["tokens"][key] for row in rows], n)
        for key in TOKEN_KEYS
    }
    summary = {
        "model": args.model,
        "results_root": str(args.results_root),
        "n_instances": n,
        "n_usage_files": sum(row["status"] == "OK" for row in rows),
        "n_missing_usage_files": sum(row["status"] == "MISSING_USAGE"
                                     for row in rows),
        "n_invalid_usage_files": sum(
            str(row["status"]).startswith("INVALID_USAGE") for row in rows
        ),
        "agent_seconds": aggregate([row["agent_seconds"] for row in rows], n),
        "attempt_count": aggregate([row["attempt_count"] for row in rows], n),
        "api_calls": aggregate([row["api_calls"] for row in rows], n),
        "tokens": token_summary,
        "cost_usd": aggregate([row["cost_usd"] for row in rows], n),
        "per_instance": rows,
    }

    out_dir = model_dir / "_metrics"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "usage.json"
    out_path.write_text(json.dumps(summary, indent=2) + "\n")

    def show(metric: dict, money: bool = False) -> str:
        total = metric["total"]
        mean = metric["average_per_reported_instance"]
        if total is None:
            return f"total=--  avg=--  coverage=0/{n}"
        if money:
            values = f"total=${total:.6f}  avg=${mean:.6f}"
        else:
            values = f"total={total:,.0f}  avg={mean:,.2f}"
        return f"{values}  coverage={metric['n_reported']}/{n}"

    print(f"=== usage — {args.model} ===")
    print(f"  Instances:       {n} (usage files {summary['n_usage_files']}/{n})")
    print(f"  Agent seconds:   {show(summary['agent_seconds'])}")
    print(f"  API calls:       {show(summary['api_calls'])}")
    print(f"  Total tokens:    {show(token_summary['total'])}")
    print(f"  Input tokens:    {show(token_summary['input'])}")
    print(f"  Cached input:    {show(token_summary['input_cached'])}")
    print(f"  Cache write:     {show(token_summary['cache_write'])}")
    print(f"  Output tokens:   {show(token_summary['output'])}")
    print(f"  Reasoning:       {show(token_summary['reasoning'])}")
    print(f"  Provider cost:   {show(summary['cost_usd'], money=True)}")
    print(f"  Wrote {out_path}")


if __name__ == "__main__":
    main()
