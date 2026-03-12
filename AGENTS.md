# Workspace Agent Policy

Policy Version: 1.2.1
Last Updated: 2026-03-12

- MUST use MCP server `CodexTools` tools for repository discovery, reads, and routine code, file, and text operations.
- MUST NOT use shell commands, shell redirection, or shell text utilities unless an MCP capability itself depends on shell or the task objectively cannot be completed without shell.
- In all other cases, shell usage is forbidden and MCP/native workspace tools must be used instead.
- Any modification to `AGENTS.md` must increment `Policy Version` and update `Last Updated`.
- Read only with `mcp__CodexTools__fs_read_text` or `mcp__CodexTools__fs_read_texts`.
- Existing-file text modifications MUST use `apply_patch`.
- Only when `apply_patch` cannot express the change cleanly or safely may text edits fall back to `mcp__CodexTools__fs_replace_text`, `mcp__CodexTools__fs_replace_regex`, or `mcp__CodexTools__fs_patch_lines`.
- New-file creation or full-file writes may use `mcp__CodexTools__fs_write_text` or `mcp__CodexTools__fs_create`.
- Batch related edits into as few `apply_patch` operations as practical.
- Use `mcp__CodexTools__fs_list`, `mcp__CodexTools__fs_list_files`, `mcp__CodexTools__fs_stat`, and `mcp__CodexTools__fs_search_text` for discovery and search.
- Prefer `mcp__CodexTools__fs_read_texts` for disjoint multi-range reads.
- Prefer Codex native plan capability for substantial tasks; do not reimplement plan tools in this workspace.
- If the task is complex, cross-module, ambiguous, or requires multi-step reasoning, use `Sequential-thinking` MCP for structured thinking when available.
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

- Only apply this section when the user's request is directly related to the current project's code, architecture, module boundaries, interface impact, or dependency structure.
- If `.nexus-map/` exists, read `INDEX.md` before starting project-related work and follow its routing guidance.
- If `.nexus-map/` does not exist and the task involves cross-module or interface changes, propose running `nexus-mapper` first; if the user wants immediate work, establish minimal structural awareness before editing unfamiliar core code.
- When you need to judge dependencies, impact radius, or ownership boundaries, prefer structure queries instead of guessing from directory names.
- If the task changes system boundaries, entries, or dependency relationships, assess whether `.nexus-map` should be refreshed afterward.
    
<!-- codextools:auto-agent-rules:v2:start -->
# Agent Rules

- Follow project instructions for this workspace.
<!-- codextools:auto-agent-rules:v2:end -->
