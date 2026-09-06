"""Normalize saved provider usage and aggregate it per benchmark instance.

Raw ``*.attempt*.json`` files are the source of truth. Rebuilding from them is
idempotent, so resumed tasks and old-output backfills cannot double count.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

from run_config import atomic_write_json


TOKEN_KEYS = ("input", "input_cached", "cache_write", "output",
              "reasoning", "total")
ATTEMPT_RE = re.compile(r"\.iter(\d+)\.attempt(\d+)\.json$")
ACTIVE_AGENT_LATENCY_DEFINITION = (
    "Sum of independently timed active model/CLI calls per instance; excludes "
    "Blender rendering, process startup/cleanup, queueing, checkpoint restore, "
    "and wall-clock gaps between resumed runs."
)


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def agent_latency_metadata() -> dict:
    """Describe the resume-safe timing contract stored in every usage file."""
    return {
        "metric": "active_agent_latency",
        "unit": "seconds",
        "definition": ACTIVE_AGENT_LATENCY_DEFINITION,
        "resume_safe": True,
    }


def _tokens(*, input_total=None, input_cached=None, cache_write=None,
            output=None, reasoning=None, total=None) -> dict:
    if total is None:
        parts = (input_total, cache_write, output)
        total = (sum(value for value in parts if value is not None)
                 if any(value is not None for value in parts) else None)
    return {"input": input_total, "input_cached": input_cached,
            "cache_write": cache_write, "output": output,
            "reasoning": reasoning, "total": total}


def _claude(raw: dict):
    metadata = raw.get("response_metadata")
    entries = metadata if isinstance(metadata, list) else [metadata]
    result = next((item for item in reversed(entries)
                   if isinstance(item, dict) and
                   ("usage" in item or "modelUsage" in item)), {})
    models = result.get("modelUsage") or {}
    if isinstance(models, dict) and models:
        totals = {key: 0 for key in TOKEN_KEYS[:-1]}
        present = {key: False for key in TOKEN_KEYS[:-1]}
        for usage in models.values():
            if not isinstance(usage, dict):
                continue
            values = {
                "input": (_number(usage.get("inputTokens")) or 0) +
                         (_number(usage.get("cacheReadInputTokens")) or 0),
                "input_cached": _number(usage.get("cacheReadInputTokens")),
                "cache_write": _number(usage.get("cacheCreationInputTokens")),
                "output": _number(usage.get("outputTokens")),
            }
            for key, value in values.items():
                if value is not None:
                    totals[key] += value
                    present[key] = True
        tokens = _tokens(
            input_total=totals["input"] if present["input"] else None,
            input_cached=totals["input_cached"] if present["input_cached"] else None,
            cache_write=totals["cache_write"] if present["cache_write"] else None,
            output=totals["output"] if present["output"] else None)
        source = "response_metadata.modelUsage"
    else:
        usage = result.get("usage") or {}
        uncached = _number(usage.get("input_tokens"))
        cached = _number(usage.get("cache_read_input_tokens"))
        tokens = _tokens(
            input_total=((uncached or 0) + (cached or 0))
            if uncached is not None or cached is not None else None,
            input_cached=cached,
            cache_write=_number(usage.get("cache_creation_input_tokens")),
            output=_number(usage.get("output_tokens")))
        source = "response_metadata.usage"
    return (tokens, _number(result.get("num_turns")),
            _number(result.get("total_cost_usd")), source)


def _openai_usage(usage: dict) -> dict:
    prompt = _number(usage.get("prompt_tokens"))
    if prompt is None:
        prompt = _number(usage.get("input_tokens"))
    cached = _number(usage.get("cached_tokens"))
    if cached is None:
        cached = _number(usage.get("cached_input_tokens"))
    completion = _number(usage.get("completion_tokens"))
    if completion is None:
        completion = _number(usage.get("output_tokens"))
    reasoning = _number(usage.get("reasoning_tokens"))
    if reasoning is None:
        reasoning = _number(usage.get("reasoning_output_tokens"))
    return _tokens(input_total=prompt, input_cached=cached, output=completion,
                   reasoning=reasoning, total=_number(usage.get("total_tokens")))


def _codex(raw: dict):
    requests = 0
    for line in str(raw.get("transcript") or "").splitlines():
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        requests += event.get("type") == "turn.completed"
    return _openai_usage(raw.get("usage") or {}), requests or 1, None, "usage"


def _qwen(raw: dict):
    metadata = raw.get("response_metadata")
    entries = metadata if isinstance(metadata, list) else [metadata]
    usage = next((item.get("usage") for item in reversed(entries)
                  if isinstance(item, dict) and item.get("usage")), None)
    if usage is None:
        usage = next((item for item in reversed(entries)
                      if isinstance(item, dict) and
                      any(key in item for key in ("prompt_tokens", "input_tokens"))), {})
    return _openai_usage(usage or {}), 1, None, "response_metadata"


def _agy(raw: dict):
    usage = raw.get("usage") or {}
    requests = str(raw.get("transcript") or "").count("PLANNER_RESPONSE") or None
    return (_tokens(input_total=_number(usage.get("prompt_tokens")),
                    output=_number(usage.get("candidates_tokens")),
                    reasoning=_number(usage.get("thoughts_tokens")),
                    total=_number(usage.get("total_tokens"))),
            requests, None, "usage")


def collect_kimi_home_usage(home: Path | str, model: str) -> dict:
    """Read Kimi's private turn store before that no-memory store is deleted."""
    totals = {key: 0 for key in ("input", "input_cached", "cache_write", "output")}
    present = {key: False for key in totals}
    requests = records = 0
    for path in Path(home).rglob("wire.jsonl"):
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "llm.request":
                requests += 1
                continue
            if event.get("type") != "usage.record" or \
                    event.get("usageScope") != "turn":
                continue
            usage = event.get("usage") or {}
            values = {
                "input": (_number(usage.get("inputOther")) or 0) +
                         (_number(usage.get("inputCacheRead")) or 0),
                "input_cached": _number(usage.get("inputCacheRead")),
                "cache_write": _number(usage.get("inputCacheCreation")),
                "output": _number(usage.get("output")),
            }
            records += 1
            for key, value in values.items():
                if value is not None:
                    totals[key] += value
                    present[key] = True
    return {
        "model": model, "api_requests": requests or None,
        "tokens": _tokens(
            input_total=totals["input"] if present["input"] else None,
            input_cached=totals["input_cached"] if present["input_cached"] else None,
            cache_write=totals["cache_write"] if present["cache_write"] else None,
            output=totals["output"] if present["output"] else None),
        "usage_records": records, "source": "kimi_home/**/wire.jsonl"}


