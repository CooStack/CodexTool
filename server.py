#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlparse
from typing import Any, Callable, Optional, Tuple, Union

from toolmodules import get_extension_tooling
from toolmodules.extensions.common import install_package_with_pip

SERVER_NAME = "CodexTools"
SERVER_VERSION = "0.8.1"
DEFAULT_PROTOCOL_VERSION = "2024-11-05"


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _ok(payload: Any) -> dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": text}]}


def _tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _require_str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"`{key}` must be a non-empty string")
    return value


def _require_str_list(args: dict[str, Any], key: str) -> list[str]:
    value = args.get(key)
    if not isinstance(value, list) or not value:
        raise ValueError(f"`{key}` must be a non-empty string array")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"`{key}[{index}]` must be a non-empty string")
        result.append(item)
    return result


def _as_int(
    value: Any,
    default: int,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    if value is None:
        result = default
    elif isinstance(value, bool):
        raise ValueError("bool is not valid for integer field")
    else:
        result = int(value)
    if minimum is not None and result < minimum:
        raise ValueError(f"value must be >= {minimum}")
    if maximum is not None and result > maximum:
        raise ValueError(f"value must be <= {maximum}")
    return result


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    return bool(value)


def _path(path_text: str) -> Path:
    return Path(path_text).expanduser().resolve()


def _resolve_workspace_root() -> Path:
    env_override = os.environ.get("CODEXTOOLS_WORKSPACE_ROOT", "").strip()
    if env_override:
        return _path(env_override)

    probe = Path.cwd().resolve()
    if shutil.which("git"):
        try:
            completed = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=str(probe),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            repo_root = completed.stdout.strip()
            if completed.returncode == 0 and repo_root:
                return Path(repo_root).resolve()
        except Exception:
            pass

    return probe


def _workspace_state_key(workspace_root: Path) -> str:
    normalized = str(workspace_root).strip()
    if os.name == "nt":
        normalized = normalized.lower()
    digest = hashlib.sha1(normalized.encode("utf-8", errors="replace")).hexdigest()
    return f"ws-{digest[:12]}"


def _paths_equal(left: Path, right: Path) -> bool:
    if os.name == "nt":
        return str(left).lower() == str(right).lower()
    return str(left) == str(right)


PROJECT_ROOT = Path(__file__).resolve().parent
PROJECT_NAME = PROJECT_ROOT.name
WORKSPACE_ROOT = _resolve_workspace_root()
WORKSPACE_STATE_KEY = _workspace_state_key(WORKSPACE_ROOT)
STATE_ROOT = PROJECT_ROOT / ".agent" / "state" / WORKSPACE_STATE_KEY
CONTROL_STATE_PATH = STATE_ROOT / "codextools_control_state.json"


def _runtime_project_name() -> str:
    try:
        workspace_root = _resolve_runtime_workspace_root()
    except Exception:
        workspace_root = WORKSPACE_ROOT

    candidate = workspace_root.name.strip()
    return candidate if candidate else PROJECT_NAME


PROC_RUN_DENY_PRIMARY_COMMANDS = {
    "cat",
    "type",
    "echo",
    "sed",
    "awk",
    "perl",
    "get-content",
    "set-content",
    "out-file",
    "copy",
    "cp",
    "move",
    "mv",
    "rm",
    "del",
    "erase",
    "mkdir",
    "md",
    "rmdir",
    "rd",
    "touch",
    "new-item",
    "ni",
}
PROC_RUN_REDIRECTION_PATTERN = re.compile(r"(?:^|\s)(?:\|>|>>|<|<<|1>|2>|1>>|2>>)(?:\s|$)")
MODEL_LINE_PATTERN = re.compile(r"(?m)^\s*model\s*=\s*[\"']([^\"']+)[\"']\s*$")
AUTO_AGENT_BLOCK_START = "<!-- codextools:auto-agent-rules:v1:start -->"
AUTO_AGENT_BLOCK_END = "<!-- codextools:auto-agent-rules:v1:end -->"
FALLBACK_AGENT_RULES_TEXT = "# Agent Rules\n\n- Follow project instructions for this workspace.\n"
_RUNTIME_MODEL_HINT = ""
_RUNTIME_WORKSPACE_ROOT_HINT = ""


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ensure_control_paths() -> None:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)


def _default_guard_policy_state() -> dict[str, Any]:
    return {
        "enforce_proc_run_policy": True,
        "audit": [],
    }


def _normalize_guard_policy_state(raw: Any) -> dict[str, Any]:
    guard = _default_guard_policy_state()
    if not isinstance(raw, dict):
        return guard

    guard["enforce_proc_run_policy"] = bool(raw.get("enforce_proc_run_policy", True))

    audit_raw = raw.get("audit")
    if isinstance(audit_raw, list):
        audit: list[dict[str, Any]] = []
        for item in audit_raw:
            if not isinstance(item, dict):
                continue
            entry_time = item.get("time")
            entry_event = item.get("event")
            entry_message = item.get("message")
            if not isinstance(entry_time, str) or not entry_time.strip():
                continue
            if not isinstance(entry_event, str) or not entry_event.strip():
                continue
            if not isinstance(entry_message, str) or not entry_message.strip():
                continue
            entry: dict[str, Any] = {
                "time": entry_time.strip(),
                "event": entry_event.strip(),
                "message": entry_message.strip(),
            }
            entry_tool = item.get("tool")
            if isinstance(entry_tool, str) and entry_tool.strip():
                entry["tool"] = entry_tool.strip()
            extra = item.get("extra")
            if isinstance(extra, dict) and extra:
                entry["extra"] = extra
            audit.append(entry)
        guard["audit"] = audit[-200:]

    return guard


def _ensure_guard_policy_state(state: dict[str, Any]) -> dict[str, Any]:
    guard = _normalize_guard_policy_state(state.get("guard_policy"))
    state["guard_policy"] = guard
    return guard


