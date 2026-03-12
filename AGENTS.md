# Workspace Agent Policy

- MUST use MCP server `CodexTools` tools for repository discovery, reads, and routine code, file, and text operations; for existing-file manual edits, MUST use `apply_patch` by default.
- MUST NOT use shell commands, shell redirection, or shell text utilities for any code, file, or text operation.
- Read only with `mcp__CodexTools__fs_read_text` or `mcp__CodexTools__fs_read_texts`.
- Write/create/append only with `mcp__CodexTools__fs_write_text` or `mcp__CodexTools__fs_create`.
- MUST use `apply_patch` for manual code patch modifications by default. Only when `apply_patch` cannot express the required text change cleanly or safely may you use `mcp__CodexTools__fs_replace_text`, `mcp__CodexTools__fs_replace_regex`, or `mcp__CodexTools__fs_patch_lines`.
- For existing-file edits, use `apply_patch` unless you have a concrete reason it cannot handle the text change cleanly; compatible IDE clients can then present structured edited-file/diff UI.
- Avoid `mcp__CodexTools__fs_write_text` for modifying existing files unless a full rewrite is genuinely safer or the patch tools cannot express the change cleanly.
- Batch related edits into as few `apply_patch` operations as practical to improve edited-file grouping in compatible clients such as Claude Code GUI IDEA integrations.
- Use `mcp__CodexTools__fs_list`, `mcp__CodexTools__fs_list_files`, `mcp__CodexTools__fs_stat`, and `mcp__CodexTools__fs_search_text` for discovery and search.
- Prefer `mcp__CodexTools__fs_read_texts` for disjoint multi-range reads.
- Prefer Codex native plan capability for substantial tasks; do not reimplement plan tools in this workspace.
- For OpenAI computer use or custom computer harness flows, call `computer_use_request_consent` before any native desktop or browser screenshot/action unless consent is already granted for the current session.
- If a step requires passwords, MFA, captchas, payment confirmation, or other sensitive manual input, do not automate it; use `computer_use_manual_prompt` and wait for the user.
- Use manual interaction mode for end-user input tools unless the user explicitly requests automation.
- Use `mcp__CodexTools__proc_run` only as a last resort when fs tools are insufficient, and explain why first.
- Use UTF-8 for text operations.
- Prefer non-`CodexTools` MCP tools for web interactions when available; use `CodexTools` web/browser tooling only when other MCP options do not provide the needed capability.
- Prefer minimal, targeted patches; do not modify unrelated code.
- When fixing a function, keep input/output contracts and key caller/callee behavior correct unless the user explicitly asks to change them.
- If the request is broad, ambiguous, or under-specified, ask follow-up questions before coding.
- Ask at most 3 questions per round; you may ask multiple rounds if needed.
- Do not write code until you are at least 95% confident you understand the user's goal, scope, and constraints.
- If confidence is below 95%, state the missing points briefly and continue clarifying.

## Nexus Map And Structure Rules

- `.nexus-map/` 存在时：开始任务前必须先读 `INDEX.md` 恢复上下文，并按其中的路由块决定下一步动作。
- `.nexus-map/` 不存在时：跨模块或接口修改前，先向用户提议运行 `nexus-mapper`；若用户需立即开始，至少先运行 `query_graph.py --summary` 建立结构感知，不要对陌生仓库盲改核心接口。
- 结构查询：任何时候需要判断依赖关系、影响半径或边界归属，优先用 `query_graph.py` 验证，不要凭目录名猜测。
- 知识库同步：任务中若改变了系统边界、入口或依赖关系，完成后评估是否需要重新运行 `nexus-mapper` 更新 `.nexus-map`。
    
<!-- codextools:auto-agent-rules:v2:start -->
# Agent Rules

- Follow project instructions for this workspace.
<!-- codextools:auto-agent-rules:v2:end -->
