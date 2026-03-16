---
name: codextoolSkill
description: Use when the user explicitly wants a role-based multi-agent workflow, wants one agent to review another agent's output, or when the task is complex enough that the assistant should directly enter a planner-worker-reviewer team mode. This skill coordinates the new `agent_team_plan` CodexTools helper and defines the communication pattern.
---

# codextoolSkill

## When To Use

Use this skill when any of the following is true:

- The user explicitly asks for multiple agents, role-based collaboration, reviewer and implementer separation, or a team workflow.
- The user wants one agent to review another agent's code or wants an iterative review loop.
- The task is complex enough that a single agent is likely to benefit from role decomposition.

Treat the task as a good candidate for this mode when it includes several of these signals:

- Multiple modules or subsystems
- Cross-stack work such as frontend plus backend plus database
- Separate implementation and review expectations
- Unclear interfaces that need planning first
- Integration work across services, loaders, frameworks, or repos

If the task clearly fits this mode, enter it directly instead of waiting for the user to explicitly approve the switch.

## First Action

Call `agent_team_plan` with the user request and any important constraints. Use the result to decide:

- whether to keep a single-agent flow
- whether to keep a single-agent flow for a genuinely small task
- which roles should exist
- what the communication protocol should be

If the plan indicates this mode should be used, call `agent_team_bootstrap` immediately with `user_requested_mode=true` to generate the shared workspace artifacts before workers start writing code. Keep `open_dashboard=true` unless the user explicitly asks not to open the GUI.

Then call `agent_team_script` to get:

- coordinator instructions
- ready-to-send role prompt templates
- per-role `send_input` seed text
- the preferred dashboard reopen contract and GUI entrypoint

## Operating Model

Use a coordinator hub, not direct subagent-to-subagent chat.

The main agent stays in the `Coordinator + Integrator` seat for the whole run.
Planning is an opening phase, not the main agent's only job.

Required topology:

- coordinator
- planner
- one worker per independent module
- reviewer
- integrator when more than one worker exists

The coordinator is responsible for:

- spawning agents
- doing the initial task decomposition
- scheduling parallel work and delaying waits until the critical path is actually blocked
- relaying reviewer findings back to workers
- maintaining shared artifacts
- routing blockers
- preparing review packets
- integrating outputs
- deciding when a round is done
- giving the final result to the user

Workers and reviewers must not directly coordinate by free-form chat. Use shared artifacts or explicit coordinator relay only.

Every agent must use a stable identity prefix in substantial outputs so that the coordinator can tell them apart quickly.

## Shared Artifacts

Use these files as the default shared contract:

- `docs/agent-team/dashboard-state.json`
- `docs/agent-team/runs/<run_id>/plan.md`
- `docs/agent-team/runs/<run_id>/interfaces.md`
- `docs/agent-team/runs/<run_id>/review-log.md`
- `docs/agent-team/runs/<run_id>/handovers/<module>.md`
- `docs/agent-team/runs/<run_id>/agent-prompts.md`
- `docs/agent-team/runs/<run_id>/dashboard-events.jsonl`

`agent_team_bootstrap` generates these files automatically and assigns identity prefixes such as:

- `[planner]`
- `[worker:frontend]`
- `[worker:backend]`
- `[reviewer]`
- `[integrator]`

The skill also ships reusable prompt templates in `assets/templates/`:

- `planner-prompt.md.template`
- `worker-prompt.md.template`
- `reviewer-prompt.md.template`
- `integrator-prompt.md.template`
- `coordinator-loop.md.template`

Minimum handoff format:

```md
Task:
Owner:
Inputs:
Outputs:
Open Questions:
Verification:
Status:
```

## Dashboard Launch

Preferred reopen contract:

- use `dashboard.state_path` returned by `agent_team_bootstrap`
- reopen with `ui_agent_team_dashboard(state_path=...)`
- treat `workspace_root` as compatibility-only fallback

Implementation entrypoints so you do not need to read code to find them:

- tool entry: `toolmodules/extensions/ui_tools.py` -> `tool_ui_agent_team_dashboard`
- detached GUI module: `toolmodules/extensions/agent_team_dashboard.py`
- manual launch command: `python -m toolmodules.extensions.agent_team_dashboard --state-path <docs/agent-team/dashboard-state.json>`
- root state file: `docs/agent-team/dashboard-state.json`
- the root state file points at the active run under `docs/agent-team/runs/<run_id>/`

## Execution Loop

1. Call `agent_team_plan`.
2. If the tool says the mode is not needed and the user did not ask for it, stay in single-agent mode.
3. If the task is complex and the user did not ask for the mode, suggest it briefly.
4. If the user requests the mode or accepts the suggestion, call `agent_team_bootstrap`.
5. Call `agent_team_script`.
6. Keep the main agent in the `Coordinator + Integrator` role for the whole run.
7. Do the opening planning pass early, then keep coordination and integration on the main agent instead of collapsing into a pure planner role.
8. Stabilize the current-run plan and interfaces, then spawn all independent workers before the first `wait`.
9. Require every worker and reviewer message to start with its assigned identity prefix.
10. While workers run, keep updating shared state, preparing reviewer packets, and handling non-blocking local coordination work.
11. Trigger the reviewer only after there is real worker output to inspect.
12. Trigger the integrator only after multiple worker slices converge.
13. Only wait when blocked on the next critical-path step.
14. Do not end the main agent turn until required worker results are consumed and final convergence is complete.

## Role Selection

The default team is small:

- `planner`
- `worker-*`
- `reviewer`

Add `integrator` only when there are multiple module owners.

If the request benefits from specialist personas, use `agency-agents-zh` selectively:

- search `references/catalog.md`
- load only the 1 to 3 most relevant agent files
- map those personas onto planner, worker, reviewer, or integrator responsibilities

Do not load the entire persona catalog.

## Suggestion Rule

When the problem is complex and the user did not request the mode, suggest it in one short message before switching. Keep the suggestion practical. Explain that the mode gives:

- clearer module ownership
- cleaner review loops
- safer cross-module communication

## Constraints

- Do not spawn many agents for a small task.
- Only wait when blocked on the next critical-path step.
- Do not let workers edit the same file set unless the user explicitly accepts that risk.
- Do not skip the reviewer when the user asked for agent-to-agent review behavior.
- Do not claim direct agent chat exists; the coordinator remains the communication hub.
- Do not allow workers to post unprefixed summaries; every major output must start with the assigned prefix.
- Do not spawn one worker and immediately `wait` unless the next critical-path step is actually blocked on that result.
