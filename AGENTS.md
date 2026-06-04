# AGENTS.md

Quick orientation for coding agents working on `agent-readiness-mcp`.

## Canonical commands

```
# install dependencies
pip install -e ".[dev]"

# run the test suite
python -m pytest tests/

# run the linter
ruff check .
```

## Do not touch

Do not touch these without an explicit ask:

- the engine wheel (`agent-readiness`) — fix scanner bugs upstream
  in [`agent-readiness`](https://github.com/harrydaihaolin/agent-readiness),
  not by patching here
- the MCP SDK (`mcp` package) — we follow upstream
- the `dist/` and `build/` directories (generated)

## Where things live

Entry points: `agent-readiness-mcp` (server). Tests live under `tests/`.

## Headless, prompt-only (v0.9.0)

This server has **no dashboard and no live-scan process**. Every scan
runs synchronously in-process and returns its report inline in chat.
The dashboard / live-scan tools (`scan_workspace_async_tool`,
`get_scan_status_tool`, `stop_scan_tool`, `list_scans_tool`,
`render_workspace_report_tool`) and the redundant
`check_workspace_readiness_tool` were removed in v0.9.0. Do not
re-add a browser surface here — prompt generation is the contract.

## Scan tools

### `inspect_tool(path)`

Fast (~200ms) pre-flight. Returns `{ enumeration, classification }`
where `classification.suggested_type` is `single_repo` / `monorepo` /
`workspace`. Always call this first to pick the scan tool.

### `scan_repo_tool(path)` / `scan_monorepo_tool(path)`

Score one repository in-process and return the `ReadinessReport` JSON
inline (`overall_score`, `pillars`, `top_action`). `scan_repo_tool`
returns the structured `multi_repo_workspace` payload if the path holds
several repos.

### `scan_workspace_tool(path, children)`

Score a workspace of independent repos. `children` is the list of
`.git` repo paths (from `inspect`'s `enumeration.repos`). Returns the
5-pillar `WorkspaceReadinessReport` inline — the fifth pillar,
Coordination, is workspace-only and its `top_action.fix_prompt` is the
paste-ready coordination prompt to surface to the user.

### `list_friction_tool(path)` / `apply_top_action_tool(path)`

`list_friction_tool` returns every WARN/ERROR finding with its
paste-ready `fix_prompt` (sorted by impact). `apply_top_action_tool`
lands the single highest-impact structured fix.

## Standard agent workflow

1. `inspect_tool(path)` → read `classification.suggested_type`.
2. Call exactly one of `scan_repo_tool` / `scan_monorepo_tool` /
   `scan_workspace_tool(path, children)`.
3. Present the score + the generated `fix_prompt`s in chat
   (`list_friction_tool` for single/monorepo; the workspace
   `top_action` for workspaces).
4. Offer `apply_top_action_tool` or to write a root `AGENTS.md`.

## Reporting issues

When something in this file is wrong, file a PR that updates AGENTS.md
*first* and the code change second.
