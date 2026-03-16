from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from .agent_team_dashboard import (
    DEFAULT_WINDOW_TITLE,
    append_dashboard_event,
    append_role_stream_chunk,
    bind_role_runtime_agent,
    build_dashboard_state,
    build_runtime_bridge_payload,
    commit_role_document_draft,
    default_dashboard_state_path,
    ingest_runtime_agent_notification,
    initialize_dashboard_runtime_files,
    launch_dashboard_process,
    mark_run_completed,
    resolve_dashboard_state_path,
    set_role_status,
    sync_runtime_agent_bridge,
    upsert_plan_step,
    write_dashboard_state,
    write_role_document_draft,
)
from .common import as_bool, as_int, require_str

_DOMAIN_RULES: list[dict[str, Any]] = [
    {
        "id": "frontend",
        "title": "Frontend Implementer",
        "persona_hint": "前端工程师 / UI 实现",
        "keywords": [
            "frontend",
            "front-end",
            "ui",
            "ux",
            "react",
            "vue",
            "next",
            "页面",
            "前端",
            "前后端",
            "跨前后端",
            "前台",
            "界面",
            "组件",
            "交互",
            "样式",
        ],
        "deliverables": ["UI implementation", "component changes", "interaction polish"],
    },
    {
        "id": "backend",
        "title": "Backend Implementer",
        "persona_hint": "后端工程师 / API 实现",
        "keywords": [
            "backend",
            "back-end",
            "api",
            "server",
            "service",
            "endpoint",
            "database",
            "db",
            "sql",
            "auth",
            "权限",
            "接口",
            "后端",
            "前后端",
            "跨前后端",
            "服务端",
            "数据库",
            "后台",
        ],
        "deliverables": ["service logic", "API changes", "data model updates"],
    },
    {
        "id": "infra",
        "title": "Infra And Integration Implementer",
        "persona_hint": "DevOps / 集成工程师",
        "keywords": [
            "deploy",
            "docker",
            "kubernetes",
            "ci",
            "cd",
            "pipeline",
            "integration",
            "集成",
            "部署",
            "环境",
            "构建",
            "workflow",
        ],
        "deliverables": ["build or deploy changes", "integration wiring", "environment updates"],
    },
    {
        "id": "data",
        "title": "Data Or Analytics Implementer",
        "persona_hint": "数据工程师 / 算法工程师",
        "keywords": [
            "etl",
            "analytics",
            "analysis",
            "report",
            "model",
            "ml",
            "ai",
            "数据",
            "分析",
            "算法",
            "训练",
            "报表",
        ],
        "deliverables": ["data pipeline changes", "analysis logic", "model integration"],
    },
    {
        "id": "game",
        "title": "Game Or Rendering Implementer",
        "persona_hint": "游戏开发 / 渲染工程师",
        "keywords": [
            "game",
            "unity",
            "unreal",
            "shader",
            "render",
            "minecraft",
            "游戏",
            "渲染",
            "着色器",
            "mod",
        ],
        "deliverables": ["gameplay or rendering changes", "runtime behavior", "asset integration"],
    },
]

_COMPLEXITY_KEYWORDS: dict[str, tuple[int, str]] = {
    "multi-module": (12, "Request spans multiple modules"),
    "cross-module": (12, "Cross-module coordination needed"),
    "frontend": (8, "Frontend work is involved"),
    "backend": (8, "Backend work is involved"),
    "database": (8, "Database or persistence work is involved"),
    "integration": (9, "External integration is involved"),
    "api": (6, "API surface changes are involved"),
    "refactor": (10, "Refactor risk is present"),
    "review": (6, "Separate review loop is useful"),
    "test": (6, "Verification planning is needed"),
    "frontend": (8, "Frontend work is involved"),
    "后端": (8, "Backend work is involved"),
    "前端": (8, "Frontend work is involved"),
    "前后端": (12, "Frontend and backend coordination is involved"),
    "跨前后端": (12, "Frontend and backend coordination is involved"),
    "代码审查": (8, "Independent review loop is useful"),
    "评审": (6, "Independent review loop is useful"),
    "闭环": (6, "Closed-loop coordination is expected"),
    "模块": (6, "Module coordination is involved"),
    "复杂": (12, "User describes the task as complex"),
    "多个模块": (12, "User mentions multiple modules"),
    "跨模块": (12, "Cross-module work is explicit"),
    "联调": (10, "Integration debugging likely"),
    "重构": (10, "Refactor risk is present"),
    "审查": (6, "Independent review role is useful"),
    "测试": (6, "Verification planning is needed"),
}

_EXPLICIT_MODE_KEYWORDS = (
    "multi-agent",
    "multi agent",
    "subagent",
    "sub-agent",
    "use agent team",
    "agent team mode",
    "role-based",
    "角色",
    "多个agent",
    "多agent",
    "协作",
    "分工",
)

_WAIT_RULE = "Only wait when blocked on the next critical-path step."
_SMALL_TASK_HINTS = (
    "single file",
    "one file",
    "rename",
    "typo",
    "small task",
    "minor fix",
    "单文件",
    "单个文件",
    "小任务",
    "小改",
    "小修",
)


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).lower()


def _collect_complexity_factors(request_text: str, constraints: list[str]) -> tuple[int, list[str]]:
    haystack = _normalize_text(" ".join([request_text, *constraints]))
    score = 10
    factors: list[str] = []
    for keyword, (weight, message) in _COMPLEXITY_KEYWORDS.items():
        if keyword.lower() in haystack:
            score += weight
            if message not in factors:
                factors.append(message)

    word_count = len([token for token in re.split(r"\s+", haystack) if token])
    if word_count >= 120:
        score += 8
        factors.append("Request contains many requirements")
    elif word_count >= 70:
        score += 4
        factors.append("Request is moderately dense")

    distinct_domains = 0
    for rule in _DOMAIN_RULES:
        if any(keyword.lower() in haystack for keyword in rule["keywords"]):
            distinct_domains += 1
    if distinct_domains >= 3:
        score += 14
        factors.append("Three or more implementation domains are involved")
    elif distinct_domains == 2:
        score += 8
        factors.append("Two implementation domains are involved")

    return min(score, 100), factors


def _select_domain_roles(request_text: str, constraints: list[str], max_workers: int) -> list[dict[str, Any]]:
    haystack = _normalize_text(" ".join([request_text, *constraints]))
    roles: list[dict[str, Any]] = []
    for rule in _DOMAIN_RULES:
        if any(keyword.lower() in haystack for keyword in rule["keywords"]):
            roles.append(
                {
                    "role_id": rule["id"],
                    "title": rule["title"],
                    "persona_hint": rule["persona_hint"],
                    "responsibility": f"Own the {rule['id']} module slice and produce {', '.join(rule['deliverables'])}.",
                    "deliverables": rule["deliverables"],
                }
            )
        if len(roles) >= max_workers:
            break

    if not roles:
        roles.append(
            {
                "role_id": "implementation",
                "title": "Implementation Worker",
                "persona_hint": "通用工程师",
                "responsibility": "Own the main implementation slice and keep changes within an explicit file boundary.",
                "deliverables": ["implementation changes", "local verification notes"],
            }
        )
    return roles


