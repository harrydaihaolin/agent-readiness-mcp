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

## Live scan tools (Plan 3)

Five additional tools surface Plan 1's progressive scan + render
infrastructure. Each is a thin wrapper over the agent-readiness CLI;
state lives entirely in `~/.agent-readiness/scans/`, not in the MCP
server process.

### `scan_workspace_async_tool(workspace_path, children?)`

Kick off `agent-readiness scan-and-view` as a detached subprocess.
Returns within ~2 seconds with `{ dashboard_url, scan_id, pid, ... }`.
The agent should share `dashboard_url` with the user. The scan runs
in the background; poll `get_scan_status_tool` before reading final
results. If a live scan already exists for `workspace_path`, returns
that scan's URL instead of starting a new one.

### `get_scan_status_tool(scan_id)`

Return current status for a scan: `{ scan_id, status, progress,
dashboard_url, overall_score, completed_at }`. Cheap; safe to call
repeatedly.

### `stop_scan_tool(scan_id | "all")`

Stop one scan or every running scan. PID-stamp verifies before SIGTERM;
mismatched stamps are skipped. Returns `{ ok, killed: [...], skipped: [...] }`.

### `list_scans_tool()`

Enumerate active + recent scans across every workspace under
`~/.agent-readiness/scans/`. Returns `{ active, recent, total_disk_bytes }`.

### `render_workspace_report_tool(workspace_path, scan_id?, output_dir?)`

Render a scan as a portable static directory. Returns
`{ index_path, output_dir, source_status, ... }`. Share `index_path`
with the user; if `file://` routing fails (browser CORS on some
setups), suggest `python -m http.server` in the output dir.

## Standard agent workflow

1. `scan_workspace_async_tool(path)` → share `dashboard_url` with the user
2. Continue with other tasks
3. Later: `get_scan_status_tool(scan_id)` → check completion
4. If user wants a shareable artifact: `render_workspace_report_tool(path)`

## Reporting issues

When something in this file is wrong, file a PR that updates AGENTS.md
*first* and the code change second.