def attempt_usage(raw: dict) -> dict:
    backend = str(raw.get("backend") or "unknown")
    if backend == "claude-code":
        tokens, requests, cost, source = _claude(raw)
    elif backend == "codex":
        tokens, requests, cost, source = _codex(raw)
    elif backend == "agy":
        tokens, requests, cost, source = _agy(raw)
    elif backend == "kimi-code":
        captured = raw.get("usage") or {}
        tokens = captured.get("tokens") or _tokens()
        requests = captured.get("api_requests")
        if requests is None:
            requests = len(raw.get("tool_calls") or []) + 1 if raw.get("text") else None
        cost, source = None, captured.get("source") or \
            "tool_calls + final response (legacy estimate)"
    elif backend in ("vllm", "qwen3"):
        tokens, requests, cost, source = _qwen(raw)
    else:
        tokens, requests, cost, source = (_tokens(),
            1 if raw.get("text") else None, None, "raw response")
    return {"backend": backend, "model": raw.get("model"),
            "agent_seconds": _number(raw.get("agent_seconds")),
            "api_calls": requests,
            "tokens": {key: tokens.get(key) for key in TOKEN_KEYS},
            "cost_usd": cost, "source": source}


def build_instance_usage(out_dir: Path, instance: str,
                         agent: str | None = None) -> dict:
    referenced_names = None
    agent_seconds = None
    checkpoint = out_dir / "process.json"
    if checkpoint.is_file():
        try:
            process = json.loads(checkpoint.read_text())
        except (OSError, ValueError):
            process = None
        if isinstance(process, dict):
            agent_seconds = _number(process.get("agent_seconds"))
            referenced_names = set()
            entries = list(process.get("iterations") or [])
            pending = process.get("pending_iteration")
            if isinstance(pending, dict):
                entries.append(pending)
            for entry in entries:
                for attempt in entry.get("attempts") or []:
                    raw_path = attempt.get("raw_path")
                    if raw_path:
                        referenced_names.add(Path(raw_path).name)
    paths = []
    for path in out_dir.glob(f"{instance}.iter*.attempt*.json"):
        if referenced_names is not None and path.name not in referenced_names:
            continue
        match = ATTEMPT_RE.search(path.name)
        if match:
            paths.append((int(match.group(1)), int(match.group(2)), path))
    paths.sort()
    attempts = []
    for iteration, attempt, path in paths:
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        entry = attempt_usage(raw)
        entry.update({"iteration": iteration, "attempt": attempt,
                      "raw_path": str(path)})
        attempts.append(entry)

    def aggregate(field, nested=None):
        values = []
        for entry in attempts:
            value = entry[field] if nested is None else entry[field][nested]
            if _number(value) is not None:
                values.append(value)
        return sum(values) if values else None

    cost = aggregate("cost_usd")
    if agent_seconds is None:
        agent_seconds = aggregate("agent_seconds")
    return {
        "version": 1, "instance": instance, "agent": agent,
        "attempt_count": len(attempts), "api_calls": aggregate("api_calls"),
        "agent_seconds": (round(agent_seconds, 3)
                          if agent_seconds is not None else None),
        "timing": agent_latency_metadata(),
        "tokens": {key: aggregate("tokens", key) for key in TOKEN_KEYS},
        "cost_usd": round(cost, 6) if cost is not None else None,
        "coverage": {
            "timing_reported_attempts": sum(
                a["agent_seconds"] is not None for a in attempts),
            "api_calls_reported_attempts": sum(a["api_calls"] is not None for a in attempts),
            "token_reported_attempts": sum(a["tokens"]["total"] is not None for a in attempts),
            "cost_reported_attempts": sum(a["cost_usd"] is not None for a in attempts)},
        "attempts": attempts}


