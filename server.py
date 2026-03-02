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
import time
import traceback
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
PLAN_STORE_BACKUP_PATH = STATE_ROOT / "codextools_plan_store.json"


def _runtime_project_name() -> str:
    try:
        workspace_root = _resolve_runtime_workspace_root()
    except Exception:
        workspace_root = WORKSPACE_ROOT

    candidate = workspace_root.name.strip()
    return candidate if candidate else PROJECT_NAME


def _plan_markdown_path() -> Path:
    try:
        workspace_root = _resolve_runtime_workspace_root()
    except Exception:
        workspace_root = WORKSPACE_ROOT
    return workspace_root / f"{_runtime_project_name()}-plan.md"

PLAN_STEP_STATUSES = {"pending", "in_progress", "completed", "blocked", "canceled"}
LEGACY_PLAN_STORE_META_PATTERN = re.compile(r"^\s*<!--\s*plan-store-meta:([A-Za-z0-9+/=]+)\s*-->\s*$")
LEGACY_PLAN_GROUP_META_PATTERN = re.compile(r"^\s*<!--\s*plan-meta:([A-Za-z0-9+/=]+)\s*-->\s*$")
LEGACY_PLAN_STEP_META_PATTERN = re.compile(
    r"^\s*-\s*\[( |x)\]\s*(.*?)\s*<!--\s*step-meta:([A-Za-z0-9+/=]+)\s*-->\s*$"
)
PLAN_SECTION_PATTERN = re.compile(r"^\s*##\s+(.+?)\s*$")
PLAN_CHECKBOX_PATTERN = re.compile(r"^\s*-\s*\[( |x|X)\]\s*(.*?)\s*$")

WRITE_GUARDED_TOOLS = {
    "fs_write_text",
    "fs_replace_text",
    "fs_replace_regex",
    "fs_patch_lines",
    "fs_delete",
    "fs_move",
    "fs_move_file",
    "fs_copy_file",
    "fs_create",
    "img_draw",
}
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
CONTINUE_CONFIRM_KEYWORDS = ("continue", "继续")
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
        "enforce_plan_for_writes": True,
        "enforce_proc_run_policy": True,
        "latest_plan_id": None,
        "continue_confirmed_plan_id": None,
        "continue_confirmed_at": None,
        "continue_confirmation_text": "",
        "audit": [],
    }


def _normalize_guard_policy_state(raw: Any) -> dict[str, Any]:
    guard = _default_guard_policy_state()
    if not isinstance(raw, dict):
        return guard

    guard["enforce_plan_for_writes"] = bool(raw.get("enforce_plan_for_writes", True))
    guard["enforce_proc_run_policy"] = bool(raw.get("enforce_proc_run_policy", True))

    latest_plan_id = raw.get("latest_plan_id")
    guard["latest_plan_id"] = latest_plan_id if isinstance(latest_plan_id, str) and latest_plan_id.strip() else None

    confirmed_plan_id = raw.get("continue_confirmed_plan_id")
    guard["continue_confirmed_plan_id"] = (
        confirmed_plan_id if isinstance(confirmed_plan_id, str) and confirmed_plan_id.strip() else None
    )

    confirmed_at = raw.get("continue_confirmed_at")
    guard["continue_confirmed_at"] = confirmed_at if isinstance(confirmed_at, str) and confirmed_at.strip() else None

    confirmation_text = raw.get("continue_confirmation_text")
    guard["continue_confirmation_text"] = confirmation_text if isinstance(confirmation_text, str) else ""

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
        "next_plan_seq": 1,
        "plans": {},
        "plan_order": [],
        "guard_policy": _default_guard_policy_state(),
    }


