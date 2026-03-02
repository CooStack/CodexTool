from __future__ import annotations

import base64
import json
import threading
import time
from typing import Any

from .common import as_bool, as_int, install_package_with_pip, require_str

_LOCK = threading.RLock()
_PLAYWRIGHT = None
_SESSIONS: dict[str, dict[str, Any]] = {}


def _safe_json_value(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except Exception:
        return str(value)


def _load_playwright_sync() -> Any:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        install_package_with_pip("playwright", "browser debugging")
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except Exception as e:
            raise RuntimeError(
                "Playwright is required for browser debugging. Install with: pip install playwright && playwright install"
            ) from e
    except Exception as e:
        raise RuntimeError(
            "Playwright is required for browser debugging. Install with: pip install playwright && playwright install"
        ) from e
    return sync_playwright, PlaywrightError, PlaywrightTimeoutError


def _get_playwright() -> Any:
    global _PLAYWRIGHT
    with _LOCK:
        if _PLAYWRIGHT is None:
            sync_playwright, _, _ = _load_playwright_sync()
            _PLAYWRIGHT = sync_playwright().start()
        return _PLAYWRIGHT


def _get_session(session_id: str) -> dict[str, Any]:
    with _LOCK:
        session = _SESSIONS.get(session_id)
    if session is None:
        raise ValueError(f"browser session not found: {session_id}")
    return session


def _ensure_browser_name(name: str) -> str:
    browser_name = name.strip().lower()
    if browser_name not in {"chromium", "firefox", "webkit"}:
        raise ValueError("`browser` must be one of: chromium, firefox, webkit")
    return browser_name


def _session_payload(session_id: str, session: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "browser": session.get("browser_name"),
        "headless": bool(session.get("headless", True)),
        "created_at": session.get("created_at"),
        "url": session.get("url") or "",
        "title": session.get("title") or "",
    }


def tool_browser_session_start(args: dict[str, Any]) -> dict[str, Any]:
    session_id_arg = args.get("session_id")
    if session_id_arg is not None and (not isinstance(session_id_arg, str) or not session_id_arg.strip()):
        raise ValueError("`session_id` must be a non-empty string when provided")

    session_id = session_id_arg.strip() if isinstance(session_id_arg, str) else f"browser-{int(time.time() * 1000)}"
    browser_name = _ensure_browser_name(str(args.get("browser", "chromium")))
    headless = as_bool(args.get("headless"), True)
    slow_mo_ms = as_int(args.get("slow_mo_ms"), 0, minimum=0, maximum=10000)
    viewport_width = as_int(args.get("viewport_width"), 1280, minimum=1, maximum=10000)
    viewport_height = as_int(args.get("viewport_height"), 720, minimum=1, maximum=10000)
    timeout_ms = as_int(args.get("timeout_ms"), 30000, minimum=1, maximum=600000)

    with _LOCK:
        if session_id in _SESSIONS:
            raise ValueError(f"browser session already exists: {session_id}")

    playwright = _get_playwright()
    launcher = getattr(playwright, browser_name)
    browser = launcher.launch(headless=headless, slow_mo=slow_mo_ms)
    context = browser.new_context(viewport={"width": viewport_width, "height": viewport_height})
    page = context.new_page()
    page.set_default_timeout(timeout_ms)

    start_url = args.get("url")
    if isinstance(start_url, str) and start_url.strip():
        page.goto(start_url.strip(), wait_until="domcontentloaded", timeout=timeout_ms)

    record = {
        "browser_name": browser_name,
        "headless": headless,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "browser": browser,
        "context": context,
        "page": page,
        "url": page.url,
        "title": page.title(),
    }

    with _LOCK:
        _SESSIONS[session_id] = record

    return {"started": True, **_session_payload(session_id, record)}


def tool_browser_session_stop(args: dict[str, Any]) -> dict[str, Any]:
    session_id = require_str(args, "session_id")

    with _LOCK:
        session = _SESSIONS.pop(session_id, None)

    if session is None:
        return {"session_id": session_id, "stopped": False, "reason": "not found"}

    page = session.get("page")
    context = session.get("context")
    browser = session.get("browser")

    for handle in (page, context, browser):
        try:
            if handle is not None:
                handle.close()
        except Exception:
            pass

    return {"session_id": session_id, "stopped": True}


def tool_browser_session_list(args: dict[str, Any]) -> dict[str, Any]:
    with _LOCK:
        payload = [_session_payload(session_id, session) for session_id, session in _SESSIONS.items()]
    return {"count": len(payload), "sessions": payload}


def tool_browser_navigate(args: dict[str, Any]) -> dict[str, Any]:
    session_id = require_str(args, "session_id")
    url = require_str(args, "url")
    wait_until = str(args.get("wait_until", "domcontentloaded")).strip().lower()
    if wait_until not in {"load", "domcontentloaded", "networkidle", "commit"}:
        raise ValueError("`wait_until` must be one of: load, domcontentloaded, networkidle, commit")
    timeout_ms = as_int(args.get("timeout_ms"), 30000, minimum=1, maximum=600000)

    session = _get_session(session_id)
    page = session["page"]
    response = page.goto(url, wait_until=wait_until, timeout=timeout_ms)
    status = response.status if response is not None else None

    session["url"] = page.url
    session["title"] = page.title()
    return {
        "session_id": session_id,
        "url": page.url,
        "title": session["title"],
        "status": status,
        "ok": bool(status is None or (200 <= status < 400)),
    }


def tool_browser_click(args: dict[str, Any]) -> dict[str, Any]:
    session_id = require_str(args, "session_id")
    selector = require_str(args, "selector")
    timeout_ms = as_int(args.get("timeout_ms"), 30000, minimum=1, maximum=600000)

    session = _get_session(session_id)
    page = session["page"]
    page.click(selector, timeout=timeout_ms)

    session["url"] = page.url
    session["title"] = page.title()
    return {"session_id": session_id, "selector": selector, "clicked": True, "url": page.url}


def tool_browser_type(args: dict[str, Any]) -> dict[str, Any]:
    session_id = require_str(args, "session_id")
    selector = require_str(args, "selector")
    text = require_str(args, "text")
    clear_first = as_bool(args.get("clear_first"), False)
    timeout_ms = as_int(args.get("timeout_ms"), 30000, minimum=1, maximum=600000)

    session = _get_session(session_id)
    page = session["page"]

    if clear_first:
        page.fill(selector, "", timeout=timeout_ms)
    page.fill(selector, text, timeout=timeout_ms)

    return {
        "session_id": session_id,
        "selector": selector,
        "typed_chars": len(text),
        "clear_first": clear_first,
    }


def tool_browser_eval(args: dict[str, Any]) -> dict[str, Any]:
    session_id = require_str(args, "session_id")
    expression = require_str(args, "expression")

    session = _get_session(session_id)
    page = session["page"]
    arg = args.get("arg")

    value = page.evaluate(expression, arg)
    safe = _safe_json_value(value)
    return {"session_id": session_id, "value": safe}


def tool_browser_capture(args: dict[str, Any]) -> dict[str, Any]:
    session_id = require_str(args, "session_id")
    full_page = as_bool(args.get("full_page"), True)

    session = _get_session(session_id)
    page = session["page"]
    raw = page.screenshot(full_page=full_page)

    return {
        "session_id": session_id,
        "image_base64": base64.b64encode(raw).decode("ascii"),
        "bytes": len(raw),
        "format": "png",
        "full_page": full_page,
    }


def tool_browser_snapshot(args: dict[str, Any]) -> dict[str, Any]:
    session_id = require_str(args, "session_id")
    max_html_chars = as_int(args.get("max_html_chars"), 4000, minimum=0, maximum=200000)
    max_text_chars = as_int(args.get("max_text_chars"), 2000, minimum=0, maximum=200000)

    session = _get_session(session_id)
    page = session["page"]

    title = page.title()
    html = page.content()
    text = ""
    try:
        text = page.inner_text("body")
    except Exception:
        text = ""

    session["url"] = page.url
    session["title"] = title

    if max_html_chars > 0:
        html = html[:max_html_chars]
    else:
        html = ""

    if max_text_chars > 0:
        text = text[:max_text_chars]
    else:
        text = ""

    return {
        "session_id": session_id,
        "url": page.url,
        "title": title,
        "html": html,
        "text": text,
        "html_length": len(html),
        "text_length": len(text),
    }


def get_browser_tooling() -> tuple[dict[str, Any], dict[str, str], dict[str, dict[str, Any]]]:
    handlers = {
        "browser_session_start": tool_browser_session_start,
        "browser_session_stop": tool_browser_session_stop,
        "browser_session_list": tool_browser_session_list,
        "browser_navigate": tool_browser_navigate,
        "browser_click": tool_browser_click,
        "browser_type": tool_browser_type,
        "browser_eval": tool_browser_eval,
        "browser_capture": tool_browser_capture,
        "browser_snapshot": tool_browser_snapshot,
    }

    descriptions = {
        "browser_session_start": "Start a Playwright browser debugging session.",
        "browser_session_stop": "Stop and dispose a browser debugging session.",
        "browser_session_list": "List active browser debugging sessions.",
        "browser_navigate": "Navigate browser page to target URL.",
        "browser_click": "Click element on page using selector.",
        "browser_type": "Fill text into an element by selector.",
        "browser_eval": "Evaluate JavaScript in browser page context.",
        "browser_capture": "Capture page screenshot and return PNG base64.",
        "browser_snapshot": "Get page URL/title plus HTML and text snapshot.",
    }

    schemas = {
        "browser_session_start": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "browser": {"type": "string", "enum": ["chromium", "firefox", "webkit"], "default": "chromium"},
                "headless": {"type": "boolean", "default": True},
                "slow_mo_ms": {"type": "integer", "minimum": 0, "maximum": 10000, "default": 0},
                "viewport_width": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 1280},
                "viewport_height": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 720},
                "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 600000, "default": 30000},
                "url": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "browser_session_stop": {
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
            "additionalProperties": False,
        },
        "browser_session_list": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "browser_navigate": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "url": {"type": "string"},
                "wait_until": {"type": "string", "enum": ["load", "domcontentloaded", "networkidle", "commit"], "default": "domcontentloaded"},
                "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 600000, "default": 30000},
            },
            "required": ["session_id", "url"],
            "additionalProperties": False,
        },
        "browser_click": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "selector": {"type": "string"},
                "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 600000, "default": 30000},
            },
            "required": ["session_id", "selector"],
            "additionalProperties": False,
        },
        "browser_type": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "selector": {"type": "string"},
                "text": {"type": "string"},
                "clear_first": {"type": "boolean", "default": False},
                "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 600000, "default": 30000},
            },
            "required": ["session_id", "selector", "text"],
            "additionalProperties": False,
        },
        "browser_eval": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "expression": {"type": "string"},
                "arg": {},
            },
            "required": ["session_id", "expression"],
            "additionalProperties": False,
        },
        "browser_capture": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "full_page": {"type": "boolean", "default": True},
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
        "browser_snapshot": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "max_html_chars": {"type": "integer", "minimum": 0, "maximum": 200000, "default": 4000},
                "max_text_chars": {"type": "integer", "minimum": 0, "maximum": 200000, "default": 2000},
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    }

    return handlers, descriptions, schemas