def mcp_agent_seconds(out_dir: Path, record_name: str) -> float | None:
    """Sum actual CLI-turn time across the current and resumed MCP attempts.

    Blender startup, rendering, checkpoint restore, and the wall-clock gap
    between invocations are outside the per-turn timers and therefore excluded.
    """
    paths = [out_dir / record_name]
    attempts_dir = out_dir / "attempts"
    if attempts_dir.is_dir():
        paths.extend(sorted(attempts_dir.glob(f"attempt-*/{record_name}")))
    total = 0.0
    found = False
    for path in paths:
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        run = record.get("agent_run") or record.get("run") or {}
        for turn in run.get("turns") or []:
            seconds = _number(turn.get("seconds")) if isinstance(turn, dict) else None
            if seconds is not None:
                total += float(seconds)
                found = True
    return round(total, 3) if found else None


def write_instance_usage(out_dir: Path, instance: str,
                         agent: str | None = None) -> dict:
    record = build_instance_usage(out_dir, instance, agent)
    atomic_write_json(out_dir / "usage.json", record)
    return record


def build_runtime_latency_summary(model_dir: Path) -> dict:
    """Aggregate runtime-recorded agent time for one model output directory.

    Only ``usage.json:agent_seconds`` is accepted here: this is the native
    runtime path, never a wall-clock reconstruction.  A partial run exposes an
    average over reported instances for live progress, but deliberately leaves
    ``average_per_instance`` null until every existing instance is covered.
    """
    directories = sorted(
        path for path in Path(model_dir).iterdir()
        if path.is_dir() and not path.name.startswith("_")
    ) if Path(model_dir).is_dir() else []
    rows = []
    for directory in directories:
        usage_path = directory / "usage.json"
        seconds = None
        status = "MISSING_USAGE"
        try:
            usage = json.loads(usage_path.read_text())
            value = (
                _number(usage.get("agent_seconds"))
                if isinstance(usage, dict) else None
            )
            if value is not None and math.isfinite(value) and value >= 0:
                seconds = float(value)
                status = "OK"
            else:
                status = "MISSING_AGENT_SECONDS"
        except (OSError, ValueError):
            if usage_path.is_file():
                status = "INVALID_USAGE"
        rows.append({
            "instance": directory.name,
            "agent_seconds": round(seconds, 6) if seconds is not None else None,
            "estimated": False,
            "status": status,
        })

    reported = [row["agent_seconds"] for row in rows
                if row["agent_seconds"] is not None]
    total = sum(reported)
    n_instances, n_reported = len(rows), len(reported)
    complete = bool(n_instances and n_reported == n_instances)
    return {
        "version": 1,
        "metric": "active_agent_latency",
        "unit": "seconds",
        "definition": ACTIVE_AGENT_LATENCY_DEFINITION,
        "model_dir": str(Path(model_dir).resolve()),
        "method": "runtime per-instance usage.json agent_seconds",
        "n_instances": n_instances,
        "agent_seconds": {
            "total": round(total, 6) if reported else None,
            "average_per_instance": (
                round(total / n_instances, 6) if complete else None
            ),
            "average_per_reported_instance": (
                round(total / n_reported, 6) if reported else None
            ),
        },
        "coverage": {
            "n_exact_instances": n_reported,
            "n_estimated_instances": 0,
            "n_missing_instances": n_instances - n_reported,
            "sources": {"runtime_usage_instances": n_reported},
        },
        "per_instance": rows,
    }