def _default_plan_store() -> dict[str, Any]:
    return {
        "version": 1,
        "project_name": _runtime_project_name(),
        "next_plan_seq": 1,
        "plans": {},
        "plan_order": [],
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

    if not isinstance(state.get("plans"), dict):
        state["plans"] = {}
    if not isinstance(state.get("plan_order"), list):
        state["plan_order"] = []
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


def _decode_legacy_plan_meta(token: str) -> Optional[dict[str, Any]]:
    try:
        raw = base64.b64decode(token.encode("ascii"), validate=True).decode("utf-8")
        loaded = json.loads(raw)
    except Exception:
        return None
    return loaded if isinstance(loaded, dict) else None


def _normalize_plan_store(store_raw: Any) -> dict[str, Any]:
    default_now = _utc_now()
    store = _default_plan_store()

    if isinstance(store_raw, dict):
        for key in ("version", "project_name", "next_plan_seq", "plans", "plan_order"):
            if key in store_raw:
                store[key] = store_raw[key]

    try:
        version = int(store.get("version", 1))
    except Exception:
        version = 1
    if version < 1:
        version = 1

    project_name_raw = store.get("project_name")
    project_name = project_name_raw.strip() if isinstance(project_name_raw, str) else ""
    if not project_name:
        project_name = _runtime_project_name()

    plans_input = store.get("plans")
    plans_input_dict = plans_input if isinstance(plans_input, dict) else {}

    plans: dict[str, Any] = {}
    for raw_plan_key, raw_plan in plans_input_dict.items():
        if not isinstance(raw_plan, dict):
            continue

        fallback_plan_id = str(raw_plan_key).strip()
        plan_id_value = raw_plan.get("id", fallback_plan_id)
        plan_id = str(plan_id_value).strip()
        if not plan_id:
            continue

        plan_created = _compact_text(raw_plan.get("created_at"), default_now)
        plan_updated = _compact_text(raw_plan.get("updated_at"), plan_created)
        plan_title = _compact_text(raw_plan.get("title"), plan_id)
        description_raw = raw_plan.get("description", "")
        description = description_raw if isinstance(description_raw, str) else str(description_raw)

        steps_out: list[dict[str, Any]] = []
        steps_raw = raw_plan.get("steps")
        if isinstance(steps_raw, list):
            for index, step_raw in enumerate(steps_raw, start=1):
                if not isinstance(step_raw, dict):
                    continue

                step_id_value = step_raw.get("id", f"{plan_id}-step-{index}")
                step_id = _compact_text(step_id_value, f"{plan_id}-step-{index}")
                step_title = _compact_text(step_raw.get("title"), f"Step {index}")
                step_status = _coerce_plan_status(step_raw.get("status", "pending"))
                step_note_raw = step_raw.get("note", "")
                step_note = step_note_raw if isinstance(step_note_raw, str) else str(step_note_raw)
                step_updated = _compact_text(step_raw.get("updated_at"), plan_updated)

                steps_out.append(
                    {
                        "id": step_id,
                        "title": step_title,
                        "status": step_status,
                        "note": step_note,
                        "updated_at": step_updated,
                    }
                )

        plans[plan_id] = {
            "id": plan_id,
            "title": plan_title,
            "description": description,
            "created_at": plan_created,
            "updated_at": plan_updated,
            "archived": bool(raw_plan.get("archived", False)),
            "steps": steps_out,
        }

    order_input = store.get("plan_order")
    order: list[str] = []
    if isinstance(order_input, list):
        for item in order_input:
            if not isinstance(item, str):
                continue
            plan_id = item.strip()
            if not plan_id or plan_id not in plans or plan_id in order:
                continue
            order.append(plan_id)

    for plan_id in plans.keys():
        if plan_id not in order:
            order.append(plan_id)

    raw_next_seq = store.get("next_plan_seq", 1)
    try:
        next_seq = int(raw_next_seq)
    except Exception:
        next_seq = 1
    if next_seq < 1:
        next_seq = 1

    max_existing_seq = 0
    for plan_id in plans.keys():
        matched = re.fullmatch(r"plan-(\d+)", plan_id)
        if not matched:
            continue
        max_existing_seq = max(max_existing_seq, int(matched.group(1)))

    if next_seq <= max_existing_seq:
        next_seq = max_existing_seq + 1

    return {
        "version": version,
        "project_name": project_name,
        "next_plan_seq": next_seq,
        "plans": plans,
        "plan_order": order,
    }


def _next_step_identifier(plan_id: str, steps: list[dict[str, Any]]) -> str:
    max_index = 0
    pattern = re.compile(rf"^{re.escape(plan_id)}-step-(\d+)$")
    for step in steps:
        step_id = _compact_text(step.get("id"), "")
        matched = pattern.fullmatch(step_id)
        if not matched:
            continue
        max_index = max(max_index, int(matched.group(1)))
    return f"{plan_id}-step-{max_index + 1}"


def _cleanup_plan_title_for_user_view(title: str) -> str:
    compact = _compact_text(title, "")
    parts = [part.strip() for part in compact.split("|")]
    if len(parts) >= 3 and re.fullmatch(r"[A-Z]+计划组", parts[0]) and re.fullmatch(r"plan-\d+", parts[1]):
        return _compact_text(parts[-1], compact)
    return compact


def _cleanup_step_title_for_user_view(title: str) -> str:
    cleaned = _compact_text(title, "")

    if "<!--" in cleaned:
        cleaned = cleaned.split("<!--", 1)[0].strip()
    if "| note:" in cleaned:
        cleaned = cleaned.split("| note:", 1)[0].strip()

    cleaned = re.sub(r"\(`plan-\d+-step-\d+`\)", "", cleaned).strip()
    cleaned = re.sub(r"\[(已完成|进行中|待办|阻塞|已取消)\]", "", cleaned).strip()
    return cleaned


def _parse_simple_plan_markdown(text: str) -> list[dict[str, Any]]:
    plans: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None

    for raw_line in text.splitlines():
        section_match = PLAN_SECTION_PATTERN.match(raw_line)
        if section_match:
            title = _cleanup_plan_title_for_user_view(section_match.group(1))
            if current and _compact_text(current.get("title"), ""):
                plans.append(current)
            current = {"title": title, "description": "", "steps": []}
            continue

        if current is None:
            continue

        stripped = raw_line.strip()
        if stripped.startswith("- 描述:"):
            current["description"] = stripped.split(":", 1)[1].strip()
            continue

        checkbox_match = PLAN_CHECKBOX_PATTERN.match(raw_line)
        if checkbox_match:
            step_title = _cleanup_step_title_for_user_view(checkbox_match.group(2))
            if not step_title:
                continue
            current_steps = current.get("steps")
            if isinstance(current_steps, list):
                current_steps.append(
                    {
                        "title": step_title,
                        "completed": checkbox_match.group(1).lower() == "x",
                    }
                )

    if current and _compact_text(current.get("title"), ""):
        plans.append(current)

    return plans
def _parse_legacy_plan_markdown(text: str) -> dict[str, Any]:
    raw_store = _default_plan_store()
    plans: dict[str, Any] = {}
    plan_order: list[str] = []
    current_plan_id: Optional[str] = None

    for line in text.splitlines():
        matched_store_meta = LEGACY_PLAN_STORE_META_PATTERN.match(line)
        if matched_store_meta:
            decoded = _decode_legacy_plan_meta(matched_store_meta.group(1))
            if isinstance(decoded, dict):
                if "version" in decoded:
                    raw_store["version"] = decoded["version"]
                if "project_name" in decoded:
                    raw_store["project_name"] = decoded["project_name"]
                if "next_plan_seq" in decoded:
                    raw_store["next_plan_seq"] = decoded["next_plan_seq"]
            continue

        matched_plan_meta = LEGACY_PLAN_GROUP_META_PATTERN.match(line)
        if matched_plan_meta:
            decoded = _decode_legacy_plan_meta(matched_plan_meta.group(1))
            if not isinstance(decoded, dict):
                current_plan_id = None
                continue

            plan_id_raw = decoded.get("id")
            if not isinstance(plan_id_raw, str) or not plan_id_raw.strip():
                current_plan_id = None
                continue

            plan_id = plan_id_raw.strip()
            plan_data = dict(decoded)
            plan_data["id"] = plan_id
            plan_data["steps"] = []
            plans[plan_id] = plan_data
            if plan_id not in plan_order:
                plan_order.append(plan_id)
            current_plan_id = plan_id
            continue

        matched_step_meta = LEGACY_PLAN_STEP_META_PATTERN.match(line)
        if matched_step_meta and current_plan_id:
            visible_title = _compact_text(matched_step_meta.group(2), "")
            decoded = _decode_legacy_plan_meta(matched_step_meta.group(3))
            step_data = dict(decoded) if isinstance(decoded, dict) else {}
            if not _compact_text(step_data.get("title"), ""):
                step_data["title"] = visible_title

            current_plan = plans.get(current_plan_id)
            if isinstance(current_plan, dict):
                current_steps = current_plan.get("steps")
                if isinstance(current_steps, list):
                    current_steps.append(step_data)

    raw_store["plans"] = plans
    raw_store["plan_order"] = plan_order
    return _normalize_plan_store(raw_store)


def _looks_like_legacy_plan_markdown(text: str) -> bool:
    return "<!-- plan-meta:" in text or "<!-- step-meta:" in text or "<!-- plan-store-meta:" in text


def _sync_store_with_user_markdown(store: dict[str, Any], markdown_text: str) -> dict[str, Any]:
    normalized = _normalize_plan_store(store)
    user_plans = _parse_simple_plan_markdown(markdown_text)

    plans_raw = normalized.get("plans")
    plans = plans_raw if isinstance(plans_raw, dict) else {}
    order_raw = normalized.get("plan_order")
    order = order_raw if isinstance(order_raw, list) else []

    title_queues: dict[str, list[str]] = {}
    for plan_id in order:
        plan = plans.get(plan_id)
        if not isinstance(plan, dict):
            continue
        title = _compact_text(plan.get("title"), plan_id)
        title_queues.setdefault(title, []).append(plan_id)

    used_plan_ids: set[str] = set()
    new_plans: dict[str, Any] = {}
    new_order: list[str] = []
    now = _utc_now()

    for user_plan in user_plans:
        user_title = _compact_text(user_plan.get("title"), "")
        if not user_title:
            continue

        matched_plan_id: Optional[str] = None
        queued = title_queues.get(user_title, [])
        while queued:
            candidate = queued.pop(0)
            if candidate in used_plan_ids:
                continue
            matched_plan_id = candidate
            break

        existing_plan = plans.get(matched_plan_id) if isinstance(matched_plan_id, str) else None
        if not isinstance(existing_plan, dict):
            matched_plan_id = _next_identifier(normalized, "next_plan_seq", "plan")
            existing_plan = None

        assert isinstance(matched_plan_id, str)
        used_plan_ids.add(matched_plan_id)

        description_raw = user_plan.get("description", "")
        description = description_raw if isinstance(description_raw, str) else str(description_raw)

        existing_steps_raw = existing_plan.get("steps") if isinstance(existing_plan, dict) else []
        existing_steps = existing_steps_raw if isinstance(existing_steps_raw, list) else []

        used_step_indexes: set[int] = set()
        new_steps: list[dict[str, Any]] = []
        user_steps_raw = user_plan.get("steps")
        user_steps = user_steps_raw if isinstance(user_steps_raw, list) else []

        for user_step in user_steps:
            step_title = _compact_text(user_step.get("title"), "")
            if not step_title:
                continue
            checked = bool(user_step.get("completed", False))

            matched_step_index = -1
            matched_step: Optional[dict[str, Any]] = None
            for idx, old_step in enumerate(existing_steps):
                if idx in used_step_indexes or not isinstance(old_step, dict):
                    continue
                if _compact_text(old_step.get("title"), "") == step_title:
                    matched_step_index = idx
                    matched_step = old_step
                    break

            if matched_step_index >= 0 and isinstance(matched_step, dict):
                used_step_indexes.add(matched_step_index)
                step_id = _compact_text(matched_step.get("id"), _next_step_identifier(matched_plan_id, existing_steps + new_steps))
                previous_status = _coerce_plan_status(matched_step.get("status", "pending"))
                if checked:
                    step_status = "completed"
                elif previous_status == "completed":
                    step_status = "pending"
                else:
                    step_status = previous_status

                step_note_raw = matched_step.get("note", "")
                step_note = step_note_raw if isinstance(step_note_raw, str) else str(step_note_raw)
                step_updated = _compact_text(matched_step.get("updated_at"), now)
                if step_status != previous_status:
                    step_updated = now
            else:
                step_id = _next_step_identifier(matched_plan_id, existing_steps + new_steps)
                step_status = "completed" if checked else "pending"
                step_note = ""
                step_updated = now

            new_steps.append(
                {
                    "id": step_id,
                    "title": step_title,
                    "status": step_status,
                    "note": step_note,
                    "updated_at": step_updated,
                }
            )

        if isinstance(existing_plan, dict):
            created_at = _compact_text(existing_plan.get("created_at"), now)
            archived = bool(existing_plan.get("archived", False))
            previous_compare = [
                (_compact_text(step.get("title"), ""), _coerce_plan_status(step.get("status", "pending")))
                for step in existing_steps
                if isinstance(step, dict)
            ]
            new_compare = [
                (_compact_text(step.get("title"), ""), _coerce_plan_status(step.get("status", "pending")))
                for step in new_steps
            ]
            title_changed = _compact_text(existing_plan.get("title"), "") != user_title
            desc_changed = _compact_text(existing_plan.get("description"), "") != _compact_text(description, "")
            steps_changed = previous_compare != new_compare
            updated_at = now if (title_changed or desc_changed or steps_changed) else _compact_text(existing_plan.get("updated_at"), created_at)
        else:
            created_at = now
            updated_at = now
            archived = False

        plan_payload = {
            "id": matched_plan_id,
            "title": user_title,
            "description": description,
            "created_at": created_at,
            "updated_at": updated_at,
            "archived": archived,
            "steps": new_steps,
        }

        new_plans[matched_plan_id] = plan_payload
        new_order.append(matched_plan_id)

    normalized["plans"] = new_plans
    normalized["plan_order"] = new_order
    return _normalize_plan_store(normalized)


def _render_plan_group_markdown(plan: dict[str, Any]) -> list[str]:
    plan_title = _compact_text(plan.get("title"), "未命名计划")
    description = _compact_text(plan.get("description"), "")

    lines = [f"## {plan_title}"]
    if description:
        lines.append(f"- 描述: {description}")

    steps_raw = plan.get("steps")
    steps = steps_raw if isinstance(steps_raw, list) else []
    if not steps:
        lines.append("- [ ] (暂无任务)")
        return lines

    for step in steps:
        if not isinstance(step, dict):
            continue
        step_title = _compact_text(step.get("title"), "")
        if not step_title:
            continue
        checked = _coerce_plan_status(step.get("status", "pending")) == "completed"
        lines.append(f"- [{'x' if checked else ' '}] {step_title}")

    if len(lines) == 1:
        lines.append("- [ ] (暂无任务)")

    return lines


def _iter_plan_groups(store: dict[str, Any], include_archived: bool) -> list[tuple[str, dict[str, Any]]]:
    plans_raw = store.get("plans")
    plans = plans_raw if isinstance(plans_raw, dict) else {}
    order_raw = store.get("plan_order")
    order = order_raw if isinstance(order_raw, list) else []

    groups: list[tuple[str, dict[str, Any]]] = []
    for plan_id in order:
        plan = plans.get(plan_id)
        if not isinstance(plan, dict):
            continue
        if not include_archived and bool(plan.get("archived", False)):
            continue
        groups.append((plan_id, plan))

    return groups


def _render_plan_markdown(
    store: dict[str, Any],
    include_archived: bool,
    only_plan_id: Optional[str] = None,
) -> str:
    normalized = _normalize_plan_store(store)
    groups = _iter_plan_groups(normalized, include_archived=include_archived)

    project_name_raw = normalized.get("project_name")
    project_name = project_name_raw.strip() if isinstance(project_name_raw, str) else ""
    if not project_name:
        project_name = _runtime_project_name()

    lines = [
        f"# {project_name}-plan",
    ]

    has_output = False
    for plan_id, plan in groups:
        if only_plan_id is not None and plan_id != only_plan_id:
            continue
        lines.append("")
        lines.extend(_render_plan_group_markdown(plan))
        has_output = True

    if not has_output:
        lines.extend(["", "_暂无计划组_"])

    return "\n".join(lines).rstrip() + "\n"


def _save_plan_store(store: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_plan_store(store)
    _ensure_control_paths()
    PLAN_STORE_BACKUP_PATH.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8", newline="")
    rendered = _render_plan_markdown(normalized, include_archived=True)
    plan_markdown_path = _plan_markdown_path()
    plan_markdown_path.parent.mkdir(parents=True, exist_ok=True)
    plan_markdown_path.write_text(rendered, encoding="utf-8", newline="")
    return normalized


def _migrate_plan_store_from_control_state() -> dict[str, Any]:
    control_state = _load_control_state()
    raw_store = {
        "version": 1,
        "project_name": _runtime_project_name(),
        "next_plan_seq": control_state.get("next_plan_seq", 1),
        "plans": control_state.get("plans", {}),
        "plan_order": control_state.get("plan_order", []),
    }
    return _normalize_plan_store(raw_store)


def _load_plan_store() -> dict[str, Any]:
    _ensure_control_paths()

    plan_markdown_path = _plan_markdown_path()
    markdown_text = ""
    if plan_markdown_path.exists():
        try:
            markdown_text = plan_markdown_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            _log(f"plan markdown read failed: {e}")
            markdown_text = ""

    if PLAN_STORE_BACKUP_PATH.exists():
        try:
            backup_text = PLAN_STORE_BACKUP_PATH.read_text(encoding="utf-8", errors="replace")
            backup_raw = json.loads(backup_text)
            store = _normalize_plan_store(backup_raw)
        except Exception as e:
            _log(f"plan backup load failed, fallback migration: {e}")
            store = _migrate_plan_store_from_control_state()

        if markdown_text:
            store = _sync_store_with_user_markdown(store, markdown_text)
        return _save_plan_store(store)

    if markdown_text and _looks_like_legacy_plan_markdown(markdown_text):
        store = _parse_legacy_plan_markdown(markdown_text)
        return _save_plan_store(store)

    if markdown_text:
        seed = _default_plan_store()
        store = _sync_store_with_user_markdown(seed, markdown_text)
        return _save_plan_store(store)

    store = _migrate_plan_store_from_control_state()
    return _save_plan_store(store)


def _get_plan(store: dict[str, Any], plan_id: str) -> dict[str, Any]:
    plans = store.get("plans")
    if not isinstance(plans, dict):
        raise ValueError("plan storage is not initialized")
    plan = plans.get(plan_id)
    if not isinstance(plan, dict):
        raise ValueError(f"plan not found: {plan_id}")
    return plan


def _load_plan_store_for_guard() -> dict[str, Any]:
    _ensure_control_paths()

    if PLAN_STORE_BACKUP_PATH.exists():
        try:
            backup_text = PLAN_STORE_BACKUP_PATH.read_text(encoding="utf-8", errors="replace")
            backup_raw = json.loads(backup_text)
            return _normalize_plan_store(backup_raw)
        except Exception as e:
            _log(f"plan guard backup load failed: {e}")

    plan_markdown_path = _plan_markdown_path()
    if plan_markdown_path.exists():
        try:
            markdown_text = plan_markdown_path.read_text(encoding="utf-8", errors="replace")
            if markdown_text:
                if _looks_like_legacy_plan_markdown(markdown_text):
                    return _parse_legacy_plan_markdown(markdown_text)
                return _sync_store_with_user_markdown(_default_plan_store(), markdown_text)
        except Exception as e:
            _log(f"plan guard markdown load failed: {e}")

    return _default_plan_store()


def _latest_plan_id_from_store(store: dict[str, Any], include_archived: bool = False) -> Optional[str]:
    normalized = _normalize_plan_store(store)
    plans_raw = normalized.get("plans")
    plans = plans_raw if isinstance(plans_raw, dict) else {}
    order_raw = normalized.get("plan_order")
    order = order_raw if isinstance(order_raw, list) else []

    for item in reversed(order):
        if not isinstance(item, str):
            continue
        plan_id = item.strip()
        if not plan_id:
            continue
        plan = plans.get(plan_id)
        if not isinstance(plan, dict):
            continue
        if not include_archived and bool(plan.get("archived", False)):
            continue
        return plan_id

    return None


def _resolve_guard_plan_id(state: dict[str, Any], store: dict[str, Any]) -> tuple[Optional[str], bool]:
    guard = _ensure_guard_policy_state(state)
    normalized = _normalize_plan_store(store)
    plans_raw = normalized.get("plans")
    plans = plans_raw if isinstance(plans_raw, dict) else {}

    current = guard.get("latest_plan_id")
    if isinstance(current, str):
        candidate = current.strip()
        if candidate and isinstance(plans.get(candidate), dict):
            return candidate, False

    fallback = _latest_plan_id_from_store(normalized, include_archived=False)
    if fallback is None:
        fallback = _latest_plan_id_from_store(normalized, include_archived=True)

    guard["latest_plan_id"] = fallback
    return fallback, True


def _set_latest_plan_guard(plan_id: str) -> None:
    normalized_plan_id = _compact_text(plan_id, "")
    if not normalized_plan_id:
        return

    state = _load_control_state()
    guard = _ensure_guard_policy_state(state)
    guard["latest_plan_id"] = normalized_plan_id
    guard["continue_confirmed_plan_id"] = None
    guard["continue_confirmed_at"] = None
    guard["continue_confirmation_text"] = ""
    _append_guard_audit(
        state,
        event="plan_created",
        message=f"plan `{normalized_plan_id}` created; continue confirmation reset.",
        tool="plan_create",
        extra={"plan_id": normalized_plan_id},
    )
    _save_control_state(state)


def _confirmation_contains_continue(text: str) -> bool:
    compact = _compact_text(text, "").lower()
    if not compact:
        return False
    return any(keyword in compact for keyword in CONTINUE_CONFIRM_KEYWORDS)


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


def _mark_plan_continue_confirmed(
    state: dict[str, Any],
    *,
    plan_id: str,
    confirmation_text: str,
    audit_event: str,
    audit_tool: str,
    extra: Optional[dict[str, Any]] = None,
) -> str:
    now = _utc_now()
    guard = _ensure_guard_policy_state(state)
    guard["latest_plan_id"] = plan_id
    guard["continue_confirmed_plan_id"] = plan_id
    guard["continue_confirmed_at"] = now
    guard["continue_confirmation_text"] = confirmation_text
    _append_guard_audit(
        state,
        event=audit_event,
        message=f"continue confirmed for plan `{plan_id}`.",
        tool=audit_tool,
        extra=extra or {"plan_id": plan_id},
    )
    return now


def _open_plan_review_dialog(plan_id: str, store: dict[str, Any]) -> Optional[str]:
    handler = TOOL_HANDLERS.get("ui_plan_confirm")
    if handler is None:
        return None

    try:
        normalized_store = _normalize_plan_store(store)
        plan_text = _render_plan_markdown(normalized_store, include_archived=True, only_plan_id=plan_id)
        plan_filename = _plan_markdown_path().name
        payload = handler(
            {
                "title": "计划确认",
                "prompt": f"请先阅读计划内容。若要调整请点击“修改计划”，改完 {plan_filename} 后再发送“继续”。",
                "plan_content": plan_text,
                "continue_label": "继续",
                "modify_label": "修改计划",
                "topmost": True,
                "bring_to_front": True,
                "focus_force": True,
            }
        )
    except Exception as e:
        _log(f"plan review dialog failed: {e}")
        return None

    if not isinstance(payload, dict):
        return None

    action = _compact_text(payload.get("action"), "").lower()
    if action in {"continue", "modify", "closed"}:
        return action
    return None


def _enforce_write_guard(tool_name: str) -> None:
    store = _load_plan_store_for_guard()
    state = _load_control_state()
    guard = _ensure_guard_policy_state(state)

    if not bool(guard.get("enforce_plan_for_writes", True)):
        return

    plan_id, changed = _resolve_guard_plan_id(state, store)
    if not plan_id:
        message = (
            "Write blocked by plan-first policy: no plan exists. "
            "Call `plan_create` first, then wait for user confirmation and call `plan_confirm_continue`."
        )
        _append_guard_audit(state, event="write_blocked_no_plan", message=message, tool=tool_name)
        _save_control_state(state)
        raise PermissionError(message)

    confirmed_plan_id = _compact_text(guard.get("continue_confirmed_plan_id"), "")
    if confirmed_plan_id != plan_id:
        action = _open_plan_review_dialog(plan_id, store)
        if action == "continue":
            _mark_plan_continue_confirmed(
                state,
                plan_id=plan_id,
                confirmation_text="dialog:continue",
                audit_event="continue_confirmed_dialog",
                audit_tool="ui_plan_confirm",
                extra={"plan_id": plan_id, "source_tool": tool_name},
            )
            _save_control_state(state)
            return

        if action in {"modify", "closed"}:
            plan_filename = _plan_markdown_path().name
            message = (
                f"Write blocked: plan `{plan_id}` requires review update. "
                f"Please edit `{plan_filename}`, then send `继续` and call `plan_confirm_continue`."
            )
            _append_guard_audit(
                state,
                event="write_blocked_plan_modify",
                message=message,
                tool=tool_name,
                extra={"required_plan_id": plan_id, "dialog_action": action},
            )
            _save_control_state(state)
            raise PermissionError(message)

        message = (
            f"Write blocked by plan-first policy: plan `{plan_id}` is not confirmed. "
            "After user explicitly says `continue` or `继续`, call `plan_confirm_continue`."
        )
        _append_guard_audit(
            state,
            event="write_blocked_no_continue",
            message=message,
            tool=tool_name,
            extra={"required_plan_id": plan_id, "confirmed_plan_id": confirmed_plan_id or None},
        )
        _save_control_state(state)
        raise PermissionError(message)

    if changed:
        _save_control_state(state)


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

    if name in WRITE_GUARDED_TOOLS:
        _enforce_write_guard(name)
        return

    if name in {"proc_run", "debug_run", "perf_benchmark"}:
        _enforce_proc_run_policy(args)



def tool_plan_create(args: dict[str, Any]) -> str:
    title = _require_str(args, "title")
    steps_arg = args.get("steps")
    if not isinstance(steps_arg, list) or not steps_arg:
        raise ValueError("`steps` must be a non-empty string array")

    description = args.get("description", "")
    if not isinstance(description, str):
        raise ValueError("`description` must be string")

    normalized_title = title.strip()
    now = _utc_now()
    store = _load_plan_store()

    plans_raw = store.get("plans")
    plans: dict[str, Any] = plans_raw if isinstance(plans_raw, dict) else {}
    plan_order_raw = store.get("plan_order")
    plan_order: list[str] = plan_order_raw if isinstance(plan_order_raw, list) else []

    matched_plan_id: Optional[str] = None
    for candidate_id in plan_order:
        candidate_plan = plans.get(candidate_id)
        if not isinstance(candidate_plan, dict):
            continue
        if _compact_text(candidate_plan.get("title"), "") == normalized_title:
            matched_plan_id = candidate_id
            break

    if not matched_plan_id:
        plan_id = _next_identifier(store, "next_plan_seq", "plan")
        created_at = now
        if plan_id not in plan_order:
            plan_order.append(plan_id)
            store["plan_order"] = plan_order
    else:
        plan_id = matched_plan_id
        existing_plan = plans.get(plan_id)
        created_at = _compact_text(existing_plan.get("created_at"), now) if isinstance(existing_plan, dict) else now

    steps: list[dict[str, Any]] = []
    for index, item in enumerate(steps_arg, start=1):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"`steps[{index - 1}]` must be a non-empty string")
        steps.append(
            {
                "id": f"{plan_id}-step-{index}",
                "title": item.strip(),
                "status": "pending",
                "note": "",
                "updated_at": now,
            }
        )

    plans[plan_id] = {
        "id": plan_id,
        "title": normalized_title,
        "description": description,
        "created_at": created_at,
        "updated_at": now,
        "archived": False,
        "steps": steps,
    }
    store["plans"] = plans

    store = _save_plan_store(store)
    _set_latest_plan_guard(plan_id)

    return _render_plan_markdown(store, include_archived=True, only_plan_id=plan_id)


def tool_plan_update(args: dict[str, Any]) -> str:
    plan_id = _require_str(args, "plan_id")

    status_raw = args.get("status")
    if not isinstance(status_raw, str) or not status_raw.strip():
        raise ValueError("`status` must be a non-empty string")
    status = _coerce_plan_status(status_raw)
    if status_raw.strip().lower() not in PLAN_STEP_STATUSES:
        allowed = ", ".join(sorted(PLAN_STEP_STATUSES))
        raise ValueError(f"`status` must be one of: {allowed}")

    step_id = args.get("step_id")
    step_index_arg = args.get("step_index")
    if step_id is None and step_index_arg is None:
        raise ValueError("Provide either `step_id` or `step_index`")

    note = args.get("note")
    if note is not None and not isinstance(note, str):
        raise ValueError("`note` must be string")

    exclusive_in_progress = _as_bool(args.get("exclusive_in_progress"), True)

    store = _load_plan_store()
    plan = _get_plan(store, plan_id)
    steps_raw = plan.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise ValueError(f"plan has no steps: {plan_id}")

    target_index = -1
    if step_id is not None:
        if not isinstance(step_id, str) or not step_id.strip():
            raise ValueError("`step_id` must be a non-empty string")
        for index, step in enumerate(steps_raw):
            if str(step.get("id", "")).strip() == step_id.strip():
                target_index = index
                break
        if target_index < 0:
            raise ValueError(f"step not found: {step_id}")
    else:
        step_index = _as_int(step_index_arg, 1, minimum=1)
        if step_index > len(steps_raw):
            raise ValueError("`step_index` is out of range")
        target_index = step_index - 1

    now = _utc_now()
    if status == "in_progress" and exclusive_in_progress:
        for index, step in enumerate(steps_raw):
            if index == target_index:
                continue
            if _coerce_plan_status(step.get("status", "pending")) == "in_progress":
                step["status"] = "pending"
                step["updated_at"] = now

    target_step = steps_raw[target_index]
    target_step["status"] = status
    target_step["updated_at"] = now
    if note is not None:
        target_step["note"] = note

    plan["updated_at"] = now
    store = _save_plan_store(store)
    return _render_plan_markdown(store, include_archived=True, only_plan_id=plan_id)


def tool_plan_view(args: dict[str, Any]) -> str:
    plan_id = _require_str(args, "plan_id")
    store = _load_plan_store()
    _ = _get_plan(store, plan_id)
    return _render_plan_markdown(store, include_archived=True, only_plan_id=plan_id)


def tool_plan_list(args: dict[str, Any]) -> str:
    include_archived = _as_bool(args.get("include_archived"), False)
    store = _load_plan_store()
    return _render_plan_markdown(store, include_archived=include_archived)


def tool_plan_archive(args: dict[str, Any]) -> str:
    plan_id = _require_str(args, "plan_id")
    archived = _as_bool(args.get("archived"), True)

    store = _load_plan_store()
    plan = _get_plan(store, plan_id)
    plan["archived"] = archived
    plan["updated_at"] = _utc_now()
    store = _save_plan_store(store)
    return _render_plan_markdown(store, include_archived=True, only_plan_id=plan_id)


def tool_plan_confirm_continue(args: dict[str, Any]) -> dict[str, Any]:
    confirmation = _require_str(args, "confirmation")
    if not _confirmation_contains_continue(confirmation):
        raise ValueError("`confirmation` must include `continue` or `继续`")

    requested_plan_id = args.get("plan_id")
    if requested_plan_id is not None and (not isinstance(requested_plan_id, str) or not requested_plan_id.strip()):
        raise ValueError("`plan_id` must be a non-empty string when provided")

    store = _load_plan_store_for_guard()
    plan_id = requested_plan_id.strip() if isinstance(requested_plan_id, str) else ""
    if plan_id:
        _ = _get_plan(store, plan_id)
    else:
        plan_id = _latest_plan_id_from_store(store, include_archived=False) or ""
        if not plan_id:
            plan_id = _latest_plan_id_from_store(store, include_archived=True) or ""
        if not plan_id:
            raise ValueError("no plan found: call `plan_create` before confirmation")

    state = _load_control_state()
    now = _mark_plan_continue_confirmed(
        state,
        plan_id=plan_id,
        confirmation_text=confirmation.strip(),
        audit_event="continue_confirmed",
        audit_tool="plan_confirm_continue",
        extra={"plan_id": plan_id, "confirmation": confirmation.strip()},
    )
    _save_control_state(state)

    return {
        "plan_id": plan_id,
        "confirmed": True,
        "confirmed_at": now,
        "confirmation": confirmation.strip(),
        "message": "Continue confirmation recorded; write tools are now allowed for this plan.",
    }


def tool_plan_guard_status(args: dict[str, Any]) -> dict[str, Any]:
    tail = _as_int(args.get("tail"), 20, minimum=1, maximum=200)

    store = _load_plan_store_for_guard()
    normalized_store = _normalize_plan_store(store)

    plans_raw = normalized_store.get("plans")
    plans = plans_raw if isinstance(plans_raw, dict) else {}
    order_raw = normalized_store.get("plan_order")
    order = order_raw if isinstance(order_raw, list) else []

    non_archived_count = 0
    for plan_id in order:
        plan = plans.get(plan_id)
        if isinstance(plan, dict) and not bool(plan.get("archived", False)):
            non_archived_count += 1

    state = _load_control_state()
    guard = _ensure_guard_policy_state(state)
    latest_plan_id, changed = _resolve_guard_plan_id(state, normalized_store)
    if changed:
        _save_control_state(state)

    confirmed_plan_id = _compact_text(guard.get("continue_confirmed_plan_id"), "") or None
    confirmed_at = _compact_text(guard.get("continue_confirmed_at"), "") or None
    confirmation_text = _compact_text(guard.get("continue_confirmation_text"), "")

    audit_raw = guard.get("audit")
    audit_entries = audit_raw if isinstance(audit_raw, list) else []
    audit_tail = audit_entries[-tail:]

    return {
        "enforce_plan_for_writes": bool(guard.get("enforce_plan_for_writes", True)),
        "enforce_proc_run_policy": bool(guard.get("enforce_proc_run_policy", True)),
        "plan_count": len(order),
        "non_archived_plan_count": non_archived_count,
        "latest_plan_id": latest_plan_id,
        "continue_confirmed_plan_id": confirmed_plan_id,
        "continue_confirmed_at": confirmed_at,
        "continue_confirmation_text": confirmation_text,
        "continue_pending": bool(latest_plan_id and confirmed_plan_id != latest_plan_id),
        "audit_count": len(audit_entries),
        "audit_tail": audit_tail,
    }



def tool_fs_read_text(args: dict[str, Any]) -> dict[str, Any]:
    path = _path(_require_str(args, "path"))
    encoding = str(args.get("encoding", "utf-8"))
    content = path.read_text(encoding=encoding, errors="replace")

    start_line = args.get("start_line")
    end_line = args.get("end_line")
    max_lines = args.get("max_lines")
    max_chars = args.get("max_chars")

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

    content = "".join(lines)

    truncated_by_chars = False
    if max_chars is not None:
        limit = _as_int(max_chars, 0, minimum=0)
        if limit and len(content) > limit:
            content = content[:limit]
            truncated_by_chars = True

    return {
        "path": str(path),
        "encoding": encoding,
        "line_count": len(all_lines),
        "selected_line_count": selected_line_count,
        "returned_line_count": len(lines),
        "truncated": truncated_by_lines or truncated_by_chars,
        "truncated_by_lines": truncated_by_lines,
        "truncated_by_chars": truncated_by_chars,
        "content": content,
    }


def tool_fs_read_texts(args: dict[str, Any]) -> dict[str, Any]:
    ranges_arg = args.get("ranges")
    paths_arg = args.get("paths")

    if ranges_arg is not None and paths_arg is not None:
        raise ValueError("Use either `paths` or `ranges`, not both")

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
                read_args: dict[str, Any] = {"path": raw_path, "encoding": encoding}

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
    "plan_create": tool_plan_create,
    "plan_update": tool_plan_update,
    "plan_view": tool_plan_view,
    "plan_list": tool_plan_list,
    "plan_archive": tool_plan_archive,
    "plan_confirm_continue": tool_plan_confirm_continue,
    "plan_guard_status": tool_plan_guard_status,
    "proc_run": tool_proc_run,
    "img_draw": tool_img_draw,
    "sound_beep": tool_sound_beep,
}


