from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .agent_team_dashboard import DEFAULT_POLL_INTERVAL_MS, ensure_dashboard_state_exists, launch_dashboard_process, resolve_dashboard_state_path
from .common import as_bool, as_int, install_package_with_pip, require_str

_TK_ENV_LOCK = threading.RLock()
_TK_ENV_PREPARED = False


def _first_existing_dir(candidates: list[Path]) -> Path | None:
    for candidate in candidates:
        try:
            if candidate.exists() and candidate.is_dir():
                return candidate.resolve()
        except Exception:
            continue
    return None


def _discover_tk_dirs() -> tuple[Path | None, Path | None]:
    roots: list[Path] = []
    for raw in (Path(sys.base_prefix), Path(sys.executable).resolve().parent.parent):
        try:
            resolved = raw.resolve()
        except Exception:
            continue
        if resolved not in roots:
            roots.append(resolved)

    tcl_candidates: list[Path] = []
    tk_candidates: list[Path] = []
    for root in roots:
        tcl_candidates.extend([root / "tcl" / "tcl8.6", root / "Lib" / "tcl8.6", root / "lib" / "tcl8.6"])
        tk_candidates.extend([root / "tcl" / "tk8.6", root / "Lib" / "tk8.6", root / "lib" / "tk8.6"])
        tcl_base = root / "tcl"
        try:
            if tcl_base.exists() and tcl_base.is_dir():
                for child in tcl_base.iterdir():
                    if not child.is_dir():
                        continue
                    lower = child.name.lower()
                    if lower.startswith("tcl8.") and child not in tcl_candidates:
                        tcl_candidates.append(child)
                    if lower.startswith("tk8.") and child not in tk_candidates:
                        tk_candidates.append(child)
        except Exception:
            pass

    return _first_existing_dir(tcl_candidates), _first_existing_dir(tk_candidates)


def _prepare_tkinter_env() -> None:
    global _TK_ENV_PREPARED

    with _TK_ENV_LOCK:
        if _TK_ENV_PREPARED:
            return

        tcl_env = os.environ.get("TCL_LIBRARY", "").strip()
        tk_env = os.environ.get("TK_LIBRARY", "").strip()

        tcl_ok = Path(tcl_env).is_dir() if tcl_env else False
        tk_ok = Path(tk_env).is_dir() if tk_env else False

        if not tcl_ok or not tk_ok:
            tcl_dir, tk_dir = _discover_tk_dirs()
            if not tcl_ok and tcl_dir is not None:
                os.environ["TCL_LIBRARY"] = str(tcl_dir)
                tcl_ok = True
            if not tk_ok and tk_dir is not None:
                os.environ["TK_LIBRARY"] = str(tk_dir)
                tk_ok = True

        _TK_ENV_PREPARED = tcl_ok and tk_ok


def _normalize_number_list(values: Any, key: str) -> list[float]:
    if not isinstance(values, list) or not values:
        raise ValueError(f"`{key}` must be a non-empty array")

    normalized: list[float] = []
    for index, item in enumerate(values):
        if isinstance(item, bool):
            raise ValueError(f"`{key}[{index}]` must be a number")
        try:
            normalized.append(float(item))
        except Exception as e:
            raise ValueError(f"`{key}[{index}]` must be a number") from e

    return normalized


def tool_ui_prompt(args: dict[str, Any]) -> dict[str, Any]:
    prompt = require_str(args, "prompt")
    options = args.get("options", [])
    if options is not None and not isinstance(options, list):
        raise ValueError("`options` must be an array")

    normalized_options: list[dict[str, str]] = []
    for index, option in enumerate(options, start=1):
        if isinstance(option, str) and option.strip():
            normalized_options.append({"id": str(index), "label": option.strip()})
        elif isinstance(option, dict):
            label = str(option.get("label", "")).strip()
            if not label:
                continue
            normalized_options.append({"id": str(option.get("id", index)), "label": label})

    return {
        "type": "prompt",
        "prompt": prompt,
        "options": normalized_options,
        "allow_free_text": as_bool(args.get("allow_free_text"), True),
        "default": args.get("default"),
    }


