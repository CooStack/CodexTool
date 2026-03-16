from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .common import install_package_with_pip

DEFAULT_WINDOW_TITLE = "多 Agent 协作面板"
DEFAULT_DASHBOARD_TITLE = DEFAULT_WINDOW_TITLE
DEFAULT_POLL_INTERVAL_MS = 900
DASHBOARD_STARTUP_GRACE_SECONDS = 1.0
_STATE_VERSION = 4
_STATUS_MODEL_VERSION = 1
_COMPLETED_STATUSES = {"approved", "completed", "done", "committed"}
_ACTIVE_STATUSES = {"active", "in_progress", "running", "working", "streaming", "editing", "reawakened"}
_OFFLINE_STATUSES = {"offline"}
_ERROR_STATUSES = {"blocked", "failed", "error"}
_THEME = {
    "background": "#090E1A",
    "surface": "#111827",
    "surface_alt": "#0F172A",
    "card": "#111827",
    "card_alt": "#172033",
    "border": "#22314D",
    "accent": "#2DD4BF",
    "accent_secondary": "#38BDF8",
    "accent_soft": "#123B3B",
    "warning": "#F59E0B",
    "danger": "#F97316",
    "success": "#22C55E",
    "text": "#F8FAFC",
    "muted": "#94A3B8",
}
# Keep the older export name alive because the detached Qt dashboard imports it.
_DEFAULT_THEME = _THEME
_EVENT_STATUS_DEFAULTS = {
    "agent_spawned": "pending",
    "agent_status_changed": "",
    "stream_chunk": "streaming",
    "draft_replaced": "editing",
    "draft_committed": "committed",
    "plan_step_upsert": "",
    "agent_closed": "offline",
    "agent_reawakened": "reawakened",
    "run_completed": "completed",
    "dashboard_shutdown": "",
}
_EVENT_DISPLAY_LABELS = {
    "agent_spawned": "启动",
    "agent_reawakened": "恢复",
    "stream_chunk": "流式",
    "draft_replaced": "草稿",
    "draft_committed": "提交",
    "agent_status_changed": "状态",
    "agent_closed": "离线",
    "run_completed": "完成",
    "plan_step_upsert": "计划",
}
_RENDER_MAX_CHARS = 16000
_RENDER_MAX_LINES = 260
_RENDER_HEAD_CHARS = 9000
_RENDER_TAIL_CHARS = 5200
_RENDER_INLINE_LIMIT = 220
_RENDER_STREAM_CHUNK_CHARS = 1800
_RENDER_STREAM_CHUNK_LINES = 32
_RENDER_STREAM_EVENTS = 24
_STATUS_PRIORITIES = {
    "pending": 10,
    "active": 20,
    "in_progress": 20,
    "running": 20,
    "working": 20,
    "reawakened": 20,
    "streaming": 24,
    "editing": 24,
    "blocked": 32,
    "failed": 32,
    "error": 32,
    "offline": 36,
    "committed": 40,
    "done": 40,
    "approved": 40,
    "completed": 40,
}


def default_dashboard_state_path(workspace_root: Path) -> Path:
    root = Path(workspace_root).expanduser().resolve()
    return root / "docs" / "agent-team" / "dashboard-state.json"


def default_dashboard_events_path(workspace_root: Path) -> Path:
    root = Path(workspace_root).expanduser().resolve()
    return root / "docs" / "agent-team" / "dashboard-events.jsonl"


def default_dashboard_drafts_dir(workspace_root: Path) -> Path:
    root = Path(workspace_root).expanduser().resolve()
    return root / "docs" / "agent-team" / "drafts"


def resolve_dashboard_state_path(*, state_path: Any = None, workspace_root: Any = None) -> Path:
    if isinstance(state_path, Path):
        return state_path.expanduser().resolve()
    if isinstance(state_path, str) and state_path.strip():
        return Path(state_path).expanduser().resolve()
    if isinstance(workspace_root, Path):
        return default_dashboard_state_path(workspace_root.expanduser().resolve())
    if isinstance(workspace_root, str) and workspace_root.strip():
        return default_dashboard_state_path(Path(workspace_root).expanduser().resolve())
    raise ValueError("`state_path` is preferred; `workspace_root` is supported for compatibility")


def infer_workspace_root_from_dashboard_state_path(state_path: Path) -> Path:
    target = Path(state_path).expanduser().resolve()
    if target.name == "dashboard-state.json" and target.parent.name == "agent-team":
        docs_dir = target.parent.parent
        if docs_dir.name == "docs":
            return docs_dir.parent.resolve()
    return target.parent.resolve()


def _default_dashboard_role_specs() -> list[dict[str, str]]:
    return [
        {
            "role_id": "planner",
            "title": "Planning Specialist",
            "persona_hint": "产品经理 / 架构师",
            "output_prefix": "[planner]",
        },
        {
            "role_id": "implementation",
            "title": "Implementation Worker",
            "persona_hint": "通用工程师",
            "output_prefix": "[worker:implementation]",
        },
        {
            "role_id": "reviewer",
            "title": "Independent Reviewer",
            "persona_hint": "代码审查 / QA",
            "output_prefix": "[reviewer]",
        },
    ]


def _coerce_dashboard_role_specs(raw_roles: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_roles, list) or not raw_roles:
        return list(_default_dashboard_role_specs())
    normalized: list[dict[str, Any]] = []
    for item in raw_roles:
        if isinstance(item, str) and item.strip():
            role_id = _slugify_role_id(item.strip()) or "agent"
            normalized.append(
                {
                    "role_id": role_id,
                    "title": item.strip(),
                    "persona_hint": "",
                    "output_prefix": f"[{role_id}]",
                }
            )
            continue
        if not isinstance(item, dict):
            continue
        role_id = _slugify_role_id(str(item.get("role_id") or item.get("id") or item.get("title") or "").strip())
        if not role_id:
            continue
        normalized.append(
            {
                "role_id": role_id,
                "title": str(item.get("title") or role_id).strip() or role_id,
                "persona_hint": str(item.get("persona_hint") or item.get("hint") or "").strip(),
                "output_prefix": str(item.get("output_prefix") or f"[{role_id}]").strip() or f"[{role_id}]",
                "status": str(item.get("status") or "pending").strip() or "pending",
                "latest_message": str(item.get("latest_message") or "").strip(),
            }
        )
    return normalized or list(_default_dashboard_role_specs())


def ensure_dashboard_state_exists(
    state_path: Path,
    *,
    workspace_root: Path | None = None,
    request: str = "",
    constraints: list[str] | None = None,
    roles: list[dict[str, Any]] | None = None,
    title: str | None = None,
    poll_interval_ms: int | None = None,
    active_run_id: str | None = None,
    auto_open: bool = False,
) -> dict[str, Any]:
    target = Path(state_path).expanduser().resolve()
    if target.exists():
        state = load_dashboard_state(target)
        runtime_init = initialize_dashboard_runtime_files(target)
        return {"state_path": str(target), "created": False, "state": state, "runtime_files": runtime_init}

    root = Path(workspace_root).expanduser().resolve() if workspace_root is not None else infer_workspace_root_from_dashboard_state_path(target)
    docs_root = target.parent.resolve()
    state = build_dashboard_state(
        workspace_root=root,
        roles=_coerce_dashboard_role_specs(roles),
        request=str(request or "Open the agent team dashboard."),
        constraints=list(constraints or []),
        auto_open=auto_open,
        poll_interval_ms=int(poll_interval_ms or DEFAULT_POLL_INTERVAL_MS),
        title=title,
        docs_root=docs_root,
        active_run_id=str(active_run_id or "adhoc").strip() or "adhoc",
    )
    state["shared_files"]["state_path"] = str(target)
    state["artifacts"]["state_path"] = str(target)
    write_dashboard_state(target, state)
    runtime_init = initialize_dashboard_runtime_files(target)
    return {"state_path": str(target), "created": True, "state": state, "runtime_files": runtime_init}


