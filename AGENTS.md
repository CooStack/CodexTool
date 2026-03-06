# Workspace Agent Policy

- MUST use MCP server `CodexTools` tools for all code, file, and text operations.
- MUST NOT use shell commands, shell redirection, or shell text utilities for any code, file, or text operation.
- Read only with `mcp__CodexTools__fs_read_text` or `mcp__CodexTools__fs_read_texts`.
- Write/create/append only with `mcp__CodexTools__fs_write_text` or `mcp__CodexTools__fs_create`.
- Replace only with `mcp__CodexTools__fs_replace_text`, `mcp__CodexTools__fs_replace_regex`, or `mcp__CodexTools__fs_patch_lines`.
- Use `mcp__CodexTools__fs_list`, `mcp__CodexTools__fs_list_files`, `mcp__CodexTools__fs_stat`, and `mcp__CodexTools__fs_search_text` for discovery and search.
- Prefer `mcp__CodexTools__fs_read_texts` for disjoint multi-range reads.
- Prefer Codex native plan capability for substantial tasks; do not reimplement plan tools in this workspace.
- For OpenAI computer use or custom computer harness flows, call `computer_use_request_consent` before any native desktop or browser screenshot/action unless consent is already granted for the current session.
- If a step requires passwords, MFA, captchas, payment confirmation, or other sensitive manual input, do not automate it; use `computer_use_manual_prompt` and wait for the user.
- Use manual interaction mode for end-user input tools unless the user explicitly requests automation.
- Use `mcp__CodexTools__proc_run` only as a last resort when fs tools are insufficient, and explain why first.
- If current CodexTools are insufficient, report the gap explicitly instead of bypassing the policy.
- Use UTF-8 for text operations.

<!-- codextools:auto-agent-rules:v1:start -->
# Agent Rules
- Follow project instructions for this workspace.
<!-- codextools:auto-agent-rules:v1:end -->