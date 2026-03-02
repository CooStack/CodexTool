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
- MUST quantify task complexity before deciding plan flow.
- If a task can be finished in a few simple steps, MAY skip plan creation and proceed directly.
- If a task requires many steps or substantial implementation (for example, building a web page from scratch), MUST create a plan before edits and keep it updated during execution.
- For tasks that require a plan, all plan content MUST be written in the user's language. Example: if the user uses Simplified Chinese, the plan MUST be in Simplified Chinese.
- For tasks that require a plan, MUST show the plan to the user for review before code modification and wait for user feedback.
- For tasks that require a plan, MUST NOT start code changes until the user explicitly confirms to continue (for example: "继续" / "continue").
- For tasks that require a plan, after receiving user confirmation, MUST call `plan_confirm_continue` before any write/edit tool.
- For plan review, MUST use `ui_plan_confirm` to show plan content and let user choose `继续` or `修改计划` when a plan is required.
- For tasks that require a plan, MUST NOT ask for or accept direct textual `继续` as plan approval before the plan review dialog is shown and completed.
- If user chooses `修改计划`, MUST stop dependent writes, let user edit `<workspace-name>-plan.md` in the current workspace root, then wait for user to send `继续` and call `plan_confirm_continue` when a plan is required.
- For tools that require end-user input (for example `ui_dialog_input`, `ui_plan_confirm`), MUST use manual interaction mode by default and wait for user completion.
- MUST NOT enable auto-submit/auto-skip for user-input tools unless the user explicitly asks for automated testing.
- When a user-input dialog is shown, MUST clearly notify the user and wait for interaction result before continuing subsequent dependent actions.
- If the user says the plan has problems, MUST revise the plan and repeat the review loop until the user is satisfied, then proceed with code changes when a plan is required.
- For reversible edits, prefer small incremental edits and verify each milestone.
- During implementation, prefer `plan_update` at each milestone and keep at most one step as `in_progress`.
- After finishing, provide `plan_view`/`plan_list` progress summary.
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
- Plan progress visualization: supported (`plan_create`, `plan_update`, `plan_view`, `plan_list`, `plan_archive`, `plan_confirm_continue`, `plan_guard_status`).
- Plan review dialog: supported (`ui_plan_confirm`), can be used for continue/modify branch.
- Feature status: complexity-gated planning workflow is implemented; simple tasks can skip plans, while complex tasks require plan creation and review.
- Change tracking and rollback: removed to reduce overhead and token usage.
- Multi-file refactor workflow: supported by combining `fs_list`/`fs_list_files` + `fs_read_text` + `fs_replace_text`/`fs_replace_regex` + `fs_patch_lines` + `fs_write_text`.
- Current limitation: no native AST-aware editing tool yet.

## Optional Future Additions

- Add `fs_apply_patch` for hunk-based code edits.
- Add path sandbox policy options for safer team environments.

<!-- codextools:auto-agent-rules:v1:start -->
# Agent Rules

- Follow project instructions for this workspace.
<!-- codextools:auto-agent-rules:v1:end -->