def build_dashboard_state(
    workspace_root: Path,
    roles: list[dict[str, Any]],
    request: str,
    constraints: list[str],
    *,
    auto_open: bool,
    poll_interval_ms: int,
    title: str | None = None,
    docs_root: Path | None = None,
    active_run_id: str | None = None,
    orchestration: dict[str, Any] | None = None,
    plan_steps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    root = Path(workspace_root).expanduser().resolve()
    agent_team_root = root / "docs" / "agent-team"
    current_docs_root = Path(docs_root).expanduser().resolve() if docs_root is not None else agent_team_root
    handovers_root = current_docs_root / "handovers"
    drafts_root = current_docs_root / "drafts"
    events_path = current_docs_root / "dashboard-events.jsonl"
    window_title = title.strip() if isinstance(title, str) and title.strip() else DEFAULT_WINDOW_TITLE
    generated_at = _now_iso()
    run_id = str(active_run_id or "default").strip() or "default"
    normalized_roles: list[dict[str, Any]] = []
    for role in roles:
        if not isinstance(role, dict):
            continue
        role_id = str(role.get("role_id", "")).strip()
        if not role_id:
            continue
        visible = role.get("visible")
        if visible is None:
            visible = role.get("show_in_dashboard", True)
        if not bool(visible):
            continue
        handover_path = handovers_root / f"{_slugify_role_id(role_id)}.md"
        normalized_roles.append(
            {
                "role_id": role_id,
                "title": str(role.get("title", role_id)).strip() or role_id,
                "persona_hint": str(role.get("persona_hint", "")).strip(),
                "output_prefix": str(role.get("output_prefix", f"[{role_id}]")),
                "runtime_agent_id": str(role.get("runtime_agent_id") or role.get("agent_id") or "").strip(),
                "runtime_agent_name": str(role.get("runtime_agent_name") or role.get("agent_name") or "").strip(),
                "handover_path": str(handover_path),
                "visible": True,
                "status": _normalize_status(str(role.get("status") or "pending")),
                "latest_message": str(role.get("latest_message") or "").strip(),
                "ready_for_review": bool(role.get("ready_for_review", False)),
                "blocked_on": _normalize_string_list(role.get("blocked_on")),
                "depends_on": _normalize_string_list(role.get("depends_on")),
                "last_updated_at": str(role.get("last_updated_at") or generated_at),
                "documents": _build_role_documents(
                    role_id,
                    docs_root=current_docs_root,
                    drafts_root=drafts_root,
                    handover_path=handover_path,
                ),
            }
        )

    interval = int(poll_interval_ms) if int(poll_interval_ms) > 0 else DEFAULT_POLL_INTERVAL_MS
    shared_files = {
        "state_path": str(default_dashboard_state_path(root)),
        "plan_path": str(current_docs_root / "plan.md"),
        "interfaces_path": str(current_docs_root / "interfaces.md"),
        "review_log_path": str(current_docs_root / "review-log.md"),
        "runtime_agents_path": str(current_docs_root / "runtime-agents.md"),
        "handovers_dir": str(handovers_root),
        "drafts_dir": str(drafts_root),
        "events_path": str(events_path),
        "runs_root": str(agent_team_root / "runs"),
    }
    runtime = {
        "gui_backend": "qt",
        "install_missing_dependencies": True,
        "status_model_version": _STATUS_MODEL_VERSION,
        "activity_watchdog": {
            "enabled": True,
            "stale_after_ms": 180000,
        },
        "auto_close": {
            "enabled": True,
            "grace_ms": 2600,
        },
    }
    return {
        "version": _STATE_VERSION,
        "generated_at": generated_at,
        "last_updated_at": generated_at,
        "active_run_id": run_id,
        "status": "pending",
        "workspace_root": str(root),
        "agent_team_root": str(agent_team_root),
        "docs_root": str(current_docs_root),
        "request": request,
        "constraints": list(constraints),
        "auto_open": bool(auto_open),
        "poll_interval_ms": interval,
        "title": window_title,
        "window_title": window_title,
        "shared_files": shared_files,
        "artifacts": shared_files,
        "ready_for_review": False,
        "ready_for_review_role_ids": [],
        "blocked_on": [],
        "depends_on": [],
        "plan_steps": _normalize_plan_steps(plan_steps),
        "roles": normalized_roles,
        "orchestration": dict(orchestration or {}),
        "appearance": dict(_THEME),
        "runtime": runtime,
    }


def write_dashboard_state(path_or_state: Path | dict[str, Any], state_or_path: dict[str, Any] | Path) -> Path:
    if isinstance(path_or_state, dict):
        state = path_or_state
        target = Path(state_or_path).expanduser().resolve()
    else:
        target = Path(path_or_state).expanduser().resolve()
        if not isinstance(state_or_path, dict):
            raise TypeError("state must be a mapping when the first argument is a path")
        state = state_or_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def load_dashboard_state(state_path: Path) -> dict[str, Any]:
    target = Path(state_path).expanduser().resolve()
    return json.loads(target.read_text(encoding="utf-8"))


def initialize_dashboard_runtime_files(state_path: Path) -> dict[str, Any]:
    state = load_dashboard_state(Path(state_path))
    docs_root = Path(str(state.get("docs_root") or Path(state_path).parent)).expanduser().resolve()
    shared_files = state.get("shared_files") or state.get("artifacts") or {}
    events_path = _as_path(shared_files.get("events_path"), docs_root / "dashboard-events.jsonl")
    drafts_dir = _as_path(shared_files.get("drafts_dir"), docs_root / "drafts")
    drafts_dir.mkdir(parents=True, exist_ok=True)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    if not events_path.exists():
        events_path.write_text("", encoding="utf-8")
    created_drafts: list[str] = []
    for role in state.get("roles", []):
        if not isinstance(role, dict):
            continue
        for document in role.get("documents", []):
            if not isinstance(document, dict):
                continue
            draft_path = _as_path(document.get("draft_path"), drafts_dir / f"{role.get('role_id', 'role')}__handover.md")
            target_path = _as_path(document.get("target_path"), docs_root / "plan.md")
            draft_path.parent.mkdir(parents=True, exist_ok=True)
            if draft_path.exists():
                continue
            draft_path.write_text(_read_optional_text(target_path), encoding="utf-8")
            created_drafts.append(str(draft_path))
    return {
        "state_path": str(Path(state_path).expanduser().resolve()),
        "events_path": str(events_path),
        "drafts_dir": str(drafts_dir),
        "created_drafts": created_drafts,
    }


def collect_dashboard_snapshot(state_path: Path) -> dict[str, Any]:
    target = Path(state_path).expanduser().resolve()
    state = load_dashboard_state(target)
    shared_files = state.get("shared_files") or state.get("artifacts") or {}
    docs_root = Path(str(state.get("docs_root") or target.parent)).expanduser().resolve()
    plan_path = _as_path(shared_files.get("plan_path"), docs_root / "plan.md")
    interfaces_path = _as_path(shared_files.get("interfaces_path"), docs_root / "interfaces.md")
    review_log_path = _as_path(shared_files.get("review_log_path"), docs_root / "review-log.md")
    events_path = _as_path(shared_files.get("events_path"), docs_root / "dashboard-events.jsonl")

    plan_markdown = _read_optional_text(plan_path)
    interfaces_markdown = _read_optional_text(interfaces_path)
    review_log_markdown = _read_optional_text(review_log_path)
    events = load_dashboard_events(events_path)
    events_by_role = _group_events_by_role(events)
    plan_items = _collect_plan_items(state.get("plan_steps"), plan_markdown, events)

    role_snapshots: list[dict[str, Any]] = []
    for role in state.get("roles", []):
        if not isinstance(role, dict) or not bool(role.get("visible", True)):
            continue
        documents = _load_role_documents(role)
        handover_doc = next((item for item in documents if item["key"] == "handover"), None)
        handover_markdown = handover_doc["committed_markdown"] if handover_doc else ""
        state_status = _normalize_status(str(role.get("status") or "pending"))
        handover_status = _normalize_status(_extract_first_list_item(handover_markdown, "Status:") or "pending")
        state_message = str(role.get("latest_message") or "").strip()
        handover_message = _extract_first_list_item(handover_markdown, "Latest Message:")
        if state_status == "pending" and not state_message and handover_status != "pending":
            base_status = handover_status
        else:
            base_status = state_status or handover_status
        base_message = state_message or handover_message
        activity = events_by_role.get(str(role.get("role_id", "")).strip(), [])
        derived = _derive_role_state(base_status, base_message, activity)
        draft_markdown = handover_doc["draft_markdown"] if handover_doc else ""
        last_event_at = str(activity[-1].get("created_at") or activity[-1].get("ts") or "") if activity else ""
        last_updated_at = str(role.get("last_updated_at") or last_event_at or state.get("last_updated_at") or "")
        blocked_on = _normalize_string_list(role.get("blocked_on"))
        depends_on = _normalize_string_list(role.get("depends_on"))
        ready_for_review = bool(role.get("ready_for_review", False))
        inactivity = _compute_role_inactivity(state, role, derived["status"], last_updated_at, last_event_at)
        role_snapshots.append(
            {
                "role_id": str(role.get("role_id", "")).strip(),
                "title": str(role.get("title", "")).strip(),
                "persona_hint": str(role.get("persona_hint", "")).strip(),
                "output_prefix": str(role.get("output_prefix", "")).strip(),
                "runtime_agent_id": str(role.get("runtime_agent_id", "")).strip(),
                "runtime_agent_name": str(role.get("runtime_agent_name", "")).strip(),
                "status": derived["status"],
                "latest_message": derived["latest_message"],
                "last_event_at": last_event_at,
                "last_updated_at": last_updated_at,
                "ready_for_review": ready_for_review,
                "blocked_on": blocked_on,
                "depends_on": depends_on,
                "handover_path": str(role.get("handover_path", "")).strip(),
                "handover_markdown": handover_markdown,
                "draft_markdown": draft_markdown,
                "documents": documents,
                "outputs": _extract_list_items(handover_markdown, "Outputs:"),
                "verification": _extract_list_items(handover_markdown, "Verification:"),
                "open_questions": _extract_list_items(handover_markdown, "Open Questions:"),
                "activity": activity[-30:],
                "inactivity": inactivity,
            }
        )

    progress = _compute_progress(plan_items, role_snapshots)
    lifecycle = _compute_lifecycle(role_snapshots)
    auto_close = _compute_auto_close(state, progress, lifecycle, events)
    state_summary = {
        "status": str(state.get("status") or "pending"),
        "active_run_id": str(state.get("active_run_id") or ""),
        "state_path": str(target),
        "docs_root": str(docs_root),
        "workspace_root": str(state.get("workspace_root") or _workspace_root_from_state(state, target)),
        "last_updated_at": str(state.get("last_updated_at") or ""),
        "ready_for_review": bool(state.get("ready_for_review", False)),
        "ready_for_review_role_ids": list(state.get("ready_for_review_role_ids") or []),
        "blocked_on": list(state.get("blocked_on") or []),
        "depends_on": list(state.get("depends_on") or []),
    }
    return {
        "state": state,
        "state_summary": state_summary,
        "orchestration": dict(state.get("orchestration") or {}),
        "roles": role_snapshots,
        "events": events[-200:],
        "timeline": events[-200:],
        "progress": progress,
        "plan_items": plan_items,
        "lifecycle": lifecycle,
        "auto_close": auto_close,
        "shared_docs": {
            "plan_markdown": plan_markdown,
            "interfaces_markdown": interfaces_markdown,
            "review_log_markdown": review_log_markdown,
        },
    }


def append_dashboard_event(state_path: Path, event: dict[str, Any]) -> dict[str, Any]:
    target = Path(state_path).expanduser().resolve()
    state = load_dashboard_state(target)
    docs_root = Path(str(state.get("docs_root") or target.parent)).expanduser().resolve()
    shared_files = state.get("shared_files") or state.get("artifacts") or {}
    events_path = _as_path(shared_files.get("events_path"), docs_root / "dashboard-events.jsonl")
    events_path.parent.mkdir(parents=True, exist_ok=True)
    normalized = _normalize_event(event, default_run_id=str(state.get("active_run_id") or "default"))
    _apply_event_to_state(state, normalized)
    write_dashboard_state(target, state)
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(normalized, ensure_ascii=False) + "\n")
    return normalized