def _append_guard_audit(
    state: dict[str, Any],
    *,
    event: str,
    message: str,
    tool: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    guard = _ensure_guard_policy_state(state)
    audit_raw = guard.get("audit")
    audit = audit_raw if isinstance(audit_raw, list) else []

    entry: dict[str, Any] = {
        "time": _utc_now(),
        "event": event,
        "message": message,
    }
    if isinstance(tool, str) and tool.strip():
        entry["tool"] = tool.strip()
    if isinstance(extra, dict) and extra:
        entry["extra"] = extra

    audit.append(entry)
    guard["audit"] = audit[-200:]


def _default_control_state() -> dict[str, Any]:
    return {
        "version": 1,
        "workspace_root": str(WORKSPACE_ROOT),
        "workspace_state_key": WORKSPACE_STATE_KEY,
        "guard_policy": _default_guard_policy_state(),
    }


def _load_control_state() -> dict[str, Any]:
    _ensure_control_paths()
    if not CONTROL_STATE_PATH.exists():
        return _default_control_state()

    try:
        loaded = json.loads(CONTROL_STATE_PATH.read_text(encoding="utf-8", errors="replace"))
        if not isinstance(loaded, dict):
            raise ValueError("state root must be object")
    except Exception as e:
        _log(f"state load failed, using defaults: {e}")
        return _default_control_state()

    state = _default_control_state()
    for key in state.keys():
        if key in loaded:
            state[key] = loaded[key]

    state["workspace_root"] = str(WORKSPACE_ROOT)
    state["workspace_state_key"] = WORKSPACE_STATE_KEY

    _ensure_guard_policy_state(state)
    return state


def _save_control_state(state: dict[str, Any]) -> None:
    _ensure_control_paths()
    CONTROL_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8", newline="")


def _next_identifier(state: dict[str, Any], seq_key: str, prefix: str) -> str:
    raw = state.get(seq_key, 1)
    try:
        seq = int(raw)
    except Exception:
        seq = 1
    if seq < 1:
        seq = 1
    state[seq_key] = seq + 1
    return f"{prefix}-{seq:04d}"


def _coerce_plan_status(value: Any) -> str:
    status = str(value).strip().lower()
    return status if status in PLAN_STEP_STATUSES else "pending"


def _compute_plan_progress(steps: list[dict[str, Any]]) -> tuple[int, int, int, dict[str, int]]:
    counts = {status: 0 for status in PLAN_STEP_STATUSES}
    for step in steps:
        status = _coerce_plan_status(step.get("status", "pending"))
        counts[status] += 1

    total_steps = len(steps)
    completed = counts["completed"]
    progress_percent = int((completed * 100) / total_steps) if total_steps > 0 else 0
    return total_steps, completed, progress_percent, counts


def _compact_text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    text = value if isinstance(value, str) else str(value)
    compact = " ".join(part for part in text.splitlines() if part.strip())
    return compact.strip() or fallback


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _path_from_workspace_uri(uri_text: str) -> Optional[Path]:
    compact = _compact_text(uri_text, "")
    if not compact:
        return None

    parsed = urlparse(compact)
    if parsed.scheme and parsed.scheme.lower() != "file":
        return None

    if parsed.scheme.lower() == "file":
        raw_path = unquote(parsed.path or "")
        if os.name == "nt" and re.fullmatch(r"/[A-Za-z]:.*", raw_path):
            raw_path = raw_path[1:]
        if parsed.netloc and os.name == "nt":
            host = parsed.netloc.strip()
            if host and host.lower() not in {"", "localhost"}:
                raw_path = f"//{host}{raw_path}"
    else:
        raw_path = compact

    try:
        return Path(raw_path).expanduser().resolve()
    except Exception:
        return None


def _capture_runtime_workspace_hint(params: dict[str, Any]) -> None:
    global _RUNTIME_WORKSPACE_ROOT_HINT

    candidates: list[str] = []

    def _add_candidate(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())

    _add_candidate(params.get("rootPath"))
    _add_candidate(params.get("rootUri"))

    workspace_folders = params.get("workspaceFolders")
    if isinstance(workspace_folders, list):
        for item in workspace_folders:
            if not isinstance(item, dict):
                continue
            _add_candidate(item.get("uri"))
            _add_candidate(item.get("path"))

    for meta_key in ("meta", "_meta"):
        meta_obj = params.get(meta_key)
        if not isinstance(meta_obj, dict):
            continue
        _add_candidate(meta_obj.get("rootPath"))
        _add_candidate(meta_obj.get("rootUri"))

    for candidate in candidates:
        parsed = _path_from_workspace_uri(candidate)
        if parsed is None:
            continue
        _RUNTIME_WORKSPACE_ROOT_HINT = str(parsed)
        return


def _resolve_runtime_workspace_root() -> Path:
    env_override = os.environ.get("CODEXTOOLS_WORKSPACE_ROOT", "").strip()
    if env_override:
        return _path(env_override)

    hint = _compact_text(_RUNTIME_WORKSPACE_ROOT_HINT, "")
    if hint:
        try:
            return _path(hint)
        except Exception:
            pass

    return WORKSPACE_ROOT


def _capture_runtime_model_hint(params: dict[str, Any]) -> None:
    global _RUNTIME_MODEL_HINT

    hints: list[str] = []

    def _add_hint(value: Any) -> None:
        if not isinstance(value, str):
            return
        compact = _compact_text(value, "")
        if compact:
            hints.append(compact)

    for key in ("model", "model_name", "modelName", "assistant_model", "assistantModel"):
        _add_hint(params.get(key))

    client_info = params.get("clientInfo")
    if isinstance(client_info, dict):
        for key in ("name", "title", "version", "model"):
            _add_hint(client_info.get(key))

    for meta_key in ("meta", "_meta"):
        meta_obj = params.get(meta_key)
        if not isinstance(meta_obj, dict):
            continue
        for key in ("model", "model_name", "modelName", "assistant_model", "assistantModel"):
            _add_hint(meta_obj.get(key))

    if hints:
        _RUNTIME_MODEL_HINT = " ".join(dict.fromkeys(hints))


def _load_model_hint_from_local_config() -> str:
    workspace_root = _resolve_runtime_workspace_root()
    config_candidates = (
        workspace_root / "codex.config.codextools.toml",
        workspace_root / "codex.config.toml",
        PROJECT_ROOT / "codex.config.codextools.toml",
        PROJECT_ROOT / "codex.config.toml",
    )
    for config_path in config_candidates:
        if not config_path.exists():
            continue
        try:
            config_text = config_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        match = MODEL_LINE_PATTERN.search(config_text)
        if not match:
            continue

        candidate = _compact_text(match.group(1), "")
        if candidate:
            return candidate

    return ""


def _resolve_agent_target_filename() -> str:
    hints: list[str] = []

    runtime_hint = _compact_text(_RUNTIME_MODEL_HINT, "")
    if runtime_hint:
        hints.append(runtime_hint)

    config_hint = _load_model_hint_from_local_config()
    if config_hint:
        hints.append(config_hint)

    for env_key in ("CODEX_MODEL", "MODEL", "OPENAI_MODEL", "ANTHROPIC_MODEL", "LLM_MODEL"):
        env_value = os.environ.get(env_key)
        if isinstance(env_value, str) and env_value.strip():
            hints.append(env_value.strip())

    merged_hint = " ".join(hints).lower()
    if "claude" in merged_hint:
        return "CLAUDE.MD"
    if "codex" in merged_hint:
        return "AGENTS.MD"
    return "AGENTS.MD"


def _load_agent_template_text(target_filename: str, workspace_root: Path) -> str:
    target_path = workspace_root / target_filename
    candidates: list[Path] = [
        workspace_root / "AGENTS.MD",
        workspace_root / "AGENTS.md",
        PROJECT_ROOT / "AGENTS.MD",
        PROJECT_ROOT / "AGENTS.md",
    ]
    if target_filename.upper() == "AGENTS.MD":
        candidates.extend(
            [
                workspace_root / "CLAUDE.MD",
                workspace_root / "CLAUDE.md",
                PROJECT_ROOT / "CLAUDE.MD",
                PROJECT_ROOT / "CLAUDE.md",
            ]
        )

    seen: set[str] = set()
    target_key = str(target_path).lower()
    for candidate in candidates:
        key = str(candidate).lower()
        if key == target_key or key in seen:
            continue
        seen.add(key)

        if not candidate.exists():
            continue

        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        normalized = _normalize_newlines(text).strip()
        if normalized:
            return normalized + "\n"

    return FALLBACK_AGENT_RULES_TEXT


def _ensure_runtime_agent_file_ready() -> dict[str, Any]:
    workspace_root = _resolve_runtime_workspace_root()
    target_filename = _resolve_agent_target_filename()
    target_path = workspace_root / target_filename

    template_text = _load_agent_template_text(target_filename, workspace_root)
    normalized_template = _normalize_newlines(template_text).strip()
    if not normalized_template:
        normalized_template = _normalize_newlines(FALLBACK_AGENT_RULES_TEXT).strip()

    workspace_root.mkdir(parents=True, exist_ok=True)

    existed = target_path.exists()
    existing_text = ""
    if existed:
        existing_text = target_path.read_text(encoding="utf-8", errors="replace")

    normalized_existing = _normalize_newlines(existing_text)
    if normalized_template and normalized_template in normalized_existing:
        return {
            "path": str(target_path),
            "filename": target_filename,
            "workspace_root": str(workspace_root),
            "action": "already_present",
        }

    if AUTO_AGENT_BLOCK_START in normalized_existing and AUTO_AGENT_BLOCK_END in normalized_existing:
        return {
            "path": str(target_path),
            "filename": target_filename,
            "workspace_root": str(workspace_root),
            "action": "already_appended",
        }

    if normalized_existing.strip():
        append_block = (
            f"{AUTO_AGENT_BLOCK_START}\n"
            f"{normalized_template}\n"
            f"{AUTO_AGENT_BLOCK_END}\n"
        )
        base_text = existing_text
        if base_text and not base_text.endswith(("\n", "\r")):
            base_text += "\n"
        if base_text and not base_text.endswith("\n\n"):
            base_text += "\n"
        target_path.write_text(base_text + append_block, encoding="utf-8", newline="")
        return {
            "path": str(target_path),
            "filename": target_filename,
            "workspace_root": str(workspace_root),
            "action": "appended",
        }

    final_text = f"{normalized_template}\n"
    target_path.write_text(final_text, encoding="utf-8", newline="")
    action = "created" if not existed else "initialized"
    return {
        "path": str(target_path),
        "filename": target_filename,
        "workspace_root": str(workspace_root),
        "action": action,
    }


def _enforce_agent_file_gate(tool_name: str) -> None:
    try:
        result = _ensure_runtime_agent_file_ready()
    except Exception as e:
        raise PermissionError(
            f"Tool call blocked: failed to prepare AGENTS/CLAUDE file before `{tool_name}`: {e}"
        ) from e

    action = _compact_text(result.get("action"), "") if isinstance(result, dict) else ""
    if action in {"created", "initialized", "appended"}:
        state = _load_control_state()
        _append_guard_audit(
            state,
            event="agent_file_prepared",
            message=f"prepared {result.get('filename', 'AGENTS/CLAUDE')} in workspace.",
            tool=tool_name,
            extra={
                "action": action,
                "path": result.get("path"),
                "workspace_root": result.get("workspace_root"),
            },
        )
        _save_control_state(state)


def _tokenize_proc_run_command(command: Any, shell_mode: bool) -> list[str]:
    if isinstance(command, list):
        if not all(isinstance(item, str) for item in command):
            raise ValueError("`command` list must contain only strings")
        return [item for item in command if item]

    if isinstance(command, str):
        if shell_mode:
            return [command]
        parsed = shlex.split(command, posix=False if os.name == "nt" else True)
        return parsed if parsed else [command]

    raise ValueError("`command` must be a string or string array")


def _primary_command_name(tokens: list[str]) -> str:
    if not tokens:
        return ""
    head = tokens[0].strip().strip('"').strip("'")
    if not head:
        return ""
    return Path(head).name.lower()


def _enforce_proc_run_policy(args: dict[str, Any]) -> None:
    state = _load_control_state()
    guard = _ensure_guard_policy_state(state)

    if not bool(guard.get("enforce_proc_run_policy", True)):
        return

    reason = args.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        message = (
            "proc_run blocked by policy: provide `reason` and explain why CodexTools fs tools are insufficient."
        )
        _append_guard_audit(state, event="proc_blocked_missing_reason", message=message, tool="proc_run")
        _save_control_state(state)
        raise PermissionError(message)

    shell_mode = _as_bool(args.get("shell"), False)
    command = args.get("command")
    tokens = _tokenize_proc_run_command(command, shell_mode)
    command_text = command if isinstance(command, str) else " ".join(tokens)

    if shell_mode:
        message = "proc_run blocked by policy: `shell=true` is disabled. Use shell=false and CodexTools fs tools."
        _append_guard_audit(
            state,
            event="proc_blocked_shell_mode",
            message=message,
            tool="proc_run",
            extra={"command": command_text},
        )
        _save_control_state(state)
        raise PermissionError(message)

    if isinstance(command_text, str) and PROC_RUN_REDIRECTION_PATTERN.search(command_text):
        message = "proc_run blocked by policy: shell redirection or pipeline detected. Use CodexTools fs tools for file/text work."
        _append_guard_audit(
            state,
            event="proc_blocked_redirection",
            message=message,
            tool="proc_run",
            extra={"command": command_text},
        )
        _save_control_state(state)
        raise PermissionError(message)

    primary = _primary_command_name(tokens)
    if primary in PROC_RUN_DENY_PRIMARY_COMMANDS:
        message = (
            f"proc_run blocked by policy: `{primary}` is reserved for file/text operations. "
            "Use CodexTools fs tools instead."
        )
        _append_guard_audit(
            state,
            event="proc_blocked_file_text_command",
            message=message,
            tool="proc_run",
            extra={"command": command_text, "primary": primary},
        )
        _save_control_state(state)
        raise PermissionError(message)


def _enforce_tool_policy(name: str, args: dict[str, Any]) -> None:
    _enforce_agent_file_gate(name)

    if name in {"proc_run", "debug_run", "perf_benchmark"}:
        _enforce_proc_run_policy(args)



_JAR_TEXT_FILE_SUFFIXES: tuple[str, ...] = (
    ".java",
    ".kt",
    ".kts",
    ".groovy",
    ".scala",
    ".xml",
    ".properties",
    ".mf",
    ".txt",
    ".md",
    ".json",
    ".yaml",
    ".yml",
    ".csv",
    ".sql",
)


def _normalize_jar_entry_name(entry_text: str) -> str:
    entry_name = entry_text.strip().replace("\\", "/").lstrip("/")
    if not entry_name:
        raise ValueError("jar entry path must be a non-empty string")
    return entry_name


def _split_jar_path_and_entry(path_text: str) -> tuple[Path, Optional[str]]:
    raw = path_text.strip()
    for marker in ("!/", "!\\"):
        if marker in raw:
            jar_text, entry_text = raw.split(marker, 1)
            jar_path = _path(jar_text)
            if jar_path.suffix.lower() != ".jar":
                raise ValueError("archive entry syntax requires a `.jar` path before `!/`")
            return jar_path, _normalize_jar_entry_name(entry_text)
    return _path(raw), None


def _apply_text_read_limits(
    content: str,
    start_line: Any,
    end_line: Any,
    max_lines: Any,
    max_chars: Any,
) -> dict[str, Any]:
    all_lines = content.splitlines(keepends=True)
    lines = all_lines
    if start_line is not None or end_line is not None:
        start = _as_int(start_line, 1, minimum=1)
        end = _as_int(end_line, len(all_lines), minimum=1)
        if end < start:
            raise ValueError("`end_line` must be >= `start_line`")
        lines = all_lines[start - 1 : end]

    selected_line_count = len(lines)
    truncated_by_lines = False
    if max_lines is not None:
        line_limit = _as_int(max_lines, 1, minimum=1)
        if len(lines) > line_limit:
            lines = lines[:line_limit]
            truncated_by_lines = True

    limited_content = "".join(lines)

    truncated_by_chars = False
    if max_chars is not None:
        limit = _as_int(max_chars, 0, minimum=0)
        if limit and len(limited_content) > limit:
            limited_content = limited_content[:limit]
            truncated_by_chars = True

    return {
        "line_count": len(all_lines),
        "selected_line_count": selected_line_count,
        "returned_line_count": len(lines),
        "truncated": truncated_by_lines or truncated_by_chars,
        "truncated_by_lines": truncated_by_lines,
        "truncated_by_chars": truncated_by_chars,
        "content": limited_content,
    }


def _is_probably_text_jar_entry(entry_name: str) -> bool:
    lower_name = entry_name.lower()
    return lower_name.endswith(_JAR_TEXT_FILE_SUFFIXES)


def _encode_binary_payload(data: bytes, binary_encoding: str) -> str:
    if binary_encoding == "hex":
        return data.hex()
    return base64.b64encode(data).decode("ascii")


_CLASS_METHOD_ACCESS_FLAGS: tuple[tuple[int, str], ...] = (
    (0x0001, "public"),
    (0x0002, "private"),
    (0x0004, "protected"),
    (0x0008, "static"),
    (0x0010, "final"),
    (0x0020, "synchronized"),
    (0x0040, "bridge"),
    (0x0080, "varargs"),
    (0x0100, "native"),
    (0x0400, "abstract"),
    (0x0800, "strict"),
    (0x1000, "synthetic"),
)

_CLASS_PRIMITIVE_TYPES: dict[str, str] = {
    "V": "void",
    "Z": "boolean",
    "B": "byte",
    "C": "char",
    "S": "short",
    "I": "int",
    "J": "long",
    "F": "float",
    "D": "double",
}


def _class_read_u1(data: bytes, offset: int) -> tuple[int, int]:
    if offset + 1 > len(data):
        raise ValueError("invalid class format: unexpected EOF for u1")
    return data[offset], offset + 1


def _class_read_u2(data: bytes, offset: int) -> tuple[int, int]:
    if offset + 2 > len(data):
        raise ValueError("invalid class format: unexpected EOF for u2")
    return int.from_bytes(data[offset : offset + 2], "big"), offset + 2


def _class_read_u4(data: bytes, offset: int) -> tuple[int, int]:
    if offset + 4 > len(data):
        raise ValueError("invalid class format: unexpected EOF for u4")
    return int.from_bytes(data[offset : offset + 4], "big"), offset + 4


def _class_cp_utf8(cp: list[Any], index: int) -> Optional[str]:
    if index <= 0 or index >= len(cp):
        return None
    item = cp[index]
    if not isinstance(item, tuple) or len(item) < 2 or item[0] != "Utf8":
        return None
    value = item[1]
    return value if isinstance(value, str) else None


def _class_cp_class_name(cp: list[Any], index: int) -> Optional[str]:
    if index <= 0 or index >= len(cp):
        return None
    item = cp[index]
    if not isinstance(item, tuple) or len(item) < 2 or item[0] != "Class":
        return None
    name_index = item[1]
    if not isinstance(name_index, int):
        return None
    class_name = _class_cp_utf8(cp, name_index)
    if not class_name:
        return None
    return class_name.replace("/", ".")


def _parse_jvm_type_descriptor(descriptor: str, index: int) -> tuple[str, int]:
    array_dims = 0
    while index < len(descriptor) and descriptor[index] == "[":
        array_dims += 1
        index += 1

    if index >= len(descriptor):
        raise ValueError("invalid descriptor: unexpected end")

    tag = descriptor[index]
    if tag in _CLASS_PRIMITIVE_TYPES:
        result = _CLASS_PRIMITIVE_TYPES[tag]
        index += 1
    elif tag == "L":
        end_index = descriptor.find(";", index)
        if end_index < 0:
            raise ValueError(f"invalid descriptor: missing ';' in `{descriptor}`")
        result = descriptor[index + 1 : end_index].replace("/", ".")
        index = end_index + 1
    else:
        raise ValueError(f"invalid descriptor tag `{tag}` in `{descriptor}`")

    if array_dims > 0:
        result += "[]" * array_dims
    return result, index


def _parse_jvm_method_descriptor(descriptor: str) -> tuple[list[str], str]:
    if not descriptor.startswith("("):
        raise ValueError(f"invalid method descriptor: `{descriptor}`")

    index = 1
    params: list[str] = []
    while index < len(descriptor) and descriptor[index] != ")":
        parsed_type, index = _parse_jvm_type_descriptor(descriptor, index)
        params.append(parsed_type)

    if index >= len(descriptor) or descriptor[index] != ")":
        raise ValueError(f"invalid method descriptor: missing `)` in `{descriptor}`")

    return_type, index = _parse_jvm_type_descriptor(descriptor, index + 1)
    if index != len(descriptor):
        raise ValueError(f"invalid method descriptor tail in `{descriptor}`")
    return params, return_type


def _class_method_access_list(flags: int) -> list[str]:
    return [name for bit, name in _CLASS_METHOD_ACCESS_FLAGS if flags & bit]


def _class_method_signature(method: dict[str, Any]) -> str:
    access_text = " ".join(method.get("access", []))
    params_text = ", ".join(method.get("params", []))
    base = f"{method.get('name', '<unknown>')}({params_text}): {method.get('return', 'unknown')}"
    return f"{access_text} {base}".strip()


def _parse_class_methods(entry_bytes: bytes) -> dict[str, Any]:
    offset = 0
    magic, offset = _class_read_u4(entry_bytes, offset)
    if magic != 0xCAFEBABE:
        raise ValueError("invalid class format: bad magic header")

    minor_version, offset = _class_read_u2(entry_bytes, offset)
    major_version, offset = _class_read_u2(entry_bytes, offset)
    cp_count, offset = _class_read_u2(entry_bytes, offset)

    cp: list[Any] = [None] * cp_count
    cp_index = 1
    while cp_index < cp_count:
        tag, offset = _class_read_u1(entry_bytes, offset)
        if tag == 1:
            text_len, offset = _class_read_u2(entry_bytes, offset)
            if offset + text_len > len(entry_bytes):
                raise ValueError("invalid class format: utf8 entry exceeds file size")
            text = entry_bytes[offset : offset + text_len].decode("utf-8", errors="replace")
            offset += text_len
            cp[cp_index] = ("Utf8", text)
        elif tag == 7:
            name_index, offset = _class_read_u2(entry_bytes, offset)
            cp[cp_index] = ("Class", name_index)
        elif tag in {8, 16, 19, 20}:
            ref_index, offset = _class_read_u2(entry_bytes, offset)
            cp[cp_index] = ("Ref1", ref_index)
        elif tag in {3, 4}:
            _, offset = _class_read_u4(entry_bytes, offset)
            cp[cp_index] = ("Num", None)
        elif tag in {5, 6}:
            _, offset = _class_read_u4(entry_bytes, offset)
            _, offset = _class_read_u4(entry_bytes, offset)
            cp[cp_index] = ("LongDouble", None)
            cp_index += 1
        elif tag in {9, 10, 11, 12, 17, 18}:
            left, offset = _class_read_u2(entry_bytes, offset)
            right, offset = _class_read_u2(entry_bytes, offset)
            cp[cp_index] = ("Ref2", left, right)
        elif tag == 15:
            kind, offset = _class_read_u1(entry_bytes, offset)
            ref_index, offset = _class_read_u2(entry_bytes, offset)
            cp[cp_index] = ("MethodHandle", kind, ref_index)
        else:
            raise ValueError(f"unsupported class constant pool tag: {tag}")
        cp_index += 1

    _, offset = _class_read_u2(entry_bytes, offset)  # class access flags
    this_class, offset = _class_read_u2(entry_bytes, offset)
    super_class, offset = _class_read_u2(entry_bytes, offset)

    interfaces_count, offset = _class_read_u2(entry_bytes, offset)
    offset += interfaces_count * 2

    fields_count, offset = _class_read_u2(entry_bytes, offset)
    for _ in range(fields_count):
        offset += 6
        attr_count, offset = _class_read_u2(entry_bytes, offset)
        for _ in range(attr_count):
            offset += 2
            attr_len, offset = _class_read_u4(entry_bytes, offset)
            offset += attr_len

    methods_count, offset = _class_read_u2(entry_bytes, offset)
    methods: list[dict[str, Any]] = []
    for _ in range(methods_count):
        method_access, offset = _class_read_u2(entry_bytes, offset)
        method_name_index, offset = _class_read_u2(entry_bytes, offset)
        method_desc_index, offset = _class_read_u2(entry_bytes, offset)
        method_attr_count, offset = _class_read_u2(entry_bytes, offset)

        method_name = _class_cp_utf8(cp, method_name_index) or f"<name#{method_name_index}>"
        method_desc = _class_cp_utf8(cp, method_desc_index) or f"<desc#{method_desc_index}>"
        params, return_type = _parse_jvm_method_descriptor(method_desc)

        methods.append(
            {
                "name": method_name,
                "descriptor": method_desc,
                "params": params,
                "return": return_type,
                "access": _class_method_access_list(method_access),
            }
        )

        for _ in range(method_attr_count):
            offset += 2
            attr_len, offset = _class_read_u4(entry_bytes, offset)
            offset += attr_len

    class_attr_count, offset = _class_read_u2(entry_bytes, offset)
    for _ in range(class_attr_count):
        offset += 2
        attr_len, offset = _class_read_u4(entry_bytes, offset)
        offset += attr_len

    public_methods = [item for item in methods if "public" in item.get("access", [])]
    return {
        "class_name": _class_cp_class_name(cp, this_class),
        "super_class_name": _class_cp_class_name(cp, super_class) if super_class > 0 else None,
        "major_version": major_version,
        "minor_version": minor_version,
        "methods": methods,
        "public_methods": public_methods,
    }

_CFR_DEFAULT_VERSION = "0.152"
_CFR_DEFAULT_FILENAME = f"cfr-{_CFR_DEFAULT_VERSION}.jar"
_CFR_DEFAULT_URL = f"https://repo1.maven.org/maven2/org/benf/cfr/{_CFR_DEFAULT_VERSION}/{_CFR_DEFAULT_FILENAME}"


def _candidate_source_entries_for_class(class_entry: str) -> list[str]:
    normalized = class_entry.replace("\\", "/")
    if not normalized.lower().endswith(".class"):
        return []

    class_path = normalized[:-6]
    outer_class_path = class_path.split("$", 1)[0]
    roots = [class_path]
    if outer_class_path not in roots:
        roots.append(outer_class_path)

    result: list[str] = []
    for root in roots:
        for ext in (".java", ".kt", ".kts", ".scala", ".groovy"):
            result.append(f"{root}{ext}")
    return result


def _try_read_text_from_zip_entry(zip_path: Path, entry_candidates: list[str], encoding: str) -> tuple[Optional[str], Optional[str]]:
    if not entry_candidates:
        return None, None

    try:
        with zipfile.ZipFile(zip_path, "r") as archive:
            names = set(archive.namelist())
            for candidate in entry_candidates:
                if candidate in names:
                    return archive.read(candidate).decode(encoding, errors="replace"), candidate
    except Exception:
        return None, None

    return None, None


def _source_jar_candidates_for(jar_path: Path) -> list[Path]:
    direct = jar_path.with_name(f"{jar_path.stem}-sources.jar")
    discovered = [child for child in jar_path.parent.glob("*sources*.jar") if child.is_file()]

    all_candidates: list[Path] = []
    if direct.exists():
        all_candidates.append(direct)

    base_stem = jar_path.stem.lower()
    discovered.sort(
        key=lambda item: (
            -len(os.path.commonprefix([base_stem, item.stem.lower().replace("-sources", "")])),
            abs(len(item.stem) - len(jar_path.stem)),
            item.name.lower(),
        )
    )
    for item in discovered:
        if item not in all_candidates:
            all_candidates.append(item)

    return all_candidates


def _find_source_for_class_entry(jar_path: Path, class_entry: str, encoding: str) -> tuple[Optional[str], dict[str, Any]]:
    entry_candidates = _candidate_source_entries_for_class(class_entry)

    source_text, source_entry = _try_read_text_from_zip_entry(jar_path, entry_candidates, encoding)
    if source_text is not None and source_entry is not None:
        return source_text, {
            "source_origin": "jar_embedded_source",
            "source_entry": source_entry,
            "source_jar": str(jar_path),
        }

    for source_jar in _source_jar_candidates_for(jar_path):
        source_text, source_entry = _try_read_text_from_zip_entry(source_jar, entry_candidates, encoding)
        if source_text is not None and source_entry is not None:
            return source_text, {
                "source_origin": "sibling_sources_jar",
                "source_entry": source_entry,
                "source_jar": str(source_jar),
            }

    return None, {}


def _find_source_for_class_file(class_path: Path, encoding: str) -> tuple[Optional[str], dict[str, Any]]:
    stem = class_path.stem.split("$", 1)[0]
    for ext in (".java", ".kt", ".kts", ".scala", ".groovy"):
        candidate = class_path.with_name(f"{stem}{ext}")
        if candidate.exists() and candidate.is_file():
            return candidate.read_text(encoding=encoding, errors="replace"), {
                "source_origin": "filesystem_source",
                "source_path": str(candidate),
            }
    return None, {}


def _resolve_java_tool(tool_name: str, explicit_path: Optional[str]) -> Optional[str]:
    exe_name = f"{tool_name}.exe" if os.name == "nt" else tool_name

    if explicit_path:
        candidate = _path(explicit_path)
        if candidate.exists() and candidate.is_file():
            if candidate.stem.lower() == tool_name.lower():
                return str(candidate)

            sibling = candidate.with_name(exe_name)
            if sibling.exists() and sibling.is_file():
                return str(sibling)

            if tool_name == "java":
                return str(candidate)

    found = shutil.which(tool_name)
    if found:
        return found

    java_home = os.environ.get("JAVA_HOME", "").strip()
    if java_home:
        candidate = Path(java_home) / "bin" / exe_name
        if candidate.exists() and candidate.is_file():
            return str(candidate.resolve())

    return None


def _resolve_decompiler_jar(decompiler_jar_arg: Optional[str]) -> Optional[Path]:
    if decompiler_jar_arg:
        explicit = _path(decompiler_jar_arg)
        if explicit.exists() and explicit.is_file():
            return explicit

    env_value = os.environ.get("CODEXTOOLS_JAVA_DECOMPILER_JAR", "").strip()
    if env_value:
        env_path = _path(env_value)
        if env_path.exists() and env_path.is_file():
            return env_path

    workspace_root = _resolve_runtime_workspace_root()
    candidates = [
        workspace_root / ".agent" / "cache" / _CFR_DEFAULT_FILENAME,
        workspace_root / ".agent" / "tools" / _CFR_DEFAULT_FILENAME,
        workspace_root / "tools" / _CFR_DEFAULT_FILENAME,
        PROJECT_ROOT / ".agent" / "cache" / _CFR_DEFAULT_FILENAME,
        PROJECT_ROOT / ".agent" / "tools" / _CFR_DEFAULT_FILENAME,
        PROJECT_ROOT / "tools" / _CFR_DEFAULT_FILENAME,
    ]
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate

    return None


def _ensure_cfr_jar(java_bin: Optional[str], decompiler_jar_arg: Optional[str]) -> Optional[Path]:
    existing = _resolve_decompiler_jar(decompiler_jar_arg)
    if existing is not None:
        return existing

    if not java_bin:
        return None

    target = _resolve_runtime_workspace_root() / ".agent" / "cache" / _CFR_DEFAULT_FILENAME
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            with urllib.request.urlopen(_CFR_DEFAULT_URL, timeout=12) as response:
                payload = response.read()
            if not payload:
                raise ValueError("downloaded empty cfr payload")
            target.write_bytes(payload)
        return target
    except Exception:
        return None


def _run_process_capture(command: list[str], timeout_sec: int = 20) -> tuple[int, str, str]:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_sec,
        shell=False,
    )
    return completed.returncode, completed.stdout, completed.stderr