def tool_ui_table(args: dict[str, Any]) -> dict[str, Any]:
    columns = args.get("columns")
    rows = args.get("rows")
    if not isinstance(columns, list) or not columns:
        raise ValueError("`columns` must be a non-empty array")
    if not isinstance(rows, list):
        raise ValueError("`rows` must be an array")

    limit = as_int(args.get("limit"), len(rows) if rows else 0, minimum=0)
    sort_by = args.get("sort_by")
    descending = as_bool(args.get("descending"), False)

    normalized_columns = [str(col) for col in columns]
    normalized_rows: list[dict[str, Any]] = []

    for row in rows:
        if isinstance(row, dict):
            normalized_rows.append({key: row.get(key) for key in normalized_columns})
        elif isinstance(row, list):
            mapped: dict[str, Any] = {}
            for index, col in enumerate(normalized_columns):
                mapped[col] = row[index] if index < len(row) else None
            normalized_rows.append(mapped)

    if isinstance(sort_by, str) and sort_by in normalized_columns:
        normalized_rows.sort(key=lambda item: str(item.get(sort_by, "")), reverse=descending)

    if limit > 0:
        normalized_rows = normalized_rows[:limit]

    return {
        "type": "table",
        "columns": normalized_columns,
        "row_count": len(normalized_rows),
        "rows": normalized_rows,
    }


def tool_ui_progress(args: dict[str, Any]) -> dict[str, Any]:
    task_id = require_str(args, "task_id")
    current = as_int(args.get("current"), 0, minimum=0)
    total = as_int(args.get("total"), 100, minimum=1)
    if current > total:
        current = total

    percent = int((current / total) * 100)
    return {
        "type": "progress",
        "task_id": task_id,
        "phase": str(args.get("phase", "running")),
        "message": str(args.get("message", "")),
        "current": current,
        "total": total,
        "percent": percent,
    }


