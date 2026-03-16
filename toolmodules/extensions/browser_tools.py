from __future__ import annotations

import base64
import json
import random
import threading
import time
from typing import Any
from urllib.parse import quote_plus

from .common import as_bool, as_int, install_package_with_pip, require_str

_LOCK = threading.RLock()
_PLAYWRIGHT = None
_SESSIONS: dict[str, dict[str, Any]] = {}

DEFAULT_HEADLESS = False
DEFAULT_SLOW_MO_MS = 120
DEFAULT_VIEWPORT_WIDTH = 1440
DEFAULT_VIEWPORT_HEIGHT = 900
DEFAULT_TIMEOUT_MS = 30000
DEFAULT_HUMAN_LIKE = True
DEFAULT_BRING_TO_FRONT = True
DEFAULT_PREFER_REAL_CHROME = True
DEFAULT_CHROME_CHANNEL = "chrome"
DEFAULT_PRE_ACTION_DELAY_MS = 120
DEFAULT_POST_ACTION_DELAY_MS = 180
DEFAULT_TYPING_DELAY_MS = 90
DEFAULT_MOUSE_MOVE_STEPS = 24
DEFAULT_MOUSE_JITTER_PX = 4
DEFAULT_CDP_URL = "http://127.0.0.1:9222"
DEFAULT_BROWSER_SNAPSHOT_MAX_HTML_CHARS = 1500
DEFAULT_BROWSER_SNAPSHOT_MAX_TEXT_CHARS = 1500
DEFAULT_BROWSER_READ_ACTIVE_MAX_HTML_CHARS = 1500
DEFAULT_BROWSER_READ_ACTIVE_MAX_TEXT_CHARS = 2000

SEARCH_ENGINE_PRESETS: dict[str, dict[str, Any]] = {
    "bing": {
        "home_url": "https://www.bing.com/",
        "search_url": "https://www.bing.com/search?q=",
        "input_selectors": ["textarea[name='q']", "input[name='q']", "#sb_form_q"],
    },
    "duckduckgo": {
        "home_url": "https://duckduckgo.com/",
        "search_url": "https://duckduckgo.com/?q=",
        "input_selectors": ["input[name='q']", "#searchbox_input"],
    },
    "google": {
        "home_url": "https://www.google.com/",
        "search_url": "https://www.google.com/search?q=",
        "input_selectors": ["textarea[name='q']", "input[name='q']"],
    },
}

AI_PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "perplexity": {
        "url": "https://www.perplexity.ai/",
        "input_selectors": ["textarea", "div[contenteditable='true']", "input[type='text']"],
        "submit_selectors": ["button[type='submit']", "button[aria-label*='Submit']"],
    },
    "chatgpt": {
        "url": "https://chatgpt.com/",
        "input_selectors": ["#prompt-textarea", "textarea", "div[contenteditable='true']"],
        "submit_selectors": ["button[data-testid='send-button']", "button[aria-label*='Send']", "button[type='submit']"],
    },
    "claude": {
        "url": "https://claude.ai/new",
        "input_selectors": ["div[contenteditable='true']", "textarea"],
        "submit_selectors": ["button[aria-label*='Send']", "button[type='submit']"],
    },
    "gemini": {
        "url": "https://gemini.google.com/app",
        "input_selectors": ["rich-textarea div[contenteditable='true']", "div[contenteditable='true']", "textarea"],
        "submit_selectors": ["button[aria-label*='Send']", "button[type='submit']"],
    },
    "deepseek": {
        "url": "https://chat.deepseek.com/",
        "input_selectors": ["textarea", "div[contenteditable='true']"],
        "submit_selectors": ["button[type='submit']", "button[aria-label*='Send']"],
    },
}

LOGIN_HINTS = [
    "登录",
    "登入",
    "sign in",
    "log in",
    "继续使用",
    "continue with",
    "continue with google",
    "continue with email",
]


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


def _optional_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _normalize_session_id(value: Any, prefix: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return f"{prefix}-{int(time.time() * 1000)}"


def _optional_str_list(value: Any, key: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"`{key}` must be an array of strings")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"`{key}[{index}]` must be a non-empty string")
        result.append(item.strip())
    return result


def _session_bool(session: dict[str, Any], args: dict[str, Any], key: str, default: bool) -> bool:
    if key in args:
        return as_bool(args.get(key), default)
    return as_bool(session.get(key), default)