def _decompile_class_content(
    *,
    class_bytes: bytes,
    class_entry: str,
    jar_path: Optional[Path],
    class_name: Optional[str],
    java_bin_arg: Optional[str],
    decompiler_jar_arg: Optional[str],
) -> tuple[str, dict[str, Any]]:
    java_bin = _resolve_java_tool("java", java_bin_arg)
    javap_bin = _resolve_java_tool("javap", java_bin_arg)

    decompiler_jar = _ensure_cfr_jar(java_bin, decompiler_jar_arg)
    if java_bin and decompiler_jar is not None:
        try:
            with tempfile.TemporaryDirectory(prefix="codextools-cfr-") as temp_dir:
                temp_root = Path(temp_dir)
                class_file = temp_root / class_entry
                class_file.parent.mkdir(parents=True, exist_ok=True)
                class_file.write_bytes(class_bytes)
                returncode, stdout, stderr = _run_process_capture(
                    [
                        java_bin,
                        "-jar",
                        str(decompiler_jar),
                        str(class_file),
                        "--silent",
                        "true",
                    ]
                )
                if stdout.strip():
                    return stdout, {
                        "source_origin": "java_decompiler_cfr",
                        "decompiler_used": "cfr",
                        "decompiler_returncode": returncode,
                        "decompiler_stderr": stderr,
                    }
        except Exception:
            pass

    if javap_bin:
        try:
            if jar_path is not None and class_name:
                returncode, stdout, stderr = _run_process_capture(
                    [javap_bin, "-classpath", str(jar_path), "-p", "-c", class_name]
                )
            else:
                with tempfile.TemporaryDirectory(prefix="codextools-javap-") as temp_dir:
                    temp_file = Path(temp_dir) / Path(class_entry).name
                    temp_file.write_bytes(class_bytes)
                    returncode, stdout, stderr = _run_process_capture(
                        [javap_bin, "-p", "-c", str(temp_file)]
                    )
            if stdout.strip():
                return stdout, {
                    "source_origin": "java_disassembly_javap",
                    "decompiler_used": "javap",
                    "decompiler_returncode": returncode,
                    "decompiler_stderr": stderr,
                }
        except Exception:
            pass

    class_info = _parse_class_methods(class_bytes)
    signatures = [_class_method_signature(item) for item in class_info.get("methods", [])]
    return "\n".join(signatures), {
        "source_origin": "methods_fallback",
        "decompiler_used": None,
        "decompile_error": "no Java decompiler available; fallback to parsed method signatures",
    }