def _normalize_string_list(raw_value: Any, field_name: str) -> list[str]:
    if raw_value is None:
        return []
    if not isinstance(raw_value, list) or any(not isinstance(item, str) for item in raw_value):
        raise ValueError(f"`{field_name}` must be an array of strings")
    return [item.strip() for item in raw_value if item.strip()]


def _resolve_workspace_root(raw_value: Any) -> Path:
    if isinstance(raw_value, str) and raw_value.strip():
        return Path(raw_value).expanduser().resolve()
    return Path.cwd().resolve()


def _build_plan_input(args: dict[str, Any], *, default_user_requested_mode: bool = False) -> dict[str, Any]:
    user_requested_mode = args.get("user_requested_mode")
    return {
        "request": require_str(args, "request"),
        "constraints": _normalize_string_list(args.get("constraints", []), "constraints"),
        "max_agents": as_int(args.get("max_agents"), 5, minimum=2, maximum=8),
        "include_reviewer": as_bool(args.get("include_reviewer"), True),
        "user_requested_mode": as_bool(user_requested_mode, default_user_requested_mode),
        "preferred_roles": _normalize_string_list(args.get("preferred_roles", []), "preferred_roles"),
    }


def _is_small_task(request_text: str, constraints: list[str], worker_roles: list[dict[str, Any]], complexity_score: int) -> bool:
    haystack = _normalize_text(" ".join([request_text, *constraints]))
    word_count = len([token for token in re.split(r"\s+", haystack) if token])
    if any(hint in haystack for hint in _SMALL_TASK_HINTS):
        return True
    return complexity_score < 32 and len(worker_roles) <= 1 and word_count <= 40


def _select_overhead_level(should_use_mode: bool, worker_roles: list[dict[str, Any]], complexity_score: int) -> str:
    if not should_use_mode:
        return "low"
    if len(worker_roles) >= 3 or complexity_score >= 72:
        return "high"
    return "medium"


