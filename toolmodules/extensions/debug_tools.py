from __future__ import annotations

import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Union

from .common import as_bool, as_int, require_str


def _normalize_command(command: Any, shell_mode: bool) -> Union[str, list[str]]:
    if isinstance(command, str):
        return command if shell_mode else shlex.split(command, posix=False)
    if isinstance(command, list) and all(isinstance(item, str) for item in command):
        if shell_mode:
            return subprocess.list2cmdline(command)
        return command
    raise ValueError("`command` must be a string or string array")


def _run_once(args: dict[str, Any]) -> dict[str, Any]:
    reason = require_str(args, "reason")
    command = args.get("command")
    shell_mode = as_bool(args.get("shell"), False)
    timeout_sec = as_int(args.get("timeout_sec"), 60, minimum=1, maximum=600)

    normalized = _normalize_command(command, shell_mode)
    cwd = args.get("cwd")
    cwd_path = Path(cwd).expanduser().resolve() if isinstance(cwd, str) and cwd.strip() else None

    run_env = None
    env_raw = args.get("env")
    if isinstance(env_raw, dict):
        run_env = {}
        for key, value in env_raw.items():
            if isinstance(key, str) and isinstance(value, str):
                run_env[key] = value

    started = time.time()
    try:
        completed = subprocess.run(
            normalized,
            cwd=str(cwd_path) if cwd_path else None,
            shell=shell_mode,
            timeout=timeout_sec,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=run_env,
        )
        return {
            "command": normalized,
            "reason": reason,
            "cwd": str(cwd_path) if cwd_path else None,
            "shell": shell_mode,
            "timed_out": False,
            "duration_ms": int((time.time() - started) * 1000),
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    except subprocess.TimeoutExpired as e:
        return {
            "command": normalized,
            "reason": reason,
            "cwd": str(cwd_path) if cwd_path else None,
            "shell": shell_mode,
            "timed_out": True,
            "duration_ms": int((time.time() - started) * 1000),
            "returncode": None,
            "stdout": e.stdout if isinstance(e.stdout, str) else "",
            "stderr": e.stderr if isinstance(e.stderr, str) else "",
        }


def tool_debug_run(args: dict[str, Any]) -> dict[str, Any]:
    return _run_once(args)


def tool_debug_trace(args: dict[str, Any]) -> dict[str, Any]:
    session_id = str(args.get("session_id") or f"trace-{int(time.time())}")
    steps = args.get("steps", [])
    if not isinstance(steps, list):
        raise ValueError("`steps` must be an array")

    compact_steps: list[dict[str, Any]] = []
    for index, step in enumerate(steps, start=1):
        if isinstance(step, str):
            compact_steps.append({"index": index, "title": step, "status": "pending"})
        elif isinstance(step, dict):
            compact_steps.append(
                {
                    "index": index,
                    "title": str(step.get("title", f"step-{index}")),
                    "status": str(step.get("status", "pending")),
                    "note": str(step.get("note", "")),
                }
            )

    return {
        "session_id": session_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "step_count": len(compact_steps),
        "steps": compact_steps,
    }


def get_debug_tooling() -> tuple[dict[str, Any], dict[str, str], dict[str, dict[str, Any]]]:
    handlers = {
        "debug_run": tool_debug_run,
        "debug_trace": tool_debug_trace,
    }
    descriptions = {
        "debug_run": "Run a debug command and capture stdout/stderr with timing.",
        "debug_trace": "Build a structured debug trace payload for session replay.",
    }
    schemas = {
        "debug_run": {
            "type": "object",
            "properties": {
                "command": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
                "reason": {"type": "string"},
                "cwd": {"type": "string"},
                "timeout_sec": {"type": "integer", "minimum": 1, "maximum": 600, "default": 60},
                "shell": {"type": "boolean", "default": False},
                "env": {"type": "object", "additionalProperties": {"type": "string"}},
            },
            "required": ["command", "reason"],
            "additionalProperties": False,
        },
        "debug_trace": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "steps": {"type": "array", "items": {"oneOf": [{"type": "string"}, {"type": "object"}]}},
            },
            "additionalProperties": False,
        },
    }
    return handlers, descriptions, schemas
