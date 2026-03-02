from __future__ import annotations

import statistics
from typing import Any

from .common import as_bool, as_int
from .debug_tools import _run_once


def tool_perf_benchmark(args: dict[str, Any]) -> dict[str, Any]:
    samples = as_int(args.get("samples"), 5, minimum=1, maximum=100)
    fail_fast = as_bool(args.get("fail_fast"), False)

    runs: list[dict[str, Any]] = []
    durations: list[int] = []
    non_zero = 0

    for _ in range(samples):
        run = _run_once(args)
        runs.append(run)
        durations.append(int(run.get("duration_ms", 0)))
        if run.get("returncode") not in (0, None):
            non_zero += 1
            if fail_fast:
                break

    summary: dict[str, Any] = {
        "samples_requested": samples,
        "samples_executed": len(runs),
        "non_zero_count": non_zero,
        "min_ms": min(durations) if durations else None,
        "max_ms": max(durations) if durations else None,
        "mean_ms": int(statistics.mean(durations)) if durations else None,
        "median_ms": int(statistics.median(durations)) if durations else None,
        "p95_ms": None,
    }

    if durations:
        sorted_durations = sorted(durations)
        p95_index = int(round((len(sorted_durations) - 1) * 0.95))
        summary["p95_ms"] = sorted_durations[p95_index]

    return {"summary": summary, "runs": runs}


def tool_perf_report_list(args: dict[str, Any]) -> dict[str, Any]:
    items = args.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("`items` must be a non-empty array")

    sort_by = str(args.get("sort_by", "value"))
    descending = as_bool(args.get("descending"), True)
    top_n = as_int(args.get("top_n"), len(items), minimum=1)

    normalized: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        value_raw = item.get("value", 0)
        try:
            value = float(value_raw)
        except Exception:
            value = 0.0
        normalized.append(
            {
                "name": name,
                "value": value,
                "unit": str(item.get("unit", "ms")),
                "category": str(item.get("category", "general")),
                "meta": item.get("meta", {}),
            }
        )

    normalized.sort(key=lambda item: item.get(sort_by, 0), reverse=descending)
    top = normalized[:top_n]

    return {
        "count": len(normalized),
        "sort_by": sort_by,
        "descending": descending,
        "top_n": top_n,
        "items": top,
    }


def get_perf_tooling() -> tuple[dict[str, Any], dict[str, str], dict[str, dict[str, Any]]]:
    handlers = {
        "perf_benchmark": tool_perf_benchmark,
        "perf_report_list": tool_perf_report_list,
    }
    descriptions = {
        "perf_benchmark": "Run command benchmarks and return min/mean/median/p95 timing summary.",
        "perf_report_list": "Sort and summarize a performance item list for ranking output.",
    }
    schemas = {
        "perf_benchmark": {
            "type": "object",
            "properties": {
                "command": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
                "reason": {"type": "string"},
                "samples": {"type": "integer", "minimum": 1, "maximum": 100, "default": 5},
                "fail_fast": {"type": "boolean", "default": False},
                "cwd": {"type": "string"},
                "timeout_sec": {"type": "integer", "minimum": 1, "maximum": 600, "default": 60},
                "shell": {"type": "boolean", "default": False},
                "env": {"type": "object", "additionalProperties": {"type": "string"}},
            },
            "required": ["command", "reason"],
            "additionalProperties": False,
        },
        "perf_report_list": {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"type": "object"}, "minItems": 1},
                "sort_by": {"type": "string", "default": "value"},
                "descending": {"type": "boolean", "default": True},
                "top_n": {"type": "integer", "minimum": 1},
            },
            "required": ["items"],
            "additionalProperties": False,
        },
    }
    return handlers, descriptions, schemas
