#!/usr/bin/env python3
from __future__ import annotations

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
from typing import Any, Callable, Optional, Tuple, Union

SERVER_NAME = "CodexTools"
SERVER_VERSION = "0.5.0"
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


TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
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
    "proc_run": "Execute a shell command and capture UTF-8 stdout/stderr. Use this for npm, npx, git, python, and any CLI tool.",
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
    "proc_run": {
        "type": "object",
        "properties": {
            "command": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
            "cwd": {"type": "string"},
            "timeout_sec": {"type": "integer", "minimum": 1, "maximum": 600, "default": 60},
            "shell": {"type": "boolean", "default": False},
            "env": {"type": "object", "additionalProperties": {"type": "string"}, "description": "Extra environment variables to set"},
        },
        "required": ["command"],
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
