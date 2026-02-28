# Agent Rules

## CodexTools Mandatory Rules

- MUST use MCP server `CodexTools` tools for all file and text operations.
- MUST NOT use shell commands for file/text operations.
- MUST NOT use shell redirection or shell text utilities for edits (`type`, `cat`, `echo`, `sed`, `awk`, `perl`, `Get-Content`, `Set-Content`, `Out-File`, `>`, `>>`).
- Read text only with `mcp__CodexTools__fs_read_text` or `mcp__CodexTools__fs_read_texts`.
- For reading multiple disjoint ranges (including multiple ranges from the same file), prefer `mcp__CodexTools__fs_read_texts` with `ranges` (`[{"path","start_line","end_line"...}]`).
- Write/create/append text only with `mcp__CodexTools__fs_write_text` or `mcp__CodexTools__fs_create`.
- Replace text only with `mcp__CodexTools__fs_replace_text`, `mcp__CodexTools__fs_replace_regex`, or `mcp__CodexTools__fs_patch_lines`.
- Use `mcp__CodexTools__fs_list`, `mcp__CodexTools__fs_list_files`, and `mcp__CodexTools__fs_stat` for file discovery and metadata.
- Use `mcp__CodexTools__fs_search_text` for batch text search.
- For task progress visualization, use plan tools (`plan_create` -> `plan_update` -> `plan_view`/`plan_list`).
- Default execution mode is plan-first: for non-trivial tasks, MUST create a plan before edits and keep it updated during execution.
- For reversible edits, use change tools (`change_begin` -> edit -> `change_get`/`change_list` -> `change_commit` or `change_rollback`).
- During implementation, prefer `plan_update` at each milestone and keep at most one step as `in_progress`.
- After finishing, provide `plan_view`/`plan_list` progress summary and close/commit active change set when appropriate.
- Use `mcp__CodexTools__fs_move`, `mcp__CodexTools__fs_move_file`, `mcp__CodexTools__fs_copy_file`, and `mcp__CodexTools__fs_delete` for move/copy/delete.
- Use `mcp__CodexTools__proc_run` only as a last resort when no `CodexTools` fs tool can complete the task.
- If `mcp__CodexTools__proc_run` is unavoidable, explicitly explain why fs tools are insufficient before using it.
- Always prefer UTF-8 (`encoding: "utf-8"`) when reading or writing text.
- If an edit cannot be completed with current fs tools, explicitly report the gap and propose adding a new MCP tool first.

## Capability Check (Current CodexTools)

- Text replacement: supported (`fs_replace_text`).
- Regex replacement: supported (`fs_replace_regex`).
- Code writing/new file creation: supported (`fs_write_text`, `fs_create`).
- Code reading/partial reading: supported (`fs_read_text` with `start_line`/`end_line`).
- Batch/limited reading for token control: supported (`fs_read_texts`, `fs_read_text` with `max_lines`/`max_chars`).
- Multi-range reading in one call: supported via `fs_read_texts.ranges`, including multiple line intervals from the same file.
- Precise line patching: supported (`fs_patch_lines`).
- Batch text search with caps/context: supported (`fs_search_text`).
- Plan progress visualization: supported (`plan_create`, `plan_update`, `plan_view`, `plan_list`, `plan_archive`).
- Feature status: plan-first execution workflow is implemented and should be used by default.
- Change tracking and rollback: supported (`change_begin`, `change_set_active`, `change_get`, `change_list`, `change_commit`, `change_rollback`) with auto snapshots for file mutations.
- Multi-file refactor workflow: supported by combining `fs_list`/`fs_list_files` + `fs_read_text` + `fs_replace_text`/`fs_replace_regex` + `fs_patch_lines` + `fs_write_text`.
- Current limitation: no native AST-aware editing tool yet.

## Optional Future Additions

- Add `fs_apply_patch` for hunk-based code edits.
- Add path sandbox policy options for safer team environments.