def tool_fs_read_text(args: dict[str, Any]) -> dict[str, Any]:
    raw_path = _require_str(args, "path")
    encoding = str(args.get("encoding", "utf-8"))

    binary_encoding = str(args.get("binary_encoding", "base64")).strip().lower()
    if binary_encoding not in {"base64", "hex"}:
        raise ValueError("`binary_encoding` must be one of: base64, hex")

    class_mode = str(args.get("class_mode", "bytecode")).strip().lower()
    if class_mode not in {"bytecode", "methods", "source"}:
        raise ValueError("`class_mode` must be one of: bytecode, methods, source")

    java_bin_arg_raw = args.get("java_bin")
    java_bin_arg: Optional[str] = None
    if java_bin_arg_raw is not None:
        if not isinstance(java_bin_arg_raw, str) or not java_bin_arg_raw.strip():
            raise ValueError("`java_bin` must be a non-empty string when provided")
        java_bin_arg = java_bin_arg_raw.strip()

    decompiler_jar_arg_raw = args.get("decompiler_jar")
    decompiler_jar_arg: Optional[str] = None
    if decompiler_jar_arg_raw is not None:
        if not isinstance(decompiler_jar_arg_raw, str) or not decompiler_jar_arg_raw.strip():
            raise ValueError("`decompiler_jar` must be a non-empty string when provided")
        decompiler_jar_arg = decompiler_jar_arg_raw.strip()

    jar_entry_arg = args.get("jar_entry")
    jar_entry: Optional[str] = None
    if jar_entry_arg is not None:
        if not isinstance(jar_entry_arg, str) or not jar_entry_arg.strip():
            raise ValueError("`jar_entry` must be a non-empty string when provided")
        jar_entry = _normalize_jar_entry_name(jar_entry_arg)

    start_line = args.get("start_line")
    end_line = args.get("end_line")
    max_lines = args.get("max_lines")
    max_chars = args.get("max_chars")

    path, inline_jar_entry = _split_jar_path_and_entry(raw_path)
    if inline_jar_entry is not None and jar_entry is not None:
        raise ValueError("Use either `path` jar `!/entry` syntax or `jar_entry`, not both")

    def _append_class_info(result: dict[str, Any], class_info: Optional[dict[str, Any]]) -> None:
        if not isinstance(class_info, dict):
            return
        result.update(
            {
                "class_name": class_info.get("class_name"),
                "super_class_name": class_info.get("super_class_name"),
                "class_major_version": class_info.get("major_version"),
                "class_minor_version": class_info.get("minor_version"),
                "declared_method_count": len(class_info.get("methods", [])),
                "public_method_count": len(class_info.get("public_methods", [])),
            }
        )

    def _build_class_result(
        *,
        class_bytes: bytes,
        class_entry: str,
        source_lookup: Callable[[], tuple[Optional[str], dict[str, Any]]],
        jar_owner: Optional[Path],
    ) -> dict[str, Any]:
        class_info: Optional[dict[str, Any]] = None
        try:
            class_info = _parse_class_methods(class_bytes)
        except Exception:
            class_info = None

        if class_mode == "methods":
            if class_info is None:
                class_info = _parse_class_methods(class_bytes)
            method_lines = [_class_method_signature(item) for item in class_info["methods"]]
            content = "\n".join(method_lines)
            content_kind = "class_methods"
            content_encoding = encoding
            extra_meta: dict[str, Any] = {"source_origin": "class_method_parser"}
        elif class_mode == "source":
            source_text, source_meta = source_lookup()
            if source_text is None:
                class_name = class_info.get("class_name") if isinstance(class_info, dict) else None
                source_text, source_meta = _decompile_class_content(
                    class_bytes=class_bytes,
                    class_entry=class_entry,
                    jar_path=jar_owner,
                    class_name=class_name,
                    java_bin_arg=java_bin_arg,
                    decompiler_jar_arg=decompiler_jar_arg,
                )
            content = source_text
            content_encoding = encoding
            source_origin = str(source_meta.get("source_origin", "")).strip().lower()
            if source_origin in {"jar_embedded_source", "sibling_sources_jar", "filesystem_source"}:
                content_kind = "class_source"
            elif source_origin == "java_disassembly_javap":
                content_kind = "class_disassembly"
            elif source_origin == "methods_fallback":
                content_kind = "class_methods"
            else:
                content_kind = "class_decompiled"
            extra_meta = source_meta
        else:
            content = _encode_binary_payload(class_bytes, binary_encoding)
            content_kind = "class_bytecode"
            content_encoding = binary_encoding
            extra_meta = {}

        limited = _apply_text_read_limits(content, start_line, end_line, max_lines, max_chars)
        result = {
            "encoding": encoding,
            "line_count": limited["line_count"],
            "selected_line_count": limited["selected_line_count"],
            "returned_line_count": limited["returned_line_count"],
            "truncated": limited["truncated"],
            "truncated_by_lines": limited["truncated_by_lines"],
            "truncated_by_chars": limited["truncated_by_chars"],
            "content": limited["content"],
            "content_kind": content_kind,
            "content_encoding": content_encoding,
            "raw_size_bytes": len(class_bytes),
        }
        result.update(extra_meta)
        _append_class_info(result, class_info)
        return result

    if path.suffix.lower() != ".jar":
        if jar_entry is not None:
            raise ValueError("`jar_entry` requires `path` to end with `.jar`")

        if path.suffix.lower() == ".class":
            class_bytes = path.read_bytes()
            class_result = _build_class_result(
                class_bytes=class_bytes,
                class_entry=path.name,
                source_lookup=lambda: _find_source_for_class_file(path, encoding),
                jar_owner=None,
            )
            class_result["path"] = str(path)
            return class_result

        if class_mode != "bytecode":
            raise ValueError("`class_mode` requires target file/entry to be `.class`")

        content = path.read_text(encoding=encoding, errors="replace")
        limited = _apply_text_read_limits(content, start_line, end_line, max_lines, max_chars)
        return {
            "path": str(path),
            "encoding": encoding,
            "line_count": limited["line_count"],
            "selected_line_count": limited["selected_line_count"],
            "returned_line_count": limited["returned_line_count"],
            "truncated": limited["truncated"],
            "truncated_by_lines": limited["truncated_by_lines"],
            "truncated_by_chars": limited["truncated_by_chars"],
            "content": limited["content"],
        }

    selected_entry = jar_entry or inline_jar_entry
    with zipfile.ZipFile(path, "r") as jar_file:
        if selected_entry is None:
            if class_mode != "bytecode":
                raise ValueError("`class_mode` requires a specific `.class` jar entry")
            entry_names = jar_file.namelist()
            content = "\n".join(entry_names)
            limited = _apply_text_read_limits(content, start_line, end_line, max_lines, max_chars)
            return {
                "path": str(path),
                "encoding": encoding,
                "line_count": limited["line_count"],
                "selected_line_count": limited["selected_line_count"],
                "returned_line_count": limited["returned_line_count"],
                "truncated": limited["truncated"],
                "truncated_by_lines": limited["truncated_by_lines"],
                "truncated_by_chars": limited["truncated_by_chars"],
                "content": limited["content"],
                "archive_type": "jar",
                "archive_entry_count": len(entry_names),
                "archive_entry": None,
                "content_kind": "jar_entry_list",
            }

        selected_entry = _normalize_jar_entry_name(selected_entry)
        if selected_entry.endswith("/"):
            raise ValueError("`jar_entry` must reference a file, not a directory")

        try:
            entry_bytes = jar_file.read(selected_entry)
        except KeyError as exc:
            raise ValueError(f"jar entry not found: {selected_entry}") from exc

    is_class_entry = selected_entry.lower().endswith(".class")
    if is_class_entry:
        class_result = _build_class_result(
            class_bytes=entry_bytes,
            class_entry=selected_entry,
            source_lookup=lambda: _find_source_for_class_entry(path, selected_entry, encoding),
            jar_owner=path,
        )
        class_result.update(
            {
                "path": f"{path}!/{selected_entry}",
                "archive_type": "jar",
                "archive_entry": selected_entry,
                "archive_entry_count": None,
            }
        )
        return class_result

    if class_mode != "bytecode":
        raise ValueError("`class_mode` requires target file/entry to be `.class`")

    is_binary = False
    entry_text = ""
    if _is_probably_text_jar_entry(selected_entry):
        entry_text = entry_bytes.decode(encoding, errors="replace")
    else:
        try:
            decoded = entry_bytes.decode(encoding)
            if "\x00" in decoded:
                is_binary = True
            else:
                entry_text = decoded
        except UnicodeDecodeError:
            is_binary = True

    if is_binary:
        content = _encode_binary_payload(entry_bytes, binary_encoding)
        content_kind = "binary"
        content_encoding = binary_encoding
    else:
        content = entry_text
        content_kind = "text"
        content_encoding = encoding

    limited = _apply_text_read_limits(content, start_line, end_line, max_lines, max_chars)
    return {
        "path": f"{path}!/{selected_entry}",
        "encoding": encoding,
        "line_count": limited["line_count"],
        "selected_line_count": limited["selected_line_count"],
        "returned_line_count": limited["returned_line_count"],
        "truncated": limited["truncated"],
        "truncated_by_lines": limited["truncated_by_lines"],
        "truncated_by_chars": limited["truncated_by_chars"],
        "content": limited["content"],
        "archive_type": "jar",
        "archive_entry": selected_entry,
        "archive_entry_count": None,
        "content_kind": content_kind,
        "content_encoding": content_encoding,
        "raw_size_bytes": len(entry_bytes),
    }