def tool_ui_dialog_input(args: dict[str, Any]) -> dict[str, Any]:
    title = str(args.get("title", "输入确认")).strip() or "输入确认"
    prompt = str(args.get("prompt", "请输入内容")).strip() or "请输入内容"
    button1_label = str(args.get("button1_label", "确定")).strip() or "确定"
    button2_label = str(args.get("button2_label", "取消")).strip() or "取消"
    default_value = str(args.get("default", ""))

    width = as_int(args.get("width"), 460, minimum=260, maximum=1600)
    compact = as_bool(args.get("compact"), False)
    accent_color = str(args.get("accent_color", "#2563eb")).strip() or "#2563eb"

    topmost = as_bool(args.get("topmost"), True)
    bring_to_front = as_bool(args.get("bring_to_front"), True)
    focus_force = as_bool(args.get("focus_force"), True)

    auto_submit_after_ms = args.get("auto_submit_after_ms")
    auto_button = str(args.get("auto_button", "button1")).strip().lower()
    if auto_button not in {"button1", "button2", "closed"}:
        raise ValueError("`auto_button` must be one of: button1, button2, closed")

    _prepare_tkinter_env()
    try:
        import tkinter as tk
    except Exception as e:
        raise RuntimeError("tkinter is required for ui_dialog_input") from e

    result: dict[str, Any] = {"button_id": "closed", "submitted": False}

    bg_window = "#ffffff"
    text_title = "#0f172a"
    text_prompt = "#475569"
    border_color = "#dbe3f2"
    input_bg = "#f8fafc"

    title_font = ("Segoe UI", 13, "bold")
    prompt_font = ("Segoe UI", 10)
    input_font = ("Segoe UI", 11)
    button_font = ("Segoe UI", 10, "bold")

    height = 182 if compact else 208

    root = tk.Tk()
    root.title(title)
    root.resizable(False, False)
    root.configure(bg=bg_window)

    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    x_pos = max(0, int((screen_w - width) / 2))
    y_pos = max(0, int((screen_h - height) / 2))
    root.geometry(f"{width}x{height}+{x_pos}+{y_pos}")

    if topmost:
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass

    value_var = tk.StringVar(value=default_value)

    def _finish(button_id: str) -> None:
        result["button_id"] = button_id
        result["submitted"] = button_id in {"button1", "button2"}
        try:
            root.quit()
        except Exception:
            pass

    root.protocol("WM_DELETE_WINDOW", lambda: _finish("closed"))

    title_label = tk.Label(root, text=title, bg=bg_window, fg=text_title, font=title_font, anchor="w")
    title_label.pack(fill="x", padx=14, pady=(12, 2))

    prompt_label = tk.Label(root, text=prompt, bg=bg_window, fg=text_prompt, font=prompt_font, anchor="w", justify="left")
    prompt_label.pack(fill="x", padx=14, pady=(0, 8))

    entry_wrap = tk.Frame(root, bg=border_color, bd=0)
    entry_wrap.pack(fill="x", padx=14, pady=(0, 10))

    entry = tk.Entry(
        entry_wrap,
        textvariable=value_var,
        relief="flat",
        bg=input_bg,
        fg=text_title,
        insertbackground=text_title,
        font=input_font,
        highlightthickness=0,
        bd=0,
    )
    entry.pack(fill="x", ipady=8, padx=1, pady=1)

    buttons_frame = tk.Frame(root, bg=bg_window)
    buttons_frame.pack(fill="x", padx=14, pady=(0, 12))

    button2 = tk.Button(
        buttons_frame,
        text=button2_label,
        command=lambda: _finish("button2"),
        bg="#e2e8f0",
        fg="#1e293b",
        activebackground="#cbd5e1",
        activeforeground="#0f172a",
        relief="flat",
        bd=0,
        padx=14,
        pady=7,
        font=button_font,
        cursor="hand2",
    )
    button2.pack(side="right")

    button1 = tk.Button(
        buttons_frame,
        text=button1_label,
        command=lambda: _finish("button1"),
        bg=accent_color,
        fg="#ffffff",
        activebackground=accent_color,
        activeforeground="#ffffff",
        relief="flat",
        bd=0,
        padx=14,
        pady=7,
        font=button_font,
        cursor="hand2",
    )
    button1.pack(side="right", padx=(0, 8))

    root.bind("<Return>", lambda _event: _finish("button1"))
    root.bind("<Escape>", lambda _event: _finish("button2"))

    if auto_submit_after_ms is not None:
        ms = as_int(auto_submit_after_ms, 100, minimum=1, maximum=600000)
        root.after(ms, lambda: _finish(auto_button))

    if bring_to_front:
        try:
            root.deiconify()
            root.lift()
        except Exception:
            pass

    if focus_force:
        try:
            root.focus_force()
        except Exception:
            pass

    try:
        entry.focus_set()
        if focus_force:
            entry.focus_force()
    except Exception:
        pass

    root.mainloop()
    input_value = value_var.get()
    root.destroy()

    label_map = {
        "button1": button1_label,
        "button2": button2_label,
        "closed": "closed",
    }

    return {
        "type": "dialog_input",
        "title": title,
        "prompt": prompt,
        "input": input_value,
        "button_id": result["button_id"],
        "button_label": label_map.get(str(result["button_id"]), "closed"),
        "submitted": bool(result["submitted"]),
        "closed": result["button_id"] == "closed",
        "topmost": topmost,
        "compact": compact,
        "accent_color": accent_color,
    }


