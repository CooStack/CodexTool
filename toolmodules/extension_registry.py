from __future__ import annotations

from typing import Any, Callable, Union

from .extensions.browser_tools import get_browser_tooling
from .extensions.computer_use_tools import get_computer_use_tooling
from .extensions.debug_tools import get_debug_tooling
from .extensions.perf_tools import get_perf_tooling
from .extensions.ui_tools import get_ui_tooling

ToolHandler = Callable[[dict[str, Any]], Union[dict[str, Any], str]]


def _merge_tooling(
    target_handlers: dict[str, ToolHandler],
    target_descriptions: dict[str, str],
    target_schemas: dict[str, dict[str, Any]],
    handlers: dict[str, ToolHandler],
    descriptions: dict[str, str],
    schemas: dict[str, dict[str, Any]],
) -> None:
    for name, handler in handlers.items():
        if name in target_handlers:
            raise ValueError(f"duplicate extension tool name: {name}")
        if name not in descriptions or name not in schemas:
            raise ValueError(f"extension tool metadata is incomplete: {name}")
        target_handlers[name] = handler
        target_descriptions[name] = descriptions[name]
        target_schemas[name] = schemas[name]


def get_extension_tooling() -> tuple[
    dict[str, ToolHandler],
    dict[str, str],
    dict[str, dict[str, Any]],
]:
    handlers: dict[str, ToolHandler] = {}
    descriptions: dict[str, str] = {}
    schemas: dict[str, dict[str, Any]] = {}

    for provider in (
        get_debug_tooling,
        get_perf_tooling,
        get_ui_tooling,
        get_browser_tooling,
        get_computer_use_tooling,
    ):
        p_handlers, p_descriptions, p_schemas = provider()
        _merge_tooling(handlers, descriptions, schemas, p_handlers, p_descriptions, p_schemas)

    return handlers, descriptions, schemas