def tool_fs_read_texts(args: dict[str, Any]) -> dict[str, Any]:
    ranges_arg = args.get("ranges")
    paths_arg = args.get("paths")

    if ranges_arg is not None and paths_arg is not None:
        raise ValueError("Use either `paths` or `ranges`, not both")

    jar_entry_default_arg = args.get("jar_entry")
    jar_entry_default: Optional[str] = None
    if jar_entry_default_arg is not None:
        if not isinstance(jar_entry_default_arg, str) or not jar_entry_default_arg.strip():
            raise ValueError("`jar_entry` must be a non-empty string when provided")
        jar_entry_default = _normalize_jar_entry_name(jar_entry_default_arg)

    binary_encoding_default = str(args.get("binary_encoding", "base64")).strip().lower()
    if binary_encoding_default not in {"base64", "hex"}:
        raise ValueError("`binary_encoding` must be one of: base64, hex")

    class_mode_default = str(args.get("class_mode", "bytecode")).strip().lower()
    if class_mode_default not in {"bytecode", "methods", "source"}:
        raise ValueError("`class_mode` must be one of: bytecode, methods, source")

    java_bin_default_arg = args.get("java_bin")
    java_bin_default: Optional[str] = None
    if java_bin_default_arg is not None:
        if not isinstance(java_bin_default_arg, str) or not java_bin_default_arg.strip():
            raise ValueError("`java_bin` must be a non-empty string when provided")
        java_bin_default = java_bin_default_arg.strip()

    decompiler_jar_default_arg = args.get("decompiler_jar")
    decompiler_jar_default: Optional[str] = None
    if decompiler_jar_default_arg is not None:
        if not isinstance(decompiler_jar_default_arg, str) or not decompiler_jar_default_arg.strip():
            raise ValueError("`decompiler_jar` must be a non-empty string when provided")
        decompiler_jar_default = decompiler_jar_default_arg.strip()

    requests: list[dict[str, Any]] = []
    if ranges_arg is not None:
        if not isinstance(ranges_arg, list) or not ranges_arg:
            raise ValueError("`ranges` must be a non-empty array")
        for index, item in enumerate(ranges_arg):
            if not isinstance(item, dict):
                raise ValueError(f"`ranges[{index}]` must be an object")
            raw_path = item.get("path")
            if not isinstance(raw_path, str) or not raw_path.strip():
                raise ValueError(f"`ranges[{index}].path` must be a non-empty string")

            request: dict[str, Any] = {"path": raw_path}
            if "start_line" in item and item.get("start_line") is not None:
                request["start_line"] = _as_int(item.get("start_line"), 1, minimum=1)
            if "end_line" in item and item.get("end_line") is not None:
                request["end_line"] = _as_int(item.get("end_line"), 1, minimum=1)
            if "max_lines" in item and item.get("max_lines") is not None:
                request["max_lines"] = _as_int(item.get("max_lines"), 1, minimum=1)
            if "max_chars" in item and item.get("max_chars") is not None:
                request["max_chars"] = _as_int(item.get("max_chars"), 0, minimum=0)
            if "jar_entry" in item and item.get("jar_entry") is not None:
                jar_entry_item = item.get("jar_entry")
                if not isinstance(jar_entry_item, str) or not jar_entry_item.strip():
                    raise ValueError(f"`ranges[{index}].jar_entry` must be a non-empty string")
                request["jar_entry"] = _normalize_jar_entry_name(jar_entry_item)
            if "binary_encoding" in item and item.get("binary_encoding") is not None:
                binary_encoding_item = str(item.get("binary_encoding")).strip().lower()
                if binary_encoding_item not in {"base64", "hex"}:
                    raise ValueError(f"`ranges[{index}].binary_encoding` must be one of: base64, hex")
                request["binary_encoding"] = binary_encoding_item
            if "class_mode" in item and item.get("class_mode") is not None:
                class_mode_item = str(item.get("class_mode")).strip().lower()
                if class_mode_item not in {"bytecode", "methods", "source"}:
                    raise ValueError(f"`ranges[{index}].class_mode` must be one of: bytecode, methods, source")
                request["class_mode"] = class_mode_item
            if "java_bin" in item and item.get("java_bin") is not None:
                java_bin_item = item.get("java_bin")
                if not isinstance(java_bin_item, str) or not java_bin_item.strip():
                    raise ValueError(f"`ranges[{index}].java_bin` must be a non-empty string")
                request["java_bin"] = java_bin_item.strip()
            if "decompiler_jar" in item and item.get("decompiler_jar") is not None:
                decompiler_jar_item = item.get("decompiler_jar")
                if not isinstance(decompiler_jar_item, str) or not decompiler_jar_item.strip():
                    raise ValueError(f"`ranges[{index}].decompiler_jar` must be a non-empty string")
                request["decompiler_jar"] = decompiler_jar_item.strip()
            requests.append(request)
    else:
        paths = _require_str_list(args, "paths")
        requests = [{"path": raw_path} for raw_path in paths]

    encoding = str(args.get("encoding", "utf-8"))
    start_line = args.get("start_line")
    end_line = args.get("end_line")
    max_lines_per_file = _as_int(args.get("max_lines_per_file"), 0, minimum=0)
    max_chars_per_file = _as_int(args.get("max_chars_per_file"), 4000, minimum=0)
    max_chars_total = _as_int(args.get("max_chars_total"), 20000, minimum=0)
    max_files = _as_int(args.get("max_files"), len(requests), minimum=1)
    include_content = _as_bool(args.get("include_content"), True)
    stop_on_error = _as_bool(args.get("stop_on_error"), False)

    results: list[dict[str, Any]] = []
    total_returned_chars = 0
    total_truncated_files = 0
    errors = 0
    truncated = False
    processed_candidates = 0

    for request in requests:
        if processed_candidates >= max_files:
            truncated = True
            break
        processed_candidates += 1
        raw_path = request["path"]
        entry: dict[str, Any] = {"path": str(_path(raw_path))}
        try:
            if include_content:
                selected_binary_encoding = request.get("binary_encoding", binary_encoding_default)
                selected_class_mode = request.get("class_mode", class_mode_default)
                selected_java_bin = request.get("java_bin", java_bin_default)
                selected_decompiler_jar = request.get("decompiler_jar", decompiler_jar_default)
                read_args: dict[str, Any] = {
                    "path": raw_path,
                    "encoding": encoding,
                    "binary_encoding": selected_binary_encoding,
                    "class_mode": selected_class_mode,
                }
                if selected_java_bin is not None:
                    read_args["java_bin"] = selected_java_bin
                    entry["java_bin"] = selected_java_bin
                if selected_decompiler_jar is not None:
                    read_args["decompiler_jar"] = selected_decompiler_jar
                    entry["decompiler_jar"] = selected_decompiler_jar
                entry["binary_encoding"] = selected_binary_encoding
                entry["class_mode"] = selected_class_mode

                selected_jar_entry = request.get("jar_entry", jar_entry_default)
                if selected_jar_entry is not None:
                    read_args["jar_entry"] = selected_jar_entry
                    entry["jar_entry"] = selected_jar_entry

                selected_start_line = request.get("start_line", start_line)
                selected_end_line = request.get("end_line", end_line)
                selected_max_lines = request.get("max_lines")
                if selected_max_lines is None and max_lines_per_file > 0:
                    selected_max_lines = max_lines_per_file
                selected_max_chars = request.get("max_chars")
                if selected_max_chars is None and max_chars_per_file > 0:
                    selected_max_chars = max_chars_per_file

                if selected_start_line is not None:
                    read_args["start_line"] = selected_start_line
                    entry["start_line"] = selected_start_line
                if selected_end_line is not None:
                    read_args["end_line"] = selected_end_line
                    entry["end_line"] = selected_end_line
                if selected_max_lines is not None:
                    read_args["max_lines"] = selected_max_lines
                    entry["max_lines"] = selected_max_lines

                char_limit = selected_max_chars
                if max_chars_total > 0:
                    remaining = max_chars_total - total_returned_chars
                    if remaining <= 0:
                        truncated = True
                        break
                    if char_limit is None or remaining < char_limit:
                        char_limit = remaining
                if char_limit is not None:
                    read_args["max_chars"] = char_limit
                    entry["max_chars"] = char_limit

                read_result = tool_fs_read_text(read_args)
                entry.update(
                    {
                        "path": read_result.get("path", entry["path"]),
                        "exists": True,
                        "line_count": read_result["line_count"],
                        "selected_line_count": read_result["selected_line_count"],
                        "returned_line_count": read_result["returned_line_count"],
                        "truncated": read_result["truncated"],
                        "truncated_by_lines": read_result["truncated_by_lines"],
                        "truncated_by_chars": read_result["truncated_by_chars"],
                        "content": read_result["content"],
                    }
                )
                for meta_key in (
                    "archive_type",
                    "archive_entry",
                    "archive_entry_count",
                    "content_kind",
                    "content_encoding",
                    "raw_size_bytes",
                    "class_name",
                    "super_class_name",
                    "class_major_version",
                    "class_minor_version",
                    "declared_method_count",
                    "public_method_count",
                    "source_origin",
                    "source_entry",
                    "source_jar",
                    "source_path",
                    "decompiler_used",
                    "decompiler_returncode",
                    "decompiler_stderr",
                    "decompile_error",
                ):
                    if meta_key in read_result:
                        entry[meta_key] = read_result[meta_key]
                total_returned_chars += len(read_result["content"])
                if read_result["truncated"]:
                    total_truncated_files += 1
            else:
                stat_result = tool_fs_stat({"path": raw_path})
                entry.update(stat_result)
        except Exception as e:
            errors += 1
            entry["error"] = f"{type(e).__name__}: {e}"
            if stop_on_error:
                raise
        results.append(entry)

    return {
        "requested_file_count": len(requests),
        "processed_file_count": len(results),
        "max_files": max_files,
        "include_content": include_content,
        "max_chars_per_file": max_chars_per_file,
        "max_chars_total": max_chars_total,
        "total_returned_chars": total_returned_chars,
        "truncated_file_count": total_truncated_files,
        "errors": errors,
        "truncated": truncated,
        "results": results,
    }

def tool_fs_write_text(args: dict[str, Any]) -> dict[str, Any]:
    path = _path(_require_str(args, "path"))
    content = args.get("content", "")
    if not isinstance(content, str):
        raise ValueError("`content` must be string")

    append = _as_bool(args.get("append"), False)
    create_dirs = _as_bool(args.get("create_dirs"), True)
    encoding = str(args.get("encoding", "utf-8"))

    if create_dirs:
        path.parent.mkdir(parents=True, exist_ok=True)

    mode = "a" if append else "w"
    with path.open(mode, encoding=encoding, newline="") as f:
        written = f.write(content)

    return {"path": str(path), "mode": mode, "encoding": encoding, "written_chars": written}

def tool_fs_replace_text(args: dict[str, Any]) -> dict[str, Any]:
    path = _path(_require_str(args, "path"))
    old = _require_str(args, "old")
    new = args.get("new", "")
    if not isinstance(new, str):
        raise ValueError("`new` must be string")
    count = _as_int(args.get("count"), 0, minimum=0)

    text = path.read_text(encoding="utf-8", errors="replace")
    available = text.count(old)
    replaced = min(available, count) if count else available
    updated = text.replace(old, new, count) if count else text.replace(old, new)
    path.write_text(updated, encoding="utf-8", newline="")

    return {"path": str(path), "replacements": replaced}


def _parse_regex_flags(flags_text: str) -> int:
    mapping = {
        "i": re.IGNORECASE,
        "m": re.MULTILINE,
        "s": re.DOTALL,
        "x": re.VERBOSE,
        "a": re.ASCII,
    }
    combined = 0
    seen: set[str] = set()
    for char in flags_text.lower():
        if char in seen:
            continue
        flag = mapping.get(char)
        if flag is None:
            raise ValueError(f"`flags` contains unsupported value: {char}")
        combined |= flag
        seen.add(char)
    return combined


def tool_fs_replace_regex(args: dict[str, Any]) -> dict[str, Any]:
    path = _path(_require_str(args, "path"))
    pattern = _require_str(args, "pattern")
    repl = args.get("repl", "")
    if not isinstance(repl, str):
        raise ValueError("`repl` must be string")

    count = _as_int(args.get("count"), 0, minimum=0)
    flags_text = args.get("flags", "")
    if not isinstance(flags_text, str):
        raise ValueError("`flags` must be string")

    compiled = re.compile(pattern, flags=_parse_regex_flags(flags_text))
    text = path.read_text(encoding="utf-8", errors="replace")
    updated, replaced = compiled.subn(repl, text, count=count)
    path.write_text(updated, encoding="utf-8", newline="")

    return {
        "path": str(path),
        "pattern": pattern,
        "flags": flags_text,
        "count": count,
        "replacements": replaced,
    }