def tool_ui_plan_confirm(args: dict[str, Any]) -> dict[str, Any]:
    plan_content = require_str(args, "plan_content")
    title = str(args.get("title", "计划确认")).strip() or "计划确认"
    prompt = str(args.get("prompt", "请先阅读计划内容，然后选择继续或修改")).strip() or "请先阅读计划内容，然后选择继续或修改"
    continue_label = str(args.get("continue_label", "继续")).strip() or "继续"
    modify_label = str(args.get("modify_label", "修改计划")).strip() or "修改计划"

    width = as_int(args.get("width"), 820, minimum=420, maximum=1800)
    height = as_int(args.get("height"), 560, minimum=320, maximum=1400)
    topmost = as_bool(args.get("topmost"), True)
    bring_to_front = as_bool(args.get("bring_to_front"), True)
    focus_force = as_bool(args.get("focus_force"), True)

    _prepare_tkinter_env()
    try:
        import tkinter as tk
    except Exception as e:
        raise RuntimeError("tkinter is required for ui_plan_confirm") from e

    result: dict[str, Any] = {"action": "closed"}

    root = tk.Tk()
    root.title(title)
    root.resizable(True, True)
    root.minsize(420, 320)
    root.configure(bg="#ffffff")

    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    x_pos = max(0, int((screen_w - width) / 2))
    y_pos = max(0, int((screen_h - height) / 2))
    root.geometry(f"{width}x{height}+{x_pos}+{y_pos}")

    if topmost:
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass

    def _finish(action: str) -> None:
        result["action"] = action
        try:
            root.quit()
        except Exception:
            pass

    root.protocol("WM_DELETE_WINDOW", lambda: _finish("closed"))

    title_label = tk.Label(root, text=title, bg="#ffffff", fg="#0f172a", font=("Segoe UI", 14, "bold"), anchor="w")
    title_label.pack(fill="x", padx=16, pady=(12, 2))

    prompt_label = tk.Label(root, text=prompt, bg="#ffffff", fg="#475569", font=("Segoe UI", 10), anchor="w", justify="left")
    prompt_label.pack(fill="x", padx=16, pady=(0, 10))

    content_wrap = tk.Frame(root, bg="#dbe3f2", bd=0)
    content_wrap.pack(fill="both", expand=True, padx=16, pady=(0, 12))

    text_widget = tk.Text(
        content_wrap,
        wrap="word",
        bg="#f8fafc",
        fg="#0f172a",
        insertbackground="#0f172a",
        relief="flat",
        bd=0,
        font=("Consolas", 10),
        padx=10,
        pady=8,
    )
    text_widget.pack(side="left", fill="both", expand=True, padx=(1, 0), pady=1)

    scrollbar = tk.Scrollbar(content_wrap, orient="vertical", command=text_widget.yview)
    scrollbar.pack(side="right", fill="y", padx=(0, 1), pady=1)
    text_widget.configure(yscrollcommand=scrollbar.set)

    text_widget.insert("1.0", plan_content)
    text_widget.configure(state="disabled")

    buttons = tk.Frame(root, bg="#ffffff")
    buttons.pack(fill="x", padx=16, pady=(0, 14))

    modify_button = tk.Button(
        buttons,
        text=modify_label,
        command=lambda: _finish("modify"),
        bg="#e2e8f0",
        fg="#1e293b",
        activebackground="#cbd5e1",
        activeforeground="#0f172a",
        relief="flat",
        bd=0,
        padx=14,
        pady=8,
        font=("Segoe UI", 10, "bold"),
        cursor="hand2",
    )
    modify_button.pack(side="right")

    continue_button = tk.Button(
        buttons,
        text=continue_label,
        command=lambda: _finish("continue"),
        bg="#2563eb",
        fg="#ffffff",
        activebackground="#2563eb",
        activeforeground="#ffffff",
        relief="flat",
        bd=0,
        padx=14,
        pady=8,
        font=("Segoe UI", 10, "bold"),
        cursor="hand2",
    )
    continue_button.pack(side="right", padx=(0, 8))

    root.bind("<Escape>", lambda _event: _finish("modify"))
    root.bind("<Control-Return>", lambda _event: _finish("continue"))

    if bring_to_front:
        try:
            root.deiconify()
            root.lift()
        except Exception:
            pass

    if focus_force:
        try:
            root.focus_force()
        except Exception:
            pass

    root.mainloop()
    root.destroy()

    action = str(result.get("action", "closed"))
    label_map = {
        "continue": continue_label,
        "modify": modify_label,
        "closed": "closed",
    }
    return {
        "type": "plan_confirm",
        "action": action,
        "confirmed": action == "continue",
        "button_label": label_map.get(action, "closed"),
        "closed": action == "closed",
    }