def _build_parallelizable_tasks(worker_roles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for role in worker_roles:
        role_id = str(role["role_id"])
        tasks.append(
            {
                "task_id": f"worker:{role_id}",
                "role_id": role_id,
                "phase": "implementation",
                "description": f"Own the `{role_id}` module slice in parallel after the shared plan and interfaces are stable.",
                "depends_on": ["shared_plan_ready", "shared_interfaces_ready"],
                "handover_path": f"docs/agent-team/runs/<run_id>/handovers/{_slugify_module_name(role_id)}.md",
            }
        )
    return tasks


def _build_blocking_points(
    worker_roles: list[dict[str, Any]],
    *,
    include_reviewer: bool,
    include_integrator: bool,
) -> list[dict[str, Any]]:
    worker_ids = [str(role["role_id"]) for role in worker_roles]
    points = [
        {
            "id": "shared_plan_ready",
            "description": "Workers wait only until the coordinator locks module boundaries and shared interfaces for the current run.",
            "blocked_roles": worker_ids,
            "depends_on": ["coordinator", "planner"],
        },
        {
            "id": "review_inputs_ready",
            "description": "The reviewer stays idle until at least one worker has a concrete handover or changed-file batch to inspect.",
            "blocked_roles": ["reviewer"] if include_reviewer else [],
            "depends_on": worker_ids,
        },
        {
            "id": "final_convergence",
            "description": "The final integration and user response wait for required worker outputs and any mandatory review findings to converge.",
            "blocked_roles": ["coordinator", "integrator"] if include_integrator else ["coordinator"],
            "depends_on": worker_ids + (["reviewer"] if include_reviewer else []),
        },
    ]
    if not include_reviewer:
        points = [item for item in points if item["id"] != "review_inputs_ready"]
    return points


def _build_wait_strategy() -> dict[str, Any]:
    return {
        "rule": _WAIT_RULE,
        "local_work_while_waiting": [
            "Keep the shared plan, interface notes, and dashboard state current while workers run.",
            "Batch independent worker spawns before the first wait so the main agent does not serialize the team by accident.",
            "Prepare reviewer packets and integration notes locally instead of blocking on sub-agent completion too early.",
        ],
        "avoid": [
            "Do not spawn one sub-agent and immediately wait unless the next critical-path step cannot proceed without that result.",
            "Do not trigger the reviewer before there is real worker output to inspect.",
            "Do not trigger the integrator before multiple worker outputs have actually converged.",
        ],
    }


def _build_review_trigger(include_reviewer: bool) -> dict[str, Any]:
    return {
        "enabled": include_reviewer,
        "when": "Trigger review only after a worker or worker batch produces concrete files, a handover draft, or a commit-ready delta.",
        "skip_when": "Skip review until there is actual output; do not serialize the workflow with an empty early review pass.",
    }


def _build_integration_trigger(include_integrator: bool) -> dict[str, Any]:
    return {
        "enabled": include_integrator,
        "when": "Trigger integration only after multiple worker slices and any required review findings are ready to merge.",
        "skip_when": "Skip integration when there is only one worker slice or when upstream worker outputs are still moving.",
    }


def _build_shared_artifacts() -> list[dict[str, str]]:
    return [
        {
            "path": "docs/agent-team/runs/<run_id>/plan.md",
            "purpose": "Global plan, module boundaries, dependencies, and success criteria.",
        },
        {
            "path": "docs/agent-team/runs/<run_id>/interfaces.md",
            "purpose": "Cross-module contracts, API signatures, shared assumptions, and ownership.",
        },
        {
            "path": "docs/agent-team/runs/<run_id>/review-log.md",
            "purpose": "Reviewer findings by round with severity and status.",
        },
        {
            "path": "docs/agent-team/runs/<run_id>/runtime-agents.md",
            "purpose": "Coordinator-owned mapping between dashboard roles and real spawned runtime agent ids.",
        },
        {
            "path": "docs/agent-team/runs/<run_id>/handovers/<module>.md",
            "purpose": "Per-module handoff notes: inputs, outputs, open questions, and verification.",
        },
        {
            "path": "docs/agent-team/runs/<run_id>/agent-prompts.md",
            "purpose": "Per-role prompt and identity-prefix conventions for all spawned agents.",
        },
        {
            "path": "docs/agent-team/dashboard-state.json",
            "purpose": "Machine-readable pointer to the current run state with role metadata, artifact paths, and auto-open settings.",
        },
        {
            "path": "docs/agent-team/runs/<run_id>/dashboard-events.jsonl",
            "purpose": "Append-only event stream for live output, lifecycle transitions, and plan-step updates.",
        },
        {
            "path": "docs/agent-team/runs/<run_id>/drafts/<role>__<doc>.md",
            "purpose": "Per-role draft documents before the coordinator commits them back to shared Markdown artifacts.",
        },
    ]


def _build_execution_loop(plan: dict[str, Any]) -> list[str]:
    worker_roles = [
        role for role in plan["roles"] if str(role["role_id"]) not in {"planner", "reviewer", "integrator"}
    ]
    include_reviewer = bool(plan["review_trigger"]["enabled"])
    include_integrator = bool(plan["integration_trigger"]["enabled"])
    steps = [
        "The main agent stays in the Coordinator + Integrator seat for the whole run and owns task decomposition, scheduling, blocker routing, review intake, and the final user response.",
        "Do the opening planning pass early, then keep coordination and integration on the main agent instead of collapsing into a pure planner role.",
        _WAIT_RULE,
        "Stabilize the current-run plan and interfaces before worker fan-out, then spawn all independent workers before the first wait.",
    ]
    for role in worker_roles:
        steps.append(
            f"Spawn a worker for `{role['role_id']}` with exclusive ownership of its module slice and require a run-scoped handoff note."
        )
    steps.append("While workers run, keep updating the shared state, prepare reviewer packets, and resolve non-blocking coordination work locally.")
    steps.append("Coordinator relays all cross-agent updates through shared artifacts or explicit send_input messages.")
    if include_reviewer:
        steps.append("Trigger the reviewer only after a worker round or worker batch produces real output, then relay findings back to the owning worker.")
    if include_integrator:
        steps.append("Trigger the integrator only after multiple worker slices converge and any required review findings are ready.")
    steps.append("Do not finish the main agent turn until required worker results are consumed and the final convergence step is complete.")
    return steps


def _recommendation_message(should_use_mode: bool, explicit_request: bool, roles: list[dict[str, Any]]) -> str:
    if explicit_request:
        return "已按你的要求切换到多 agent 分工模式，我会先做角色拆分、共享文档和通信约束，再进入实现。"
    if should_use_mode:
        role_summary = "、".join(role["role_id"] for role in roles[:4])
        return (
            "这个需求已默认进入多 agent 分工模式。"
            f"我会先由 {role_summary} 建立计划、分模块实现并走独立审查，"
            "这样跨模块沟通和回归风险会更可控。"
        )
    return "当前需求用单 agent 直接推进更高效；如果你想要更强的分工和审查闭环，也可以手动启用多 agent 模式。"


def _role_output_prefix(role_id: str) -> str:
    if role_id == "planner":
        return "[planner]"
    if role_id == "reviewer":
        return "[reviewer]"
    if role_id == "integrator":
        return "[integrator]"
    if role_id == "implementation":
        return "[worker:implementation]"
    return f"[worker:{role_id}]"


def _build_role_prompt_templates(roles: list[dict[str, Any]]) -> dict[str, str]:
    templates: dict[str, str] = {}
    for role in roles:
        role_id = str(role["role_id"])
        prefix = _role_output_prefix(role_id)
        title = str(role["title"])
        responsibility = str(role["responsibility"])
        if role_id == "planner":
            body = "\n".join(
                [
                    f"{prefix} You are the {title}.",
                    "Your job is to refine the current request into module boundaries, interfaces, and execution order for the current run only.",
                    "The coordinator keeps long-lived ownership of scheduling, blocker routing, review intake, and final integration.",
                    "Read the shared plan and interfaces docs first. Do not implement production code in this role.",
                    "Every substantial reply must start with your prefix exactly as shown above.",
                    "Output sections: Scope, Module Boundaries, Interface Contracts, Risks, Next Actions.",
                ]
            )
        elif role_id == "reviewer":
            body = "\n".join(
                [
                    f"{prefix} You are the {title}.",
                    "Your job is to review outputs against requirements, integration contracts, regressions, and testing gaps.",
                    "Do not edit code unless the coordinator explicitly asks for a patch review rewrite.",
                    "Every substantial reply must start with your prefix exactly as shown above.",
                    "Output sections: Findings, Severity, Required Fixes, Verification Gaps, Approval Status.",
                ]
            )
        elif role_id == "integrator":
            body = "\n".join(
                [
                    f"{prefix} You are the {title}.",
                    "Your job is to reconcile worker outputs, resolve interface mismatches, and prepare the combined final result.",
                    "Read all handovers and the review log before proposing integration changes.",
                    "Every substantial reply must start with your prefix exactly as shown above.",
                    "Output sections: Integrated Changes, Resolved Conflicts, Remaining Risks, Final Verification.",
                ]
            )
        else:
            body = "\n".join(
                [
                    f"{prefix} You are the {title}.",
                    f"Primary responsibility: {responsibility}",
                    "Own only your assigned module or file boundary. Do not rewrite other workers' areas unless explicitly instructed.",
                    "Every substantial reply must start with your prefix exactly as shown above.",
                    "Output sections: Changes Made, Files Touched, Open Questions, Verification, Handover.",
                ]
            )
        templates[role_id] = body + "\n"
    return templates


def _build_script_lines(
    plan: dict[str, Any],
    workspace_root: str,
    include_bootstrap: bool,
) -> list[str]:
    roles = plan["roles"]
    wait_rule = str(plan["wait_strategy"]["rule"])
    run_docs_root = "docs/agent-team/runs/<run_id>"
    lines = [
        "# Coordinator Flow",
        "",
        f"Main agent role: `{plan['main_agent_role']}`.",
        "Planning is an opening phase responsibility, not the main agent's only job.",
        "",
        f"Rule: `{wait_rule}`",
        "Read structured state first. Use `active_run_id`, `status`, `ready_for_review_role_ids`, `blocked_on`, `depends_on`, and `last_updated_at` from the dashboard state JSON before reading long Markdown.",
        "",
        "1. Call `agent_team_plan` with the user request.",
    ]
    if include_bootstrap:
        lines.append(
            f"2. Call `agent_team_bootstrap` with `workspace_root={workspace_root}` so shared docs and prefix rules exist before worker execution."
        )
        lines.append(
            "3. Read the returned `dashboard.state_path`; bootstrap writes the root state file that points at the current run-scoped docs."
        )
        lines.append(
            "4. If the dashboard closes later, reopen it with `ui_agent_team_dashboard` using that `state_path` value directly. Use `workspace_root` only as a compatibility fallback."
        )
        next_index = 5
    else:
        next_index = 2

    lines.append(
        f"{next_index}. Do the opening decomposition locally, then let the planner refine `{run_docs_root}/plan.md` and `{run_docs_root}/interfaces.md` without handing away coordination."
    )
    idx = next_index + 1
    lines.append(
        f"{idx}. Once shared plan and interfaces are stable, spawn every independent worker in parallel before the first wait."
    )
    idx += 1
    for role in roles:
        role_id = role["role_id"]
        if role_id in {"planner", "reviewer", "integrator"}:
            continue
        lines.append(
            f"{idx}. Spawn `{role_id}` with prefix `{role['output_prefix']}` and exclusive ownership of its module slice plus `{run_docs_root}/handovers/{_slugify_module_name(role_id)}.md`."
        )
        idx += 1
    lines.append(
        f"{idx}. While workers run, prefer one `agent_team_dashboard_sync(action=\"sync_runtime_bridge\", ...)` call per coordination round and pass raw `spawn_results`, `wait_result`, `close_results`, or `notifications` directly so runtime agent bindings and terminal updates reach `{run_docs_root}/dashboard-events.jsonl` with minimal coordinator overhead. Use `build_runtime_bridge_payload` only when you explicitly want to inspect the merged payload first."
    )
    idx += 1
    lines.append(
        f"{idx}. Keep doing local coordination work while sub-agents run: update blockers, prepare review packets, batch follow-up tasks, and collect integration notes."
    )
    idx += 1
    if any(role["role_id"] == "reviewer" for role in roles):
        lines.append(
            f"{idx}. Trigger the reviewer only after a worker round yields concrete output; pass changed files, the handover, and the relevant requirements."
        )
        idx += 1
        lines.append(f"{idx}. Relay reviewer findings back to the owning worker. Repeat until the reviewer approves.")
        idx += 1
    if any(role["role_id"] == "integrator" for role in roles):
        lines.append(
            f"{idx}. Trigger the integrator only after multiple workers converge, then prepare the final user-facing summary."
        )
        idx += 1
    lines.append(
        f"{idx}. Only call `wait` when the next critical-path step is blocked on a sub-agent result; otherwise continue local coordination and convergence work."
    )
    return lines


def _slugify_module_name(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", name.strip()).strip("-").lower()
    return slug or "module"


def _handoff_template(module_id: str, owner_title: str, prefix: str) -> str:
    return "\n".join(
        [
            f"# Handover: {module_id}",
            "",
            f"Identity Prefix: `{prefix}`",
            f"Owner: {owner_title}",
            "Runtime Agent:",
            "- agent_id: ",
            "- agent_name: ",
            "",
            "Task:",
            "",
            "Inputs:",
            "- ",
            "",
            "Outputs:",
            "- ",
            "",
            "Open Questions:",
            "- ",
            "",
            "Verification:",
            "- ",
            "",
            "Status:",
            "- pending",
            "",
            "Latest Message:",
            f"- {prefix} ",
            "",
        ]
    ) + "\n"


def _build_bootstrap_files(
    docs_root: Path,
    roles: list[dict[str, Any]],
    request: str,
    constraints: list[str],
    *,
    run_id: str,
    plan_steps: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    handovers_root = docs_root / "handovers"
    file_map: dict[str, str] = {}

    role_lines: list[str] = []
    for role in roles:
        prefix = _role_output_prefix(str(role["role_id"]))
        role_lines.append(
            f"- `{role['role_id']}`: prefix `{prefix}`; responsibility: {role['responsibility']}"
        )

    checklist_lines = [
        f"- [{'x' if str(step.get('status') or '') in {'approved', 'committed', 'completed', 'done'} else ' '}] {step['label']}"
        for step in (plan_steps or [])
        if isinstance(step, dict) and str(step.get("label") or "").strip()
    ]
    if not checklist_lines:
        checklist_lines = [
            "- [ ] [planner] Finalize module boundaries and shared interfaces.",
            *[
                f"- [ ] {_role_output_prefix(str(role['role_id']))} Complete the `{role['role_id']}` implementation slice."
                for role in roles
                if str(role["role_id"]) != "planner"
            ],
        ]

    file_map[str(docs_root / "plan.md")] = "\n".join(
        [
            "# Agent Team Plan",
            "",
            "## User Request",
            request,
            "",
            "## Constraints",
            *([f"- {item}" for item in constraints] or ["- None provided"]),
            "",
            "## Active Run",
            f"- Run ID: {run_id}",
            f"- Docs Root: {docs_root}",
            "",
            "## Runtime Bridge",
            "- Coordinator records spawned runtime agents in `runtime-agents.md`.",
            "- Prefer one `agent_team_dashboard_sync(action=\"sync_runtime_bridge\", ...)` call per coordination round instead of many small sync calls.",
            "- `sync_runtime_bridge` can carry `bindings`, `spawn_results`, `wait_result`, `close_results`, and raw `notifications` in one payload so finished agents flip to terminal state promptly.",
            "- Use `build_runtime_bridge_payload` only when you need to preview or transform the merged payload before syncing.",
            "",
            "## Roles",
            *role_lines,
            "",
            "## Module Boundaries",
            "- Fill this in after planner output.",
            "",
            "## Success Criteria",
            "- Define before implementation starts.",
            "",
            "## Execution Checklist",
            *checklist_lines,
            "",
        ]
    ) + "\n"

    file_map[str(docs_root / "interfaces.md")] = "\n".join(
        [
            "# Shared Interfaces",
            "",
            "## Rules",
            "- Update this file before workers modify cross-module contracts.",
            "- Every interface note should identify the owning role and the consuming roles.",
            "- Use the identity prefixes from `agent-prompts.md` when logging decisions.",
            "",
            "## Contracts",
            "- Add API signatures, shared types, and ownership notes here.",
            "",
        ]
    ) + "\n"

    file_map[str(docs_root / "review-log.md")] = "\n".join(
        [
            "# Review Log",
            "",
            "## Severity Levels",
            "- Critical",
            "- Important",
            "- Minor",
            "",
            "## Findings Template",
            "- Round:",
            "- Reviewer:",
            "- Prefix:",
            "- Finding:",
            "- Status:",
            "",
        ]
    ) + "\n"

    file_map[str(docs_root / "runtime-agents.md")] = "\n".join(
        [
            "# Runtime Agent Bindings",
            "",
            "Coordinator updates this file after each `spawn_agent` call and whenever a runtime agent is rebound.",
            "",
            "## Workflow",
            "- Prefer `agent_team_dashboard_sync(action=\"sync_runtime_bridge\", ...)` directly from `spawn_agent`, `wait`, and `close_agent` shaped results so bindings and notifications land in the dashboard in one step.",
            "- If you only need one half, you can still use `bind_agent` or `ingest_agent_notification` directly.",
            "- Keep this file aligned with the dashboard state so the UI and the shared docs describe the same runtime mapping.",
            "",
            "## Bindings",
            *[
                f"- `{role['role_id']}` -> agent_id: ; agent_name: ; prefix: `{_role_output_prefix(str(role['role_id']))}`"
                for role in roles
            ],
            "",
        ]
    ) + "\n"

    prompt_lines = [
        "# Agent Prompt Conventions",
        "",
        "Every spawned agent must prefix each major update, handoff entry, and summary with its assigned identity prefix.",
        "",
        "## Prefixes",
    ]
    for role in roles:
        prompt_lines.append(f"- `{role['role_id']}` -> `{_role_output_prefix(str(role['role_id']))}`")
    prompt_lines.extend(
        [
            "",
            "## Required Output Shape",
            "- Start every substantial message with the assigned prefix.",
            "- Keep the prefix stable for the whole task.",
            "- Use the same prefix in handoff documents and review logs.",
            "",
            "## Runtime Binding",
            "- The coordinator must bind the real runtime `agent_id` to your dashboard role before expecting live dashboard updates.",
            "- Prefer `agent_team_dashboard_sync(action=\"sync_runtime_bridge\", ...)` with raw spawn, wait, and close results; use `build_runtime_bridge_payload` only for inspection or staged batching.",
            "- If batching is not practical, fall back to `bind_agent` and `ingest_agent_notification` separately.",
            "",
            "## Example",
            "- `[worker:frontend] Updated the settings panel and documented the new props in interfaces.md.`",
            "- `[reviewer] Important: frontend and backend disagree on the payload field name.`",
            "",
        ]
    )
    file_map[str(docs_root / "agent-prompts.md")] = "\n".join(prompt_lines) + "\n"

    for role in roles:
        role_id = str(role["role_id"])
        if role_id in {"planner", "reviewer", "integrator"}:
            module_id = role_id
        else:
            module_id = role_id
        file_map[str(handovers_root / f"{_slugify_module_name(module_id)}.md")] = _handoff_template(
            module_id=module_id,
            owner_title=str(role["title"]),
            prefix=_role_output_prefix(role_id),
        )

    return file_map


def _build_agent_team_plan_model_from_input(plan_input: dict[str, Any]) -> dict[str, Any]:
    request = str(plan_input["request"])
    constraints = list(plan_input["constraints"])
    max_agents = int(plan_input["max_agents"])
    include_reviewer_requested = bool(plan_input["include_reviewer"])
    preferred_roles = list(plan_input["preferred_roles"])

    normalized_request = _normalize_text(request)
    explicit_request = bool(plan_input["user_requested_mode"]) or any(
        keyword in normalized_request for keyword in _EXPLICIT_MODE_KEYWORDS
    )
    complexity_score, complexity_factors = _collect_complexity_factors(request, constraints)

    max_workers = max(1, max_agents - (2 if include_reviewer_requested else 1))
    worker_roles = _select_domain_roles(request, constraints, max_workers=max_workers)
    small_task = _is_small_task(request, constraints, worker_roles, complexity_score)
    should_use_mode = explicit_request or (complexity_score >= 45 and not small_task)
    if explicit_request:
        activation_reason = "user_requested"
    elif should_use_mode:
        activation_reason = "complexity_suggested"
    elif small_task:
        activation_reason = "small_task_single_agent"
    else:
        activation_reason = "not_needed"

    include_reviewer = include_reviewer_requested and (explicit_request or len(worker_roles) > 1 or complexity_score >= 52)
    include_integrator = len(worker_roles) > 1
    orchestration_overhead_level = _select_overhead_level(should_use_mode, worker_roles, complexity_score)

    planner_role = {
        "role_id": "planner",
        "title": "Planning Specialist",
        "persona_hint": "产品经理 / 架构师",
        "responsibility": "Refine module boundaries, shared interfaces, and execution order for the current run while the coordinator retains orchestration ownership.",
        "deliverables": ["task decomposition", "module boundaries", "shared contracts"],
    }
    reviewer_role = {
        "role_id": "reviewer",
        "title": "Independent Reviewer",
        "persona_hint": "代码审查 / QA",
        "responsibility": "Review outputs against requirements, integration contracts, regressions, and verification gaps.",
        "deliverables": ["review findings", "risk summary", "go/no-go recommendation"],
    }
    integrator_role = {
        "role_id": "integrator",
        "title": "Integrator",
        "persona_hint": "技术负责人 / 集成负责人",
        "responsibility": "Merge worker outputs, resolve interface mismatches, and prepare the final user-facing result.",
        "deliverables": ["integration summary", "final verification notes", "final response outline"],
    }

    roles: list[dict[str, Any]] = [planner_role, *worker_roles]
    if include_reviewer:
        roles.append(reviewer_role)
    if include_integrator:
        roles.append(integrator_role)

    for role in roles:
        role["output_prefix"] = _role_output_prefix(str(role["role_id"]))
        if preferred_roles:
            role["preferred_by_user"] = role["role_id"] in preferred_roles or role["title"] in preferred_roles

    recommendation_message = _recommendation_message(should_use_mode, explicit_request, roles)
    parallelizable_tasks = _build_parallelizable_tasks(worker_roles)
    blocking_points = _build_blocking_points(
        worker_roles,
        include_reviewer=include_reviewer,
        include_integrator=include_integrator,
    )
    wait_strategy = _build_wait_strategy()
    review_trigger = _build_review_trigger(include_reviewer)
    integration_trigger = _build_integration_trigger(include_integrator)

    return {
        "should_use_mode": should_use_mode,
        "activation_reason": activation_reason,
        "complexity_score": complexity_score,
        "complexity_factors": complexity_factors,
        "summary": "Role-based multi-agent orchestration plan for the current request.",
        "recommended_user_message": recommendation_message,
        "main_agent_role": "Coordinator + Integrator",
        "parallelizable_tasks": parallelizable_tasks,
        "blocking_points": blocking_points,
        "wait_strategy": wait_strategy,
        "review_trigger": review_trigger,
        "integration_trigger": integration_trigger,
        "orchestration_overhead_level": orchestration_overhead_level,
        "roles": roles,
        "shared_artifacts": _build_shared_artifacts(),
        "communication_model": {
            "topology": "hub_and_spoke",
            "rule": "Workers and reviewer do not communicate directly. The coordinator relays messages or uses shared artifacts as the single source of truth.",
            "identity_prefix_rule": "Each agent must prefix major updates and handoff entries with its assigned identity prefix.",
            "message_schema": [
                "Task",
                "Owner",
                "Identity Prefix",
                "Inputs",
                "Outputs",
                "Open Questions",
                "Verification",
                "Status",
            ],
        },
        "execution_loop": _build_execution_loop(
            {
                "roles": roles,
                "review_trigger": review_trigger,
                "integration_trigger": integration_trigger,
            }
        ),
        "spawn_order": [role["role_id"] for role in roles],
        "notes": [
            "Prefer one agent per independent module or responsibility.",
            "Keep write ownership disjoint to reduce merge conflicts.",
            "Use reviewer findings as structured input for the next worker round.",
            _WAIT_RULE,
            "When this mode is selected, bootstrap shared artifacts and the dashboard before workers begin execution.",
        ],
    }


def _build_agent_team_plan_model(
    args: dict[str, Any],
    *,
    default_user_requested_mode: bool = False,
) -> dict[str, Any]:
    plan_input = _build_plan_input(args, default_user_requested_mode=default_user_requested_mode)
    return _build_agent_team_plan_model_from_input(plan_input)


def _build_agent_team_context(
    args: dict[str, Any],
    *,
    default_user_requested_mode: bool = False,
) -> dict[str, Any]:
    plan_input = _build_plan_input(args, default_user_requested_mode=default_user_requested_mode)
    workspace_root = _resolve_workspace_root(args.get("workspace_root"))
    plan = _build_agent_team_plan_model_from_input(plan_input)
    return {
        "request": str(plan_input["request"]),
        "constraints": list(plan_input["constraints"]),
        "workspace_root": workspace_root,
        "plan_input": plan_input,
        "plan": plan,
        "orchestration": _build_orchestration_state(plan),
        "dashboard_contract": _build_dashboard_contract(workspace_root),
    }


def _current_run_id() -> str:
    return f"run-{time.time_ns()}"


def _select_run_docs_root(workspace_root: Path, run_id: str) -> Path:
    return workspace_root / "docs" / "agent-team" / "runs" / run_id


def _build_orchestration_state(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "main_agent_role": plan["main_agent_role"],
        "parallelizable_tasks": plan["parallelizable_tasks"],
        "blocking_points": plan["blocking_points"],
        "wait_strategy": plan["wait_strategy"],
        "review_trigger": plan["review_trigger"],
        "integration_trigger": plan["integration_trigger"],
        "orchestration_overhead_level": plan["orchestration_overhead_level"],
    }


def _build_dashboard_contract(workspace_root: Path) -> dict[str, Any]:
    resolved_workspace_root = Path(workspace_root).expanduser().resolve()
    return {
        "preferred_reopen_arg": "state_path",
        "compatible_reopen_arg": "workspace_root",
        "shared_fact_root": "docs/agent-team",
        "state_path": str(default_dashboard_state_path(resolved_workspace_root)),
        "state_path_pattern": "docs/agent-team/dashboard-state.json",
        "run_docs_root_pattern": "docs/agent-team/runs/<run_id>",
        "reopen_tool": "ui_agent_team_dashboard",
        "tool_entry": "toolmodules.extensions.ui_tools.tool_ui_agent_team_dashboard",
        "gui_module": "toolmodules.extensions.agent_team_dashboard",
    }


def _build_structured_plan_steps(plan: dict[str, Any]) -> list[dict[str, str]]:
    steps: list[dict[str, str]] = [
        {
            "id": "shared-plan-ready",
            "label": "Coordinator keeps ownership while the planner finalizes shared plan and interfaces.",
            "status": "pending",
            "owner_role_id": "planner",
            "source": "bootstrap",
        }
    ]
    for task in plan["parallelizable_tasks"]:
        steps.append(
            {
                "id": str(task["task_id"]),
                "label": str(task["description"]),
                "status": "pending",
                "owner_role_id": str(task["role_id"]),
                "source": "bootstrap",
            }
        )
    if bool(plan["review_trigger"]["enabled"]):
        steps.append(
            {
                "id": "review-ready-output",
                "label": "Reviewer inspects concrete worker output after a real handover or file delta exists.",
                "status": "pending",
                "owner_role_id": "reviewer",
                "source": "bootstrap",
            }
        )
    if bool(plan["integration_trigger"]["enabled"]):
        steps.append(
            {
                "id": "integration-convergence",
                "label": "Integrator converges multi-worker output after required review findings are resolved.",
                "status": "pending",
                "owner_role_id": "integrator",
                "source": "bootstrap",
            }
        )
    return steps


def tool_agent_team_plan(args: dict[str, Any]) -> dict[str, Any]:
    return _build_agent_team_context(args)["plan"]


def tool_agent_team_script(args: dict[str, Any]) -> dict[str, Any]:
    context = _build_agent_team_context(args, default_user_requested_mode=True)
    include_bootstrap = as_bool(args.get("include_bootstrap"), True)
    workspace_root = str(context["workspace_root"])
    plan = context["plan"]
    prompt_templates = _build_role_prompt_templates(plan["roles"])
    script_lines = _build_script_lines(plan, workspace_root=workspace_root, include_bootstrap=include_bootstrap)

    coordinator_script = "\n".join(script_lines) + "\n"
    send_input_templates: dict[str, str] = {}
    for role in plan["roles"]:
        role_id = str(role["role_id"])
        prefix = str(role["output_prefix"])
        send_input_templates[role_id] = (
            f"{prefix} Follow your assigned responsibility.\n"
            f"Prefix every substantial update with `{prefix}`.\n"
            "Read the shared docs first, then report changes, open questions, and verification.\n"
            "Prefer `agent_team_dashboard_sync(action=\"build_runtime_bridge_payload\", ...)` followed by `agent_team_dashboard_sync(action=\"sync_runtime_bridge\", ...)` once per round so the dashboard updates with minimal coordinator overhead.\n"
        )

    return {
        "plan": plan,
        "orchestration": context["orchestration"],
        "dashboard_contract": context["dashboard_contract"],
        "coordinator_script": coordinator_script,
        "role_prompt_templates": prompt_templates,
        "send_input_templates": send_input_templates,
        "notes": [
            "Use the planner template before any worker starts writing code.",
            "Keep worker write scopes disjoint.",
            "Always relay reviewer findings through the coordinator.",
        ],
    }


def tool_agent_team_bootstrap(args: dict[str, Any]) -> dict[str, Any]:
    context = _build_agent_team_context(args, default_user_requested_mode=True)
    request = context["request"]
    workspace_root = context["workspace_root"]
    overwrite = as_bool(args.get("overwrite"), False)
    open_dashboard = as_bool(args.get("open_dashboard"), True)
    dashboard_title = str(args.get("dashboard_title", DEFAULT_WINDOW_TITLE)).strip() or DEFAULT_WINDOW_TITLE
    python_executable = str(args.get("python_executable") or "").strip() or None
    dashboard_topmost = as_bool(args.get("dashboard_topmost"), True)
    dashboard_bring_to_front = as_bool(args.get("dashboard_bring_to_front"), True)
    poll_interval_ms = as_int(args.get("poll_interval_ms"), 1200, minimum=250, maximum=60000)

    plan = context["plan"]
    constraints = context["constraints"]
    plan_steps = _build_structured_plan_steps(plan)
    run_id = _current_run_id()
    run_docs_root = _select_run_docs_root(workspace_root, run_id)
    files = _build_bootstrap_files(
        run_docs_root,
        plan["roles"],
        request,
        constraints,
        run_id=run_id,
        plan_steps=plan_steps,
    )

    written_files: list[str] = []
    skipped_files: list[str] = []
    for raw_path, content in files.items():
        path = Path(raw_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not overwrite:
            skipped_files.append(str(path))
            continue
        path.write_text(content, encoding="utf-8")
        written_files.append(str(path))

    dashboard_state = build_dashboard_state(
        workspace_root=workspace_root,
        roles=plan["roles"],
        request=request,
        constraints=constraints,
        auto_open=open_dashboard,
        title=dashboard_title,
        poll_interval_ms=poll_interval_ms,
        docs_root=run_docs_root,
        active_run_id=run_id,
        orchestration=context["orchestration"],
        plan_steps=_build_structured_plan_steps(plan),
    )
    state_path = default_dashboard_state_path(workspace_root)
    write_dashboard_state(state_path, dashboard_state)
    runtime_init = initialize_dashboard_runtime_files(state_path)
    if str(state_path) not in written_files:
        written_files.append(str(state_path))
    if runtime_init["events_path"] not in written_files:
        written_files.append(runtime_init["events_path"])
    for created_draft in runtime_init["created_drafts"]:
        if created_draft not in written_files:
            written_files.append(created_draft)

    if open_dashboard:
        dashboard = launch_dashboard_process(
            state_path,
            python_executable=python_executable,
            title=dashboard_title,
            topmost=dashboard_topmost,
            bring_to_front=dashboard_bring_to_front,
            poll_interval_ms=poll_interval_ms,
        )
        dashboard["active_run_id"] = run_id
        dashboard["docs_root"] = str(run_docs_root)
        if not bool(dashboard.get("launched")):
            error = str(dashboard.get("error") or "").strip() or "unknown dashboard launch failure"
            raise RuntimeError(f"agent team bootstrap failed because the dashboard GUI did not launch: {error}")
    else:
        dashboard = {
            "state_path": str(state_path),
            "launched": False,
            "pid": None,
            "error": "",
            "active_run_id": run_id,
            "docs_root": str(run_docs_root),
        }

    dashboard_contract = dict(context["dashboard_contract"])
    dashboard_contract["state_path"] = str(state_path)
    dashboard_contract["active_run_id"] = run_id
    dashboard_contract["docs_root"] = str(run_docs_root)

    return {
        "workspace_root": str(workspace_root),
        "run_id": run_id,
        "docs_root": str(run_docs_root),
        "plan": plan,
        "orchestration": context["orchestration"],
        "dashboard_contract": dashboard_contract,
        "written_files": written_files,
        "skipped_files": skipped_files,
        "dashboard": dashboard,
        "runtime_files": runtime_init,
        "identity_prefixes": {role["role_id"]: role["output_prefix"] for role in plan["roles"]},
        "message": "Generated shared agent-team templates and identity-prefix conventions.",
    }


def tool_agent_team_dashboard_sync(args: dict[str, Any]) -> dict[str, Any]:
    state_path = resolve_dashboard_state_path(
        state_path=args.get("state_path"),
        workspace_root=args.get("workspace_root"),
    )
    if not state_path.exists():
        raise ValueError(f"dashboard state file does not exist: {state_path}")

    action = require_str(args, "action").strip()
    if action == "append_event":
        event = args.get("event")
        if not isinstance(event, dict):
            raise ValueError("`event` must be an object when action=`append_event`")
        result = append_dashboard_event(state_path, event)
    elif action == "set_status":
        result = set_role_status(
            state_path,
            require_str(args, "role_id"),
            require_str(args, "status"),
            str(args.get("message") or ""),
            ready_for_review=args.get("ready_for_review"),
            blocked_on=args.get("blocked_on"),
            depends_on=args.get("depends_on"),
        )
    elif action == "stream_chunk":
        result = append_role_stream_chunk(
            state_path,
            require_str(args, "role_id"),
            require_str(args, "content"),
            document_key=str(args.get("document_key") or "handover"),
            message=str(args.get("message") or ""),
        )
    elif action == "write_draft":
        result = write_role_document_draft(
            state_path,
            require_str(args, "role_id"),
            str(args.get("document_key") or "handover"),
            require_str(args, "content"),
            message=str(args.get("message") or ""),
        )
    elif action == "commit_draft":
        result = commit_role_document_draft(
            state_path,
            require_str(args, "role_id"),
            str(args.get("document_key") or "handover"),
            status=str(args.get("status") or "committed"),
            message=str(args.get("message") or ""),
            ready_for_review=args.get("ready_for_review"),
            blocked_on=args.get("blocked_on"),
            depends_on=args.get("depends_on"),
        )
    elif action == "plan_step":
        result = upsert_plan_step(
            state_path,
            require_str(args, "step_id"),
            require_str(args, "title"),
            require_str(args, "status"),
            owner_role_id=str(args.get("role_id") or ""),
        )
    elif action == "bind_agent":
        result = bind_role_runtime_agent(
            state_path,
            require_str(args, "role_id"),
            require_str(args, "agent_id"),
            agent_name=str(args.get("agent_name") or ""),
            status=str(args.get("status") or "active"),
            message=str(args.get("message") or ""),
        )
    elif action == "ingest_agent_notification":
        notification = args.get("notification")
        if not isinstance(notification, dict):
            raise ValueError("`notification` must be an object when action=`ingest_agent_notification`")
        result = ingest_runtime_agent_notification(
            state_path,
            notification,
            role_id=str(args.get("role_id") or ""),
            document_key=str(args.get("document_key") or "handover"),
            auto_commit=as_bool(args.get("auto_commit"), True),
            update_plan=as_bool(args.get("update_plan"), True),
        )
    elif action == "sync_runtime_bridge":
        bindings = args.get("bindings")
        notifications = args.get("notifications")
        spawn_results = args.get("spawn_results")
        wait_result = args.get("wait_result")
        close_results = args.get("close_results")
        if bindings is not None and (not isinstance(bindings, list) or any(not isinstance(item, dict) for item in bindings)):
            raise ValueError("`bindings` must be an array of objects when action=`sync_runtime_bridge`")
        if notifications is not None and (not isinstance(notifications, list) or any(not isinstance(item, dict) for item in notifications)):
            raise ValueError("`notifications` must be an array of objects when action=`sync_runtime_bridge`")
        if spawn_results is not None and (not isinstance(spawn_results, list) or any(not isinstance(item, dict) for item in spawn_results)):
            raise ValueError("`spawn_results` must be an array of objects when action=`sync_runtime_bridge`")
        if wait_result is not None and not isinstance(wait_result, dict):
            raise ValueError("`wait_result` must be an object when action=`sync_runtime_bridge`")
        if close_results is not None and (not isinstance(close_results, list) or any(not isinstance(item, dict) for item in close_results)):
            raise ValueError("`close_results` must be an array of objects when action=`sync_runtime_bridge`")
        result = sync_runtime_agent_bridge(
            state_path,
            bindings=bindings if isinstance(bindings, list) else None,
            notifications=notifications if isinstance(notifications, list) else None,
            spawn_results=spawn_results if isinstance(spawn_results, list) else None,
            wait_result=wait_result if isinstance(wait_result, dict) else None,
            close_results=close_results if isinstance(close_results, list) else None,
            auto_commit=as_bool(args.get("auto_commit"), True),
            update_plan=as_bool(args.get("update_plan"), True),
        )
    elif action == "build_runtime_bridge_payload":
        bindings = args.get("bindings")
        spawn_results = args.get("spawn_results")
        notifications = args.get("notifications")
        wait_result = args.get("wait_result")
        close_results = args.get("close_results")
        if bindings is not None and (not isinstance(bindings, list) or any(not isinstance(item, dict) for item in bindings)):
            raise ValueError("`bindings` must be an array of objects when action=`build_runtime_bridge_payload`")
        if spawn_results is not None and (not isinstance(spawn_results, list) or any(not isinstance(item, dict) for item in spawn_results)):
            raise ValueError("`spawn_results` must be an array of objects when action=`build_runtime_bridge_payload`")
        if notifications is not None and (not isinstance(notifications, list) or any(not isinstance(item, dict) for item in notifications)):
            raise ValueError("`notifications` must be an array of objects when action=`build_runtime_bridge_payload`")
        if wait_result is not None and not isinstance(wait_result, dict):
            raise ValueError("`wait_result` must be an object when action=`build_runtime_bridge_payload`")
        if close_results is not None and (not isinstance(close_results, list) or any(not isinstance(item, dict) for item in close_results)):
            raise ValueError("`close_results` must be an array of objects when action=`build_runtime_bridge_payload`")
        result = build_runtime_bridge_payload(
            bindings=bindings if isinstance(bindings, list) else None,
            spawn_results=spawn_results if isinstance(spawn_results, list) else None,
            notifications=notifications if isinstance(notifications, list) else None,
            wait_result=wait_result if isinstance(wait_result, dict) else None,
            close_results=close_results if isinstance(close_results, list) else None,
        )
    elif action == "complete_run":
        result = mark_run_completed(state_path, str(args.get("message") or ""))
    else:
        raise ValueError(f"unsupported dashboard sync action: {action}")
    return {
        "type": "agent_team_dashboard_sync",
        "action": action,
        "state_path": str(state_path),
        "result": result,
    }


def get_agent_orchestration_tooling() -> tuple[dict[str, Any], dict[str, str], dict[str, dict[str, Any]]]:
    handlers = {
        "agent_team_plan": tool_agent_team_plan,
        "agent_team_bootstrap": tool_agent_team_bootstrap,
        "agent_team_script": tool_agent_team_script,
        "agent_team_dashboard_sync": tool_agent_team_dashboard_sync,
    }
    descriptions = {
        "agent_team_plan": "Analyze a request, decide whether role-based multi-agent mode is warranted, and return a structured orchestration plan with roles, shared artifacts, and communication rules.",
        "agent_team_bootstrap": "Generate shared agent-team docs, handoff templates, and identity-prefix conventions for a role-based multi-agent workflow.",
        "agent_team_script": "Generate coordinator instructions plus reusable planner, worker, reviewer, and integrator prompt templates for the role-based multi-agent workflow.",
        "agent_team_dashboard_sync": "Append live dashboard events, update lifecycle state, manage drafts, and commit shared Markdown artifacts for an agent-team run.",
    }
    schemas = {
        "agent_team_plan": {
            "type": "object",
            "properties": {
                "request": {"type": "string"},
                "constraints": {"type": "array", "items": {"type": "string"}},
                "max_agents": {"type": "integer", "minimum": 2, "maximum": 8, "default": 5},
                "include_reviewer": {"type": "boolean", "default": True},
                "user_requested_mode": {"type": "boolean", "default": False},
                "preferred_roles": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["request"],
            "additionalProperties": False,
        },
        "agent_team_bootstrap": {
            "type": "object",
            "properties": {
                "request": {"type": "string"},
                "constraints": {"type": "array", "items": {"type": "string"}},
                "workspace_root": {"type": "string"},
                "max_agents": {"type": "integer", "minimum": 2, "maximum": 8, "default": 5},
                "include_reviewer": {"type": "boolean", "default": True},
                "user_requested_mode": {"type": "boolean", "default": True},
                "preferred_roles": {"type": "array", "items": {"type": "string"}},
                "overwrite": {"type": "boolean", "default": False},
                "open_dashboard": {"type": "boolean", "default": True},
                "dashboard_title": {"type": "string", "default": DEFAULT_WINDOW_TITLE},
                "python_executable": {"type": "string"},
                "dashboard_topmost": {"type": "boolean", "default": True},
                "dashboard_bring_to_front": {"type": "boolean", "default": True},
                "poll_interval_ms": {"type": "integer", "minimum": 250, "maximum": 60000, "default": 1200},
            },
            "required": ["request"],
            "additionalProperties": False,
        },
        "agent_team_script": {
            "type": "object",
            "properties": {
                "request": {"type": "string"},
                "constraints": {"type": "array", "items": {"type": "string"}},
                "workspace_root": {"type": "string"},
                "max_agents": {"type": "integer", "minimum": 2, "maximum": 8, "default": 5},
                "include_reviewer": {"type": "boolean", "default": True},
                "user_requested_mode": {"type": "boolean", "default": True},
                "preferred_roles": {"type": "array", "items": {"type": "string"}},
                "include_bootstrap": {"type": "boolean", "default": True},
            },
            "required": ["request"],
            "additionalProperties": False,
        },
        "agent_team_dashboard_sync": {
            "type": "object",
            "properties": {
                "workspace_root": {"type": "string"},
                "state_path": {"type": "string"},
                "action": {
                    "type": "string",
                    "enum": ["append_event", "set_status", "stream_chunk", "write_draft", "commit_draft", "plan_step", "bind_agent", "ingest_agent_notification", "sync_runtime_bridge", "build_runtime_bridge_payload", "complete_run"],
                },
                "event": {"type": "object"},
                "notification": {"type": "object"},
                "bindings": {"type": "array", "items": {"type": "object"}},
                "notifications": {"type": "array", "items": {"type": "object"}},
                "spawn_results": {"type": "array", "items": {"type": "object"}},
                "wait_result": {"type": "object"},
                "close_results": {"type": "array", "items": {"type": "object"}},
                "role_id": {"type": "string"},
                "agent_id": {"type": "string"},
                "agent_name": {"type": "string"},
                "status": {"type": "string"},
                "message": {"type": "string"},
                "document_key": {"type": "string"},
                "content": {"type": "string"},
                "step_id": {"type": "string"},
                "title": {"type": "string"},
                "auto_commit": {"type": "boolean"},
                "update_plan": {"type": "boolean"},
                "ready_for_review": {"type": "boolean"},
                "blocked_on": {"type": "array", "items": {"type": "string"}},
                "depends_on": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    }
    return handlers, descriptions, schemas