def tool_fs_patch_lines(args: dict[str, Any]) -> dict[str, Any]:
    path = _path(_require_str(args, "path"))
    start_line = _as_int(args.get("start_line"), 0, minimum=1)
    end_arg = args.get("end_line")
    end_line = _as_int(end_arg, start_line, minimum=0) if end_arg is not None else start_line
    content = args.get("content", "")
    if not isinstance(content, str):
        raise ValueError("`content` must be string")
    encoding = str(args.get("encoding", "utf-8"))

    text = path.read_text(encoding=encoding, errors="replace")
    lines = text.splitlines(keepends=True)
    line_count = len(lines)

    if start_line > line_count + 1:
        raise ValueError("`start_line` is out of range")
    if end_line > line_count:
        raise ValueError("`end_line` is out of range")
    if end_line < start_line - 1:
        raise ValueError("`end_line` must be >= `start_line - 1`")

    replacement_lines = content.splitlines(keepends=True)
    removed = lines[start_line - 1 : end_line]
    updated_lines = lines[: start_line - 1] + replacement_lines + lines[end_line:]
    path.write_text("".join(updated_lines), encoding=encoding, newline="")

    return {
        "path": str(path),
        "encoding": encoding,
        "start_line": start_line,
        "end_line": end_line,
        "insert_mode": end_line == start_line - 1,
        "removed_lines": len(removed),
        "added_lines": len(replacement_lines),
        "line_count_before": line_count,
        "line_count_after": len(updated_lines),
    }


def tool_fs_list(args: dict[str, Any]) -> dict[str, Any]:
    base = _path(_require_str(args, "path"))
    if not base.is_dir():
        raise ValueError(f"not a directory: {base}")

    recursive = _as_bool(args.get("recursive"), False)
    pattern = str(args.get("pattern", "*"))
    max_entries = _as_int(args.get("max_entries"), 1000, minimum=1)

    iterator = base.rglob(pattern) if recursive else base.glob(pattern)
    entries: list[dict[str, Any]] = []
    truncated = False
    for child in iterator:
        entries.append(
            {
                "path": str(child),
                "relative": str(child.relative_to(base)),
                "kind": "directory" if child.is_dir() else "file" if child.is_file() else "other",
            }
        )
        if len(entries) >= max_entries:
            truncated = True
            break

    return {"path": str(base), "count": len(entries), "truncated": truncated, "entries": entries}


def tool_fs_list_files(args: dict[str, Any]) -> dict[str, Any]:
    base = _path(_require_str(args, "path"))
    if not base.is_dir():
        raise ValueError(f"not a directory: {base}")

    recursive = _as_bool(args.get("recursive"), False)
    pattern = str(args.get("pattern", "*"))
    max_entries = _as_int(args.get("max_entries"), 1000, minimum=1)

    iterator = base.rglob(pattern) if recursive else base.glob(pattern)
    entries: list[dict[str, Any]] = []
    truncated = False
    for child in iterator:
        if not child.is_file():
            continue
        entries.append(
            {
                "path": str(child),
                "relative": str(child.relative_to(base)),
                "size_bytes": child.stat().st_size,
            }
        )
        if len(entries) >= max_entries:
            truncated = True
            break

    return {"path": str(base), "count": len(entries), "truncated": truncated, "entries": entries}


def _trim_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return text[:max_chars]
    return text[: max_chars - 3] + "..."


def _build_excerpt(lines: list[str], line_index: int, before_lines: int, after_lines: int, max_chars: int) -> str:
    start = max(0, line_index - before_lines)
    end = min(len(lines), line_index + after_lines + 1)
    return _trim_text("\n".join(lines[start:end]), max_chars)


def tool_fs_search_text(args: dict[str, Any]) -> dict[str, Any]:
    base = _path(_require_str(args, "path"))
    if not base.is_dir():
        raise ValueError(f"not a directory: {base}")

    queries_arg = args.get("queries")
    if queries_arg is None:
        queries = [_require_str(args, "query")]
    else:
        if not isinstance(queries_arg, list) or not queries_arg:
            raise ValueError("`queries` must be a non-empty string array")
        queries = []
        for index, item in enumerate(queries_arg):
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"`queries[{index}]` must be a non-empty string")
            queries.append(item)

    recursive = _as_bool(args.get("recursive"), True)
    pattern = str(args.get("pattern", "*"))
    regex_mode = _as_bool(args.get("regex"), False)
    case_sensitive = _as_bool(args.get("case_sensitive"), True)
    flags_text = str(args.get("flags", ""))
    encoding = str(args.get("encoding", "utf-8"))

    before_lines = _as_int(args.get("before_lines"), 0, minimum=0, maximum=100)
    after_lines = _as_int(args.get("after_lines"), 0, minimum=0, maximum=100)
    max_excerpt_chars = _as_int(args.get("max_excerpt_chars"), 220, minimum=0)
    max_match_chars = _as_int(args.get("max_match_chars"), 120, minimum=0)
    max_files = _as_int(args.get("max_files"), 500, minimum=1)
    max_file_bytes = _as_int(args.get("max_file_bytes"), 2 * 1024 * 1024, minimum=0)
    max_matches = _as_int(args.get("max_matches"), 200, minimum=1)
    max_matches_per_file = _as_int(args.get("max_matches_per_file"), 20, minimum=1)
    max_skipped_files = _as_int(args.get("max_skipped_files"), 100, minimum=0)
    include_preview = _as_bool(args.get("include_preview"), True)

    regex_flags = _parse_regex_flags(flags_text) if flags_text else 0
    if not case_sensitive:
        regex_flags |= re.IGNORECASE

    compiled_queries: list[tuple[str, re.Pattern[str]]] = []
    for query in queries:
        pattern_text = query if regex_mode else re.escape(query)
        compiled_queries.append((query, re.compile(pattern_text, flags=regex_flags)))

    iterator = base.rglob(pattern) if recursive else base.glob(pattern)
    matches: list[dict[str, Any]] = []
    skipped_files: list[dict[str, Any]] = []
    skipped_count = 0
    files_scanned = 0
    matched_files = 0
    truncated = False

    for child in iterator:
        if not child.is_file():
            continue
        if files_scanned >= max_files:
            truncated = True
            break

        files_scanned += 1
        try:
            if max_file_bytes > 0 and child.stat().st_size > max_file_bytes:
                skipped_count += 1
                if len(skipped_files) < max_skipped_files:
                    skipped_files.append({"path": str(child), "reason": "file too large"})
                continue
            text = child.read_text(encoding=encoding, errors="replace")
        except Exception as e:
            skipped_count += 1
            if len(skipped_files) < max_skipped_files:
                skipped_files.append({"path": str(child), "reason": f"{type(e).__name__}: {e}"})
            continue

        lines = text.splitlines()
        file_match_count = 0
        file_has_match = False
        stop_all = False

        for line_index, line_text in enumerate(lines):
            if file_match_count >= max_matches_per_file:
                break
            for query_text, compiled in compiled_queries:
                if file_match_count >= max_matches_per_file:
                    break
                for match in compiled.finditer(line_text):
                    file_has_match = True
                    file_match_count += 1
                    record: dict[str, Any] = {
                        "path": str(child),
                        "relative": str(child.relative_to(base)),
                        "line": line_index + 1,
                        "column": match.start() + 1,
                        "end_column": match.end() + 1,
                        "query": query_text,
                        "match": _trim_text(match.group(0), max_match_chars),
                    }
                    if include_preview:
                        record["preview"] = _build_excerpt(lines, line_index, before_lines, after_lines, max_excerpt_chars)
                    matches.append(record)

                    if len(matches) >= max_matches:
                        truncated = True
                        stop_all = True
                        break
                    if file_match_count >= max_matches_per_file:
                        break
                if stop_all:
                    break
            if stop_all:
                break

        if file_has_match:
            matched_files += 1
        if len(matches) >= max_matches:
            break

    return {
        "path": str(base),
        "pattern": pattern,
        "queries": queries,
        "regex": regex_mode,
        "case_sensitive": case_sensitive,
        "encoding": encoding,
        "scanned_files": files_scanned,
        "matched_files": matched_files,
        "match_count": len(matches),
        "max_matches": max_matches,
        "max_matches_per_file": max_matches_per_file,
        "max_file_bytes": max_file_bytes,
        "skipped_files_count": skipped_count,
        "skipped_files": skipped_files,
        "truncated": truncated,
        "matches": matches,
    }


def tool_fs_stat(args: dict[str, Any]) -> dict[str, Any]:
    path = _path(_require_str(args, "path"))
    if not path.exists():
        return {"path": str(path), "exists": False}
    st = path.stat()
    return {
        "path": str(path),
        "exists": True,
        "kind": "directory" if path.is_dir() else "file" if path.is_file() else "other",
        "size_bytes": st.st_size,
        "modified": st.st_mtime,
        "created": st.st_ctime,
    }


def tool_fs_delete(args: dict[str, Any]) -> dict[str, Any]:
    path = _path(_require_str(args, "path"))
    recursive = _as_bool(args.get("recursive"), False)
    if not path.exists():
        return {"path": str(path), "deleted": False, "reason": "not found"}

    if path.is_dir():
        if recursive:
            shutil.rmtree(path)
        else:
            path.rmdir()
    else:
        path.unlink()
    return {"path": str(path), "deleted": True}


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _move_or_copy_path(src: Path, dst: Path, copy: bool, overwrite: bool) -> dict[str, Any]:
    if not src.exists():
        raise ValueError(f"source not found: {src}")
    if src == dst:
        raise ValueError("source and destination must be different")
    if dst.exists():
        if not overwrite:
            raise ValueError(f"destination exists: {dst}. Set overwrite=true to replace.")
        _remove_path(dst)

    dst.parent.mkdir(parents=True, exist_ok=True)
    if copy:
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=False)
        else:
            shutil.copy2(src, dst)
        action = "copied"
    else:
        shutil.move(str(src), str(dst))
        action = "moved"

    return {"source": str(src), "destination": str(dst), "action": action}


def tool_fs_move(args: dict[str, Any]) -> dict[str, Any]:
    src = _path(_require_str(args, "source"))
    dst = _path(_require_str(args, "destination"))
    copy = _as_bool(args.get("copy"), False)
    overwrite = _as_bool(args.get("overwrite"), False)

    if not src.exists():
        raise ValueError(f"source not found: {src}")
    if src == dst:
        raise ValueError("source and destination must be different")
    if dst.exists() and not overwrite:
        raise ValueError(f"destination exists: {dst}. Set overwrite=true to replace.")

    return _move_or_copy_path(src, dst, copy=copy, overwrite=overwrite)


def tool_fs_move_file(args: dict[str, Any]) -> dict[str, Any]:
    src = _path(_require_str(args, "source"))
    dst = _path(_require_str(args, "destination"))
    overwrite = _as_bool(args.get("overwrite"), False)
    if not src.exists():
        raise ValueError(f"source not found: {src}")
    if not src.is_file():
        raise ValueError(f"source is not a file: {src}")
    if dst.exists() and dst.is_dir():
        raise ValueError(f"destination is a directory: {dst}")
    if dst.exists() and not overwrite:
        raise ValueError(f"destination exists: {dst}. Set overwrite=true to replace.")
    return _move_or_copy_path(src, dst, copy=False, overwrite=overwrite)


def tool_fs_copy_file(args: dict[str, Any]) -> dict[str, Any]:
    src = _path(_require_str(args, "source"))
    dst = _path(_require_str(args, "destination"))
    overwrite = _as_bool(args.get("overwrite"), False)
    if not src.exists():
        raise ValueError(f"source not found: {src}")
    if not src.is_file():
        raise ValueError(f"source is not a file: {src}")
    if dst.exists() and dst.is_dir():
        raise ValueError(f"destination is a directory: {dst}")
    if dst.exists() and not overwrite:
        raise ValueError(f"destination exists: {dst}. Set overwrite=true to replace.")
    return _move_or_copy_path(src, dst, copy=True, overwrite=overwrite)