def tool_ui_line_chart(args: dict[str, Any]) -> dict[str, Any]:
    y_values = _normalize_number_list(args.get("y"), "y")

    x_raw = args.get("x")
    if x_raw is None:
        x_values = [float(index + 1) for index in range(len(y_values))]
    else:
        x_values = _normalize_number_list(x_raw, "x")
        if len(x_values) != len(y_values):
            raise ValueError("`x` and `y` must have the same length")

    title = str(args.get("title", "折线图")).strip() or "折线图"
    x_label = str(args.get("x_label", "X")).strip() or "X"
    y_label = str(args.get("y_label", "Y")).strip() or "Y"
    line_color = str(args.get("line_color", "#1f77b4")).strip() or "#1f77b4"
    marker = str(args.get("marker", "o")).strip() or "o"
    line_style = str(args.get("line_style", "-")).strip() or "-"
    show = as_bool(args.get("show"), True)
    grid = as_bool(args.get("grid"), True)
    width = float(as_int(args.get("width"), 8, minimum=2, maximum=30))
    height = float(as_int(args.get("height"), 5, minimum=2, maximum=30))
    dpi = as_int(args.get("dpi"), 120, minimum=60, maximum=600)
    display_seconds = float(as_int(args.get("display_seconds"), 3, minimum=1, maximum=120))

    output_arg = args.get("path")
    if output_arg is not None and (not isinstance(output_arg, str) or not output_arg.strip()):
        raise ValueError("`path` must be a non-empty string when provided")

    if isinstance(output_arg, str) and output_arg.strip():
        output_path = Path(output_arg).expanduser().resolve()
    else:
        output_path = (Path.cwd() / ".agent" / "charts" / f"line-{int(time.time() * 1000)}.png").resolve()

    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        import matplotlib

        if not show:
            matplotlib.use("Agg")

        import matplotlib.pyplot as plt
    except ImportError:
        install_package_with_pip("matplotlib", "ui_line_chart")
        try:
            import matplotlib

            if not show:
                matplotlib.use("Agg")

            import matplotlib.pyplot as plt
        except Exception as e:
            raise RuntimeError("matplotlib is required for ui_line_chart") from e
    except Exception as e:
        raise RuntimeError("matplotlib is required for ui_line_chart") from e

    fig, ax = plt.subplots(figsize=(width, height), dpi=dpi)
    ax.plot(x_values, y_values, color=line_color, marker=marker, linestyle=line_style)
    ax.set_title(title)
    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)
    ax.grid(grid)

    fig.tight_layout()
    fig.savefig(output_path)

    shown = False
    show_error = ""
    if show:
        try:
            plt.show(block=False)
            plt.pause(display_seconds)
            shown = True
        except Exception as e:
            show_error = f"{type(e).__name__}: {e}"

    plt.close(fig)

    return {
        "type": "line_chart",
        "path": str(output_path),
        "title": title,
        "point_count": len(y_values),
        "shown": shown,
        "show_error": show_error,
        "width": width,
        "height": height,
        "dpi": dpi,
    }


