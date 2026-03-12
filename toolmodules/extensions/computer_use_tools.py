from __future__ import annotations

import base64
import io
import threading
import time
from typing import Any, Optional

from . import browser_tools, ui_tools
from .common import as_bool, as_int, install_package_with_pip, require_str

_LOCK = threading.RLock()
_CONSENT_STATE: dict[str, dict[str, Any]] = {}
_COMPUTER_USE_SESSIONS: dict[str, dict[str, Any]] = {}
_PYAUTOGUI = None
_RAPIDOCR_ENGINE = None
_SUPPORTED_ACTION_TYPES = {
    "click",
    "double_click",
    "scroll",
    "type",
    "wait",
    "keypress",
    "drag",
    "move",
    "screenshot",
    "click_text",
    "double_click_text",
    "move_text",
}


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _normalize_session_id(value: Any, prefix: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return f"{prefix}-{int(time.time() * 1000)}"


def _consent_scope(session_id: Optional[str]) -> str:
    normalized = session_id.strip() if isinstance(session_id, str) else ""
    return f"session:{normalized}" if normalized else "global"


def _normalize_safety_checks(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []

    normalized: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        check_id = str(item.get("id", "")).strip()
        if not check_id:
            continue
        entry: dict[str, str] = {"id": check_id}
        code = str(item.get("code", "")).strip()
        message = str(item.get("message", "")).strip()
        if code:
            entry["code"] = code
        if message:
            entry["message"] = message
        normalized.append(entry)
    return normalized


def _risk_text(environment: str, task: str, action_summary: str, safety_checks: list[dict[str, str]]) -> str:
    env_label = "原生桌面" if environment == "desktop" else "浏览器会话"
    lines = [
        "## 使用前确认",
        f"- 你即将允许 AI 在{env_label}中读取截图，并执行鼠标移动、点击、键盘输入、滚动、拖拽等操作。",
        "- 这类操作可能改变窗口焦点、表单内容、聊天输入框、文件选择、下载行为、页面跳转，甚至触发不可逆操作。",
        "",
        "## 风险提示",
        "- 画面中可能包含账号、密码、验证码、支付信息、隐私数据或企业敏感信息。",
        "- AI 可能误点、误输、误提交，或在复杂界面中定位错误对象。",
        "- 在登录、支付、转账、删除、发布、发送消息、运行程序、系统设置修改等高风险步骤，请优先人工接管。",
        "- 如果你不确定当前动作是否安全，请拒绝并改为手动操作。",
    ]

    if task:
        lines.extend(["", "## 当前任务", f"- {task}"])

    if action_summary:
        lines.extend(["", "## 当前动作", f"- {action_summary}"])

    if safety_checks:
        lines.extend(["", "## OpenAI 安全检查", "- 当前轮返回了安全检查，请确认站点、窗口、动作与预期完全一致后再继续。"])
        for check in safety_checks:
            parts = [check.get("code", "未知代码") or "未知代码"]
            message = check.get("message", "")
            if message:
                parts.append(message)
            lines.append(f"- {' | '.join(parts)}")

    lines.extend(
        [
            "",
            "## 人工接管建议",
            "- 密码、短信验证码、MFA、支付确认、验证码、隐私文本等敏感内容请由你手动输入。",
            "- 原生桌面模式下，建议关闭与任务无关的窗口，避免 AI 误操作到其他应用。",
        ]
    )
    return "\n".join(lines)


def _show_consent_dialog(
    *,
    session_id: Optional[str],
    environment: str,
    task: str,
    action_summary: str,
    safety_checks: list[dict[str, str]],
) -> dict[str, Any]:
    plan_content = _risk_text(environment, task, action_summary, safety_checks)
    result = ui_tools.tool_ui_plan_confirm(
        {
            "title": "Computer Use 使用确认",
            "prompt": "请先阅读风险说明。只有在你确认当前任务和动作都符合预期时，才点击“同意并继续”。",
            "plan_content": plan_content,
            "continue_label": "同意并继续",
            "modify_label": "拒绝",
            "topmost": True,
            "bring_to_front": True,
            "focus_force": True,
        }
    )
    action = str(result.get("action", "closed")).strip().lower() if isinstance(result, dict) else "closed"
    granted = action == "continue"
    scope = _consent_scope(session_id)
    payload = {
        "scope": scope,
        "session_id": session_id or "",
        "environment": environment,
        "granted": granted,
        "action": action,
        "granted_at": _utc_now() if granted else None,
        "task": task,
        "action_summary": action_summary,
        "safety_checks": safety_checks,
    }
    if granted:
        _CONSENT_STATE[scope] = payload
    return payload


def _get_consent_environment(session_id: Optional[str]) -> str:
    if isinstance(session_id, str) and session_id.strip():
        with _LOCK:
            session = _COMPUTER_USE_SESSIONS.get(session_id.strip())
        if isinstance(session, dict):
            env = str(session.get("environment", "desktop")).strip().lower()
            if env in {"desktop", "browser"}:
                return env
    return "desktop"


def _ensure_consent(
    *,
    session_id: Optional[str],
    task: str,
    action_summary: str,
    safety_checks: list[dict[str, str]],
    force_reconfirm: bool,
) -> dict[str, Any]:
    environment = _get_consent_environment(session_id)
    scope = _consent_scope(session_id)
    existing = _CONSENT_STATE.get(scope)
    if existing and existing.get("granted") and not force_reconfirm:
        return {
            "scope": scope,
            "session_id": session_id or "",
            "environment": environment,
            "granted": True,
            "action": "cached",
            "granted_at": existing.get("granted_at"),
            "task": existing.get("task", ""),
            "action_summary": action_summary,
            "safety_checks": safety_checks,
        }

    result = _show_consent_dialog(
        session_id=session_id,
        environment=environment,
        task=task,
        action_summary=action_summary,
        safety_checks=safety_checks,
    )
    if not result.get("granted"):
        raise PermissionError("computer use 已被用户拒绝，未执行任何界面动作。")
    return result


def _get_pyautogui() -> Any:
    global _PYAUTOGUI
    with _LOCK:
        if _PYAUTOGUI is not None:
            return _PYAUTOGUI

        try:
            import pyautogui  # type: ignore
        except ImportError:
            install_package_with_pip("pyautogui", "native desktop computer use")
            try:
                import pyautogui  # type: ignore
            except Exception as exc:
                raise RuntimeError(
                    "pyautogui is required for native desktop control. Run `python -m pip install pyautogui` manually if auto-install fails."
                ) from exc

        pyautogui.PAUSE = 0.005
        _PYAUTOGUI = pyautogui
        return _PYAUTOGUI


def _desktop_size() -> tuple[int, int]:
    pyautogui = _get_pyautogui()
    size = pyautogui.size()
    return int(size.width), int(size.height)


def _require_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"`{name}` must be a number")
    try:
        return float(value)
    except Exception as exc:
        raise ValueError(f"`{name}` must be a number") from exc


def _to_int(value: float) -> int:
    return int(round(value))


def _as_float(
    value: Any,
    default: float,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> float:
    if value is None:
        result = default
    elif isinstance(value, bool):
        raise ValueError("bool is not valid for float field")
    else:
        result = float(value)
    if minimum is not None and result < minimum:
        raise ValueError(f"value must be >= {minimum}")
    if maximum is not None and result > maximum:
        raise ValueError(f"value must be <= {maximum}")
    return result


def _resolve_type_delay_ms(action: dict[str, Any], default_ms: int = 12) -> int:
    interval_seconds = action.get("interval_seconds")
    if interval_seconds is not None:
        seconds = _as_float(interval_seconds, default_ms / 1000.0, minimum=0.0, maximum=10.0)
        return _to_int(seconds * 1000)
    return as_int(action.get("interval_ms"), default_ms, minimum=0, maximum=10000)


def _get_session(session_id: str) -> dict[str, Any]:
    with _LOCK:
        session = _COMPUTER_USE_SESSIONS.get(session_id)
    if not isinstance(session, dict):
        raise ValueError(f"computer use session not found: {session_id}")
    return session


def _store_session(session_id: str, session: dict[str, Any]) -> None:
    with _LOCK:
        _COMPUTER_USE_SESSIONS[session_id] = session


def _remove_session(session_id: str) -> Optional[dict[str, Any]]:
    with _LOCK:
        return _COMPUTER_USE_SESSIONS.pop(session_id, None)


def _session_payload(session_id: str, session: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "environment": session.get("environment", "desktop"),
        "display_width": session.get("display_width"),
        "display_height": session.get("display_height"),
        "created_at": session.get("created_at"),
        "browser_session_id": session.get("browser_session_id", ""),
    }


def _normalize_action_list(args: dict[str, Any]) -> tuple[Optional[str], list[dict[str, Any]], list[dict[str, str]]]:
    call_id = args.get("call_id")
    normalized_call_id = str(call_id).strip() if isinstance(call_id, str) and call_id.strip() else None
    safety_checks = _normalize_safety_checks(args.get("pending_safety_checks"))

    raw_call = args.get("computer_call")
    if isinstance(raw_call, dict):
        if normalized_call_id is None:
            raw_call_id = raw_call.get("call_id")
            if isinstance(raw_call_id, str) and raw_call_id.strip():
                normalized_call_id = raw_call_id.strip()
        if not safety_checks:
            safety_checks = _normalize_safety_checks(raw_call.get("pending_safety_checks"))
        if args.get("actions") is None and args.get("action") is None:
            raw_actions = raw_call.get("actions")
            if isinstance(raw_actions, list) and raw_actions:
                args = {**args, "actions": raw_actions}
            elif isinstance(raw_call.get("action"), dict):
                args = {**args, "action": raw_call.get("action")}

    raw_actions = args.get("actions")
    raw_action = args.get("action")
    if raw_actions is None and raw_action is None:
        raise ValueError("Provide `computer_call`, `action`, or `actions`.")

    action_list: list[dict[str, Any]] = []
    if isinstance(raw_actions, list):
        for index, item in enumerate(raw_actions):
            if not isinstance(item, dict):
                raise ValueError(f"`actions[{index}]` must be an object")
            action_list.append(item)
    elif isinstance(raw_action, dict):
        action_list.append(raw_action)
    else:
        raise ValueError("`action` must be an object or `actions` must be an array of objects")

    if not action_list:
        raise ValueError("No computer actions provided")

    normalized_actions: list[dict[str, Any]] = []
    for index, action in enumerate(action_list):
        action_type = str(action.get("type", "")).strip()
        if not action_type:
            raise ValueError(f"`actions[{index}].type` must be a non-empty string")
        if action_type not in _SUPPORTED_ACTION_TYPES:
            raise ValueError(
                f"Unsupported computer action `{action_type}`. Supported: {', '.join(sorted(_SUPPORTED_ACTION_TYPES))}"
            )
        normalized_actions.append(action)

    return normalized_call_id, normalized_actions, safety_checks


def _summarize_actions(actions: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for action in actions:
        action_type = str(action.get("type", "")).strip() or "unknown"
        counts[action_type] = counts.get(action_type, 0) + 1
    return "，".join(f"{name} × {count}" for name, count in counts.items())


def _pyautogui_button(button: str) -> str:
    normalized = button.strip().lower()
    mapping = {
        "left": "left",
        "right": "right",
        "middle": "middle",
        "wheel": "middle",
    }
    if normalized not in mapping:
        raise ValueError("desktop click button must be one of: left, right, middle")
    return mapping[normalized]


def _pyautogui_key_name(key: str) -> str:
    normalized = key.strip()
    upper = normalized.upper()
    mapping = {
        "CTRL": "ctrl",
        "CONTROL": "ctrl",
        "CMD": "win",
        "COMMAND": "win",
        "META": "win",
        "WIN": "win",
        "ALT": "alt",
        "OPTION": "alt",
        "SHIFT": "shift",
        "ENTER": "enter",
        "RETURN": "enter",
        "SPACE": "space",
        "SPACEBAR": "space",
        "ESC": "esc",
        "ESCAPE": "esc",
        "TAB": "tab",
        "BACKSPACE": "backspace",
        "DELETE": "delete",
        "DEL": "delete",
        "HOME": "home",
        "END": "end",
        "PAGEUP": "pageup",
        "PAGEDOWN": "pagedown",
        "ARROWUP": "up",
        "ARROWDOWN": "down",
        "ARROWLEFT": "left",
        "ARROWRIGHT": "right",
        "UP": "up",
        "DOWN": "down",
        "LEFT": "left",
        "RIGHT": "right",
    }
    return mapping.get(upper, normalized.lower())


def _get_pil_ocr_modules() -> tuple[Any, Any, Any]:
    try:
        from PIL import Image, ImageFilter, ImageOps
    except ImportError:
        install_package_with_pip("pillow", "computer use OCR")
        try:
            from PIL import Image, ImageFilter, ImageOps
        except Exception as exc:
            raise RuntimeError(
                "Pillow is required for computer use OCR. Run `python -m pip install pillow` manually if auto-install fails."
            ) from exc
    except Exception as exc:
        raise RuntimeError("Pillow is required for computer use OCR.") from exc
    return Image, ImageFilter, ImageOps


def _get_ocr_engine() -> Any:
    global _RAPIDOCR_ENGINE
    with _LOCK:
        if _RAPIDOCR_ENGINE is not None:
            return _RAPIDOCR_ENGINE

        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore
        except ImportError:
            install_package_with_pip("rapidocr_onnxruntime", "computer use OCR")
            try:
                from rapidocr_onnxruntime import RapidOCR  # type: ignore
            except Exception as exc:
                raise RuntimeError(
                    "rapidocr_onnxruntime is required for OCR. Run `python -m pip install rapidocr_onnxruntime` manually if auto-install fails."
                ) from exc

        _RAPIDOCR_ENGINE = RapidOCR()
        return _RAPIDOCR_ENGINE


def _normalize_region(region: Any, image_width: int, image_height: int) -> Optional[dict[str, int]]:
    if region is None:
        return None
    if not isinstance(region, dict):
        raise ValueError("`region` must be an object")

    if any(key in region for key in ("width", "height", "x", "y")):
        left = _to_int(_require_number(region.get("x", region.get("left")), "region.x"))
        top = _to_int(_require_number(region.get("y", region.get("top")), "region.y"))
        width = _to_int(_require_number(region.get("width"), "region.width"))
        height = _to_int(_require_number(region.get("height"), "region.height"))
        right = left + width
        bottom = top + height
    else:
        left = _to_int(_require_number(region.get("left"), "region.left"))
        top = _to_int(_require_number(region.get("top"), "region.top"))
        right = _to_int(_require_number(region.get("right"), "region.right"))
        bottom = _to_int(_require_number(region.get("bottom"), "region.bottom"))

    left = max(0, min(left, image_width))
    top = max(0, min(top, image_height))
    right = max(0, min(right, image_width))
    bottom = max(0, min(bottom, image_height))

    if right <= left or bottom <= top:
        raise ValueError("`region` must describe a non-empty area inside the screenshot")

    return {
        "left": left,
        "top": top,
        "right": right,
        "bottom": bottom,
        "width": right - left,
        "height": bottom - top,
    }


def _capture_desktop_screenshot_bytes() -> tuple[bytes, int, int]:
    pyautogui = _get_pyautogui()
    image = pyautogui.screenshot()
    width, height = image.size
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    payload = buffer.getvalue()
    return payload, int(width), int(height)


def _capture_session_png_payload(session: dict[str, Any], *, full_page: bool) -> dict[str, Any]:
    environment = str(session.get("environment", "desktop")).strip().lower()
    if environment == "browser":
        browser_session_id = str(session.get("browser_session_id", "")).strip()
        if not browser_session_id:
            raise ValueError("browser computer use session is missing `browser_session_id`")
        capture = browser_tools.tool_browser_capture({"session_id": browser_session_id, "full_page": full_page})
        browser_session = browser_tools._get_session(browser_session_id)
        page = browser_session["page"]
        image_base64 = str(capture.get("image_base64", ""))
        image_bytes = base64.b64decode(image_base64) if image_base64 else b""
        return {
            "session_id": session.get("id", ""),
            "image_base64": image_base64,
            "image_bytes": image_bytes,
            "image_url": f"data:image/png;base64,{image_base64}" if image_base64 else "",
            "bytes": capture.get("bytes", len(image_bytes)),
            "format": capture.get("format", "png"),
            "full_page": bool(capture.get("full_page", full_page)),
            "display_width": session.get("display_width"),
            "display_height": session.get("display_height"),
            "current_url": page.url,
            "title": browser_session.get("title") or page.title() or "",
        }

    image_bytes, width, height = _capture_desktop_screenshot_bytes()
    session["display_width"] = width
    session["display_height"] = height
    image_base64 = base64.b64encode(image_bytes).decode("ascii")
    return {
        "session_id": session.get("id", ""),
        "image_base64": image_base64,
        "image_bytes": image_bytes,
        "image_url": f"data:image/png;base64,{image_base64}",
        "bytes": len(image_bytes),
        "format": "png",
        "full_page": True,
        "display_width": width,
        "display_height": height,
        "current_url": "",
        "title": "",
    }


def _capture_desktop_screenshot() -> tuple[str, int, int, int]:
    payload, width, height = _capture_desktop_screenshot_bytes()
    return base64.b64encode(payload).decode("ascii"), len(payload), width, height


def _capture_session_screenshot(session: dict[str, Any], *, full_page: bool) -> dict[str, Any]:
    capture = _capture_session_png_payload(session, full_page=full_page)
    return {
        "session_id": capture["session_id"],
        "image_base64": capture["image_base64"],
        "image_url": capture["image_url"],
        "bytes": capture["bytes"],
        "format": capture["format"],
        "full_page": capture["full_page"],
        "display_width": capture["display_width"],
        "display_height": capture["display_height"],
        "current_url": capture["current_url"],
        "title": capture["title"],
    }


def _preprocess_image_for_ocr(image: Any, upscale_factor: int) -> tuple[Any, int]:
    Image, ImageFilter, ImageOps = _get_pil_ocr_modules()
    working = ImageOps.autocontrast(ImageOps.grayscale(image.convert("RGB")))
    factor = max(1, upscale_factor)
    if factor > 1:
        resampling = getattr(Image, "Resampling", None)
        lanczos = resampling.LANCZOS if resampling is not None else Image.LANCZOS
        working = working.resize((working.width * factor, working.height * factor), lanczos)
    working = working.filter(ImageFilter.SHARPEN)
    return working, factor


def _normalize_ocr_polygon(raw_box: Any, *, scale_factor: int, offset_x: int, offset_y: int) -> Optional[list[dict[str, int]]]:
    if not isinstance(raw_box, (list, tuple)) or not raw_box:
        return None

    points: list[tuple[float, float]] = []
    if len(raw_box) == 4 and all(isinstance(item, (list, tuple)) and len(item) >= 2 for item in raw_box):
        for point in raw_box:
            points.append((float(point[0]), float(point[1])))
    elif len(raw_box) >= 8 and len(raw_box) % 2 == 0:
        for index in range(0, len(raw_box), 2):
            points.append((float(raw_box[index]), float(raw_box[index + 1])))
    else:
        return None

    factor = max(1, scale_factor)
    normalized: list[dict[str, int]] = []
    for x_value, y_value in points:
        normalized.append(
            {
                "x": _to_int((x_value / factor) + offset_x),
                "y": _to_int((y_value / factor) + offset_y),
            }
        )
    return normalized


def _build_bbox_from_polygon(polygon: list[dict[str, int]]) -> dict[str, Any]:
    xs = [point["x"] for point in polygon]
    ys = [point["y"] for point in polygon]
    left = min(xs)
    top = min(ys)
    right = max(xs)
    bottom = max(ys)
    return {
        "left": left,
        "top": top,
        "right": right,
        "bottom": bottom,
        "width": max(0, right - left),
        "height": max(0, bottom - top),
        "center": {
            "x": _to_int((left + right) / 2),
            "y": _to_int((top + bottom) / 2),
        },
    }


def _parse_ocr_entries(
    raw_result: Any,
    *,
    scale_factor: int,
    offset_x: int,
    offset_y: int,
    min_confidence: float,
) -> tuple[list[dict[str, Any]], Optional[float]]:
    entries_raw = raw_result
    elapsed: Optional[float] = None
    if isinstance(raw_result, tuple):
        if raw_result:
            entries_raw = raw_result[0]
        if len(raw_result) > 1:
            try:
                elapsed = float(raw_result[1])
            except Exception:
                elapsed = None

    if entries_raw is None:
        return [], elapsed
    if not isinstance(entries_raw, list):
        return [], elapsed

    entries: list[dict[str, Any]] = []
    for index, item in enumerate(entries_raw):
        raw_box: Any = None
        text = ""
        score_value = 0.0

        if isinstance(item, dict):
            raw_box = item.get("box", item.get("points"))
            text = str(item.get("text", "")).strip()
            score_value = _as_float(item.get("score"), 0.0, minimum=0.0)
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            raw_box = item[0]
            text = str(item[1]).strip()
            if len(item) > 2:
                score_value = _as_float(item[2], 0.0, minimum=0.0)
        else:
            continue

        if not text or score_value < min_confidence:
            continue

        polygon = _normalize_ocr_polygon(raw_box, scale_factor=scale_factor, offset_x=offset_x, offset_y=offset_y)
        if not polygon:
            continue

        bbox = _build_bbox_from_polygon(polygon)
        entries.append(
            {
                "index": index + 1,
                "text": text,
                "score": round(score_value, 4),
                "polygon": polygon,
                "bbox": {key: value for key, value in bbox.items() if key != "center"},
                "center": bbox["center"],
            }
        )

    entries.sort(key=lambda item: (item["bbox"]["top"], item["bbox"]["left"], item["index"]))
    for index, entry in enumerate(entries, start=1):
        entry["index"] = index
    return entries, elapsed


def _normalize_match_text(text: str, *, case_sensitive: bool, normalize_whitespace: bool) -> str:
    normalized = " ".join(text.split()) if normalize_whitespace else text
    return normalized if case_sensitive else normalized.casefold()


def _filter_ocr_matches(
    entries: list[dict[str, Any]],
    query: str,
    *,
    match_mode: str,
    case_sensitive: bool,
    normalize_whitespace: bool,
    max_results: int,
) -> list[dict[str, Any]]:
    normalized_query = _normalize_match_text(query, case_sensitive=case_sensitive, normalize_whitespace=normalize_whitespace)
    if not normalized_query:
        return []

    matches: list[dict[str, Any]] = []
    for entry in entries:
        normalized_text = _normalize_match_text(
            str(entry.get("text", "")),
            case_sensitive=case_sensitive,
            normalize_whitespace=normalize_whitespace,
        )
        matched = False
        if match_mode == "exact":
            matched = normalized_text == normalized_query
        elif match_mode == "starts_with":
            matched = normalized_text.startswith(normalized_query)
        else:
            matched = normalized_query in normalized_text
        if not matched:
            continue
        match_entry = dict(entry)
        match_entry["match_mode"] = match_mode
        matches.append(match_entry)
        if max_results > 0 and len(matches) >= max_results:
            break
    return matches


def _perform_ocr_on_capture(
    capture: dict[str, Any],
    *,
    region: Any,
    min_confidence: float,
    upscale_factor: int,
) -> dict[str, Any]:
    Image, _, _ = _get_pil_ocr_modules()
    image_bytes = capture.get("image_bytes")
    if not isinstance(image_bytes, (bytes, bytearray)) or not image_bytes:
        raise ValueError("failed to capture screenshot bytes for OCR")

    with Image.open(io.BytesIO(image_bytes)) as opened_image:
        source_image = opened_image.convert("RGB")

    region_payload = _normalize_region(region, source_image.width, source_image.height)
    offset_x = 0
    offset_y = 0
    working_image = source_image
    if region_payload is not None:
        offset_x = region_payload["left"]
        offset_y = region_payload["top"]
        working_image = source_image.crop(
            (
                region_payload["left"],
                region_payload["top"],
                region_payload["right"],
                region_payload["bottom"],
            )
        )

    preprocessed_image, scale_factor = _preprocess_image_for_ocr(working_image, upscale_factor)
    buffer = io.BytesIO()
    preprocessed_image.save(buffer, format="PNG")
    prepared_bytes = buffer.getvalue()

    engine = _get_ocr_engine()
    raw_result = engine(prepared_bytes)
    entries, elapsed = _parse_ocr_entries(
        raw_result,
        scale_factor=scale_factor,
        offset_x=offset_x,
        offset_y=offset_y,
        min_confidence=min_confidence,
    )

    return {
        "entry_count": len(entries),
        "entries": entries,
        "recognized_text": "\n".join(entry["text"] for entry in entries),
        "ocr_engine": "rapidocr_onnxruntime",
        "elapsed_seconds": round(elapsed, 4) if elapsed is not None else None,
        "region": region_payload,
        "upscale_factor": scale_factor,
        "min_confidence": min_confidence,
    }


def _resolve_text_target(session: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    query = str(action.get("text", "")).strip()
    if not query:
        raise ValueError("text-based action requires non-empty `text`")

    match_mode = str(action.get("match_mode", "contains")).strip().lower() or "contains"
    if match_mode not in {"contains", "exact", "starts_with"}:
        raise ValueError("`match_mode` must be one of: contains, exact, starts_with")

    occurrence = as_int(action.get("occurrence"), 1, minimum=1, maximum=1000)
    max_results = as_int(action.get("max_results"), max(occurrence, 10), minimum=1, maximum=200)
    min_confidence = _as_float(action.get("min_confidence"), 0.0, minimum=0.0, maximum=1.0)
    upscale_factor = as_int(action.get("upscale_factor"), 2, minimum=1, maximum=4)
    case_sensitive = as_bool(action.get("case_sensitive"), False)
    normalize_whitespace = as_bool(action.get("normalize_whitespace"), True)
    full_page = as_bool(action.get("full_page"), True)

    capture = _capture_session_png_payload(session, full_page=full_page)
    ocr_payload = _perform_ocr_on_capture(
        capture,
        region=action.get("region"),
        min_confidence=min_confidence,
        upscale_factor=upscale_factor,
    )
    matches = _filter_ocr_matches(
        ocr_payload["entries"],
        query,
        match_mode=match_mode,
        case_sensitive=case_sensitive,
        normalize_whitespace=normalize_whitespace,
        max_results=max_results,
    )
    if not matches:
        raise ValueError(f"No OCR text matched `{query}`")
    if occurrence > len(matches):
        raise ValueError(f"Requested occurrence {occurrence} exceeds available OCR matches: {len(matches)}")

    return {
        "query": query,
        "match_mode": match_mode,
        "matches": matches,
        "target": matches[occurrence - 1],
        "ocr": ocr_payload,
        "capture": {
            "session_id": capture["session_id"],
            "bytes": capture["bytes"],
            "format": capture["format"],
            "full_page": capture["full_page"],
            "display_width": capture["display_width"],
            "display_height": capture["display_height"],
            "current_url": capture["current_url"],
            "title": capture["title"],
        },
    }


def _execute_desktop_action(session: dict[str, Any], action: dict[str, Any], wait_ms_default: int) -> dict[str, Any]:
    pyautogui = _get_pyautogui()
    action_type = str(action.get("type", "")).strip()
    result_extra: dict[str, Any] = {}

    if action_type == "click":
        button = str(action.get("button", "left")).strip().lower() or "left"
        if button == "back":
            pyautogui.hotkey("alt", "left")
        elif button == "forward":
            pyautogui.hotkey("alt", "right")
        else:
            x = _to_int(_require_number(action.get("x"), "x"))
            y = _to_int(_require_number(action.get("y"), "y"))
            pyautogui.click(x=x, y=y, button=_pyautogui_button(button))
    elif action_type == "double_click":
        x = _to_int(_require_number(action.get("x"), "x"))
        y = _to_int(_require_number(action.get("y"), "y"))
        button = _pyautogui_button(str(action.get("button", "left")))
        pyautogui.doubleClick(x=x, y=y, button=button)
    elif action_type == "move":
        x = _to_int(_require_number(action.get("x"), "x"))
        y = _to_int(_require_number(action.get("y"), "y"))
        pyautogui.moveTo(x, y)
    elif action_type in {"click_text", "double_click_text", "move_text"}:
        resolved = _resolve_text_target(session, action)
        target = resolved["target"]
        center = target["center"]
        offset_x = _to_int(_require_number(action.get("offset_x", 0), "offset_x"))
        offset_y = _to_int(_require_number(action.get("offset_y", 0), "offset_y"))
        x = center["x"] + offset_x
        y = center["y"] + offset_y
        if action_type == "move_text":
            pyautogui.moveTo(x, y)
        elif action_type == "double_click_text":
            button = _pyautogui_button(str(action.get("button", "left")))
            pyautogui.doubleClick(x=x, y=y, button=button)
        else:
            button = _pyautogui_button(str(action.get("button", "left")))
            pyautogui.click(x=x, y=y, button=button)
        result_extra = {
            "query": resolved["query"],
            "match_mode": resolved["match_mode"],
            "match_count": len(resolved["matches"]),
            "target_text": target["text"],
            "target_center": {"x": x, "y": y},
            "target_bbox": target["bbox"],
            "ocr_region": resolved["ocr"].get("region"),
        }
    elif action_type == "drag":
        path = action.get("path")
        if not isinstance(path, list) or len(path) < 2:
            raise ValueError("`drag.path` must contain at least two coordinates")
        first = path[0]
        if not isinstance(first, dict):
            raise ValueError("`drag.path[0]` must be an object")
        start_x = _to_int(_require_number(first.get("x"), "path[0].x"))
        start_y = _to_int(_require_number(first.get("y"), "path[0].y"))
        pyautogui.moveTo(start_x, start_y)
        pyautogui.mouseDown()
        for index, point in enumerate(path[1:], start=1):
            if not isinstance(point, dict):
                raise ValueError(f"`drag.path[{index}]` must be an object")
            point_x = _to_int(_require_number(point.get("x"), f"path[{index}].x"))
            point_y = _to_int(_require_number(point.get("y"), f"path[{index}].y"))
            pyautogui.moveTo(point_x, point_y)
        pyautogui.mouseUp()
    elif action_type == "scroll":
        x = _to_int(_require_number(action.get("x"), "x"))
        y = _to_int(_require_number(action.get("y"), "y"))
        scroll_x = _to_int(_require_number(action.get("scroll_x", action.get("scrollX", 0)), "scroll_x"))
        scroll_y = _to_int(_require_number(action.get("scroll_y", action.get("scrollY", 0)), "scroll_y"))
        pyautogui.moveTo(x, y)
        if scroll_y:
            pyautogui.scroll(-scroll_y, x=x, y=y)
        if scroll_x and hasattr(pyautogui, "hscroll"):
            pyautogui.hscroll(scroll_x, x=x, y=y)
    elif action_type == "keypress":
        keys = action.get("keys")
        if not isinstance(keys, list) or not keys:
            raise ValueError("`keypress.keys` must be a non-empty array")
        normalized = [_pyautogui_key_name(str(key)) for key in keys if str(key).strip()]
        if not normalized:
            raise ValueError("`keypress.keys` must contain at least one non-empty key")
        if len(normalized) == 1:
            pyautogui.press(normalized[0])
        else:
            pyautogui.hotkey(*normalized)
    elif action_type == "type":
        text = str(action.get("text", ""))
        if not text:
            raise ValueError("`type.text` must be a non-empty string")
        interval_seconds = _resolve_type_delay_ms(action) / 1000.0
        pyautogui.write(text, interval=interval_seconds)
    elif action_type == "wait":
        duration_ms = as_int(action.get("duration_ms"), wait_ms_default, minimum=0, maximum=600000)
        time.sleep(duration_ms / 1000.0)
    elif action_type == "screenshot":
        pass
    else:
        raise ValueError(f"Unsupported desktop action `{action_type}`")

    width, height = _desktop_size()
    session["display_width"] = width
    session["display_height"] = height
    result = {
        "type": action_type,
        "environment": "desktop",
        "display_width": width,
        "display_height": height,
        "current_url": "",
        "title": "",
    }
    result.update(result_extra)
    return result


def _execute_browser_action(session: dict[str, Any], action: dict[str, Any], wait_ms_default: int) -> dict[str, Any]:
    browser_session_id = str(session.get("browser_session_id", "")).strip()
    if not browser_session_id:
        raise ValueError("browser computer use session is missing `browser_session_id`")

    browser_session = browser_tools._get_session(browser_session_id)
    page = browser_session["page"]
    action_type = str(action.get("type", "")).strip()
    result_extra: dict[str, Any] = {}

    if action_type == "click":
        button = str(action.get("button", "left")).strip().lower() or "left"
        if button == "back":
            page.go_back(wait_until="domcontentloaded")
        elif button == "forward":
            page.go_forward(wait_until="domcontentloaded")
        else:
            x = _require_number(action.get("x"), "x")
            y = _require_number(action.get("y"), "y")
            page.mouse.click(x, y, button=button)
    elif action_type == "double_click":
        x = _require_number(action.get("x"), "x")
        y = _require_number(action.get("y"), "y")
        page.mouse.dblclick(x, y, button=str(action.get("button", "left")).strip().lower() or "left")
    elif action_type == "move":
        x = _require_number(action.get("x"), "x")
        y = _require_number(action.get("y"), "y")
        page.mouse.move(x, y)
    elif action_type in {"click_text", "double_click_text", "move_text"}:
        resolved = _resolve_text_target(session, action)
        target = resolved["target"]
        center = target["center"]
        offset_x = _require_number(action.get("offset_x", 0), "offset_x")
        offset_y = _require_number(action.get("offset_y", 0), "offset_y")
        x = center["x"] + offset_x
        y = center["y"] + offset_y
        if action_type == "move_text":
            page.mouse.move(x, y)
        elif action_type == "double_click_text":
            page.mouse.dblclick(x, y, button=str(action.get("button", "left")).strip().lower() or "left")
        else:
            page.mouse.click(x, y, button=str(action.get("button", "left")).strip().lower() or "left")
        result_extra = {
            "query": resolved["query"],
            "match_mode": resolved["match_mode"],
            "match_count": len(resolved["matches"]),
            "target_text": target["text"],
            "target_center": {"x": _to_int(x), "y": _to_int(y)},
            "target_bbox": target["bbox"],
            "ocr_region": resolved["ocr"].get("region"),
        }
    elif action_type == "drag":
        path = action.get("path")
        if not isinstance(path, list) or len(path) < 2:
            raise ValueError("`drag.path` must contain at least two coordinates")
        first = path[0]
        if not isinstance(first, dict):
            raise ValueError("`drag.path[0]` must be an object")
        start_x = _require_number(first.get("x"), "path[0].x")
        start_y = _require_number(first.get("y"), "path[0].y")
        page.mouse.move(start_x, start_y)
        page.mouse.down()
        for index, point in enumerate(path[1:], start=1):
            if not isinstance(point, dict):
                raise ValueError(f"`drag.path[{index}]` must be an object")
            page.mouse.move(
                _require_number(point.get("x"), f"path[{index}].x"),
                _require_number(point.get("y"), f"path[{index}].y"),
            )
        page.mouse.up()
    elif action_type == "scroll":
        x = _require_number(action.get("x"), "x")
        y = _require_number(action.get("y"), "y")
        scroll_x = _require_number(action.get("scroll_x", action.get("scrollX", 0)), "scroll_x")
        scroll_y = _require_number(action.get("scroll_y", action.get("scrollY", 0)), "scroll_y")
        page.mouse.move(x, y)
        page.mouse.wheel(scroll_x, scroll_y)
    elif action_type == "keypress":
        keys = action.get("keys")
        if not isinstance(keys, list) or not keys:
            raise ValueError("`keypress.keys` must be a non-empty array")
        for key in keys:
            key_text = str(key).strip()
            if not key_text:
                continue
            page.keyboard.press(" " if key_text.upper() == "SPACE" else key_text)
    elif action_type == "type":
        text = str(action.get("text", ""))
        if not text:
            raise ValueError("`type.text` must be a non-empty string")
        page.keyboard.type(text, delay=_resolve_type_delay_ms(action))
    elif action_type == "wait":
        duration_ms = as_int(action.get("duration_ms"), wait_ms_default, minimum=0, maximum=600000)
        time.sleep(duration_ms / 1000.0)
    elif action_type == "screenshot":
        pass
    else:
        raise ValueError(f"Unsupported browser action `{action_type}`")

    browser_session["url"] = page.url
    browser_session["title"] = page.title()
    result = {
        "type": action_type,
        "environment": "browser",
        "display_width": session.get("display_width"),
        "display_height": session.get("display_height"),
        "current_url": page.url,
        "title": browser_session.get("title") or "",
    }
    result.update(result_extra)
    return result


def _execute_one_action(session: dict[str, Any], action: dict[str, Any], wait_ms_default: int) -> dict[str, Any]:
    environment = str(session.get("environment", "desktop")).strip().lower()
    if environment == "browser":
        return _execute_browser_action(session, action, wait_ms_default)
    return _execute_desktop_action(session, action, wait_ms_default)


def tool_computer_use_session_start(args: dict[str, Any]) -> dict[str, Any]:
    environment = str(args.get("environment", "desktop")).strip().lower() or "desktop"
    if environment not in {"desktop", "browser"}:
        raise ValueError("`environment` must be one of: desktop, browser")

    session_id = _normalize_session_id(args.get("session_id"), "computer-use")
    with _LOCK:
        if session_id in _COMPUTER_USE_SESSIONS:
            raise ValueError(f"computer use session already exists: {session_id}")

    if environment == "browser":
        browser_started = browser_tools.tool_browser_session_start(
            {
                "session_id": session_id,
                "browser": str(args.get("browser", "chromium")),
                "headless": as_bool(args.get("headless"), False),
                "slow_mo_ms": as_int(args.get("slow_mo_ms"), 0, minimum=0, maximum=10000),
                "viewport_width": as_int(args.get("viewport_width"), 1280, minimum=1, maximum=10000),
                "viewport_height": as_int(args.get("viewport_height"), 720, minimum=1, maximum=10000),
                "timeout_ms": as_int(args.get("timeout_ms"), 30000, minimum=1, maximum=600000),
                "url": args.get("url"),
            }
        )
        session = {
            "id": session_id,
            "environment": "browser",
            "created_at": browser_started.get("created_at", _utc_now()),
            "display_width": as_int(args.get("viewport_width"), 1280, minimum=1, maximum=10000),
            "display_height": as_int(args.get("viewport_height"), 720, minimum=1, maximum=10000),
            "browser_session_id": session_id,
        }
    else:
        width, height = _desktop_size()
        session = {
            "id": session_id,
            "environment": "desktop",
            "created_at": _utc_now(),
            "display_width": width,
            "display_height": height,
            "browser_session_id": "",
        }

    _store_session(session_id, session)

    payload = {"started": True, **_session_payload(session_id, session)}
    if as_bool(args.get("capture_initial_screenshot"), False):
        payload["initial_screenshot"] = tool_computer_use_screenshot(
            {
                "session_id": session_id,
                "full_page": as_bool(args.get("initial_full_page"), False),
                "task": str(args.get("task", "")).strip(),
            }
        )
    return payload


def tool_computer_use_session_stop(args: dict[str, Any]) -> dict[str, Any]:
    session_id = require_str(args, "session_id")
    session = _remove_session(session_id)
    _CONSENT_STATE.pop(_consent_scope(session_id), None)

    if not isinstance(session, dict):
        return {"session_id": session_id, "stopped": False, "reason": "not found"}

    if str(session.get("environment", "desktop")) == "browser":
        browser_tools.tool_browser_session_stop({"session_id": session_id})

    return {"session_id": session_id, "stopped": True, "environment": session.get("environment", "desktop")}


def tool_computer_use_request_consent(args: dict[str, Any]) -> dict[str, Any]:
    session_id_raw = args.get("session_id")
    session_id = session_id_raw.strip() if isinstance(session_id_raw, str) and session_id_raw.strip() else None
    task = str(args.get("task", "")).strip()
    action_summary = str(args.get("action_summary", "computer use 操作")).strip() or "computer use 操作"
    safety_checks = _normalize_safety_checks(args.get("pending_safety_checks"))
    force_reconfirm = as_bool(args.get("force_reconfirm"), False)
    environment = _get_consent_environment(session_id)

    if not force_reconfirm:
        existing = _CONSENT_STATE.get(_consent_scope(session_id))
        if existing and existing.get("granted"):
            return {
                "scope": _consent_scope(session_id),
                "session_id": session_id or "",
                "environment": environment,
                "granted": True,
                "action": "cached",
                "granted_at": existing.get("granted_at"),
                "task": existing.get("task", ""),
                "action_summary": action_summary,
                "safety_checks": safety_checks,
            }

    return _show_consent_dialog(
        session_id=session_id,
        environment=environment,
        task=task,
        action_summary=action_summary,
        safety_checks=safety_checks,
    )


def tool_computer_use_revoke_consent(args: dict[str, Any]) -> dict[str, Any]:
    session_id_raw = args.get("session_id")
    session_id = session_id_raw.strip() if isinstance(session_id_raw, str) and session_id_raw.strip() else None
    removed = _CONSENT_STATE.pop(_consent_scope(session_id), None)
    return {
        "scope": _consent_scope(session_id),
        "session_id": session_id or "",
        "revoked": removed is not None,
    }


def tool_computer_use_execute(args: dict[str, Any]) -> dict[str, Any]:
    session_id = require_str(args, "session_id")
    session = _get_session(session_id)
    call_id, actions, safety_checks = _normalize_action_list(args)
    task = str(args.get("task", "")).strip()
    force_reconfirm = as_bool(args.get("force_reconfirm"), False)
    capture_after_default = call_id is not None
    capture_after = as_bool(args.get("capture_after"), capture_after_default)
    full_page = as_bool(args.get("full_page"), True)
    wait_ms = as_int(args.get("wait_ms"), 300, minimum=0, maximum=600000)

    consent = _ensure_consent(
        session_id=session_id,
        task=task,
        action_summary=_summarize_actions(actions),
        safety_checks=safety_checks,
        force_reconfirm=force_reconfirm,
    )

    executed: list[dict[str, Any]] = []
    for action in actions:
        executed.append(_execute_one_action(session, action, wait_ms))

    screenshot_payload: Optional[dict[str, Any]] = None
    if capture_after:
        screenshot_payload = _capture_session_screenshot(session, full_page=full_page)

    computer_call_output = None
    if call_id and screenshot_payload is not None:
        computer_call_output = {
            "type": "computer_call_output",
            "call_id": call_id,
            "acknowledged_safety_checks": safety_checks,
            "output": {
                "type": "computer_screenshot",
                "image_url": screenshot_payload["image_url"],
                "detail": "original",
            },
            "current_url": screenshot_payload.get("current_url", ""),
        }

    return {
        "session_id": session_id,
        "environment": session.get("environment", "desktop"),
        "consent": consent,
        "action_count": len(actions),
        "executed_actions": executed,
        "current_url": screenshot_payload.get("current_url", "") if screenshot_payload else "",
        "screenshot": screenshot_payload,
        "computer_call_output": computer_call_output,
    }


def tool_computer_use_screenshot(args: dict[str, Any]) -> dict[str, Any]:
    session_id = require_str(args, "session_id")
    session = _get_session(session_id)
    task = str(args.get("task", "")).strip()
    full_page = as_bool(args.get("full_page"), True)
    force_reconfirm = as_bool(args.get("force_reconfirm"), False)

    consent = _ensure_consent(
        session_id=session_id,
        task=task,
        action_summary="读取当前界面截图",
        safety_checks=[],
        force_reconfirm=force_reconfirm,
    )
    screenshot_payload = _capture_session_screenshot(session, full_page=full_page)
    return {
        "session_id": session_id,
        "environment": session.get("environment", "desktop"),
        "consent": consent,
        **screenshot_payload,
    }


def tool_computer_use_manual_prompt(args: dict[str, Any]) -> dict[str, Any]:
    prompt = require_str(args, "prompt")
    title = str(args.get("title", "需要你手动操作")).strip() or "需要你手动操作"
    require_input_value = as_bool(args.get("require_input"), False)
    default_value = str(args.get("default", ""))

    dialog_prompt = prompt.strip()
    if require_input_value:
        dialog_prompt = (
            f"{dialog_prompt}\n\n"
            "请你手动输入或确认敏感信息。AI 不会自动填写密码、验证码、MFA、支付或其他敏感内容。"
        )
    else:
        dialog_prompt = (
            f"{dialog_prompt}\n\n"
            "请你手动完成该步骤。完成后点击“我已完成”。如不希望继续，请点击“取消”。"
        )

    result = ui_tools.tool_ui_dialog_input(
        {
            "title": title,
            "prompt": dialog_prompt,
            "default": default_value,
            "button1_label": "提交" if require_input_value else "我已完成",
            "button2_label": "取消",
            "topmost": True,
            "bring_to_front": True,
            "focus_force": True,
            "compact": False,
            "accent_color": "#2563eb",
        }
    )

    input_value = str(result.get("input", "")) if isinstance(result, dict) else ""
    completed = bool(isinstance(result, dict) and result.get("button_id") == "button1")
    if require_input_value and completed and not input_value.strip():
        raise ValueError("该步骤需要用户输入内容，但未收到输入。")

    return {
        "completed": completed,
        "cancelled": bool(isinstance(result, dict) and result.get("button_id") != "button1"),
        "input": input_value,
        "button_id": result.get("button_id") if isinstance(result, dict) else "closed",
        "button_label": result.get("button_label") if isinstance(result, dict) else "closed",
        "submitted": bool(result.get("submitted")) if isinstance(result, dict) else False,
        "title": title,
        "prompt": dialog_prompt,
    }


def tool_computer_use_ocr(args: dict[str, Any]) -> dict[str, Any]:
    session_id = require_str(args, "session_id")
    session = _get_session(session_id)
    task = str(args.get("task", "")).strip()
    full_page = as_bool(args.get("full_page"), True)
    force_reconfirm = as_bool(args.get("force_reconfirm"), False)
    include_screenshot = as_bool(args.get("include_screenshot"), False)
    min_confidence = _as_float(args.get("min_confidence"), 0.0, minimum=0.0, maximum=1.0)
    upscale_factor = as_int(args.get("upscale_factor"), 2, minimum=1, maximum=4)

    consent = _ensure_consent(
        session_id=session_id,
        task=task,
        action_summary="读取当前界面并执行 OCR 识别",
        safety_checks=[],
        force_reconfirm=force_reconfirm,
    )
    capture = _capture_session_png_payload(session, full_page=full_page)
    ocr_payload = _perform_ocr_on_capture(
        capture,
        region=args.get("region"),
        min_confidence=min_confidence,
        upscale_factor=upscale_factor,
    )

    result = {
        "session_id": session_id,
        "environment": session.get("environment", "desktop"),
        "consent": consent,
        "entry_count": ocr_payload["entry_count"],
        "entries": ocr_payload["entries"],
        "recognized_text": ocr_payload["recognized_text"],
        "ocr_engine": ocr_payload["ocr_engine"],
        "elapsed_seconds": ocr_payload["elapsed_seconds"],
        "region": ocr_payload["region"],
        "min_confidence": ocr_payload["min_confidence"],
        "upscale_factor": ocr_payload["upscale_factor"],
        "display_width": capture["display_width"],
        "display_height": capture["display_height"],
        "current_url": capture["current_url"],
        "title": capture["title"],
    }
    if include_screenshot:
        result["screenshot"] = {
            "session_id": capture["session_id"],
            "image_base64": capture["image_base64"],
            "image_url": capture["image_url"],
            "bytes": capture["bytes"],
            "format": capture["format"],
            "full_page": capture["full_page"],
            "display_width": capture["display_width"],
            "display_height": capture["display_height"],
            "current_url": capture["current_url"],
            "title": capture["title"],
        }
    return result


def tool_computer_use_find_text(args: dict[str, Any]) -> dict[str, Any]:
    session_id = require_str(args, "session_id")
    session = _get_session(session_id)
    query = require_str(args, "text")
    task = str(args.get("task", "")).strip()
    full_page = as_bool(args.get("full_page"), True)
    force_reconfirm = as_bool(args.get("force_reconfirm"), False)
    include_screenshot = as_bool(args.get("include_screenshot"), False)
    match_mode = str(args.get("match_mode", "contains")).strip().lower() or "contains"
    if match_mode not in {"contains", "exact", "starts_with"}:
        raise ValueError("`match_mode` must be one of: contains, exact, starts_with")

    occurrence = as_int(args.get("occurrence"), 1, minimum=1, maximum=1000)
    max_results = as_int(args.get("max_results"), max(occurrence, 10), minimum=1, maximum=200)
    min_confidence = _as_float(args.get("min_confidence"), 0.0, minimum=0.0, maximum=1.0)
    upscale_factor = as_int(args.get("upscale_factor"), 2, minimum=1, maximum=4)
    case_sensitive = as_bool(args.get("case_sensitive"), False)
    normalize_whitespace = as_bool(args.get("normalize_whitespace"), True)

    consent = _ensure_consent(
        session_id=session_id,
        task=task,
        action_summary=f"读取当前界面并查找文字：{query}",
        safety_checks=[],
        force_reconfirm=force_reconfirm,
    )
    capture = _capture_session_png_payload(session, full_page=full_page)
    ocr_payload = _perform_ocr_on_capture(
        capture,
        region=args.get("region"),
        min_confidence=min_confidence,
        upscale_factor=upscale_factor,
    )
    matches = _filter_ocr_matches(
        ocr_payload["entries"],
        query,
        match_mode=match_mode,
        case_sensitive=case_sensitive,
        normalize_whitespace=normalize_whitespace,
        max_results=max_results,
    )
    target = matches[occurrence - 1] if occurrence <= len(matches) else None

    result = {
        "session_id": session_id,
        "environment": session.get("environment", "desktop"),
        "consent": consent,
        "text": query,
        "match_mode": match_mode,
        "case_sensitive": case_sensitive,
        "normalize_whitespace": normalize_whitespace,
        "found": target is not None,
        "match_count": len(matches),
        "matches": matches,
        "target": target,
        "requested_occurrence": occurrence,
        "region": ocr_payload["region"],
        "min_confidence": ocr_payload["min_confidence"],
        "upscale_factor": ocr_payload["upscale_factor"],
        "recognized_text": ocr_payload["recognized_text"],
        "display_width": capture["display_width"],
        "display_height": capture["display_height"],
        "current_url": capture["current_url"],
        "title": capture["title"],
    }
    if include_screenshot:
        result["screenshot"] = {
            "session_id": capture["session_id"],
            "image_base64": capture["image_base64"],
            "image_url": capture["image_url"],
            "bytes": capture["bytes"],
            "format": capture["format"],
            "full_page": capture["full_page"],
            "display_width": capture["display_width"],
            "display_height": capture["display_height"],
            "current_url": capture["current_url"],
            "title": capture["title"],
        }
    return result


def get_computer_use_tooling() -> tuple[dict[str, Any], dict[str, str], dict[str, dict[str, Any]]]:
    region_schema = {
        "type": "object",
        "properties": {
            "x": {"type": "number"},
            "y": {"type": "number"},
            "width": {"type": "number", "minimum": 1},
            "height": {"type": "number", "minimum": 1},
            "left": {"type": "number"},
            "top": {"type": "number"},
            "right": {"type": "number"},
            "bottom": {"type": "number"},
        },
        "additionalProperties": False,
    }
    safety_checks_schema = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "code": {"type": "string"},
                "message": {"type": "string"},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    }
    handlers = {
        "computer_use_session_start": tool_computer_use_session_start,
        "computer_use_session_stop": tool_computer_use_session_stop,
        "computer_use_request_consent": tool_computer_use_request_consent,
        "computer_use_revoke_consent": tool_computer_use_revoke_consent,
        "computer_use_execute": tool_computer_use_execute,
        "computer_use_screenshot": tool_computer_use_screenshot,
        "computer_use_ocr": tool_computer_use_ocr,
        "computer_use_find_text": tool_computer_use_find_text,
        "computer_use_manual_prompt": tool_computer_use_manual_prompt,
    }
    descriptions = {
        "computer_use_session_start": "Start an OpenAI computer-use style session. Defaults to native desktop control and also supports browser mode.",
        "computer_use_session_stop": "Stop a computer-use session and clear stored consent for that session.",
        "computer_use_request_consent": "Show a Chinese risk/consent popup before AI captures screenshots or performs desktop/browser computer-use actions.",
        "computer_use_revoke_consent": "Revoke previously granted computer-use consent for a session or the global scope.",
        "computer_use_execute": "Execute OpenAI computer-use actions against the native desktop or browser harness and optionally return a ready-to-send computer_call_output payload.",
        "computer_use_screenshot": "Capture a native desktop or browser screenshot for a computer-use loop after confirming Chinese risk consent.",
        "computer_use_ocr": "Capture a desktop/browser screenshot and run OCR to return recognized text boxes with coordinates.",
        "computer_use_find_text": "Run OCR on the current desktop/browser screenshot and locate matching text with click-ready coordinates.",
        "computer_use_manual_prompt": "Show a popup telling the user to manually complete a sensitive or non-automated step.",
    }
    schemas = {
        "computer_use_session_start": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "environment": {"type": "string", "enum": ["desktop", "browser"], "default": "desktop"},
                "browser": {"type": "string", "enum": ["chromium", "firefox", "webkit"], "default": "chromium"},
                "headless": {"type": "boolean", "default": False},
                "slow_mo_ms": {"type": "integer", "minimum": 0, "maximum": 10000, "default": 0},
                "viewport_width": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 1280},
                "viewport_height": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 720},
                "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 600000, "default": 30000},
                "url": {"type": "string"},
                "capture_initial_screenshot": {"type": "boolean", "default": False},
                "initial_full_page": {"type": "boolean", "default": True},
                "task": {"type": "string", "default": ""}
            },
            "additionalProperties": False
        },
        "computer_use_session_stop": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"}
            },
            "required": ["session_id"],
            "additionalProperties": False
        },
        "computer_use_request_consent": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "task": {"type": "string", "default": ""},
                "action_summary": {"type": "string", "default": "computer use 操作"},
                "pending_safety_checks": safety_checks_schema,
                "force_reconfirm": {"type": "boolean", "default": False}
            },
            "additionalProperties": False
        },
        "computer_use_revoke_consent": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"}
            },
            "additionalProperties": False
        },
        "computer_use_execute": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "computer_call": {"type": "object"},
                "call_id": {"type": "string"},
                "action": {"type": "object"},
                "actions": {"type": "array", "items": {"type": "object"}, "minItems": 1},
                "pending_safety_checks": safety_checks_schema,
                "task": {"type": "string", "default": ""},
                "force_reconfirm": {"type": "boolean", "default": False},
                "capture_after": {"type": "boolean", "default": False},
                "full_page": {"type": "boolean", "default": True},
                "wait_ms": {"type": "integer", "minimum": 0, "maximum": 600000, "default": 300}
            },
            "required": ["session_id"],
            "additionalProperties": False
        },
        "computer_use_screenshot": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "task": {"type": "string", "default": ""},
                "full_page": {"type": "boolean", "default": True},
                "force_reconfirm": {"type": "boolean", "default": False}
            },
            "required": ["session_id"],
            "additionalProperties": False
        },
        "computer_use_ocr": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "task": {"type": "string", "default": ""},
                "full_page": {"type": "boolean", "default": True},
                "force_reconfirm": {"type": "boolean", "default": False},
                "region": region_schema,
                "min_confidence": {"type": "number", "minimum": 0, "maximum": 1, "default": 0},
                "upscale_factor": {"type": "integer", "minimum": 1, "maximum": 4, "default": 2},
                "include_screenshot": {"type": "boolean", "default": False}
            },
            "required": ["session_id"],
            "additionalProperties": False
        },
        "computer_use_find_text": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "text": {"type": "string"},
                "task": {"type": "string", "default": ""},
                "full_page": {"type": "boolean", "default": True},
                "force_reconfirm": {"type": "boolean", "default": False},
                "region": region_schema,
                "match_mode": {"type": "string", "enum": ["contains", "exact", "starts_with"], "default": "contains"},
                "case_sensitive": {"type": "boolean", "default": False},
                "normalize_whitespace": {"type": "boolean", "default": True},
                "occurrence": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 1},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 200, "default": 10},
                "min_confidence": {"type": "number", "minimum": 0, "maximum": 1, "default": 0},
                "upscale_factor": {"type": "integer", "minimum": 1, "maximum": 4, "default": 2},
                "include_screenshot": {"type": "boolean", "default": False}
            },
            "required": ["session_id", "text"],
            "additionalProperties": False
        },
        "computer_use_manual_prompt": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "default": "需要你手动操作"},
                "prompt": {"type": "string"},
                "default": {"type": "string", "default": ""},
                "require_input": {"type": "boolean", "default": False}
            },
            "required": ["prompt"],
            "additionalProperties": False
        }
    }
    return handlers, descriptions, schemas