def _latency_coverage_score(summary: dict) -> tuple[int, int]:
    coverage = summary.get("coverage") or {}
    exact = _number(coverage.get("n_exact_instances")) or 0
    estimated = _number(coverage.get("n_estimated_instances")) or 0
    return int(exact + estimated), int(exact)


def write_runtime_latency_summary(model_dir: Path) -> dict:
    """Refresh ``_metrics/latency.json`` without degrading a better backfill."""
    summary = build_runtime_latency_summary(model_dir)
    path = Path(model_dir) / "_metrics" / "latency.json"
    existing = None
    try:
        loaded = json.loads(path.read_text())
        if isinstance(loaded, dict):
            existing = loaded
    except (OSError, ValueError):
        pass

    same_population = (
        existing is not None and
        existing.get("n_instances") == summary["n_instances"]
    )
    replace = (not same_population or
               _latency_coverage_score(summary) >=
               _latency_coverage_score(existing or {}))
    if replace:
        atomic_write_json(path, summary)
        average = summary["agent_seconds"]["average_per_instance"]
        final_path = path.with_name("final_metrics.json")
        if average is not None and final_path.is_file():
            try:
                final = json.loads(final_path.read_text())
            except (OSError, ValueError):
                final = None
            if isinstance(final, dict):
                final["avg_latency_s"] = average
                atomic_write_json(final_path, final)
    return summary


def runtime_latency_line(summary: dict) -> str:
    """Format a concise batch latency line for all graded entry points."""
    metric = summary["agent_seconds"]
    coverage = summary["coverage"]
    total = metric["total"]
    average = metric["average_per_instance"]
    qualifier = ""
    if average is None:
        average = metric["average_per_reported_instance"]
        qualifier = " over reported instances"
    total_text = "--" if total is None else f"{total:.3f}s"
    average_text = "--" if average is None else f"{average:.3f}s/instance"
    return (
        f"Active agent latency: total={total_text}, avg={average_text}"
        f"{qualifier}, coverage={coverage['n_exact_instances']}/"
        f"{summary['n_instances']} (render/resume gaps excluded)"
    )