def tool_ui_agent_team_dashboard(args: dict[str, Any]) -> dict[str, Any]:
    state_path = resolve_dashboard_state_path(
        state_path=args.get("state_path"),
        workspace_root=args.get("workspace_root"),
    )
    ensure_result = ensure_dashboard_state_exists(
        state_path,
        workspace_root=Path(str(args.get("workspace_root")).strip()).expanduser().resolve() if str(args.get("workspace_root") or "").strip() else None,
        request=str(args.get("request") or "").strip(),
        constraints=args.get("constraints") if isinstance(args.get("constraints"), list) else None,
        roles=args.get("roles") if isinstance(args.get("roles"), list) else None,
        title=str(args.get("title") or "").strip() or None,
        poll_interval_ms=as_int(args.get("poll_interval_ms"), DEFAULT_POLL_INTERVAL_MS, minimum=250, maximum=60000) if args.get("poll_interval_ms") is not None else None,
        active_run_id=str(args.get("active_run_id") or "").strip() or None,
        auto_open=False,
    )

    launch = launch_dashboard_process(
        state_path,
        python_executable=str(args.get("python_executable") or "").strip() or None,
        title=str(args.get("title") or "").strip() or None,
        topmost=as_bool(args.get("topmost"), True),
        bring_to_front=as_bool(args.get("bring_to_front"), True),
        poll_interval_ms=args.get("poll_interval_ms"),
    )
    return {
        "type": "agent_team_dashboard",
        "created_state": bool(ensure_result.get("created")),
        "runtime_files": ensure_result.get("runtime_files"),
        **launch,
    }


