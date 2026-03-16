from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .agent_team_dashboard_runtime import (
    DEFAULT_DASHBOARD_TITLE,
    DEFAULT_POLL_INTERVAL_MS,
    DEFAULT_WINDOW_TITLE,
    _THEME,
    append_dashboard_event,
    append_agent_stream_chunk,
    append_role_stream_chunk,
    bind_role_runtime_agent,
    build_dashboard_process_command,
    build_dashboard_state,
    build_runtime_bridge_payload,
    collect_dashboard_snapshot,
    commit_agent_draft,
    commit_role_document_draft,
    dashboard_state_path,
    default_dashboard_drafts_dir,
    default_dashboard_events_path,
    default_dashboard_state_path,
    ensure_dashboard_state_exists,
    initialize_dashboard_runtime_files,
    infer_workspace_root_from_dashboard_state_path,
    sync_runtime_agent_bridge,
    ingest_runtime_agent_notification,
    launch_dashboard_process,
    load_dashboard_events,
    load_dashboard_state,
    mark_run_completed,
    read_dashboard_events_since,
    record_agent_status,
    resolve_dashboard_state_path,
    replace_agent_draft,
    set_role_status,
    spawn_dashboard_process,
    upsert_plan_step,
    write_dashboard_state,
    write_role_document_draft,
)


def _parse_cli_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Open the detached agent-team dashboard window.")
    parser.add_argument("--state-path", required=True)
    parser.add_argument("--title", default="")
    parser.add_argument("--topmost", default="1")
    parser.add_argument("--bring-to-front", default="1")
    parser.add_argument("--poll-interval-ms", type=int, default=None)
    parser.add_argument("--backend", default="qt")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_cli_args(argv or sys.argv[1:])
    state_path = Path(args.state_path).expanduser().resolve()
    ensure_dashboard_state_exists(
        state_path,
        workspace_root=infer_workspace_root_from_dashboard_state_path(state_path),
        title=args.title,
        poll_interval_ms=args.poll_interval_ms,
        auto_open=False,
    )
    initialize_dashboard_runtime_files(state_path)
    state = load_dashboard_state(state_path)
    backend = str(args.backend or (state.get("runtime") or {}).get("gui_backend") or "qt").strip().lower()
    if backend != "qt":
        raise RuntimeError(f"Unsupported dashboard backend: {backend}")
    from .agent_team_dashboard_qt import run_dashboard_qt

    return run_dashboard_qt(
        state_path,
        title=args.title,
        topmost=str(args.topmost).strip() != "0",
        bring_to_front=str(args.bring_to_front).strip() != "0",
        poll_interval_ms=args.poll_interval_ms,
    )


if __name__ == "__main__":
    raise SystemExit(main())