TOOL_DESCRIPTIONS: dict[str, str] = {
    "fs_read_text": "Read a UTF-8 text file. Supports line range, max_lines and max_chars truncation.",
    "fs_read_texts": "Read multiple UTF-8 text files in one call with per-file/total caps and optional per-range line windows.",
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
    "plan_create": "Create a plan group and persist it to root markdown file `<project>-plan.md`.",
    "plan_update": "Update one plan step and sync markdown checklist state.",
    "plan_view": "View one plan group from markdown checklist store.",
    "plan_list": "List plan groups from markdown checklist store.",
    "plan_archive": "Archive or unarchive one markdown plan group.",
    "plan_confirm_continue": "Record explicit user continue confirmation for a plan before write tools are allowed.",
    "plan_guard_status": "View plan-first/write guard state and recent policy audit events.",
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
                        "max_chars": {"type": "integer", "minimum": 0}
                    },
                    "required": ["path"],
                    "additionalProperties": False
                },
                "minItems": 1
            },
            "encoding": {"type": "string", "default": "utf-8"},
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
    "plan_create": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "description": {"type": "string", "default": ""},
            "steps": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        },
        "required": ["title", "steps"],
        "additionalProperties": False,
    },
    "plan_update": {
        "type": "object",
        "properties": {
            "plan_id": {"type": "string"},
            "step_id": {"type": "string"},
            "step_index": {"type": "integer", "minimum": 1},
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "completed", "blocked", "canceled"],
            },
            "note": {"type": "string"},
            "exclusive_in_progress": {"type": "boolean", "default": True},
        },
        "required": ["plan_id", "status"],
        "anyOf": [{"required": ["step_id"]}, {"required": ["step_index"]}],
        "additionalProperties": False,
    },
    "plan_view": {
        "type": "object",
        "properties": {
            "plan_id": {"type": "string"},
        },
        "required": ["plan_id"],
        "additionalProperties": False,
    },
    "plan_list": {
        "type": "object",
        "properties": {
            "include_archived": {"type": "boolean", "default": False},
        },
        "additionalProperties": False,
    },
    "plan_archive": {
        "type": "object",
        "properties": {
            "plan_id": {"type": "string"},
            "archived": {"type": "boolean", "default": True},
        },
        "required": ["plan_id"],
        "additionalProperties": False,
    },
    "plan_confirm_continue": {
        "type": "object",
        "properties": {
            "confirmation": {"type": "string"},
            "plan_id": {"type": "string"},
        },
        "required": ["confirmation"],
        "additionalProperties": False,
    },
    "plan_guard_status": {
        "type": "object",
        "properties": {
            "tail": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
        },
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