def load_dashboard_events(events_path: Path) -> list[dict[str, Any]]:
    target = Path(events_path).expanduser().resolve()
    if not target.exists():
        return []
    events: list[dict[str, Any]] = []
    for raw_line in target.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("type"):
            events.append(item)
    return events


def read_dashboard_events_since(events_path: Path, offset: int = 0) -> dict[str, Any]:
    target = Path(events_path).expanduser().resolve()
    if not target.exists():
        return {"events": [], "next_offset": 0}
    with target.open("rb") as handle:
        handle.seek(max(offset, 0))
        chunk = handle.read()
        next_offset = handle.tell()
    events: list[dict[str, Any]] = []
    for raw_line in chunk.decode("utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("type"):
            events.append(item)
    return {"events": events, "next_offset": next_offset}


def set_role_status(
    state_path: Path,
    role_id: str,
    status: str,
    message: str = "",
    *,
    ready_for_review: Any = None,
    blocked_on: list[str] | None = None,
    depends_on: list[str] | None = None,
) -> dict[str, Any]:
    return record_agent_status(
        Path(state_path),
        role_id,
        status,
        message=message,
        event_type="agent_status_changed",
        ready_for_review=ready_for_review,
        blocked_on=blocked_on,
        depends_on=depends_on,
    )


def append_role_stream_chunk(
    state_path: Path,
    role_id: str,
    chunk: str,
    *,
    document_key: str = "handover",
    message: str = "",
) -> dict[str, Any]:
    state = load_dashboard_state(Path(state_path))
    document = _resolve_role_document(state, role_id, document_key)
    existing = _read_optional_text(_as_path(document.get("draft_path"), Path(document["target_path"])))
    updated = existing + chunk
    _as_path(document.get("draft_path"), Path(document["target_path"])).write_text(updated, encoding="utf-8")
    chunk_preview = _compress_stream_chunk_for_event(chunk)
    summary = str(message or "").strip() or _compact_inline_text(chunk, limit=_RENDER_INLINE_LIMIT) or "流式片段"
    return append_dashboard_event(
        Path(state_path),
        {
            "type": "stream_chunk",
            "role_id": role_id,
            "document_key": document_key,
            "message": summary,
            "payload": {
                "chunk": chunk_preview,
                "chunk_char_count": len(str(chunk or "")),
                "document_key": document_key,
            },
        },
    )


def append_agent_stream_chunk(state_path: Path, role_id: str, chunk: str, *, message: str = "") -> dict[str, Any]:
    return append_role_stream_chunk(state_path, role_id, chunk, document_key="handover", message=message)


def bind_role_runtime_agent(
    state_path: Path,
    role_id: str,
    agent_id: str,
    *,
    agent_name: str = "",
    status: str = "active",
    message: str = "",
) -> dict[str, Any]:
    target = Path(state_path).expanduser().resolve()
    state = load_dashboard_state(target)
    normalized_status = _normalize_status(status)
    normalized_role_id = str(role_id).strip()
    for role in state.get("roles", []):
        if not isinstance(role, dict) or str(role.get("role_id") or "").strip() != normalized_role_id:
            continue
        current_agent_id = str(role.get("runtime_agent_id") or "").strip()
        current_agent_name = str(role.get("runtime_agent_name") or "").strip()
        current_status = _normalize_status(str(role.get("status") or "pending"))
        if current_agent_id == agent_id and current_agent_id:
            changed = False
            if agent_name and not current_agent_name:
                role["runtime_agent_name"] = agent_name
                changed = True
            if changed:
                role["last_updated_at"] = _now_iso()
                write_dashboard_state(target, state)
            merged_status = _merge_role_status(current_status, normalized_status, event_type="agent_spawned")
            return {
                "type": "agent_spawned",
                "role_id": normalized_role_id,
                "status": merged_status,
                "message": message or f"{role_id} bound to runtime agent {agent_id}",
                "payload": {
                    "status": merged_status,
                    "message": message or f"{role_id} bound to runtime agent {agent_id}",
                    "runtime_agent_id": agent_id,
                    "runtime_agent_name": agent_name or current_agent_name,
                    "skipped_duplicate_binding": True,
                },
            }
    return append_dashboard_event(
        target,
        {
            "type": "agent_spawned",
            "role_id": role_id,
            "status": normalized_status,
            "message": message or f"{role_id} bound to runtime agent {agent_id}",
            "payload": {
                "status": normalized_status,
                "message": message or f"{role_id} bound to runtime agent {agent_id}",
                "runtime_agent_id": agent_id,
                "runtime_agent_name": agent_name,
            },
        },
    )


def write_role_document_draft(
    state_path: Path,
    role_id: str,
    document_key: str,
    content: str,
    *,
    message: str = "",
) -> dict[str, Any]:
    state = load_dashboard_state(Path(state_path))
    document = _resolve_role_document(state, role_id, document_key)
    draft_path = _as_path(document.get("draft_path"), Path(document["target_path"]))
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text(content, encoding="utf-8")
    return append_dashboard_event(
        Path(state_path),
        {
            "type": "draft_replaced",
            "role_id": role_id,
            "document_key": document_key,
            "message": message or f"{role_id} updated {document_key} draft",
            "payload": {"document_key": document_key},
        },
    )


def replace_agent_draft(state_path: Path, role_id: str, content: str, *, message: str = "") -> dict[str, Any]:
    return write_role_document_draft(state_path, role_id, "handover", content, message=message)


def commit_role_document_draft(
    state_path: Path,
    role_id: str,
    document_key: str,
    *,
    status: str = "committed",
    message: str = "",
    ready_for_review: Any = None,
    blocked_on: list[str] | None = None,
    depends_on: list[str] | None = None,
) -> dict[str, Any]:
    state = load_dashboard_state(Path(state_path))
    document = _resolve_role_document(state, role_id, document_key)
    draft_path = _as_path(document.get("draft_path"), Path(document["target_path"]))
    target_path = _as_path(document.get("target_path"), draft_path)
    content = _read_optional_text(draft_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(content, encoding="utf-8")
    return append_dashboard_event(
        Path(state_path),
        {
            "type": "draft_committed",
            "role_id": role_id,
            "document_key": document_key,
            "status": status,
            "message": message or f"{role_id} committed {document_key}",
            "payload": _build_status_payload(
                status=_normalize_status(status),
                message=message or f"{role_id} committed {document_key}",
                ready_for_review=ready_for_review,
                blocked_on=blocked_on,
                depends_on=depends_on,
                document_key=document_key,
            ),
        },
    )


def commit_agent_draft(state_path: Path, role_id: str, *, status: str = "committed", latest_message: str = "") -> dict[str, Any]:
    return commit_role_document_draft(state_path, role_id, "handover", status=status, message=latest_message)


def upsert_plan_step(
    state_path: Path,
    step_id: str,
    title: str,
    status: str,
    *,
    owner_role_id: str = "",
) -> dict[str, Any]:
    return append_dashboard_event(
        Path(state_path),
        {
            "type": "plan_step_upsert",
            "role_id": owner_role_id,
            "status": status,
            "message": title,
            "payload": {
                "step_id": step_id,
                "title": title,
                "status": _normalize_status(status),
                "owner_role_id": owner_role_id,
            },
        },
    )


def mark_run_completed(state_path: Path, message: str = "") -> dict[str, Any]:
    ready_at = _iso_from_epoch(time.time() + 2.6)
    return append_dashboard_event(
        Path(state_path),
        {
            "type": "run_completed",
            "message": message or "所有 Agent 任务已完成",
            "payload": {"message": message or "所有 Agent 任务已完成", "ready_at": ready_at},
        },
    )


def ingest_runtime_agent_notification(
    state_path: Path,
    notification: dict[str, Any],
    *,
    role_id: str = "",
    document_key: str = "handover",
    event_type: str = "",
    auto_commit: bool = True,
    update_plan: bool = True,
) -> dict[str, Any]:
    state = load_dashboard_state(Path(state_path))
    resolved_role = _resolve_role_by_runtime_agent(state, str(notification.get("agent_id") or "").strip(), fallback_role_id=role_id)
    if resolved_role is None:
        raise ValueError("runtime agent binding not found for notification")

    resolved_role_id = str(resolved_role.get("role_id") or "").strip()
    runtime_agent_id = str(notification.get("agent_id") or "").strip()
    lifecycle_event_type = str(event_type or "").strip()
    status_key, content = _extract_runtime_notification_status(notification)
    normalized_status = _normalize_runtime_notification_status(status_key)
    summary = _summarize_runtime_notification(content, fallback=runtime_agent_id or resolved_role_id)
    results: list[dict[str, Any]] = []

    results.append(
        record_agent_status(
            Path(state_path),
            resolved_role_id,
            normalized_status,
            message=summary,
            ready_for_review=_runtime_notification_ready_for_review(normalized_status, resolved_role),
        )
    )

    if content:
        results.append(
            append_role_stream_chunk(
                Path(state_path),
                resolved_role_id,
                content if content.endswith("\n") else f"{content}\n",
                document_key=document_key,
                message=summary,
            )
        )
        if auto_commit and normalized_status in _COMPLETED_STATUSES:
            write_role_document_draft(
                Path(state_path),
                resolved_role_id,
                document_key,
                content if content.endswith("\n") else f"{content}\n",
                message=summary,
            )
            results.append(
                commit_role_document_draft(
                    Path(state_path),
                    resolved_role_id,
                    document_key,
                    status=normalized_status,
                    message=summary,
                    ready_for_review=_runtime_notification_ready_for_review(normalized_status, resolved_role),
                )
            )

    if lifecycle_event_type:
        lifecycle_payload: dict[str, Any] = {
            "status": normalized_status,
            "message": summary,
            "status_key": status_key,
            "runtime_agent_id": runtime_agent_id,
            "runtime_lifecycle": lifecycle_event_type.removeprefix("agent_"),
        }
        runtime_agent_name = str(resolved_role.get("runtime_agent_name") or "").strip()
        if runtime_agent_name:
            lifecycle_payload["runtime_agent_name"] = runtime_agent_name
        results.append(
            append_dashboard_event(
                Path(state_path),
                {
                    "type": lifecycle_event_type,
                    "role_id": resolved_role_id,
                    "status": normalized_status,
                    "message": summary,
                    "payload": lifecycle_payload,
                },
            )
        )

    plan_updates: list[dict[str, Any]] = []
    if update_plan:
        for step in _matching_plan_steps_for_role(state, resolved_role_id):
            plan_updates.append(
                upsert_plan_step(
                    Path(state_path),
                    str(step.get("id") or f"worker:{resolved_role_id}"),
                    str(step.get("label") or step.get("id") or resolved_role_id),
                    _plan_status_for_runtime_notification(normalized_status),
                    owner_role_id=resolved_role_id,
                )
            )
    return {
        "role_id": resolved_role_id,
        "agent_id": runtime_agent_id,
        "event_type": lifecycle_event_type,
        "status_key": status_key,
        "status": normalized_status,
        "message": summary,
        "content": content,
        "events": results,
        "plan_updates": plan_updates,
    }


def sync_runtime_agent_bridge(
    state_path: Path,
    *,
    bindings: list[dict[str, Any]] | None = None,
    notifications: list[dict[str, Any]] | None = None,
    spawn_results: list[dict[str, Any]] | None = None,
    wait_result: dict[str, Any] | None = None,
    close_results: list[dict[str, Any]] | None = None,
    auto_commit: bool = True,
    update_plan: bool = True,
) -> dict[str, Any]:
    resolved_payload = build_runtime_bridge_payload(
        bindings=bindings,
        spawn_results=spawn_results,
        notifications=notifications,
        wait_result=wait_result,
        close_results=close_results,
    )
    resolved_bindings = resolved_payload["bindings"]
    resolved_notifications = resolved_payload["notifications"]
    applied_bindings: list[dict[str, Any]] = []
    applied_notifications: list[dict[str, Any]] = []

    for binding in resolved_bindings:
        if not isinstance(binding, dict):
            continue
        role_id = str(binding.get("role_id") or "").strip()
        agent_id = str(binding.get("agent_id") or "").strip()
        if not role_id or not agent_id:
            continue
        applied_bindings.append(
            bind_role_runtime_agent(
                Path(state_path),
                role_id,
                agent_id,
                agent_name=str(binding.get("agent_name") or ""),
                status=str(binding.get("status") or "active"),
                message=str(binding.get("message") or ""),
            )
        )

    for item in resolved_notifications:
        if not isinstance(item, dict):
            continue
        notification = item.get("notification") if isinstance(item.get("notification"), dict) else item
        if not isinstance(notification, dict):
            continue
        applied_notifications.append(
            ingest_runtime_agent_notification(
                Path(state_path),
                notification,
                role_id=str(item.get("role_id") or ""),
                document_key=str(item.get("document_key") or "handover"),
                event_type=str(item.get("event_type") or ""),
                auto_commit=bool(item.get("auto_commit")) if "auto_commit" in item else auto_commit,
                update_plan=bool(item.get("update_plan")) if "update_plan" in item else update_plan,
            )
        )

    return {
        "binding_count": len(applied_bindings),
        "notification_count": len(applied_notifications),
        "bindings": applied_bindings,
        "notifications": applied_notifications,
        "resolved_binding_count": len(resolved_bindings),
        "resolved_notification_count": len(resolved_notifications),
    }


def build_runtime_bridge_payload(
    *,
    bindings: list[dict[str, Any]] | None = None,
    spawn_results: list[dict[str, Any]] | None = None,
    notifications: list[dict[str, Any]] | None = None,
    wait_result: dict[str, Any] | None = None,
    close_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    built_bindings: list[dict[str, Any]] = []
    built_notifications: list[dict[str, Any]] = []

    for item in bindings or []:
        if not isinstance(item, dict):
            continue
        role_id = str(item.get("role_id") or "").strip()
        agent_id = str(item.get("agent_id") or "").strip()
        if not role_id or not agent_id:
            continue
        built_bindings.append(
            {
                "role_id": role_id,
                "agent_id": agent_id,
                "agent_name": str(item.get("agent_name") or item.get("nickname") or "").strip(),
                "status": str(item.get("status") or "active").strip() or "active",
                "message": str(item.get("message") or "").strip(),
            }
        )

    for item in spawn_results or []:
        if not isinstance(item, dict):
            continue
        role_id = str(item.get("role_id") or "").strip()
        agent_id = str(item.get("agent_id") or "").strip()
        if not role_id or not agent_id:
            continue
        built_bindings.append(
            {
                "role_id": role_id,
                "agent_id": agent_id,
                "agent_name": str(item.get("agent_name") or item.get("nickname") or "").strip(),
                "status": str(item.get("status") or "active").strip() or "active",
                "message": str(item.get("message") or "").strip(),
            }
        )

    for item in notifications or []:
        if not isinstance(item, dict):
            continue
        notification = item.get("notification") if isinstance(item.get("notification"), dict) else item
        if not isinstance(notification, dict):
            continue
        built_notifications.append(
            {
                "role_id": str(item.get("role_id") or "").strip(),
                "document_key": str(item.get("document_key") or "handover").strip() or "handover",
                "event_type": str(item.get("event_type") or "").strip(),
                "notification": notification,
            }
        )

    wait_status = (wait_result or {}).get("status")
    if isinstance(wait_status, dict):
        for agent_id, status_payload in wait_status.items():
            if isinstance(status_payload, dict):
                built_notifications.append(
                    {
                        "notification": {
                            "agent_id": str(agent_id).strip(),
                            "status": status_payload,
                        }
                    }
                )

    for item in close_results or []:
        normalized_close = _normalize_bridge_result_notification(item)
        if normalized_close is not None:
            built_notifications.append(normalized_close)

    return {
        "bindings": built_bindings,
        "notifications": built_notifications,
        "binding_count": len(built_bindings),
        "notification_count": len(built_notifications),
    }


def record_agent_status(
    state_path: Path,
    role_id: str,
    status: str,
    *,
    message: str = "",
    event_type: str = "agent_status_changed",
    ready_for_review: Any = None,
    blocked_on: list[str] | None = None,
    depends_on: list[str] | None = None,
) -> dict[str, Any]:
    return append_dashboard_event(
        Path(state_path),
        {
            "type": event_type,
            "role_id": role_id,
            "status": status,
            "message": message,
            "payload": _build_status_payload(
                status=_normalize_status(status),
                message=message,
                ready_for_review=ready_for_review,
                blocked_on=blocked_on,
                depends_on=depends_on,
            ),
        },
    )


def build_dashboard_process_command(
    state_path: Path,
    *,
    title: str | None = None,
    python_executable: str | None = None,
    topmost: bool = True,
    bring_to_front: bool = True,
    poll_interval_ms: int | None = None,
) -> list[str]:
    target = Path(state_path).expanduser().resolve()
    state = load_dashboard_state(target)
    runtime = state.get("runtime") or {}
    window_title = title.strip() if isinstance(title, str) and title.strip() else str(state.get("window_title") or DEFAULT_WINDOW_TITLE)
    command = [
        python_executable.strip() if isinstance(python_executable, str) and python_executable.strip() else _dashboard_python_executable(),
        "-m",
        "toolmodules.extensions.agent_team_dashboard",
        "--state-path",
        str(target),
        "--title",
        window_title,
        "--topmost",
        "1" if topmost else "0",
        "--bring-to-front",
        "1" if bring_to_front else "0",
        "--backend",
        str(runtime.get("gui_backend") or "qt"),
    ]
    if poll_interval_ms is not None:
        command.extend(["--poll-interval-ms", str(int(poll_interval_ms))])
    return command


def _launch_dashboard_process_impl(
    state_path: Path,
    *,
    python_executable: str | None = None,
    title: str | None = None,
    topmost: bool = True,
    bring_to_front: bool = True,
    poll_interval_ms: int | None = None,
) -> dict[str, Any]:
    target = Path(state_path).expanduser().resolve()
    state = load_dashboard_state(target)
    command = build_dashboard_process_command(
        target,
        python_executable=python_executable,
        title=title,
        topmost=topmost,
        bring_to_front=bring_to_front,
        poll_interval_ms=poll_interval_ms,
    )
    launch_details = {
        "state_path": str(target),
        "window_title": str(state.get("window_title") or DEFAULT_WINDOW_TITLE),
        "command": command,
        "gui_backend": str((state.get("runtime") or {}).get("gui_backend") or "qt"),
    }
    try:
        _ensure_dashboard_backend_available(state, python_executable=python_executable)
        workspace_root = _workspace_root_from_state(state, target)
        creationflags = 0
        if os.name == "nt":
            creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0)
        process = subprocess.Popen(
            command,
            cwd=str(workspace_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except Exception as exc:
        return {
            "launched": False,
            "pid": None,
            **launch_details,
            "error": f"{type(exc).__name__}: {exc}",
        }
    deadline = time.monotonic() + DASHBOARD_STARTUP_GRACE_SECONDS
    while True:
        returncode = process.poll()
        if returncode is not None:
            return {
                "launched": False,
                "pid": process.pid,
                **launch_details,
                "error": f"dashboard process exited during startup with code {returncode}",
            }
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    return {
        "launched": True,
        "pid": process.pid,
        **launch_details,
        "error": "",
    }


def launch_dashboard_process(
    state_path: Path,
    *,
    python_executable: str | None = None,
    title: str | None = None,
    topmost: bool = True,
    bring_to_front: bool = True,
    poll_interval_ms: int | None = None,
) -> dict[str, Any]:
    return _launch_dashboard_process_impl(
        state_path,
        python_executable=python_executable,
        title=title,
        topmost=topmost,
        bring_to_front=bring_to_front,
        poll_interval_ms=poll_interval_ms,
    )


spawn_dashboard_process = launch_dashboard_process


dashboard_state_path = default_dashboard_state_path


def _build_role_documents(role_id: str, *, docs_root: Path, drafts_root: Path, handover_path: Path) -> list[dict[str, str]]:
    documents = [
        {
            "key": "handover",
            "label": "Handover",
            "target_path": str(handover_path),
            "draft_path": str(drafts_root / f"{role_id}__handover.md"),
        }
    ]
    if role_id == "planner":
        documents.extend(
            [
                {
                    "key": "plan",
                    "label": "Plan",
                    "target_path": str(docs_root / "plan.md"),
                    "draft_path": str(drafts_root / f"{role_id}__plan.md"),
                },
                {
                    "key": "interfaces",
                    "label": "Interfaces",
                    "target_path": str(docs_root / "interfaces.md"),
                    "draft_path": str(drafts_root / f"{role_id}__interfaces.md"),
                },
            ]
        )
    if role_id == "reviewer":
        documents.append(
            {
                "key": "review_log",
                "label": "Review Log",
                "target_path": str(docs_root / "review-log.md"),
                "draft_path": str(drafts_root / f"{role_id}__review_log.md"),
            }
        )
    return documents


def _load_role_documents(role: dict[str, Any]) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for document in role.get("documents", []):
        if not isinstance(document, dict):
            continue
        target_path = _as_path(document.get("target_path"), Path(role.get("handover_path") or "."))
        draft_path = _as_path(document.get("draft_path"), target_path)
        committed_markdown = _read_optional_text(target_path)
        draft_markdown = _read_optional_text(draft_path)
        snapshots.append(
            {
                "key": str(document.get("key", "")).strip(),
                "label": str(document.get("label", "")).strip(),
                "target_path": str(target_path),
                "draft_path": str(draft_path),
                "committed_markdown": committed_markdown,
                "draft_markdown": draft_markdown,
                "dirty": draft_markdown != committed_markdown,
            }
        )
    return snapshots


def _resolve_role_document(state: dict[str, Any], role_id: str, document_key: str) -> dict[str, Any]:
    wanted_role = role_id.strip()
    wanted_key = document_key.strip() or "handover"
    for role in state.get("roles", []):
        if not isinstance(role, dict):
            continue
        if str(role.get("role_id", "")).strip() != wanted_role:
            continue
        for document in role.get("documents", []):
            if not isinstance(document, dict):
                continue
            if str(document.get("key", "")).strip() == wanted_key:
                return document
    raise ValueError(f"dashboard role/document not found: role_id={wanted_role}, document_key={wanted_key}")


def _group_events_by_role(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        role_id = str(event.get("role_id", "")).strip()
        if not role_id:
            continue
        grouped.setdefault(role_id, []).append(event)
    return grouped


def _derive_role_state(base_status: str, base_message: str, events: list[dict[str, Any]]) -> dict[str, str]:
    status = base_status
    latest_message = base_message
    for event in events:
        event_message = str(event.get("message", "")).strip() or str((event.get("payload") or {}).get("message", "")).strip()
        if event_message:
            latest_message = event_message
        event_status = str(event.get("status", "")).strip() or str((event.get("payload") or {}).get("status", "")).strip()
        if not event_status:
            event_status = _EVENT_STATUS_DEFAULTS.get(str(event.get("type", "")).strip(), "")
            if event_status == "offline" and status in _COMPLETED_STATUSES:
                event_status = "completed"
        if event_status:
            status = _merge_role_status(status, event_status, event_type=str(event.get("type", "")).strip())
    return {
        "status": _normalize_status(status),
        "latest_message": latest_message,
    }


def _collect_plan_items(
    raw_state_steps: Any,
    plan_markdown: str,
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    state_items = _normalize_plan_steps(raw_state_steps)
    if state_items:
        return state_items

    event_items: dict[str, dict[str, Any]] = {}
    for event in events:
        if str(event.get("type", "")).strip() != "plan_step_upsert":
            continue
        payload = event.get("payload") or {}
        step_id = str(payload.get("step_id") or event.get("id") or "").strip()
        if not step_id:
            continue
        event_items[step_id] = {
            "id": step_id,
            "label": str(payload.get("title") or event.get("message") or step_id).strip(),
            "status": _normalize_status(str(payload.get("status") or event.get("status") or "pending")),
            "owner_role_id": str(payload.get("owner_role_id") or event.get("role_id") or "").strip(),
            "source": "plan_event",
        }
    if event_items:
        return list(event_items.values())

    checklist_matches = re.findall(r"(?m)^\s*[-*]\s+\[( |x|X)\]\s+(.+?)\s*$", plan_markdown)
    if checklist_matches:
        items: list[dict[str, Any]] = []
        for index, (mark, label) in enumerate(checklist_matches, start=1):
            items.append(
                {
                    "id": f"checklist-{index}",
                    "label": label,
                    "status": "completed" if mark.lower() == "x" else "pending",
                    "owner_role_id": _extract_owner_from_label(label),
                    "source": "plan_checklist",
                }
            )
        return items
    return []


def _compute_progress(plan_items: list[dict[str, Any]], roles: list[dict[str, Any]]) -> dict[str, Any]:
    if plan_items:
        total = len(plan_items)
        completed = sum(1 for item in plan_items if item.get("status") in _COMPLETED_STATUSES)
        active = sum(1 for item in plan_items if item.get("status") in _ACTIVE_STATUSES)
        pending = max(total - completed - active, 0)
        percent = int((completed * 100) / total) if total else 0
        return {
            "source": str(plan_items[0].get("source") or "plan_items"),
            "total": total,
            "completed": completed,
            "active": active,
            "pending": pending,
            "percent": percent,
            "items": plan_items,
        }
    total = len(roles)
    completed = sum(1 for role in roles if role.get("status") in _COMPLETED_STATUSES)
    active = sum(1 for role in roles if role.get("status") in _ACTIVE_STATUSES)
    pending = max(total - completed - active, 0)
    percent = int((completed * 100) / total) if total else 0
    return {
        "source": "handover_status",
        "total": total,
        "completed": completed,
        "active": active,
        "pending": pending,
        "percent": percent,
        "items": [],
    }


def _compute_role_inactivity(
    state: dict[str, Any],
    role: dict[str, Any],
    status: str,
    last_updated_at: str,
    last_event_at: str,
) -> dict[str, Any]:
    runtime = state.get("runtime") or {}
    watchdog = runtime.get("activity_watchdog") or {}
    enabled = bool(watchdog.get("enabled", True))
    stale_after_ms = max(int(watchdog.get("stale_after_ms", 180000) or 0), 0)
    normalized_status = _normalize_status(status)
    last_signal_at = str(last_updated_at or last_event_at or state.get("last_updated_at") or "").strip()
    if not enabled:
        return {
            "enabled": False,
            "suspected_disconnect": False,
            "idle_ms": 0,
            "stale_after_ms": stale_after_ms,
            "last_signal_at": last_signal_at,
            "reason": "disabled",
        }
    if normalized_status not in _ACTIVE_STATUSES:
        return {
            "enabled": True,
            "suspected_disconnect": False,
            "idle_ms": 0,
            "stale_after_ms": stale_after_ms,
            "last_signal_at": last_signal_at,
            "reason": "inactive_status",
        }
    if stale_after_ms <= 0:
        return {
            "enabled": True,
            "suspected_disconnect": False,
            "idle_ms": 0,
            "stale_after_ms": stale_after_ms,
            "last_signal_at": last_signal_at,
            "reason": "threshold_disabled",
        }

    last_signal_epoch = _epoch_from_iso(last_signal_at)
    if last_signal_epoch <= 0:
        return {
            "enabled": True,
            "suspected_disconnect": True,
            "idle_ms": stale_after_ms,
            "stale_after_ms": stale_after_ms,
            "last_signal_at": "",
            "reason": "no_signal",
        }

    idle_ms = max(int((time.time() - last_signal_epoch) * 1000), 0)
    return {
        "enabled": True,
        "suspected_disconnect": idle_ms >= stale_after_ms,
        "idle_ms": idle_ms,
        "stale_after_ms": stale_after_ms,
        "last_signal_at": last_signal_at,
        "reason": "stale" if idle_ms >= stale_after_ms else "healthy",
    }


def _compute_lifecycle(roles: list[dict[str, Any]]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    for role in roles:
        status = str(role.get("status") or "pending")
        by_status[status] = by_status.get(status, 0) + 1
    active_roles = [role["role_id"] for role in roles if role.get("status") in _ACTIVE_STATUSES]
    completed_roles = [role["role_id"] for role in roles if role.get("status") in _COMPLETED_STATUSES]
    offline_roles = [role["role_id"] for role in roles if role.get("status") in _OFFLINE_STATUSES]
    suspected_disconnect_roles = [
        role["role_id"]
        for role in roles
        if isinstance(role.get("inactivity"), dict) and bool((role.get("inactivity") or {}).get("suspected_disconnect"))
    ]
    return {
        "counts": by_status,
        "active_role_ids": active_roles,
        "completed_role_ids": completed_roles,
        "offline_role_ids": offline_roles,
        "suspected_disconnect_role_ids": suspected_disconnect_roles,
        "all_terminal": len(active_roles) == 0 and len(completed_roles) + len(offline_roles) == len(roles),
    }


def _compute_auto_close(
    state: dict[str, Any],
    progress: dict[str, Any],
    lifecycle: dict[str, Any],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    runtime = state.get("runtime") or {}
    auto_close = runtime.get("auto_close") or {}
    enabled = bool(auto_close.get("enabled", True))
    grace_ms = int(auto_close.get("grace_ms", 2600))
    if not enabled:
        return {"enabled": False, "should_close": False, "reason": "disabled", "grace_ms": grace_ms}
    run_completed_event = next((event for event in reversed(events) if str(event.get("type", "")).strip() == "run_completed"), None)
    if run_completed_event is not None:
        ready_at = str((run_completed_event.get("payload") or {}).get("ready_at") or run_completed_event.get("created_at") or "")
        deadline_passed = _event_deadline_passed(ready_at)
        return {
            "enabled": True,
            "should_close": True,
            "reason": "run_completed_event",
            "grace_ms": grace_ms,
            "ready_at": ready_at,
            "deadline_passed": deadline_passed,
        }
    plan_complete = progress.get("total", 0) > 0 and progress.get("completed", 0) >= progress.get("total", 0)
    if plan_complete and not lifecycle.get("active_role_ids"):
        latest_terminal_ts = _latest_terminal_timestamp(events)
        ready_at = _iso_from_epoch(latest_terminal_ts + (grace_ms / 1000.0)) if latest_terminal_ts else ""
        return {
            "enabled": True,
            "should_close": True,
            "reason": "all_tasks_completed",
            "grace_ms": grace_ms,
            "ready_at": ready_at,
            "deadline_passed": _event_deadline_passed(ready_at),
        }
    return {"enabled": True, "should_close": False, "reason": "waiting", "grace_ms": grace_ms, "ready_at": "", "deadline_passed": False}


def _ensure_dashboard_backend_available(state: dict[str, Any], *, python_executable: str | None = None) -> None:
    runtime = state.get("runtime") or {}
    backend = str(runtime.get("gui_backend") or "qt").strip().lower()
    if backend != "qt":
        return
    target_python = python_executable.strip() if isinstance(python_executable, str) and python_executable.strip() else sys.executable
    if _python_has_module(target_python, "PySide6"):
        return
    if bool(runtime.get("install_missing_dependencies", True)):
        _install_package_with_target_python(target_python, "PySide6", "agent team dashboard Qt GUI")
        return
    raise RuntimeError("PySide6 is required for the agent team dashboard Qt GUI. Install it with `python -m pip install PySide6`.")


def _workspace_root_from_state(state: dict[str, Any], fallback_state_path: Path) -> Path:
    workspace_root_raw = str(state.get("workspace_root", "")).strip()
    if workspace_root_raw:
        return Path(workspace_root_raw).expanduser().resolve()
    return fallback_state_path.parent.parent.parent


def _dashboard_python_executable() -> str:
    if os.name == "nt":
        candidate = Path(sys.executable).with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
    return sys.executable


def _apply_event_to_state(state: dict[str, Any], event: dict[str, Any]) -> None:
    event_time = str(event.get("created_at") or event.get("ts") or _now_iso())
    state["last_updated_at"] = event_time
    state["active_run_id"] = str(event.get("run_id") or state.get("active_run_id") or "default")

    payload = event.get("payload") or {}
    event_type = str(event.get("type") or "").strip()
    if event_type == "plan_step_upsert":
        _upsert_plan_step_state(
            state,
            {
                "id": str(payload.get("step_id") or event.get("id") or "").strip(),
                "label": str(payload.get("title") or event.get("message") or payload.get("step_id") or "").strip(),
                "status": str(payload.get("status") or event.get("status") or "pending"),
                "owner_role_id": str(payload.get("owner_role_id") or event.get("role_id") or "").strip(),
                "source": "plan_event",
            },
        )
    role_id = str(event.get("role_id") or "").strip()
    if role_id and event_type != "plan_step_upsert":
        for role in state.get("roles", []):
            if not isinstance(role, dict) or str(role.get("role_id") or "").strip() != role_id:
                continue
            event_status = str(event.get("status") or payload.get("status") or _EVENT_STATUS_DEFAULTS.get(str(event.get("type") or "").strip(), "")).strip()
            if event_status:
                role["status"] = _merge_role_status(str(role.get("status") or ""), event_status, event_type=event_type)
            message = str(event.get("message") or payload.get("message") or "").strip()
            if message:
                role["latest_message"] = message
            if "runtime_agent_id" in payload:
                role["runtime_agent_id"] = str(payload.get("runtime_agent_id") or "").strip()
            if "runtime_agent_name" in payload:
                role["runtime_agent_name"] = str(payload.get("runtime_agent_name") or "").strip()
            role["last_updated_at"] = event_time
            if "blocked_on" in payload:
                role["blocked_on"] = _normalize_string_list(payload.get("blocked_on"))
            if "depends_on" in payload:
                role["depends_on"] = _normalize_string_list(payload.get("depends_on"))
            if "ready_for_review" in payload and payload.get("ready_for_review") is not None:
                role["ready_for_review"] = bool(payload.get("ready_for_review"))
            elif event_type == "draft_committed":
                role["ready_for_review"] = _default_ready_for_review(role)
            elif role.get("status") in _ACTIVE_STATUSES or role.get("status") in _ERROR_STATUSES:
                role["ready_for_review"] = False
            break

    if event_type == "run_completed":
        state["status"] = "completed"
    _refresh_dashboard_state(state)


def _build_status_payload(
    *,
    status: str,
    message: str,
    ready_for_review: Any = None,
    blocked_on: list[str] | None = None,
    depends_on: list[str] | None = None,
    document_key: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": status, "message": message}
    if ready_for_review is not None:
        payload["ready_for_review"] = bool(ready_for_review)
    if blocked_on is not None:
        payload["blocked_on"] = blocked_on
    if depends_on is not None:
        payload["depends_on"] = depends_on
    if document_key is not None:
        payload["document_key"] = document_key
    return payload


def _resolve_role_by_runtime_agent(state: dict[str, Any], agent_id: str, *, fallback_role_id: str = "") -> dict[str, Any] | None:
    wanted_agent_id = agent_id.strip()
    if wanted_agent_id:
        for role in state.get("roles", []):
            if not isinstance(role, dict):
                continue
            if str(role.get("runtime_agent_id") or "").strip() == wanted_agent_id:
                return role
    wanted_role_id = fallback_role_id.strip()
    if wanted_role_id:
        for role in state.get("roles", []):
            if not isinstance(role, dict):
                continue
            if str(role.get("role_id") or "").strip() == wanted_role_id:
                return role
    return None


def _extract_runtime_notification_status(notification: dict[str, Any]) -> tuple[str, str]:
    raw_status = notification.get("status")
    if isinstance(raw_status, dict):
        for key in ("completed", "errored", "error", "failed", "interrupted", "cancelled", "streaming", "running"):
            value = raw_status.get(key)
            if isinstance(value, str) and value.strip():
                return key, value
        for key, value in raw_status.items():
            if isinstance(value, str) and value.strip():
                return str(key).strip(), value
    if isinstance(raw_status, str) and raw_status.strip():
        return raw_status.strip(), str(notification.get("content") or notification.get("message") or "").strip()
    return "completed", str(notification.get("content") or notification.get("message") or "").strip()


def _normalize_runtime_notification_status(status_key: str) -> str:
    normalized = _normalize_status(status_key)
    if normalized in {"completed", "done", "approved"}:
        return "completed"
    if normalized in {"streaming", "running", "working", "active", "in_progress"}:
        return "streaming"
    if normalized in {"interrupted", "cancelled", "blocked"}:
        return "blocked"
    if normalized in {"errored", "error", "failed"}:
        return "error"
    return normalized or "completed"


def _summarize_runtime_notification(content: str, *, fallback: str) -> str:
    text = str(content or "").strip()
    if not text:
        return fallback
    first_line = text.splitlines()[0].strip()
    return first_line or fallback


def _runtime_notification_ready_for_review(status: str, role: dict[str, Any]) -> bool:
    if status not in _COMPLETED_STATUSES:
        return False
    return _default_ready_for_review(role)


def _matching_plan_steps_for_role(state: dict[str, Any], role_id: str) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    for step in state.get("plan_steps", []):
        if not isinstance(step, dict):
            continue
        if str(step.get("owner_role_id") or "").strip() == role_id:
            matched.append(step)
    return matched


def _plan_status_for_runtime_notification(status: str) -> str:
    if status in _COMPLETED_STATUSES:
        return "completed"
    if status in _ERROR_STATUSES:
        return "blocked"
    if status in _ACTIVE_STATUSES:
        return "in_progress"
    return status


def _default_ready_for_review(role: dict[str, Any]) -> bool:
    role_id = str(role.get("role_id") or "").strip()
    if role_id in {"planner", "reviewer", "integrator"}:
        return False
    return str(role.get("status") or "") in _COMPLETED_STATUSES


def _status_priority(status: str) -> int:
    normalized = _normalize_status(status)
    return _STATUS_PRIORITIES.get(normalized, 15 if normalized else 0)


def _merge_role_status(current_status: str, incoming_status: str, *, event_type: str = "") -> str:
    current = _normalize_status(current_status)
    incoming = _normalize_status(incoming_status)
    normalized_event_type = str(event_type or "").strip()
    if not incoming:
        return current
    if not current:
        return incoming
    if normalized_event_type in {"agent_spawned", "agent_reawakened"} and current not in {"", "pending"}:
        return current if _status_priority(current) >= _status_priority(incoming) else incoming
    if incoming in _OFFLINE_STATUSES and current in (_COMPLETED_STATUSES | _ERROR_STATUSES):
        return current
    if _status_priority(incoming) < _status_priority(current):
        return current
    return incoming


def _refresh_dashboard_state(state: dict[str, Any]) -> None:
    ready_roles: list[str] = []
    blocked_entries: list[dict[str, Any]] = []
    depends_on: set[str] = set()
    active_found = False
    blocked_found = False
    completed_roles = 0
    visible_roles = 0

    for role in state.get("roles", []):
        if not isinstance(role, dict) or not bool(role.get("visible", True)):
            continue
        visible_roles += 1
        role_status = _normalize_status(str(role.get("status") or "pending"))
        role["status"] = role_status
        role["blocked_on"] = _normalize_string_list(role.get("blocked_on"))
        role["depends_on"] = _normalize_string_list(role.get("depends_on"))
        role["ready_for_review"] = bool(role.get("ready_for_review", False))
        if role["ready_for_review"]:
            ready_roles.append(str(role.get("role_id") or ""))
        if role["blocked_on"]:
            blocked_found = True
            blocked_entries.append({"role_id": str(role.get("role_id") or ""), "reasons": list(role["blocked_on"])})
        depends_on.update(role["depends_on"])
        if role_status in _ACTIVE_STATUSES:
            active_found = True
        if role_status in _COMPLETED_STATUSES or role_status in _OFFLINE_STATUSES:
            completed_roles += 1

    state["ready_for_review_role_ids"] = ready_roles
    state["ready_for_review"] = bool(ready_roles)
    state["blocked_on"] = blocked_entries
    state["depends_on"] = sorted(depends_on)
    if blocked_found:
        state["status"] = "blocked"
    elif active_found:
        state["status"] = "in_progress"
    elif visible_roles > 0 and completed_roles == visible_roles:
        state["status"] = "completed"
    else:
        state["status"] = str(state.get("status") or "pending") if str(state.get("status") or "").strip() == "completed" else "pending"


def _as_path(raw_value: Any, fallback: Path) -> Path:
    if isinstance(raw_value, str) and raw_value.strip():
        return Path(raw_value).expanduser().resolve()
    return fallback.expanduser().resolve()


def _read_optional_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _compact_inline_text(text: str, *, limit: int = _RENDER_INLINE_LIMIT) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "").strip())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(limit - 1, 0)].rstrip() + "…"


def _compress_text_for_render(
    text: str,
    *,
    max_chars: int = _RENDER_MAX_CHARS,
    max_lines: int = _RENDER_MAX_LINES,
    head_chars: int = _RENDER_HEAD_CHARS,
    tail_chars: int = _RENDER_TAIL_CHARS,
) -> str:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not normalized:
        return ""

    lines = normalized.split("\n")
    if max_lines > 0 and len(lines) > max_lines:
        head_lines = max(max_lines // 2, 1)
        tail_lines = max(max_lines - head_lines, 1)
        omitted_lines = max(len(lines) - head_lines - tail_lines, 0)
        lines = lines[:head_lines] + [f"... [为避免界面卡顿，已省略 {omitted_lines} 行] ..."] + lines[-tail_lines:]
        normalized = "\n".join(lines)

    if max_chars > 0 and len(normalized) > max_chars:
        safe_head = max(min(head_chars, max_chars - 48), 1)
        safe_tail = max(min(tail_chars, max_chars - safe_head - 48), 0)
        omitted_chars = max(len(normalized) - safe_head - safe_tail, 0)
        tail_text = normalized[-safe_tail:].lstrip() if safe_tail else ""
        normalized = (
            normalized[:safe_head].rstrip()
            + f"\n\n... [为避免界面卡顿，已省略 {omitted_chars} 个字符] ...\n\n"
            + tail_text
        ).rstrip()
    return normalized


def _compress_stream_chunk_for_event(chunk: str) -> str:
    return _compress_text_for_render(
        chunk,
        max_chars=_RENDER_STREAM_CHUNK_CHARS,
        max_lines=_RENDER_STREAM_CHUNK_LINES,
        head_chars=max(_RENDER_STREAM_CHUNK_CHARS // 2, 1),
        tail_chars=max(_RENDER_STREAM_CHUNK_CHARS // 3, 1),
    )


def _strip_markdown_inline(text: str) -> str:
    stripped = re.sub(r"`([^`]+)`", r"\1", str(text or ""))
    stripped = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1", stripped)
    stripped = stripped.replace("**", "").replace("__", "").replace("*", "")
    return stripped


def _render_markdown_as_text(markdown_text: str, *, fallback_title: str = "") -> str:
    source = str(markdown_text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not source.strip():
        source = fallback_title.strip()
    lines = source.split("\n")
    rendered: list[str] = []
    in_code_block = False

    for raw_line in lines:
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            if in_code_block:
                rendered.append("----- 代码结束 -----")
                rendered.append("")
                in_code_block = False
            else:
                language = stripped[3:].strip()
                rendered.append("")
                rendered.append(f"----- 代码块 {language or 'text'} -----")
                in_code_block = True
            continue

        if in_code_block:
            rendered.append(raw_line)
            continue

        heading_match = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading_match:
            title = _strip_markdown_inline(heading_match.group(2)).strip()
            if title:
                if rendered and rendered[-1]:
                    rendered.append("")
                rendered.append(title)
                underline_char = "=" if len(heading_match.group(1)) == 1 else "-"
                rendered.append(underline_char * min(max(len(title), 4), 48))
            continue

        checklist_match = re.match(r"^(\s*)[-*]\s+\[([ xX])\]\s+(.*)$", raw_line)
        if checklist_match:
            indent = checklist_match.group(1)
            mark = "x" if checklist_match.group(2).lower() == "x" else " "
            rendered.append(f"{indent}[{mark}] {_strip_markdown_inline(checklist_match.group(3)).strip()}")
            continue

        bullet_match = re.match(r"^(\s*)[-*+]\s+(.*)$", raw_line)
        if bullet_match:
            indent = bullet_match.group(1)
            rendered.append(f"{indent}• {_strip_markdown_inline(bullet_match.group(2)).strip()}")
            continue

        quote_match = re.match(r"^\s*>\s?(.*)$", raw_line)
        if quote_match:
            rendered.append(f"引用: {_strip_markdown_inline(quote_match.group(1)).strip()}")
            continue

        rendered.append(_strip_markdown_inline(raw_line))

    text = "\n".join(rendered).strip("\n")
    return _compress_text_for_render(text, max_chars=_RENDER_MAX_CHARS, max_lines=_RENDER_MAX_LINES)


def _render_role_activity_as_text(role: dict[str, Any]) -> str:
    activity = list(role.get("activity") or [])
    if not activity:
        return "活动输出\n========\n\n当前还没有流式事件。\n"

    lines = ["活动输出", "========", ""]
    for event in reversed(activity[-_RENDER_STREAM_EVENTS:]):
        event_type = str(event.get("type", "")).strip()
        timestamp = str(event.get("created_at") or event.get("ts") or "").strip()
        label = _EVENT_DISPLAY_LABELS.get(event_type, event_type or "事件")
        status = str(event.get("status") or (event.get("payload") or {}).get("status") or "").strip()
        message = _compact_inline_text(str(event.get("message") or event.get("text") or "").strip(), limit=320)
        payload = event.get("payload") or {}
        chunk = str(payload.get("chunk") or "").rstrip("\n")

        lines.append(f"[{label}] {timestamp or 'n/a'}")
        if status:
            lines.append(f"状态: {status}")
        if message:
            lines.append(f"消息: {message}")
        if chunk:
            lines.append("内容:")
            lines.extend(f"    {line}" if line else "" for line in chunk.splitlines())
        lines.append("-" * 72)
        lines.append("")

    return _compress_text_for_render("\n".join(lines).rstrip() + "\n")


def _normalize_bridge_result_notification(item: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    source = item.get("result") if isinstance(item.get("result"), dict) else item
    if not isinstance(source, dict):
        return None
    status_payload = source.get("status")
    if not isinstance(status_payload, dict):
        return None
    agent_id = str(item.get("agent_id") or source.get("agent_id") or "").strip()
    if not agent_id:
        return None
    return {
        "role_id": str(item.get("role_id") or "").strip(),
        "document_key": str(item.get("document_key") or "handover").strip() or "handover",
        "event_type": "agent_closed",
        "notification": {
            "agent_id": agent_id,
            "status": status_payload,
        },
    }


def _normalize_status(status: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", status.strip().lower()).strip("_")
    return normalized or "pending"


def _normalize_string_list(raw_value: Any) -> list[str]:
    if raw_value is None:
        return []
    if isinstance(raw_value, str):
        return [raw_value.strip()] if raw_value.strip() else []
    if not isinstance(raw_value, list):
        return []
    normalized: list[str] = []
    for item in raw_value:
        if isinstance(item, str) and item.strip():
            normalized.append(item.strip())
    return normalized


def _normalize_plan_steps(raw_value: Any) -> list[dict[str, Any]]:
    if raw_value is None or not isinstance(raw_value, list):
        return []
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(raw_value, start=1):
        if not isinstance(item, dict):
            continue
        step_id = str(item.get("id") or item.get("step_id") or f"step-{index}").strip()
        if not step_id:
            continue
        label = str(item.get("label") or item.get("title") or step_id).strip() or step_id
        normalized.append(
            {
                "id": step_id,
                "label": label,
                "status": _normalize_status(str(item.get("status") or "pending")),
                "owner_role_id": str(item.get("owner_role_id") or item.get("role_id") or "").strip(),
                "source": str(item.get("source") or "state").strip() or "state",
            }
        )
    return normalized


def _upsert_plan_step_state(state: dict[str, Any], step: dict[str, Any]) -> None:
    wanted_id = str(step.get("id") or "").strip()
    if not wanted_id:
        return
    normalized_step = _normalize_plan_steps([step])
    if not normalized_step:
        return
    steps = _normalize_plan_steps(state.get("plan_steps"))
    for index, existing in enumerate(steps):
        if str(existing.get("id") or "").strip() != wanted_id:
            continue
        steps[index] = normalized_step[0]
        state["plan_steps"] = steps
        return
    steps.append(normalized_step[0])
    state["plan_steps"] = steps


def _slugify_role_id(role_id: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(role_id).strip()).strip("-").lower()
    return slug or "role"


def _extract_first_list_item(markdown: str, heading: str) -> str:
    items = _extract_list_items(markdown, heading)
    return items[0] if items else ""


def _extract_list_items(markdown: str, heading: str) -> list[str]:
    lines = markdown.splitlines()
    heading_text = heading.strip()
    for index, raw_line in enumerate(lines):
        if raw_line.strip() != heading_text:
            continue
        items: list[str] = []
        for follow in lines[index + 1 :]:
            stripped = follow.strip()
            if not stripped:
                if items:
                    break
                continue
            if stripped.endswith(":") and not stripped.startswith(("-", "*")):
                break
            if stripped.startswith(("- ", "* ")):
                items.append(stripped[2:].strip())
            elif items:
                break
        return items
    return []


def _extract_owner_from_label(label: str) -> str:
    match = re.search(r"\[(.+?)\]", label)
    if not match:
        return ""
    return match.group(1).strip()


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _normalize_event(event: dict[str, Any], *, default_run_id: str = "default") -> dict[str, Any]:
    if not isinstance(event, dict):
        raise ValueError("dashboard event must be a mapping")
    event_type = str(event.get("type") or event.get("event_type") or "").strip()
    if not event_type:
        raise ValueError("dashboard event `type` is required")
    payload = event.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("dashboard event `payload` must be an object when provided")
    timestamp = str(event.get("ts") or event.get("timestamp") or _now_iso()).strip() or _now_iso()
    role_id = str(event.get("role_id") or payload.get("role_id") or "").strip()
    status = str(event.get("status") or payload.get("status") or "").strip()
    message = str(event.get("message") or payload.get("message") or "").strip()
    document_key = str(event.get("document_key") or payload.get("document_key") or "").strip()
    event_id = str(event.get("id") or f"evt-{time.time_ns()}")
    return {
        "id": event_id,
        "ts": timestamp,
        "created_at": timestamp,
        "type": event_type,
        "role_id": role_id,
        "run_id": str(event.get("run_id") or payload.get("run_id") or default_run_id or "default"),
        "status": _normalize_status(status) if status else "",
        "message": message,
        "text": message,
        "document_key": document_key,
        "payload": payload,
        "event_id": event_id,
    }


def _latest_terminal_timestamp(events: list[dict[str, Any]]) -> float:
    latest = 0.0
    for event in events:
        candidate = _epoch_from_iso(str(event.get("created_at") or event.get("ts") or ""))
        latest = max(latest, candidate)
    return latest


def _event_deadline_passed(ready_at: str) -> bool:
    if not ready_at:
        return False
    return time.time() >= _epoch_from_iso(ready_at)


def _epoch_from_iso(value: str) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        from datetime import datetime

        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return 0.0


def _iso_from_epoch(epoch_value: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch_value))


def _python_has_module(python_executable: str, module_name: str) -> bool:
    candidate = str(python_executable or "").strip()
    if not candidate:
        return importlib.util.find_spec(module_name) is not None
    completed = subprocess.run(
        [candidate, "-c", f"import importlib.util, sys; sys.stdout.write('1' if importlib.util.find_spec('{module_name}') else '0')"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return completed.returncode == 0 and completed.stdout.strip() == "1"


def _install_package_with_target_python(python_executable: str, package_name: str, feature_name: str) -> None:
    candidate = str(python_executable or "").strip()
    if not candidate:
        install_package_with_pip(package_name, feature_name)
        return
    try:
        if Path(candidate).resolve() == Path(sys.executable).resolve():
            install_package_with_pip(package_name, feature_name)
            return
    except Exception:
        pass
    completed = subprocess.run(
        [candidate, "-m", "pip", "install", package_name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "pip install failed").strip()
        raise RuntimeError(
            f"Automatic dependency install failed for {feature_name}. "
            f"Run `{candidate} -m pip install {package_name}` manually. Details: {detail}"
        )