def get_ui_tooling() -> tuple[dict[str, Any], dict[str, str], dict[str, dict[str, Any]]]:
    handlers = {
        "ui_prompt": tool_ui_prompt,
        "ui_table": tool_ui_table,
        "ui_progress": tool_ui_progress,
        "ui_dialog_input": tool_ui_dialog_input,
        "ui_plan_confirm": tool_ui_plan_confirm,
        "ui_line_chart": tool_ui_line_chart,
        "ui_agent_team_dashboard": tool_ui_agent_team_dashboard,
    }
    descriptions = {
        "ui_prompt": "Build a structured interactive prompt payload.",
        "ui_table": "Build a structured table payload with optional sorting and limit.",
        "ui_progress": "Build a structured progress payload for long-running tasks.",
        "ui_dialog_input": "Show a real input dialog with two buttons and return text plus clicked button.",
        "ui_plan_confirm": "Show a plan review dialog and return continue/modify decision.",
        "ui_line_chart": "Render a line chart to PNG and optionally display it in a window.",
        "ui_agent_team_dashboard": "Open the agent team dashboard window from a dashboard `state_path`; `workspace_root` remains supported for compatibility.",
    }
    schemas = {
        "ui_prompt": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "options": {"type": "array", "items": {"oneOf": [{"type": "string"}, {"type": "object"}]}},
                "allow_free_text": {"type": "boolean", "default": True},
                "default": {},
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
        "ui_table": {
            "type": "object",
            "properties": {
                "columns": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "rows": {"type": "array", "items": {"oneOf": [{"type": "object"}, {"type": "array"}]}},
                "sort_by": {"type": "string"},
                "descending": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "minimum": 0},
            },
            "required": ["columns", "rows"],
            "additionalProperties": False,
        },
        "ui_progress": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "phase": {"type": "string", "default": "running"},
                "message": {"type": "string", "default": ""},
                "current": {"type": "integer", "minimum": 0, "default": 0},
                "total": {"type": "integer", "minimum": 1, "default": 100},
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
        "ui_dialog_input": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "default": "输入确认"},
                "prompt": {"type": "string", "default": "请输入内容"},
                "default": {"type": "string", "default": ""},
                "button1_label": {"type": "string", "default": "确定"},
                "button2_label": {"type": "string", "default": "取消"},
                "width": {"type": "integer", "minimum": 260, "maximum": 1600, "default": 460},
                "compact": {"type": "boolean", "default": False},
                "accent_color": {"type": "string", "default": "#2563eb"},
                "topmost": {"type": "boolean", "default": True},
                "bring_to_front": {"type": "boolean", "default": True},
                "focus_force": {"type": "boolean", "default": True},
                "auto_submit_after_ms": {"type": "integer", "minimum": 1, "maximum": 600000},
                "auto_button": {"type": "string", "enum": ["button1", "button2", "closed"], "default": "button1"}
            },
            "additionalProperties": False,
        },
        "ui_plan_confirm": {
            "type": "object",
            "properties": {
                "plan_content": {"type": "string"},
                "title": {"type": "string", "default": "计划确认"},
                "prompt": {"type": "string", "default": "请先阅读计划内容，然后选择继续或修改"},
                "continue_label": {"type": "string", "default": "继续"},
                "modify_label": {"type": "string", "default": "修改计划"},
                "width": {"type": "integer", "minimum": 420, "maximum": 1800, "default": 820},
                "height": {"type": "integer", "minimum": 320, "maximum": 1400, "default": 560},
                "topmost": {"type": "boolean", "default": True},
                "bring_to_front": {"type": "boolean", "default": True},
                "focus_force": {"type": "boolean", "default": True}
            },
            "required": ["plan_content"],
            "additionalProperties": False,
        },
        "ui_line_chart": {
            "type": "object",
            "properties": {
                "x": {"type": "array", "items": {"type": "number"}},
                "y": {"type": "array", "items": {"type": "number"}, "minItems": 1},
                "title": {"type": "string", "default": "折线图"},
                "x_label": {"type": "string", "default": "X"},
                "y_label": {"type": "string", "default": "Y"},
                "line_color": {"type": "string", "default": "#1f77b4"},
                "marker": {"type": "string", "default": "o"},
                "line_style": {"type": "string", "default": "-"},
                "grid": {"type": "boolean", "default": True},
                "show": {"type": "boolean", "default": True},
                "display_seconds": {"type": "integer", "minimum": 1, "maximum": 120, "default": 3},
                "width": {"type": "integer", "minimum": 2, "maximum": 30, "default": 8},
                "height": {"type": "integer", "minimum": 2, "maximum": 30, "default": 5},
                "dpi": {"type": "integer", "minimum": 60, "maximum": 600, "default": 120},
                "path": {"type": "string"}
            },
            "required": ["y"],
            "additionalProperties": False,
        },
        "ui_agent_team_dashboard": {
            "type": "object",
            "properties": {
                "workspace_root": {
                    "type": "string",
                    "description": "Compatibility fallback for resolving `docs/agent-team/dashboard-state.json`.",
                },
                "state_path": {
                    "type": "string",
                    "description": "Preferred dashboard state file path, typically `dashboard.state_path` from `agent_team_bootstrap`.",
                },
                "request": {
                    "type": "string",
                    "description": "When the dashboard state file does not exist yet, seed the generated state with this request text.",
                },
                "constraints": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional constraint list used when auto-generating a missing dashboard state file.",
                },
                "roles": {
                    "type": "array",
                    "description": "Optional role specs used when auto-generating a missing dashboard state file. Items may be strings or objects with `role_id`, `title`, `persona_hint`, and `output_prefix`.",
                    "items": {
                        "oneOf": [
                            {"type": "string"},
                            {
                                "type": "object",
                                "properties": {
                                    "role_id": {"type": "string"},
                                    "title": {"type": "string"},
                                    "persona_hint": {"type": "string"},
                                    "output_prefix": {"type": "string"},
                                    "status": {"type": "string"},
                                    "latest_message": {"type": "string"},
                                },
                                "additionalProperties": False,
                            },
                        ]
                    },
                },
                "active_run_id": {
                    "type": "string",
                    "description": "Optional run id to store in a newly generated dashboard state file.",
                },
                "python_executable": {"type": "string"},
                "title": {"type": "string"},
                "topmost": {"type": "boolean", "default": True},
                "bring_to_front": {"type": "boolean", "default": True},
                "poll_interval_ms": {"type": "integer", "minimum": 250, "maximum": 60000},
            },
            "required": [],
            "anyOf": [
                {"required": ["state_path"]},
                {"required": ["workspace_root"]},
            ],
            "additionalProperties": False,
        },
    }
    return handlers, descriptions, schemas