def tool_fs_create(args: dict[str, Any]) -> dict[str, Any]:
    path = _path(_require_str(args, "path"))
    kind = str(args.get("kind", "file")).strip().lower()
    parents = _as_bool(args.get("parents"), True)
    overwrite = _as_bool(args.get("overwrite"), False)
    encoding = str(args.get("encoding", "utf-8"))
    content = args.get("content", "")
    if not isinstance(content, str):
        raise ValueError("`content` must be string")

    if kind in {"dir", "directory"}:
        if path.exists():
            if path.is_dir():
                return {"path": str(path), "kind": "directory", "created": False, "exists": True}
            raise ValueError(f"path exists and is not a directory: {path}")
        path.mkdir(parents=parents, exist_ok=False)
        return {"path": str(path), "kind": "directory", "created": True, "exists": True}

    if kind != "file":
        raise ValueError("`kind` must be one of: file, dir, directory")

    existed = path.exists()
    if existed and path.is_dir():
        raise ValueError(f"path exists and is a directory: {path}")
    if existed and not overwrite:
        raise ValueError(f"file exists: {path}. Set overwrite=true to replace.")
    if parents:
        path.parent.mkdir(parents=True, exist_ok=True)
    written = path.write_text(content, encoding=encoding, newline="")
    return {
        "path": str(path),
        "kind": "file",
        "created": not existed,
        "overwritten": existed,
        "encoding": encoding,
        "written_chars": written,
    }


def _normalize_command(command: Any, shell_mode: bool) -> Union[str, list[str]]:
    if isinstance(command, str):
        if shell_mode:
            return command
        parts = shlex.split(command, posix=False if os.name == "nt" else True)
        if os.name == "nt" and parts:
            resolved = shutil.which(parts[0])
            if resolved:
                parts[0] = resolved
        return parts
    if isinstance(command, list) and all(isinstance(x, str) for x in command):
        if shell_mode:
            return subprocess.list2cmdline(command) if os.name == "nt" else " ".join(shlex.quote(x) for x in command)
        if os.name == "nt" and command:
            resolved = shutil.which(command[0])
            if resolved:
                command = [resolved] + command[1:]
        return command
    raise ValueError("`command` must be a string or string array")


def tool_proc_run(args: dict[str, Any]) -> dict[str, Any]:
    if "command" not in args:
        raise ValueError("`command` is required")

    reason = _require_str(args, "reason")
    shell_mode = _as_bool(args.get("shell"), False)
    command = _normalize_command(args["command"], shell_mode)
    timeout_sec = _as_int(args.get("timeout_sec"), 60, minimum=1, maximum=600)
    cwd = args.get("cwd")
    cwd_path = _path(cwd) if isinstance(cwd, str) and cwd.strip() else None

    # Build environment: inherit current env, merge user overrides
    run_env = os.environ.copy()
    run_env["PYTHONUTF8"] = "1"
    extra_env = args.get("env")
    if isinstance(extra_env, dict):
        for k, v in extra_env.items():
            if isinstance(k, str) and isinstance(v, str):
                run_env[k] = v

    started = time.time()
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd_path) if cwd_path else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
            shell=shell_mode,
            env=run_env,
        )
        return {
            "command": command,
            "shell": shell_mode,
            "reason": reason,
            "cwd": str(cwd_path) if cwd_path else None,
            "timed_out": False,
            "duration_ms": int((time.time() - started) * 1000),
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    except subprocess.TimeoutExpired as e:
        stdout = e.stdout if isinstance(e.stdout, str) else ""
        stderr = e.stderr if isinstance(e.stderr, str) else ""
        return {
            "command": command,
            "shell": shell_mode,
            "reason": reason,
            "cwd": str(cwd_path) if cwd_path else None,
            "timed_out": True,
            "duration_ms": int((time.time() - started) * 1000),
            "returncode": None,
            "stdout": stdout,
            "stderr": stderr,
        }
    except FileNotFoundError as e:
        return {
            "command": command,
            "shell": shell_mode,
            "reason": reason,
            "cwd": str(cwd_path) if cwd_path else None,
            "timed_out": False,
            "duration_ms": int((time.time() - started) * 1000),
            "returncode": -1,
            "stdout": "",
            "stderr": f"FileNotFoundError: {e}. Try shell=true or use full path.",
        }


def tool_img_draw(args: dict[str, Any]) -> dict[str, Any]:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        install_package_with_pip("pillow", "img_draw")
        try:
            from PIL import Image, ImageDraw, ImageFont
        except Exception as e:
            raise RuntimeError(f"Pillow is required: {e}") from e
    except Exception as e:
        raise RuntimeError(f"Pillow is required: {e}") from e

    path = _path(_require_str(args, "path"))
    width = _as_int(args.get("width"), 1024, minimum=1, maximum=20000)
    height = _as_int(args.get("height"), 1024, minimum=1, maximum=20000)
    background = args.get("background", "#ffffff")
    shapes = args.get("shapes", [])
    if not isinstance(shapes, list):
        raise ValueError("`shapes` must be a list")

    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGBA", (width, height), color=background)
    draw = ImageDraw.Draw(img)
    skipped: list[str] = []

    for i, shape in enumerate(shapes):
        if not isinstance(shape, dict):
            skipped.append(f"shape[{i}] not object")
            continue
        kind = str(shape.get("type", "")).lower()
        try:
            if kind == "line":
                draw.line(
                    [
                        (_as_int(shape.get("x1"), 0), _as_int(shape.get("y1"), 0)),
                        (_as_int(shape.get("x2"), 0), _as_int(shape.get("y2"), 0)),
                    ],
                    fill=shape.get("color", "#000000"),
                    width=_as_int(shape.get("width"), 1, minimum=1),
                )
            elif kind == "rectangle":
                draw.rectangle(
                    [
                        (_as_int(shape.get("x1"), 0), _as_int(shape.get("y1"), 0)),
                        (_as_int(shape.get("x2"), 0), _as_int(shape.get("y2"), 0)),
                    ],
                    outline=shape.get("outline", "#000000"),
                    fill=shape.get("fill"),
                    width=_as_int(shape.get("width"), 1, minimum=1),
                )
            elif kind == "ellipse":
                draw.ellipse(
                    [
                        (_as_int(shape.get("x1"), 0), _as_int(shape.get("y1"), 0)),
                        (_as_int(shape.get("x2"), 0), _as_int(shape.get("y2"), 0)),
                    ],
                    outline=shape.get("outline", "#000000"),
                    fill=shape.get("fill"),
                    width=_as_int(shape.get("width"), 1, minimum=1),
                )
            elif kind == "text":
                text = str(shape.get("text", ""))
                x = _as_int(shape.get("x"), 0)
                y = _as_int(shape.get("y"), 0)
                color = shape.get("color", "#000000")
                size = _as_int(shape.get("font_size"), 20, minimum=1, maximum=512)
                font_path = shape.get("font_path")
                if isinstance(font_path, str) and font_path.strip():
                    font = ImageFont.truetype(font_path, size=size)
                else:
                    font = ImageFont.load_default()
                draw.text((x, y), text, fill=color, font=font)
            elif kind == "polygon":
                points = shape.get("points", [])
                if not isinstance(points, list) or not points:
                    raise ValueError("polygon requires points")
                normalized: list[tuple[int, int]] = []
                for point in points:
                    if not isinstance(point, (list, tuple)) or len(point) != 2:
                        raise ValueError("point must be [x,y]")
                    normalized.append((_as_int(point[0], 0), _as_int(point[1], 0)))
                draw.polygon(normalized, outline=shape.get("outline", "#000000"), fill=shape.get("fill"))
            else:
                skipped.append(f"shape[{i}] unknown type: {kind}")
        except Exception as e:
            skipped.append(f"shape[{i}] failed: {e}")

    fmt = str(args.get("format", "")).strip().upper()
    if not fmt:
        fmt = path.suffix[1:].upper() if path.suffix else "PNG"
    if fmt == "JPG":
        fmt = "JPEG"
    if fmt in {"JPEG", "BMP"} and img.mode == "RGBA":
        img = img.convert("RGB")

    img.save(path, format=fmt)
    return {
        "path": str(path),
        "format": fmt,
        "width": width,
        "height": height,
        "shape_count": len(shapes),
        "skipped": skipped,
    }


def tool_sound_beep(args: dict[str, Any]) -> dict[str, Any]:
    freq = _as_int(args.get("frequency"), 1200, minimum=37, maximum=32767)
    duration = _as_int(args.get("duration_ms"), 180, minimum=10, maximum=10000)
    repeat = _as_int(args.get("repeat"), 1, minimum=1, maximum=20)
    interval = _as_int(args.get("interval_ms"), 80, minimum=0, maximum=5000)

    if sys.platform.startswith("win"):
        import winsound

        for i in range(repeat):
            winsound.Beep(freq, duration)
            if i < repeat - 1 and interval > 0:
                time.sleep(interval / 1000.0)
        mode = "winsound"
    else:
        for i in range(repeat):
            print("\a", end="", flush=True)
            time.sleep(duration / 1000.0)
            if i < repeat - 1 and interval > 0:
                time.sleep(interval / 1000.0)
        mode = "terminal-bell"

    return {"played": True, "mode": mode, "frequency": freq, "duration_ms": duration, "repeat": repeat}


TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], Union[dict[str, Any], str]]] = {
    "fs_read_text": tool_fs_read_text,
    "fs_read_texts": tool_fs_read_texts,
    "fs_write_text": tool_fs_write_text,
    "fs_replace_text": tool_fs_replace_text,
    "fs_replace_regex": tool_fs_replace_regex,
    "fs_patch_lines": tool_fs_patch_lines,
    "fs_list": tool_fs_list,
    "fs_list_files": tool_fs_list_files,
    "fs_search_text": tool_fs_search_text,
    "fs_stat": tool_fs_stat,
    "fs_delete": tool_fs_delete,
    "fs_move": tool_fs_move,
    "fs_move_file": tool_fs_move_file,
    "fs_copy_file": tool_fs_copy_file,
    "fs_create": tool_fs_create,
    "proc_run": tool_proc_run,
    "img_draw": tool_img_draw,
    "sound_beep": tool_sound_beep,
}


TOOL_DESCRIPTIONS: dict[str, str] = {
    "fs_read_text": "Read a UTF-8 text file or a `.jar` entry. Supports line range, max_lines/max_chars truncation, `.class` bytecode/method parsing, and source-first Java decompile via `class_mode=source`.",
    "fs_read_texts": "Read multiple UTF-8 files or `.jar` entries with per-file/total caps, optional per-range windows, and class read modes (`bytecode|methods|source`).",
    "fs_write_text": "Write or append UTF-8 text to a file. Creates parent dirs by default.",
    "fs_replace_text": "Find and replace text in a UTF-8 file.",
    "fs_replace_regex": "Find and replace text with a regular expression in a UTF-8 file.",
    "fs_patch_lines": "Replace or insert a line range in a UTF-8 text file.",
    "fs_list": "List files and directories matching a glob pattern.",
    "fs_list_files": "List files matching a glob pattern.",
    "fs_search_text": "Search text in multiple files with batch queries and capped match/snippet output.",
    "fs_stat": "Get file/directory metadata: existence, size, timestamps.",
    "fs_delete": "Delete a file or directory. Use recursive=true for non-empty dirs.",
    "fs_move": "Move or copy a file/directory. Set copy=true to copy instead of move.",
    "fs_move_file": "Move a single file from source to destination.",
    "fs_copy_file": "Copy a single file from source to destination.",
    "fs_create": "Create a file or directory. Use kind=file|dir.",
    "proc_run": "Execute a shell command and capture UTF-8 stdout/stderr. Requires `reason` and blocks shell/file-text operations that should use fs tools.",
    "img_draw": "Draw an image with primitive shapes (line, rect, ellipse, text, polygon). Requires Pillow.",
    "sound_beep": "Play a beep notification sound.",
}


TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "fs_read_text": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "encoding": {"type": "string", "default": "utf-8"},
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
            "max_lines": {"type": "integer", "minimum": 1},
            "max_chars": {"type": "integer", "minimum": 0},
            "jar_entry": {"type": "string", "description": "Entry path inside .jar. Optional when using `path` syntax `archive.jar!/entry`."},
            "binary_encoding": {"type": "string", "enum": ["base64", "hex"], "default": "base64", "description": "Encoding used when returning binary entry data such as .class."},
            "class_mode": {"type": "string", "enum": ["bytecode", "methods", "source"], "default": "bytecode", "description": "How to read .class targets: raw bytecode encoding, parsed method signatures, or source-first lookup with Java decompile fallback."},
            "java_bin": {"type": "string", "description": "Optional path to Java executable used for decompile/disassemble when class_mode=source."},
            "decompiler_jar": {"type": "string", "description": "Optional path to decompiler jar (for example CFR). When omitted, server will try built-in discovery/download."},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    "fs_read_texts": {
        "type": "object",
        "properties": {
            "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "ranges": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "start_line": {"type": "integer", "minimum": 1},
                        "end_line": {"type": "integer", "minimum": 1},
                        "max_lines": {"type": "integer", "minimum": 1},
                        "max_chars": {"type": "integer", "minimum": 0},
                        "jar_entry": {"type": "string", "description": "Entry path inside .jar for this range item."},
                        "binary_encoding": {"type": "string", "enum": ["base64", "hex"], "description": "Encoding for binary jar entry data."},
                        "class_mode": {"type": "string", "enum": ["bytecode", "methods", "source"], "description": "How to read .class target for this range item (source mode tries source first, then decompile)."},
                        "java_bin": {"type": "string", "description": "Optional Java executable path for this range item when class_mode=source."},
                        "decompiler_jar": {"type": "string", "description": "Optional decompiler jar path for this range item when class_mode=source."}
                    },
                    "required": ["path"],
                    "additionalProperties": False
                },
                "minItems": 1
            },
            "encoding": {"type": "string", "default": "utf-8"},
            "jar_entry": {"type": "string", "description": "Default entry path inside .jar when reading jar files."},
            "binary_encoding": {"type": "string", "enum": ["base64", "hex"], "default": "base64", "description": "Default encoding for binary jar entry data such as .class."},
            "class_mode": {"type": "string", "enum": ["bytecode", "methods", "source"], "default": "bytecode", "description": "Default class read mode for .class targets, including source-first Java decompile."},
            "java_bin": {"type": "string", "description": "Default Java executable path used by source mode when decompilation is needed."},
            "decompiler_jar": {"type": "string", "description": "Default decompiler jar path used by source mode when decompilation is needed."},
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
            "max_lines_per_file": {"type": "integer", "minimum": 0, "default": 0},
            "max_chars_per_file": {"type": "integer", "minimum": 0, "default": 4000},
            "max_chars_total": {"type": "integer", "minimum": 0, "default": 20000},
            "max_files": {"type": "integer", "minimum": 1},
            "include_content": {"type": "boolean", "default": True},
            "stop_on_error": {"type": "boolean", "default": False}
        },
        "anyOf": [{"required": ["paths"]}, {"required": ["ranges"]}],
        "additionalProperties": False,
    },
    "fs_write_text": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "append": {"type": "boolean", "default": False},
            "create_dirs": {"type": "boolean", "default": True},
            "encoding": {"type": "string", "default": "utf-8"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    },
    "fs_replace_text": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old": {"type": "string"},
            "new": {"type": "string", "default": ""},
            "count": {"type": "integer", "minimum": 0, "default": 0},
        },
        "required": ["path", "old"],
        "additionalProperties": False,
    },
    "fs_replace_regex": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "pattern": {"type": "string"},
            "repl": {"type": "string", "default": ""},
            "count": {"type": "integer", "minimum": 0, "default": 0},
            "flags": {"type": "string", "default": "", "description": "Python regex flags string: i,m,s,x,a"},
        },
        "required": ["path", "pattern"],
        "additionalProperties": False,
    },
    "fs_patch_lines": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 0, "description": "Set to start_line-1 to insert before start_line"},
            "content": {"type": "string", "default": ""},
            "encoding": {"type": "string", "default": "utf-8"},
        },
        "required": ["path", "start_line"],
        "additionalProperties": False,
    },
    "fs_list": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "recursive": {"type": "boolean", "default": False},
            "pattern": {"type": "string", "default": "*"},
            "max_entries": {"type": "integer", "minimum": 1, "default": 1000},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    "fs_list_files": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "recursive": {"type": "boolean", "default": False},
            "pattern": {"type": "string", "default": "*"},
            "max_entries": {"type": "integer", "minimum": 1, "default": 1000},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    "fs_search_text": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "query": {"type": "string", "description": "Single query string. Use this or `queries`."},
            "queries": {"type": "array", "items": {"type": "string"}, "description": "Batch query list."},
            "recursive": {"type": "boolean", "default": True},
            "pattern": {"type": "string", "default": "*"},
            "regex": {"type": "boolean", "default": False, "description": "Treat query/query list as regex patterns."},
            "case_sensitive": {"type": "boolean", "default": True},
            "flags": {"type": "string", "default": "", "description": "Python regex flags string: i,m,s,x,a"},
            "encoding": {"type": "string", "default": "utf-8"},
            "before_lines": {"type": "integer", "minimum": 0, "maximum": 100, "default": 0},
            "after_lines": {"type": "integer", "minimum": 0, "maximum": 100, "default": 0},
            "max_excerpt_chars": {"type": "integer", "minimum": 0, "default": 220},
            "max_match_chars": {"type": "integer", "minimum": 0, "default": 120},
            "max_files": {"type": "integer", "minimum": 1, "default": 500},
            "max_file_bytes": {"type": "integer", "minimum": 0, "default": 2097152},
            "max_matches": {"type": "integer", "minimum": 1, "default": 200},
            "max_matches_per_file": {"type": "integer", "minimum": 1, "default": 20},
            "max_skipped_files": {"type": "integer", "minimum": 0, "default": 100},
            "include_preview": {"type": "boolean", "default": True},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    "fs_stat": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File or directory path to inspect"},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    "fs_delete": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File or directory to delete"},
            "recursive": {"type": "boolean", "default": False, "description": "If true, delete non-empty directories recursively"},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    "fs_move": {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Source path"},
            "destination": {"type": "string", "description": "Destination path"},
            "copy": {"type": "boolean", "default": False, "description": "If true, copy instead of move"},
            "overwrite": {"type": "boolean", "default": False, "description": "If true, overwrite existing destination"},
        },
        "required": ["source", "destination"],
        "additionalProperties": False,
    },
    "fs_move_file": {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Source file path"},
            "destination": {"type": "string", "description": "Destination file path"},
            "overwrite": {"type": "boolean", "default": False, "description": "If true, overwrite existing destination file"},
        },
        "required": ["source", "destination"],
        "additionalProperties": False,
    },
    "fs_copy_file": {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Source file path"},
            "destination": {"type": "string", "description": "Destination file path"},
            "overwrite": {"type": "boolean", "default": False, "description": "If true, overwrite existing destination file"},
        },
        "required": ["source", "destination"],
        "additionalProperties": False,
    },
    "fs_create": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to create"},
            "kind": {"type": "string", "default": "file", "description": "file | dir | directory"},
            "content": {"type": "string", "default": "", "description": "File content when kind=file"},
            "encoding": {"type": "string", "default": "utf-8"},
            "parents": {"type": "boolean", "default": True, "description": "Create parent directories when needed"},
            "overwrite": {"type": "boolean", "default": False, "description": "Overwrite target file when kind=file"},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    "proc_run": {
        "type": "object",
        "properties": {
            "command": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
            "reason": {"type": "string", "description": "Explain why CodexTools fs tools are insufficient for this command."},
            "cwd": {"type": "string"},
            "timeout_sec": {"type": "integer", "minimum": 1, "maximum": 600, "default": 60},
            "shell": {"type": "boolean", "default": False},
            "env": {"type": "object", "additionalProperties": {"type": "string"}, "description": "Extra environment variables to set"},
        },
        "required": ["command", "reason"],
        "additionalProperties": False,
    },
    "img_draw": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "width": {"type": "integer", "minimum": 1, "maximum": 20000, "default": 1024},
            "height": {"type": "integer", "minimum": 1, "maximum": 20000, "default": 1024},
            "background": {"type": "string", "default": "#ffffff"},
            "format": {"type": "string"},
            "shapes": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    "sound_beep": {
        "type": "object",
        "properties": {
            "frequency": {"type": "integer", "minimum": 37, "maximum": 32767, "default": 1200},
            "duration_ms": {"type": "integer", "minimum": 10, "maximum": 10000, "default": 180},
            "repeat": {"type": "integer", "minimum": 1, "maximum": 20, "default": 1},
            "interval_ms": {"type": "integer", "minimum": 0, "maximum": 5000, "default": 80},
        },
        "additionalProperties": False,
    },
}

_EXTENSION_HANDLERS, _EXTENSION_DESCRIPTIONS, _EXTENSION_SCHEMAS = get_extension_tooling()
for _tool_name in _EXTENSION_HANDLERS.keys():
    if _tool_name in TOOL_HANDLERS:
        raise ValueError(f"extension tool conflicts with built-in tool: {_tool_name}")

TOOL_HANDLERS.update(_EXTENSION_HANDLERS)
TOOL_DESCRIPTIONS.update(_EXTENSION_DESCRIPTIONS)
TOOL_SCHEMAS.update(_EXTENSION_SCHEMAS)


def _read_message(stdin: Any) -> Tuple[Optional[dict[str, Any]], str]:
    """
    Accept two wire formats:
    1) LSP-like framing: Content-Length headers + JSON body.
    2) JSONL framing: one JSON object per line.
    Returns: (message, wire_mode) where wire_mode is "content-length" or "jsonl".
    """

    first = stdin.readline()
    if not first:
        return None, "content-length"

    # JSONL mode: first non-empty line is a JSON object.
    stripped = first.strip()
    if stripped.startswith(b"{") and stripped.endswith(b"}"):
        return json.loads(stripped.decode("utf-8")), "jsonl"

    headers: dict[str, str] = {}
    line = first
    while True:
        if line in (b"\r\n", b"\n"):
            break
        text = line.decode("ascii", errors="strict").strip()
        if text:
            if ":" not in text:
                raise ValueError("invalid header")
            k, v = text.split(":", 1)
            headers[k.strip().lower()] = v.strip()
        line = stdin.readline()
        if not line:
            raise ValueError("incomplete headers")

    length = int(headers.get("content-length", "0"))
    if length <= 0:
        raise ValueError("missing content-length")

    body = stdin.read(length)
    if len(body) != length:
        raise ValueError("incomplete body")
    return json.loads(body.decode("utf-8")), "content-length"


def _write_message(stdout: Any, payload: dict[str, Any], wire_mode: str) -> None:
    raw_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if wire_mode == "jsonl":
        stdout.write((raw_text + "\n").encode("utf-8"))
        stdout.flush()
        return

    raw = raw_text.encode("utf-8")
    stdout.write(f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii"))
    stdout.write(raw)
    stdout.flush()


def _handle(method: str, params: dict[str, Any]) -> Optional[dict[str, Any]]:
    if method == "initialize":
        protocol = params.get("protocolVersion")
        if not isinstance(protocol, str) or not protocol:
            protocol = DEFAULT_PROTOCOL_VERSION
        _capture_runtime_model_hint(params)
        _capture_runtime_workspace_hint(params)
        return {
            "protocolVersion": protocol,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    if method == "ping":
        return {}

    if method == "tools/list":
        tools = []
        for name, handler in TOOL_HANDLERS.items():
            _ = handler
            tools.append(
                {
                    "name": name,
                    "description": TOOL_DESCRIPTIONS[name],
                    "inputSchema": TOOL_SCHEMAS[name],
                }
            )
        return {"tools": tools}

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments", {})
        if not isinstance(name, str) or not name:
            raise ValueError("tools/call missing `name`")
        if not isinstance(args, dict):
            raise ValueError("tools/call `arguments` must be object")
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            return _tool_error(f"unknown tool: {name}")
        try:
            _enforce_tool_policy(name, args)
            return _ok(handler(args))
        except Exception as e:
            _log(f"tool failed ({name}): {e}")
            _log(traceback.format_exc())
            return _tool_error(f"{type(e).__name__}: {e}")

    if method.startswith("notifications/"):
        return None

    if method in {"resources/list", "prompts/list"}:
        key = "resources" if method == "resources/list" else "prompts"
        return {key: []}

    raise ValueError(f"method not found: {method}")


def main() -> None:
    os.environ.setdefault("PYTHONUTF8", "1")
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    while True:
        try:
            msg, wire_mode = _read_message(stdin)
        except Exception as e:
            _log(f"read error: {e}")
            _log(traceback.format_exc())
            _write_message(
                stdout,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": str(e)},
                },
                "content-length",
            )
            continue

        if msg is None:
            break

        if not isinstance(msg, dict):
            _write_message(
                stdout,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "invalid request"},
                },
                "content-length",
            )
            continue

        has_id = "id" in msg
        req_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params", {})

        try:
            if not isinstance(method, str):
                raise ValueError("missing method")
            if params is None:
                params = {}
            if not isinstance(params, dict):
                raise ValueError("params must be object")

            result = _handle(method, params)
            if has_id:
                _write_message(
                    stdout,
                    {"jsonrpc": "2.0", "id": req_id, "result": result if result is not None else {}},
                    wire_mode,
                )
        except Exception as e:
            _log(f"request error: {e}")
            _log(traceback.format_exc())
            if has_id:
                _write_message(
                    stdout,
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32603, "message": str(e)},
                    },
                    wire_mode,
                )


if __name__ == "__main__":
    main()