def _session_int(
    session: dict[str, Any],
    args: dict[str, Any],
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if key in args:
        return as_int(args.get(key), default, minimum=minimum, maximum=maximum)
    return as_int(session.get(key), default, minimum=minimum, maximum=maximum)


def _sleep_ms(delay_ms: int) -> None:
    if delay_ms > 0:
        time.sleep(delay_ms / 1000.0)


def _sleep_with_jitter(
    session: dict[str, Any],
    args: dict[str, Any],
    key: str,
    default: int,
    minimum: int,
    maximum: int,
    jitter_ms: int,
) -> int:
    base_delay = _session_int(session, args, key, default, minimum=minimum, maximum=maximum)
    if base_delay <= 0:
        return 0
    rng = session["rng"]
    jitter = rng.randint(0, jitter_ms) if jitter_ms > 0 else 0
    actual_delay = min(maximum, base_delay + jitter)
    _sleep_ms(actual_delay)
    return actual_delay


def _bring_page_to_front(session: dict[str, Any], args: dict[str, Any] | None = None) -> bool:
    effective_args = args or {}
    should_bring = _session_bool(session, effective_args, "bring_to_front", DEFAULT_BRING_TO_FRONT)
    if not should_bring or bool(session.get("headless", DEFAULT_HEADLESS)):
        return False
    page = session["page"]
    try:
        page.bring_to_front()
        return True
    except Exception:
        return False


def _update_session_page_state(session: dict[str, Any]) -> None:
    page = session["page"]
    session["url"] = page.url
    try:
        session["title"] = page.title()
    except Exception:
        session["title"] = ""


def _get_frame_title(frame: Any) -> str:
    try:
        value = frame.evaluate("() => document.title || ''")
        return value if isinstance(value, str) else ""
    except Exception:
        return ""


def _frame_meta(page: Any, frame: Any, frame_index: int) -> dict[str, Any]:
    return {
        "frame_index": frame_index,
        "frame_name": frame.name or "",
        "frame_url": frame.url or "",
        "frame_title": _get_frame_title(frame),
        "is_main_frame": frame == page.main_frame,
    }


def _resolve_frame(session: dict[str, Any], args: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    page = session["page"]
    frames = list(page.frames)
    if not frames:
        raise ValueError("no frames available in current page")

    frame_index_arg = args.get("frame_index")
    frame_name = _optional_str(args.get("frame_name"))
    frame_url_contains = _optional_str(args.get("frame_url_contains"))

    if frame_index_arg is not None:
        frame_index = as_int(frame_index_arg, 0, minimum=0, maximum=max(0, len(frames) - 1))
        frame = frames[frame_index]
        return frame, _frame_meta(page, frame, frame_index)

    if frame_name is None and frame_url_contains is None:
        main_frame = page.main_frame
        for index, frame in enumerate(frames):
            if frame == main_frame:
                return frame, _frame_meta(page, frame, index)
        return frames[0], _frame_meta(page, frames[0], 0)

    for index, frame in enumerate(frames):
        if frame_name is not None and (frame.name or "") != frame_name:
            continue
        if frame_url_contains is not None and frame_url_contains not in (frame.url or ""):
            continue
        return frame, _frame_meta(page, frame, index)

    selectors = []
    if frame_name is not None:
        selectors.append(f"frame_name={frame_name}")
    if frame_url_contains is not None:
        selectors.append(f"frame_url_contains={frame_url_contains}")
    raise ValueError(f"matching frame not found: {', '.join(selectors)}")


def _frame_text(frame: Any, timeout_ms: int) -> str:
    try:
        return frame.locator("body").inner_text(timeout=timeout_ms)
    except Exception:
        return ""


def _frame_list_payload(page: Any) -> list[dict[str, Any]]:
    return [_frame_meta(page, frame, index) for index, frame in enumerate(page.frames)]


def _resolve_locator(session: dict[str, Any], args: dict[str, Any], selector: str, timeout_ms: int) -> tuple[Any, Any, dict[str, Any]]:
    frame, frame_meta = _resolve_frame(session, args)
    locator = frame.locator(selector).first
    locator.wait_for(state="visible", timeout=timeout_ms)
    try:
        locator.scroll_into_view_if_needed(timeout=timeout_ms)
    except Exception:
        pass
    box = locator.bounding_box()
    return locator, box, frame_meta


def _find_first_visible_selector(session: dict[str, Any], args: dict[str, Any], selectors: list[str], timeout_ms: int) -> tuple[str, dict[str, Any]]:
    frame, frame_meta = _resolve_frame(session, args)
    probe_timeout = max(250, min(timeout_ms, 1500))
    failures: list[str] = []
    for selector in selectors:
        try:
            locator = frame.locator(selector).first
            locator.wait_for(state="visible", timeout=probe_timeout)
            return selector, frame_meta
        except Exception as exc:
            failures.append(f"{selector}: {exc}")
    raise ValueError(f"no visible selector matched. tried: {', '.join(selectors)}")


def _match_page(page: Any, url_contains: str | None, title_contains: str | None) -> bool:
    if url_contains is not None and url_contains not in (page.url or ""):
        return False
    if title_contains is not None:
        try:
            title = page.title() or ""
        except Exception:
            title = ""
        if title_contains not in title:
            return False
    return True


def _pick_page_from_context(
    context: Any,
    *,
    page_index: Any,
    url_contains: str | None,
    title_contains: str | None,
    create_page_if_missing: bool,
) -> Any:
    pages = list(context.pages)
    if page_index is not None:
        if not pages:
            raise ValueError("selected CDP context has no pages")
        normalized_index = as_int(page_index, 0, minimum=0, maximum=len(pages) - 1)
        return pages[normalized_index]

    for page in reversed(pages):
        if _match_page(page, url_contains, title_contains):
            return page

    for page in reversed(pages):
        url = (page.url or "").strip()
        if url and url != "about:blank":
            return page

    if pages:
        return pages[-1]
    if create_page_if_missing:
        return context.new_page()
    raise ValueError("selected CDP context has no pages")


def _search_preset(engine: str) -> dict[str, Any]:
    normalized = engine.strip().lower()
    preset = SEARCH_ENGINE_PRESETS.get(normalized)
    if preset is None:
        allowed = ", ".join(sorted(SEARCH_ENGINE_PRESETS))
        raise ValueError(f"`engine` must be one of: {allowed}")
    return preset


def _ai_preset(provider: str) -> dict[str, Any]:
    normalized = provider.strip().lower()
    preset = AI_PROVIDER_PRESETS.get(normalized)
    if preset is None:
        allowed = ", ".join(sorted(AI_PROVIDER_PRESETS))
        raise ValueError(f"`provider` must be one of: {allowed}")
    return preset


def _looks_like_login_page(text: str) -> bool:
    lowered = text.strip().lower()
    if not lowered:
        return False
    return any(hint in lowered for hint in LOGIN_HINTS)


def _human_move_to_selector(
    session: dict[str, Any],
    args: dict[str, Any],
    selector: str,
    timeout_ms: int,
) -> tuple[Any, dict[str, float] | None, int, dict[str, Any]]:
    locator, box, frame_meta = _resolve_locator(session, args, selector, timeout_ms)
    if box is None:
        return locator, None, 0, frame_meta

    rng = session["rng"]
    page = session["page"]
    mouse_move_steps = _session_int(
        session,
        args,
        "mouse_move_steps",
        DEFAULT_MOUSE_MOVE_STEPS,
        minimum=1,
        maximum=120,
    )
    mouse_jitter_px = _session_int(
        session,
        args,
        "mouse_jitter_px",
        DEFAULT_MOUSE_JITTER_PX,
        minimum=0,
        maximum=50,
    )

    target_x = box["x"] + (box["width"] / 2)
    target_y = box["y"] + (box["height"] / 2)
    if mouse_jitter_px > 0:
        target_x += rng.uniform(-mouse_jitter_px, mouse_jitter_px)
        target_y += rng.uniform(-mouse_jitter_px, mouse_jitter_px)

    distance_hint = max(abs(target_x - session.get("mouse_x", target_x)), abs(target_y - session.get("mouse_y", target_y)))
    effective_steps = max(mouse_move_steps, min(60, int(distance_hint / 20) + 8))
    page.mouse.move(target_x, target_y, steps=effective_steps)
    session["mouse_x"] = target_x
    session["mouse_y"] = target_y
    return locator, {"x": round(target_x, 2), "y": round(target_y, 2)}, effective_steps, frame_meta


def _human_click_selector(
    session: dict[str, Any],
    args: dict[str, Any],
    selector: str,
    timeout_ms: int,
) -> dict[str, Any]:
    human_like = _session_bool(session, args, "human_like", DEFAULT_HUMAN_LIKE)
    page = session["page"]
    frame, frame_meta = _resolve_frame(session, args)

    _bring_page_to_front(session, args)

    if not human_like:
        frame.locator(selector).first.click(timeout=timeout_ms)
        _update_session_page_state(session)
        return {**frame_meta, "human_like": False, "pre_action_delay_ms": 0, "post_action_delay_ms": 0}

    pre_delay = _sleep_with_jitter(
        session,
        args,
        "pre_action_delay_ms",
        DEFAULT_PRE_ACTION_DELAY_MS,
        minimum=0,
        maximum=5000,
        jitter_ms=40,
    )
    locator, mouse_target, effective_steps, move_frame_meta = _human_move_to_selector(session, args, selector, timeout_ms)

    if mouse_target is None:
        locator.click(timeout=timeout_ms)
    else:
        _sleep_with_jitter(
            session,
            args,
            "pre_action_delay_ms",
            DEFAULT_PRE_ACTION_DELAY_MS,
            minimum=0,
            maximum=5000,
            jitter_ms=20,
        )
        page.mouse.down()
        _sleep_ms(session["rng"].randint(35, 110))
        page.mouse.up()

    post_delay = _sleep_with_jitter(
        session,
        args,
        "post_action_delay_ms",
        DEFAULT_POST_ACTION_DELAY_MS,
        minimum=0,
        maximum=5000,
        jitter_ms=60,
    )
    _update_session_page_state(session)
    return {
        **move_frame_meta,
        "human_like": True,
        "mouse_target": mouse_target,
        "mouse_move_steps": effective_steps,
        "pre_action_delay_ms": pre_delay,
        "post_action_delay_ms": post_delay,
    }


def _human_type_selector(
    session: dict[str, Any],
    args: dict[str, Any],
    selector: str,
    text: str,
    timeout_ms: int,
) -> dict[str, Any]:
    human_like = _session_bool(session, args, "human_like", DEFAULT_HUMAN_LIKE)
    page = session["page"]
    frame, frame_meta = _resolve_frame(session, args)

    _bring_page_to_front(session, args)

    if not human_like:
        frame.locator(selector).first.fill(text, timeout=timeout_ms)
        _update_session_page_state(session)
        return {
            **frame_meta,
            "human_like": False,
            "typing_delay_ms": 0,
            "pre_action_delay_ms": 0,
            "post_action_delay_ms": 0,
        }

    pre_delay = _sleep_with_jitter(
        session,
        args,
        "pre_action_delay_ms",
        DEFAULT_PRE_ACTION_DELAY_MS,
        minimum=0,
        maximum=5000,
        jitter_ms=40,
    )
    locator, mouse_target, effective_steps, move_frame_meta = _human_move_to_selector(session, args, selector, timeout_ms)

    if mouse_target is None:
        locator.click(timeout=timeout_ms)
    else:
        page.mouse.down()
        _sleep_ms(session["rng"].randint(35, 110))
        page.mouse.up()

    try:
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
    except Exception:
        try:
            locator.fill("", timeout=timeout_ms)
        except Exception:
            frame.locator(selector).first.fill("", timeout=timeout_ms)

    typing_delay_ms = _session_int(
        session,
        args,
        "typing_delay_ms",
        DEFAULT_TYPING_DELAY_MS,
        minimum=0,
        maximum=2000,
    )
    if text:
        page.keyboard.type(text, delay=typing_delay_ms)

    post_delay = _sleep_with_jitter(
        session,
        args,
        "post_action_delay_ms",
        DEFAULT_POST_ACTION_DELAY_MS,
        minimum=0,
        maximum=5000,
        jitter_ms=60,
    )
    _update_session_page_state(session)
    return {
        **move_frame_meta,
        "human_like": True,
        "mouse_target": mouse_target,
        "mouse_move_steps": effective_steps,
        "typing_delay_ms": typing_delay_ms,
        "pre_action_delay_ms": pre_delay,
        "post_action_delay_ms": post_delay,
    }


def _session_payload(session_id: str, session: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "browser": session.get("browser_name"),
        "headless": bool(session.get("headless", DEFAULT_HEADLESS)),
        "browser_visible": not bool(session.get("headless", DEFAULT_HEADLESS)),
        "created_at": session.get("created_at"),
        "url": session.get("url") or "",
        "title": session.get("title") or "",
        "slow_mo_ms": int(session.get("slow_mo_ms", DEFAULT_SLOW_MO_MS)),
        "human_like": bool(session.get("human_like", DEFAULT_HUMAN_LIKE)),
        "bring_to_front": bool(session.get("bring_to_front", DEFAULT_BRING_TO_FRONT)),
        "typing_delay_ms": int(session.get("typing_delay_ms", DEFAULT_TYPING_DELAY_MS)),
        "pre_action_delay_ms": int(session.get("pre_action_delay_ms", DEFAULT_PRE_ACTION_DELAY_MS)),
        "post_action_delay_ms": int(session.get("post_action_delay_ms", DEFAULT_POST_ACTION_DELAY_MS)),
        "mouse_move_steps": int(session.get("mouse_move_steps", DEFAULT_MOUSE_MOVE_STEPS)),
        "mouse_jitter_px": int(session.get("mouse_jitter_px", DEFAULT_MOUSE_JITTER_PX)),
        "prefer_real_chrome": bool(session.get("prefer_real_chrome", DEFAULT_PREFER_REAL_CHROME)),
        "chrome_channel_requested": session.get("chrome_channel_requested") or "",
        "chrome_channel": session.get("chrome_channel") or "",
        "chrome_channel_fallback": session.get("chrome_channel_fallback") or "",
        "attached_via_cdp": bool(session.get("attached_via_cdp", False)),
        "cdp_url": session.get("cdp_url") or "",
    }


def tool_browser_session_start(args: dict[str, Any]) -> dict[str, Any]:
    session_id_arg = args.get("session_id")
    if session_id_arg is not None and (not isinstance(session_id_arg, str) or not session_id_arg.strip()):
        raise ValueError("`session_id` must be a non-empty string when provided")

    session_id = session_id_arg.strip() if isinstance(session_id_arg, str) else f"browser-{int(time.time() * 1000)}"
    browser_name = _ensure_browser_name(str(args.get("browser", "chromium")))
    headless = as_bool(args.get("headless"), DEFAULT_HEADLESS)
    slow_mo_ms = as_int(args.get("slow_mo_ms"), DEFAULT_SLOW_MO_MS, minimum=0, maximum=10000)
    viewport_width = as_int(args.get("viewport_width"), DEFAULT_VIEWPORT_WIDTH, minimum=1, maximum=10000)
    viewport_height = as_int(args.get("viewport_height"), DEFAULT_VIEWPORT_HEIGHT, minimum=1, maximum=10000)
    timeout_ms = as_int(args.get("timeout_ms"), DEFAULT_TIMEOUT_MS, minimum=1, maximum=600000)
    human_like = as_bool(args.get("human_like"), DEFAULT_HUMAN_LIKE)
    bring_to_front = as_bool(args.get("bring_to_front"), DEFAULT_BRING_TO_FRONT)
    prefer_real_chrome = as_bool(args.get("prefer_real_chrome"), DEFAULT_PREFER_REAL_CHROME)
    pre_action_delay_ms = as_int(
        args.get("pre_action_delay_ms"),
        DEFAULT_PRE_ACTION_DELAY_MS,
        minimum=0,
        maximum=5000,
    )
    post_action_delay_ms = as_int(
        args.get("post_action_delay_ms"),
        DEFAULT_POST_ACTION_DELAY_MS,
        minimum=0,
        maximum=5000,
    )
    typing_delay_ms = as_int(args.get("typing_delay_ms"), DEFAULT_TYPING_DELAY_MS, minimum=0, maximum=2000)
    mouse_move_steps = as_int(args.get("mouse_move_steps"), DEFAULT_MOUSE_MOVE_STEPS, minimum=1, maximum=120)
    mouse_jitter_px = as_int(args.get("mouse_jitter_px"), DEFAULT_MOUSE_JITTER_PX, minimum=0, maximum=50)
    chrome_channel_requested = _optional_str(args.get("chrome_channel"))
    if chrome_channel_requested is None and browser_name == "chromium" and prefer_real_chrome:
        chrome_channel_requested = DEFAULT_CHROME_CHANNEL

    with _LOCK:
        if session_id in _SESSIONS:
            raise ValueError(f"browser session already exists: {session_id}")

    playwright = _get_playwright()
    _, PlaywrightError, _ = _load_playwright_sync()
    launcher = getattr(playwright, browser_name)

    launch_kwargs: dict[str, Any] = {"headless": headless, "slow_mo": slow_mo_ms}
    if browser_name == "chromium" and not headless:
        launch_kwargs["args"] = [f"--window-size={viewport_width},{viewport_height}"]

    chrome_channel = ""
    chrome_channel_fallback = ""
    if browser_name == "chromium" and prefer_real_chrome and chrome_channel_requested:
        try:
            browser = launcher.launch(channel=chrome_channel_requested, **launch_kwargs)
            chrome_channel = chrome_channel_requested
        except PlaywrightError as e:
            browser = launcher.launch(**launch_kwargs)
            chrome_channel_fallback = str(e)
    else:
        browser = launcher.launch(**launch_kwargs)

    context = browser.new_context(viewport={"width": viewport_width, "height": viewport_height})
    page = context.new_page()
    page.set_default_timeout(timeout_ms)

    record = {
        "browser_name": browser_name,
        "headless": headless,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "browser": browser,
        "context": context,
        "page": page,
        "url": page.url,
        "title": page.title(),
        "slow_mo_ms": slow_mo_ms,
        "human_like": human_like,
        "bring_to_front": bring_to_front,
        "typing_delay_ms": typing_delay_ms,
        "pre_action_delay_ms": pre_action_delay_ms,
        "post_action_delay_ms": post_action_delay_ms,
        "mouse_move_steps": mouse_move_steps,
        "mouse_jitter_px": mouse_jitter_px,
        "prefer_real_chrome": prefer_real_chrome,
        "chrome_channel_requested": chrome_channel_requested or "",
        "chrome_channel": chrome_channel,
        "chrome_channel_fallback": chrome_channel_fallback,
        "rng": random.Random(time.time_ns()),
        "mouse_x": viewport_width / 2,
        "mouse_y": viewport_height / 2,
    }

    _bring_page_to_front(record)

    start_url = args.get("url")
    if isinstance(start_url, str) and start_url.strip():
        page.goto(start_url.strip(), wait_until="domcontentloaded", timeout=timeout_ms)
        _sleep_with_jitter(
            record,
            {},
            "post_action_delay_ms",
            DEFAULT_POST_ACTION_DELAY_MS,
            minimum=0,
            maximum=5000,
            jitter_ms=80,
        )

    _update_session_page_state(record)

    with _LOCK:
        _SESSIONS[session_id] = record

    return {"started": True, **_session_payload(session_id, record)}


def tool_browser_session_attach(args: dict[str, Any]) -> dict[str, Any]:
    session_id = _normalize_session_id(args.get("session_id"), "browser")
    browser_name = _ensure_browser_name(str(args.get("browser", "chromium")))
    if browser_name != "chromium":
        raise ValueError("`browser_session_attach` currently supports only chromium-based browsers via CDP")

    timeout_ms = as_int(args.get("timeout_ms"), DEFAULT_TIMEOUT_MS, minimum=1, maximum=600000)
    cdp_url = _optional_str(args.get("cdp_url")) or DEFAULT_CDP_URL
    url_contains = _optional_str(args.get("url_contains"))
    title_contains = _optional_str(args.get("title_contains"))
    context_index = as_int(args.get("context_index"), 0, minimum=0, maximum=9999)
    page_index = args.get("page_index")
    create_page_if_missing = as_bool(args.get("create_page_if_missing"), True)
    bring_to_front = as_bool(args.get("bring_to_front"), DEFAULT_BRING_TO_FRONT)
    human_like = as_bool(args.get("human_like"), DEFAULT_HUMAN_LIKE)
    slow_mo_ms = as_int(args.get("slow_mo_ms"), DEFAULT_SLOW_MO_MS, minimum=0, maximum=10000)
    pre_action_delay_ms = as_int(args.get("pre_action_delay_ms"), DEFAULT_PRE_ACTION_DELAY_MS, minimum=0, maximum=5000)
    post_action_delay_ms = as_int(args.get("post_action_delay_ms"), DEFAULT_POST_ACTION_DELAY_MS, minimum=0, maximum=5000)
    typing_delay_ms = as_int(args.get("typing_delay_ms"), DEFAULT_TYPING_DELAY_MS, minimum=0, maximum=2000)
    mouse_move_steps = as_int(args.get("mouse_move_steps"), DEFAULT_MOUSE_MOVE_STEPS, minimum=1, maximum=120)
    mouse_jitter_px = as_int(args.get("mouse_jitter_px"), DEFAULT_MOUSE_JITTER_PX, minimum=0, maximum=50)

    with _LOCK:
        if session_id in _SESSIONS:
            raise ValueError(f"browser session already exists: {session_id}")

    playwright = _get_playwright()
    _, PlaywrightError, _ = _load_playwright_sync()
    try:
        browser = playwright.chromium.connect_over_cdp(cdp_url, timeout=timeout_ms)
    except PlaywrightError as exc:
        raise RuntimeError(
            f"failed to attach to existing browser via CDP at {cdp_url}. Start Chrome/Edge with --remote-debugging-port=9222 and retry"
        ) from exc

    contexts = list(browser.contexts)
    if not contexts:
        raise RuntimeError(f"no browser contexts found via CDP at {cdp_url}")
    selected_context_index = as_int(context_index, 0, minimum=0, maximum=len(contexts) - 1)
    context = contexts[selected_context_index]
    page = _pick_page_from_context(
        context,
        page_index=page_index,
        url_contains=url_contains,
        title_contains=title_contains,
        create_page_if_missing=create_page_if_missing,
    )
    page.set_default_timeout(timeout_ms)

    record = {
        "browser_name": browser_name,
        "headless": False,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "browser": browser,
        "context": context,
        "page": page,
        "url": page.url,
        "title": "",
        "slow_mo_ms": slow_mo_ms,
        "human_like": human_like,
        "bring_to_front": bring_to_front,
        "typing_delay_ms": typing_delay_ms,
        "pre_action_delay_ms": pre_action_delay_ms,
        "post_action_delay_ms": post_action_delay_ms,
        "mouse_move_steps": mouse_move_steps,
        "mouse_jitter_px": mouse_jitter_px,
        "prefer_real_chrome": True,
        "chrome_channel_requested": "",
        "chrome_channel": "attached-cdp",
        "chrome_channel_fallback": "",
        "attached_via_cdp": True,
        "cdp_url": cdp_url,
        "rng": random.Random(time.time_ns()),
        "mouse_x": DEFAULT_VIEWPORT_WIDTH / 2,
        "mouse_y": DEFAULT_VIEWPORT_HEIGHT / 2,
    }

    _bring_page_to_front(record)
    _update_session_page_state(record)

    with _LOCK:
        _SESSIONS[session_id] = record

    payload = _session_payload(session_id, record)
    payload.update(
        {
            "attached": True,
            "context_index": selected_context_index,
            "page_count": len(context.pages),
        }
    )
    return payload


def tool_browser_session_stop(args: dict[str, Any]) -> dict[str, Any]:
    session_id = require_str(args, "session_id")

    with _LOCK:
        session = _SESSIONS.pop(session_id, None)

    if session is None:
        return {"session_id": session_id, "stopped": False, "reason": "not found"}

    if bool(session.get("attached_via_cdp", False)):
        return {"session_id": session_id, "stopped": True, "detached": True}

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
    _ = args
    with _LOCK:
        payload = [_session_payload(session_id, session) for session_id, session in _SESSIONS.items()]
    return {"count": len(payload), "sessions": payload}


def tool_browser_frame_list(args: dict[str, Any]) -> dict[str, Any]:
    session_id = require_str(args, "session_id")
    session = _get_session(session_id)
    page = session["page"]
    frames = _frame_list_payload(page)
    return {"session_id": session_id, "count": len(frames), "frames": frames}


def tool_browser_navigate(args: dict[str, Any]) -> dict[str, Any]:
    session_id = require_str(args, "session_id")
    url = require_str(args, "url")
    wait_until = str(args.get("wait_until", "domcontentloaded")).strip().lower()
    if wait_until not in {"load", "domcontentloaded", "networkidle", "commit"}:
        raise ValueError("`wait_until` must be one of: load, domcontentloaded, networkidle, commit")
    timeout_ms = as_int(args.get("timeout_ms"), DEFAULT_TIMEOUT_MS, minimum=1, maximum=600000)

    session = _get_session(session_id)
    _bring_page_to_front(session)
    page = session["page"]
    response = page.goto(url, wait_until=wait_until, timeout=timeout_ms)
    status = response.status if response is not None else None
    post_delay = _sleep_with_jitter(
        session,
        {},
        "post_action_delay_ms",
        DEFAULT_POST_ACTION_DELAY_MS,
        minimum=0,
        maximum=5000,
        jitter_ms=80,
    )

    _update_session_page_state(session)
    return {
        "session_id": session_id,
        "url": page.url,
        "title": session["title"],
        "status": status,
        "ok": bool(status is None or (200 <= status < 400)),
        "post_action_delay_ms": post_delay,
    }


def tool_browser_press_key(args: dict[str, Any]) -> dict[str, Any]:
    session_id = require_str(args, "session_id")
    key = require_str(args, "key")
    session = _get_session(session_id)
    page = session["page"]

    _bring_page_to_front(session, args)
    pre_delay = _sleep_with_jitter(
        session,
        args,
        "pre_action_delay_ms",
        DEFAULT_PRE_ACTION_DELAY_MS,
        minimum=0,
        maximum=5000,
        jitter_ms=30,
    )
    page.keyboard.press(key)
    post_delay = _sleep_with_jitter(
        session,
        args,
        "post_action_delay_ms",
        DEFAULT_POST_ACTION_DELAY_MS,
        minimum=0,
        maximum=5000,
        jitter_ms=40,
    )
    _update_session_page_state(session)
    return {
        "session_id": session_id,
        "key": key,
        "pressed": True,
        "url": session.get("url") or "",
        "title": session.get("title") or "",
        "pre_action_delay_ms": pre_delay,
        "post_action_delay_ms": post_delay,
    }


def tool_browser_click(args: dict[str, Any]) -> dict[str, Any]:
    session_id = require_str(args, "session_id")
    selector = require_str(args, "selector")
    timeout_ms = as_int(args.get("timeout_ms"), DEFAULT_TIMEOUT_MS, minimum=1, maximum=600000)

    session = _get_session(session_id)
    click_meta = _human_click_selector(session, args, selector, timeout_ms)
    return {
        "session_id": session_id,
        "selector": selector,
        "clicked": True,
        "url": session.get("url") or "",
        **click_meta,
    }


def tool_browser_type(args: dict[str, Any]) -> dict[str, Any]:
    session_id = require_str(args, "session_id")
    selector = require_str(args, "selector")
    text = require_str(args, "text")
    clear_first = as_bool(args.get("clear_first"), False)
    timeout_ms = as_int(args.get("timeout_ms"), DEFAULT_TIMEOUT_MS, minimum=1, maximum=600000)

    session = _get_session(session_id)
    type_meta = _human_type_selector(session, args, selector, text, timeout_ms)
    return {
        "session_id": session_id,
        "selector": selector,
        "typed_chars": len(text),
        "clear_first": clear_first,
        **type_meta,
    }


def tool_browser_eval(args: dict[str, Any]) -> dict[str, Any]:
    session_id = require_str(args, "session_id")
    expression = require_str(args, "expression")

    session = _get_session(session_id)
    frame, frame_meta = _resolve_frame(session, args)
    arg = args.get("arg")

    value = frame.evaluate(expression, arg)
    safe = _safe_json_value(value)
    return {"session_id": session_id, **frame_meta, "value": safe}


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
    max_html_chars = as_int(args.get("max_html_chars"), DEFAULT_BROWSER_SNAPSHOT_MAX_HTML_CHARS, minimum=0, maximum=200000)
    max_text_chars = as_int(args.get("max_text_chars"), DEFAULT_BROWSER_SNAPSHOT_MAX_TEXT_CHARS, minimum=0, maximum=200000)
    timeout_ms = as_int(args.get("timeout_ms"), DEFAULT_TIMEOUT_MS, minimum=1, maximum=600000)

    session = _get_session(session_id)
    frame, frame_meta = _resolve_frame(session, args)

    title = frame_meta["frame_title"]
    html = frame.content()
    text = _frame_text(frame, timeout_ms)

    _update_session_page_state(session)

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
        "url": frame_meta["frame_url"],
        "title": title,
        "html": html,
        "text": text,
        "html_length": len(html),
        "text_length": len(text),
        **frame_meta,
    }


def _ensure_visible_browser_session(args: dict[str, Any], *, prefix: str, start_url: str | None = None) -> tuple[str, bool, bool, str]:
    session_id = _normalize_session_id(args.get("session_id"), prefix)
    try:
        _get_session(session_id)
        return session_id, False, False, ""
    except Exception:
        pass

    strategy = str(args.get("session_strategy", "new_visible")).strip().lower() or "new_visible"
    if strategy not in {"new_visible", "reuse", "cdp", "attach_or_new", "auto"}:
        raise ValueError("`session_strategy` must be one of: new_visible, reuse, cdp, attach_or_new, auto")

    cdp_error = ""
    if strategy in {"cdp", "attach_or_new", "auto"}:
        try:
            tool_browser_session_attach(
                {
                    "session_id": session_id,
                    "browser": str(args.get("browser", "chromium")),
                    "cdp_url": args.get("cdp_url"),
                    "timeout_ms": args.get("timeout_ms"),
                    "context_index": args.get("context_index", 0),
                    "page_index": args.get("page_index"),
                    "url_contains": args.get("url_contains"),
                    "title_contains": args.get("title_contains"),
                    "create_page_if_missing": args.get("create_page_if_missing", True),
                    "bring_to_front": args.get("bring_to_front", True),
                    "human_like": args.get("human_like", True),
                    "slow_mo_ms": args.get("slow_mo_ms", DEFAULT_SLOW_MO_MS),
                    "pre_action_delay_ms": args.get("pre_action_delay_ms", DEFAULT_PRE_ACTION_DELAY_MS),
                    "post_action_delay_ms": args.get("post_action_delay_ms", DEFAULT_POST_ACTION_DELAY_MS),
                    "typing_delay_ms": args.get("typing_delay_ms", DEFAULT_TYPING_DELAY_MS),
                    "mouse_move_steps": args.get("mouse_move_steps", DEFAULT_MOUSE_MOVE_STEPS),
                    "mouse_jitter_px": args.get("mouse_jitter_px", DEFAULT_MOUSE_JITTER_PX),
                }
            )
            return session_id, True, True, ""
        except Exception as exc:
            cdp_error = str(exc)
            if strategy == "cdp":
                raise

    if strategy == "reuse":
        raise ValueError(f"browser session not found: {session_id}")

    start_payload = {
        "session_id": session_id,
        "browser": str(args.get("browser", "chromium")),
        "headless": as_bool(args.get("headless"), False),
        "slow_mo_ms": as_int(args.get("slow_mo_ms"), DEFAULT_SLOW_MO_MS, minimum=0, maximum=10000),
        "viewport_width": as_int(args.get("viewport_width"), DEFAULT_VIEWPORT_WIDTH, minimum=1, maximum=10000),
        "viewport_height": as_int(args.get("viewport_height"), DEFAULT_VIEWPORT_HEIGHT, minimum=1, maximum=10000),
        "timeout_ms": as_int(args.get("timeout_ms"), DEFAULT_TIMEOUT_MS, minimum=1, maximum=600000),
        "human_like": as_bool(args.get("human_like"), DEFAULT_HUMAN_LIKE),
        "bring_to_front": as_bool(args.get("bring_to_front"), DEFAULT_BRING_TO_FRONT),
        "prefer_real_chrome": as_bool(args.get("prefer_real_chrome"), DEFAULT_PREFER_REAL_CHROME),
        "chrome_channel": args.get("chrome_channel", DEFAULT_CHROME_CHANNEL),
        "pre_action_delay_ms": as_int(args.get("pre_action_delay_ms"), DEFAULT_PRE_ACTION_DELAY_MS, minimum=0, maximum=5000),
        "post_action_delay_ms": as_int(args.get("post_action_delay_ms"), DEFAULT_POST_ACTION_DELAY_MS, minimum=0, maximum=5000),
        "typing_delay_ms": as_int(args.get("typing_delay_ms"), DEFAULT_TYPING_DELAY_MS, minimum=0, maximum=2000),
        "mouse_move_steps": as_int(args.get("mouse_move_steps"), DEFAULT_MOUSE_MOVE_STEPS, minimum=1, maximum=120),
        "mouse_jitter_px": as_int(args.get("mouse_jitter_px"), DEFAULT_MOUSE_JITTER_PX, minimum=0, maximum=50),
    }
    effective_url = start_url or _optional_str(args.get("url"))
    if effective_url is not None:
        start_payload["url"] = effective_url
    tool_browser_session_start(start_payload)
    return session_id, True, False, cdp_error


def tool_browser_handoff_start(args: dict[str, Any]) -> dict[str, Any]:
    mode = str(args.get("mode", "auto")).strip().lower() or "auto"
    if mode not in {"auto", "desktop", "cdp", "visible"}:
        raise ValueError("`mode` must be one of: auto, desktop, cdp, visible")

    session_id = _normalize_session_id(args.get("session_id"), "browser-handoff")
    task = str(args.get("task", "接手当前浏览器")).strip()

    if mode in {"auto", "cdp"}:
        try:
            attached = tool_browser_session_attach(
                {
                    "session_id": session_id,
                    "browser": str(args.get("browser", "chromium")),
                    "cdp_url": args.get("cdp_url"),
                    "timeout_ms": args.get("timeout_ms"),
                    "context_index": args.get("context_index", 0),
                    "page_index": args.get("page_index"),
                    "url_contains": args.get("url_contains"),
                    "title_contains": args.get("title_contains"),
                    "create_page_if_missing": args.get("create_page_if_missing", True),
                    "bring_to_front": args.get("bring_to_front", True),
                    "human_like": args.get("human_like", True),
                }
            )
            return {
                "started": True,
                "mode": "cdp",
                "session_id": session_id,
                "browser_session_id": session_id,
                "computer_use_session_id": "",
                "browser_session": attached,
            }
        except Exception as exc:
            if mode == "cdp":
                raise
            cdp_error = str(exc)
    else:
        cdp_error = ""

    if mode == "visible":
        browser_session = tool_browser_session_start(
            {
                "session_id": session_id,
                "browser": str(args.get("browser", "chromium")),
                "headless": as_bool(args.get("headless"), False),
                "slow_mo_ms": as_int(args.get("slow_mo_ms"), DEFAULT_SLOW_MO_MS, minimum=0, maximum=10000),
                "viewport_width": as_int(args.get("viewport_width"), DEFAULT_VIEWPORT_WIDTH, minimum=1, maximum=10000),
                "viewport_height": as_int(args.get("viewport_height"), DEFAULT_VIEWPORT_HEIGHT, minimum=1, maximum=10000),
                "timeout_ms": as_int(args.get("timeout_ms"), DEFAULT_TIMEOUT_MS, minimum=1, maximum=600000),
                "human_like": as_bool(args.get("human_like"), DEFAULT_HUMAN_LIKE),
                "bring_to_front": as_bool(args.get("bring_to_front"), DEFAULT_BRING_TO_FRONT),
                "prefer_real_chrome": as_bool(args.get("prefer_real_chrome"), DEFAULT_PREFER_REAL_CHROME),
                "chrome_channel": args.get("chrome_channel", DEFAULT_CHROME_CHANNEL),
                "url": args.get("url"),
            }
        )
        return {
            "started": True,
            "mode": "visible",
            "session_id": session_id,
            "browser_session_id": session_id,
            "computer_use_session_id": "",
            "browser_session": browser_session,
        }

    from . import computer_use_tools

    desktop_started = False
    try:
        desktop_session = computer_use_tools._get_session(session_id)
    except Exception:
        desktop_session = computer_use_tools.tool_computer_use_session_start(
            {
                "session_id": session_id,
                "environment": "desktop",
                "capture_initial_screenshot": as_bool(args.get("capture_initial_screenshot"), False),
                "initial_full_page": as_bool(args.get("initial_full_page"), True),
                "task": task,
            }
        )
        desktop_started = True

    return {
        "started": True,
        "mode": "desktop",
        "session_id": session_id,
        "browser_session_id": "",
        "computer_use_session_id": session_id,
        "desktop_started": desktop_started,
        "cdp_error": cdp_error,
        "computer_use_session": desktop_session,
    }


def tool_browser_read_active(args: dict[str, Any]) -> dict[str, Any]:
    session_id = _normalize_session_id(args.get("session_id"), "browser-read")
    source = str(args.get("source", "auto")).strip().lower() or "auto"
    if source not in {"auto", "browser", "desktop"}:
        raise ValueError("`source` must be one of: auto, browser, desktop")

    browser_error = ""
    if source in {"auto", "browser"}:
        try:
            snapshot = tool_browser_snapshot(
                {
                    "session_id": session_id,
                    "frame_index": args.get("frame_index"),
                    "frame_name": args.get("frame_name"),
                    "frame_url_contains": args.get("frame_url_contains"),
                    "max_html_chars": as_int(args.get("max_html_chars"), DEFAULT_BROWSER_READ_ACTIVE_MAX_HTML_CHARS, minimum=0, maximum=200000),
                    "max_text_chars": as_int(args.get("max_text_chars"), DEFAULT_BROWSER_READ_ACTIVE_MAX_TEXT_CHARS, minimum=0, maximum=200000),
                    "timeout_ms": as_int(args.get("timeout_ms"), DEFAULT_TIMEOUT_MS, minimum=1, maximum=600000),
                }
            )
            return {
                "session_id": session_id,
                "source": "browser_session",
                "read_mode": "dom_snapshot",
                **snapshot,
            }
        except Exception as exc:
            browser_error = str(exc)
            if source == "browser":
                raise

    from . import computer_use_tools

    desktop_started = False
    try:
        computer_use_tools._get_session(session_id)
    except Exception:
        computer_use_tools.tool_computer_use_session_start({"session_id": session_id, "environment": "desktop"})
        desktop_started = True

    ocr_payload = computer_use_tools.tool_computer_use_ocr(
        {
            "session_id": session_id,
            "task": str(args.get("task", "读取当前浏览器内容")).strip(),
            "full_page": as_bool(args.get("full_page"), True),
            "force_reconfirm": as_bool(args.get("force_reconfirm"), False),
            "region": args.get("region"),
            "min_confidence": args.get("min_confidence"),
            "upscale_factor": args.get("upscale_factor"),
            "include_screenshot": as_bool(args.get("include_screenshot"), False),
        }
    )
    return {
        "session_id": session_id,
        "source": "desktop_ocr",
        "read_mode": "ocr",
        "desktop_started": desktop_started,
        "browser_error": browser_error,
        **ocr_payload,
    }


def tool_browser_search_visible(args: dict[str, Any]) -> dict[str, Any]:
    query = require_str(args, "query")
    engine = str(args.get("engine", "bing")).strip().lower() or "bing"
    preset = _search_preset(engine)
    timeout_ms = as_int(args.get("timeout_ms"), DEFAULT_TIMEOUT_MS, minimum=1, maximum=600000)
    max_text_chars = as_int(args.get("max_text_chars"), 5000, minimum=0, maximum=200000)
    max_html_chars = as_int(args.get("max_html_chars"), 2000, minimum=0, maximum=200000)

    session_id, created_session, attached_via_cdp, cdp_error = _ensure_visible_browser_session(
        args,
        prefix="browser-search",
        start_url=preset["home_url"],
    )

    tool_browser_navigate({"session_id": session_id, "url": preset["home_url"], "timeout_ms": timeout_ms})

    submitted_via = "typed"
    input_selector = ""
    selector_error = ""
    try:
        session = _get_session(session_id)
        input_selector, _ = _find_first_visible_selector(session, {}, list(preset["input_selectors"]), timeout_ms)
        tool_browser_type({"session_id": session_id, "selector": input_selector, "text": query, "timeout_ms": timeout_ms})
        tool_browser_press_key({"session_id": session_id, "key": str(args.get("submit_key", "Enter"))})
    except Exception as exc:
        selector_error = str(exc)
        submitted_via = "url"
        tool_browser_navigate(
            {
                "session_id": session_id,
                "url": f"{preset['search_url']}{quote_plus(query)}",
                "timeout_ms": timeout_ms,
            }
        )

    snapshot = tool_browser_snapshot(
        {
            "session_id": session_id,
            "max_text_chars": max_text_chars,
            "max_html_chars": max_html_chars,
            "timeout_ms": timeout_ms,
        }
    )
    return {
        "session_id": session_id,
        "engine": engine,
        "query": query,
        "submitted_via": submitted_via,
        "input_selector": input_selector,
        "selector_error": selector_error,
        "created_session": created_session,
        "attached_via_cdp": attached_via_cdp,
        "cdp_error": cdp_error,
        "snapshot": snapshot,
        "url": snapshot.get("url", ""),
        "title": snapshot.get("title", ""),
        "text": snapshot.get("text", ""),
    }


def tool_browser_ask_visible_ai(args: dict[str, Any]) -> dict[str, Any]:
    prompt = require_str(args, "prompt")
    provider = str(args.get("provider", "perplexity")).strip().lower() or "perplexity"
    provider_url = _optional_str(args.get("url"))
    input_selectors = _optional_str_list(args.get("input_selectors"), "input_selectors")
    submit_selectors = _optional_str_list(args.get("submit_selectors"), "submit_selectors")
    submit_key = _optional_str(args.get("submit_key")) or "Enter"
    timeout_ms = as_int(args.get("timeout_ms"), DEFAULT_TIMEOUT_MS, minimum=1, maximum=600000)
    wait_for_reply_ms = as_int(args.get("wait_for_reply_ms"), 1800, minimum=0, maximum=600000)
    max_text_chars = as_int(args.get("max_text_chars"), 7000, minimum=0, maximum=200000)
    max_html_chars = as_int(args.get("max_html_chars"), 2000, minimum=0, maximum=200000)

    if provider_url is not None:
        provider_meta = {
            "url": provider_url,
            "input_selectors": input_selectors or ["textarea", "div[contenteditable='true']", "input[type='text']"],
            "submit_selectors": submit_selectors or ["button[type='submit']", "button[aria-label*='Send']"],
        }
        provider_name = "custom"
    else:
        provider_meta = dict(_ai_preset(provider))
        if input_selectors:
            provider_meta["input_selectors"] = input_selectors
        if submit_selectors:
            provider_meta["submit_selectors"] = submit_selectors
        provider_name = provider

    session_id, created_session, attached_via_cdp, cdp_error = _ensure_visible_browser_session(
        args,
        prefix="browser-ai",
        start_url=str(provider_meta["url"]),
    )

    tool_browser_navigate({"session_id": session_id, "url": str(provider_meta["url"]), "timeout_ms": timeout_ms})
    initial_snapshot = tool_browser_snapshot(
        {"session_id": session_id, "max_text_chars": 2500, "max_html_chars": 0, "timeout_ms": timeout_ms}
    )

    selector_error = ""
    try:
        session = _get_session(session_id)
        input_selector, _ = _find_first_visible_selector(session, {}, list(provider_meta["input_selectors"]), timeout_ms)
    except Exception as exc:
        selector_error = str(exc)
        from . import computer_use_tools

        reason = "找不到 AI 输入框"
        if _looks_like_login_page(str(initial_snapshot.get("text", ""))):
            reason = "当前页面看起来需要先登录或人工确认"
        manual_prompt = computer_use_tools.tool_computer_use_manual_prompt(
            {
                "title": "需要你手动接管浏览器",
                "prompt": f"{reason}。请在可见浏览器里完成登录/验证后，再继续。",
                "default": "已处理",
                "require_input": False,
            }
        )
        return {
            "session_id": session_id,
            "provider": provider_name,
            "url": str(provider_meta["url"]),
            "submitted": False,
            "manual_needed": True,
            "reason": reason,
            "selector_error": selector_error,
            "created_session": created_session,
            "attached_via_cdp": attached_via_cdp,
            "cdp_error": cdp_error,
            "snapshot": initial_snapshot,
            "manual_prompt": manual_prompt,
        }

    tool_browser_type({"session_id": session_id, "selector": input_selector, "text": prompt, "timeout_ms": timeout_ms})

    submit_selector = ""
    submitted_via = "key"
    try:
        session = _get_session(session_id)
        submit_selector, _ = _find_first_visible_selector(session, {}, list(provider_meta.get("submit_selectors", [])), min(timeout_ms, 1500))
    except Exception:
        submit_selector = ""

    if submit_selector:
        tool_browser_click({"session_id": session_id, "selector": submit_selector, "timeout_ms": timeout_ms})
        submitted_via = "click"
    else:
        tool_browser_press_key({"session_id": session_id, "key": submit_key})

    _sleep_ms(wait_for_reply_ms)
    snapshot = tool_browser_snapshot(
        {
            "session_id": session_id,
            "max_text_chars": max_text_chars,
            "max_html_chars": max_html_chars,
            "timeout_ms": timeout_ms,
        }
    )
    return {
        "session_id": session_id,
        "provider": provider_name,
        "url": snapshot.get("url", ""),
        "submitted": True,
        "submitted_via": submitted_via,
        "input_selector": input_selector,
        "submit_selector": submit_selector,
        "created_session": created_session,
        "attached_via_cdp": attached_via_cdp,
        "cdp_error": cdp_error,
        "snapshot": snapshot,
        "text": snapshot.get("text", ""),
        "title": snapshot.get("title", ""),
    }


def get_browser_tooling() -> tuple[dict[str, Any], dict[str, str], dict[str, dict[str, Any]]]:
    handlers = {
        "browser_session_start": tool_browser_session_start,
        "browser_session_attach": tool_browser_session_attach,
        "browser_session_stop": tool_browser_session_stop,
        "browser_session_list": tool_browser_session_list,
        "browser_handoff_start": tool_browser_handoff_start,
        "browser_read_active": tool_browser_read_active,
        "browser_frame_list": tool_browser_frame_list,
        "browser_navigate": tool_browser_navigate,
        "browser_press_key": tool_browser_press_key,
        "browser_click": tool_browser_click,
        "browser_type": tool_browser_type,
        "browser_search_visible": tool_browser_search_visible,
        "browser_ask_visible_ai": tool_browser_ask_visible_ai,
        "browser_eval": tool_browser_eval,
        "browser_capture": tool_browser_capture,
        "browser_snapshot": tool_browser_snapshot,
    }

    descriptions = {
        "browser_session_start": "Start a visible Playwright browser debugging session with human-like defaults.",
        "browser_session_attach": "Attach to an existing Chromium/Chrome/Edge instance over CDP without modifying the user browser window.",
        "browser_session_stop": "Stop and dispose a browser debugging session.",
        "browser_session_list": "List active browser debugging sessions.",
        "browser_handoff_start": "Take over the current browser in auto mode: try CDP first, then fall back to visible desktop handoff.",
        "browser_read_active": "Read the current browser content with compact defaults from an attached session or via desktop OCR handoff.",
        "browser_frame_list": "List frames in the current page for iframe debugging.",
        "browser_navigate": "Navigate browser page to target URL.",
        "browser_press_key": "Press a keyboard key in the current browser page.",
        "browser_click": "Click an element with optional human-like mouse movement.",
        "browser_type": "Type text into an element with optional human-like keyboard input.",
        "browser_search_visible": "Search the web in a visible browser window, typing like a human when possible.",
        "browser_ask_visible_ai": "Open a visible AI site, type a prompt, and try to submit it; falls back to manual handoff if login is required.",
        "browser_eval": "Evaluate JavaScript in browser page or selected frame context.",
        "browser_capture": "Capture page screenshot and return PNG base64.",
        "browser_snapshot": "Get page or selected frame URL/title plus compact HTML and text snapshot.",
    }

    base_browser_session_properties = {
        "session_id": {"type": "string"},
        "browser": {"type": "string", "enum": ["chromium", "firefox", "webkit"], "default": "chromium"},
        "headless": {"type": "boolean", "default": False},
        "slow_mo_ms": {"type": "integer", "minimum": 0, "maximum": 10000, "default": 120},
        "viewport_width": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 1440},
        "viewport_height": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 900},
        "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 600000, "default": 30000},
        "human_like": {"type": "boolean", "default": True},
        "bring_to_front": {"type": "boolean", "default": True},
        "prefer_real_chrome": {"type": "boolean", "default": True},
        "chrome_channel": {"type": "string", "default": "chrome"},
        "pre_action_delay_ms": {"type": "integer", "minimum": 0, "maximum": 5000, "default": 120},
        "post_action_delay_ms": {"type": "integer", "minimum": 0, "maximum": 5000, "default": 180},
        "typing_delay_ms": {"type": "integer", "minimum": 0, "maximum": 2000, "default": 90},
        "mouse_move_steps": {"type": "integer", "minimum": 1, "maximum": 120, "default": 24},
        "mouse_jitter_px": {"type": "integer", "minimum": 0, "maximum": 50, "default": 4},
        "url": {"type": "string"},
    }

    schemas = {
        "browser_session_start": {
            "type": "object",
            "properties": dict(base_browser_session_properties),
            "additionalProperties": False,
        },
        "browser_session_attach": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "browser": {"type": "string", "enum": ["chromium"], "default": "chromium"},
                "cdp_url": {"type": "string", "default": "http://127.0.0.1:9222"},
                "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 600000, "default": 30000},
                "context_index": {"type": "integer", "minimum": 0, "default": 0},
                "page_index": {"type": "integer", "minimum": 0},
                "url_contains": {"type": "string"},
                "title_contains": {"type": "string"},
                "create_page_if_missing": {"type": "boolean", "default": True},
                "human_like": {"type": "boolean", "default": True},
                "bring_to_front": {"type": "boolean", "default": True},
                "slow_mo_ms": {"type": "integer", "minimum": 0, "maximum": 10000, "default": 120},
                "pre_action_delay_ms": {"type": "integer", "minimum": 0, "maximum": 5000, "default": 120},
                "post_action_delay_ms": {"type": "integer", "minimum": 0, "maximum": 5000, "default": 180},
                "typing_delay_ms": {"type": "integer", "minimum": 0, "maximum": 2000, "default": 90},
                "mouse_move_steps": {"type": "integer", "minimum": 1, "maximum": 120, "default": 24},
                "mouse_jitter_px": {"type": "integer", "minimum": 0, "maximum": 50, "default": 4}
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
        "browser_handoff_start": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "mode": {"type": "string", "enum": ["auto", "desktop", "cdp", "visible"], "default": "auto"},
                "task": {"type": "string", "default": "接手当前浏览器"},
                "browser": {"type": "string", "enum": ["chromium", "firefox", "webkit"], "default": "chromium"},
                "cdp_url": {"type": "string", "default": "http://127.0.0.1:9222"},
                "context_index": {"type": "integer", "minimum": 0, "default": 0},
                "page_index": {"type": "integer", "minimum": 0},
                "url_contains": {"type": "string"},
                "title_contains": {"type": "string"},
                "create_page_if_missing": {"type": "boolean", "default": True},
                "capture_initial_screenshot": {"type": "boolean", "default": False},
                "initial_full_page": {"type": "boolean", "default": True},
                "headless": {"type": "boolean", "default": False},
                "slow_mo_ms": {"type": "integer", "minimum": 0, "maximum": 10000, "default": 120},
                "viewport_width": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 1440},
                "viewport_height": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 900},
                "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 600000, "default": 30000},
                "human_like": {"type": "boolean", "default": True},
                "bring_to_front": {"type": "boolean", "default": True},
                "prefer_real_chrome": {"type": "boolean", "default": True},
                "chrome_channel": {"type": "string", "default": "chrome"},
                "url": {"type": "string"}
            },
            "additionalProperties": False,
        },
        "browser_read_active": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "source": {"type": "string", "enum": ["auto", "browser", "desktop"], "default": "auto"},
                "task": {"type": "string", "default": "读取当前浏览器内容"},
                "frame_index": {"type": "integer", "minimum": 0},
                "frame_name": {"type": "string"},
                "frame_url_contains": {"type": "string"},
                "max_html_chars": {"type": "integer", "minimum": 0, "maximum": 200000, "default": DEFAULT_BROWSER_READ_ACTIVE_MAX_HTML_CHARS},
                "max_text_chars": {"type": "integer", "minimum": 0, "maximum": 200000, "default": DEFAULT_BROWSER_READ_ACTIVE_MAX_TEXT_CHARS},
                "full_page": {"type": "boolean", "default": True},
                "force_reconfirm": {"type": "boolean", "default": False},
                "region": {"type": "object"},
                "min_confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "upscale_factor": {"type": "integer", "minimum": 1, "maximum": 4},
                "include_screenshot": {"type": "boolean", "default": False},
                "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 600000, "default": 30000}
            },
            "additionalProperties": False,
        },
        "browser_frame_list": {
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
            "additionalProperties": False,
        },
        "browser_navigate": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "url": {"type": "string"},
                "wait_until": {"type": "string", "enum": ["load", "domcontentloaded", "networkidle", "commit"], "default": "domcontentloaded"},
                "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 600000, "default": 30000}
            },
            "required": ["session_id", "url"],
            "additionalProperties": False,
        },
        "browser_press_key": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "key": {"type": "string"},
                "bring_to_front": {"type": "boolean"},
                "pre_action_delay_ms": {"type": "integer", "minimum": 0, "maximum": 5000},
                "post_action_delay_ms": {"type": "integer", "minimum": 0, "maximum": 5000}
            },
            "required": ["session_id", "key"],
            "additionalProperties": False,
        },
        "browser_click": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "selector": {"type": "string"},
                "frame_index": {"type": "integer", "minimum": 0},
                "frame_name": {"type": "string"},
                "frame_url_contains": {"type": "string"},
                "human_like": {"type": "boolean"},
                "bring_to_front": {"type": "boolean"},
                "pre_action_delay_ms": {"type": "integer", "minimum": 0, "maximum": 5000},
                "post_action_delay_ms": {"type": "integer", "minimum": 0, "maximum": 5000},
                "mouse_move_steps": {"type": "integer", "minimum": 1, "maximum": 120},
                "mouse_jitter_px": {"type": "integer", "minimum": 0, "maximum": 50},
                "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 600000, "default": 30000}
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
                "frame_index": {"type": "integer", "minimum": 0},
                "frame_name": {"type": "string"},
                "frame_url_contains": {"type": "string"},
                "human_like": {"type": "boolean"},
                "bring_to_front": {"type": "boolean"},
                "pre_action_delay_ms": {"type": "integer", "minimum": 0, "maximum": 5000},
                "post_action_delay_ms": {"type": "integer", "minimum": 0, "maximum": 5000},
                "typing_delay_ms": {"type": "integer", "minimum": 0, "maximum": 2000},
                "mouse_move_steps": {"type": "integer", "minimum": 1, "maximum": 120},
                "mouse_jitter_px": {"type": "integer", "minimum": 0, "maximum": 50},
                "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 600000, "default": 30000}
            },
            "required": ["session_id", "selector", "text"],
            "additionalProperties": False,
        },
        "browser_search_visible": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "query": {"type": "string"},
                "engine": {"type": "string", "enum": ["bing", "duckduckgo", "google"], "default": "bing"},
                "session_strategy": {"type": "string", "enum": ["new_visible", "reuse", "cdp", "attach_or_new", "auto"], "default": "new_visible"},
                "browser": {"type": "string", "enum": ["chromium", "firefox", "webkit"], "default": "chromium"},
                "cdp_url": {"type": "string"},
                "context_index": {"type": "integer", "minimum": 0, "default": 0},
                "page_index": {"type": "integer", "minimum": 0},
                "url_contains": {"type": "string"},
                "title_contains": {"type": "string"},
                "create_page_if_missing": {"type": "boolean", "default": True},
                "submit_key": {"type": "string", "default": "Enter"},
                "headless": {"type": "boolean", "default": False},
                "slow_mo_ms": {"type": "integer", "minimum": 0, "maximum": 10000, "default": 120},
                "viewport_width": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 1440},
                "viewport_height": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 900},
                "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 600000, "default": 30000},
                "max_html_chars": {"type": "integer", "minimum": 0, "maximum": 200000, "default": 2000},
                "max_text_chars": {"type": "integer", "minimum": 0, "maximum": 200000, "default": 5000}
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "browser_ask_visible_ai": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "provider": {"type": "string", "enum": ["perplexity", "chatgpt", "claude", "gemini", "deepseek"], "default": "perplexity"},
                "url": {"type": "string"},
                "prompt": {"type": "string"},
                "input_selectors": {"type": "array", "items": {"type": "string"}},
                "submit_selectors": {"type": "array", "items": {"type": "string"}},
                "submit_key": {"type": "string", "default": "Enter"},
                "session_strategy": {"type": "string", "enum": ["new_visible", "reuse", "cdp", "attach_or_new", "auto"], "default": "new_visible"},
                "browser": {"type": "string", "enum": ["chromium", "firefox", "webkit"], "default": "chromium"},
                "cdp_url": {"type": "string"},
                "context_index": {"type": "integer", "minimum": 0, "default": 0},
                "page_index": {"type": "integer", "minimum": 0},
                "url_contains": {"type": "string"},
                "title_contains": {"type": "string"},
                "create_page_if_missing": {"type": "boolean", "default": True},
                "headless": {"type": "boolean", "default": False},
                "slow_mo_ms": {"type": "integer", "minimum": 0, "maximum": 10000, "default": 120},
                "viewport_width": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 1440},
                "viewport_height": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 900},
                "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 600000, "default": 30000},
                "wait_for_reply_ms": {"type": "integer", "minimum": 0, "maximum": 600000, "default": 1800},
                "max_html_chars": {"type": "integer", "minimum": 0, "maximum": 200000, "default": 2000},
                "max_text_chars": {"type": "integer", "minimum": 0, "maximum": 200000, "default": 7000}
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
        "browser_eval": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "frame_index": {"type": "integer", "minimum": 0},
                "frame_name": {"type": "string"},
                "frame_url_contains": {"type": "string"},
                "expression": {"type": "string"},
                "arg": {}
            },
            "required": ["session_id", "expression"],
            "additionalProperties": False,
        },
        "browser_capture": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "full_page": {"type": "boolean", "default": True}
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
        "browser_snapshot": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "frame_index": {"type": "integer", "minimum": 0},
                "frame_name": {"type": "string"},
                "frame_url_contains": {"type": "string"},
                "max_html_chars": {"type": "integer", "minimum": 0, "maximum": 200000, "default": DEFAULT_BROWSER_SNAPSHOT_MAX_HTML_CHARS},
                "max_text_chars": {"type": "integer", "minimum": 0, "maximum": 200000, "default": DEFAULT_BROWSER_SNAPSHOT_MAX_TEXT_CHARS},
                "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 600000, "default": 30000}
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    }

    return handlers, descriptions, schemas
