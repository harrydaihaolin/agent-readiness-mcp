# agent-readiness-mcp

MCP server exposing `agent-readiness` to coding agents (Claude Desktop,
Cursor, custom hosts).

**Headless and prompt-only:** every scan runs synchronously in-process
and returns its report inline in chat — there is no browser, no
dashboard, and no background scan. The tools, all backed by the
[`agent-readiness`](https://pypi.org/project/agent-readiness/) engine
wheel:

- `inspect(path)` — fast (~200ms) pre-flight. Returns
  `{ enumeration, classification }`, where
  `classification.suggested_type` is `single_repo` / `monorepo` /
  `workspace`. Call this first to pick the scan tool.
- `scan_repo(path)` / `scan_monorepo(path)` — scan one repository and
  return the readiness report (overall score, per-pillar scores, every
  check, and the `top_action` pin). On a multi-repo path `scan_repo`
  returns a structured `multi_repo_workspace` payload pointing at
  `scan_workspace_tool`.
- `scan_workspace_tool(path, children)` — scan a workspace of
  independent repos (`children` = the `.git` repo paths from
  `inspect`). Returns the 5-pillar `WorkspaceReadinessReport`; the
  fifth pillar, Coordination, is workspace-only.
- `detect_workspace(path)` / `enumerate_workspace(path)` — richer
  classification / raw enumeration envelopes for callers that want them.
- `list_friction(path)` — return every WARN/ERROR finding paired with
  its paste-ready `fix_prompt`, sorted by `score_impact` descending.
  Drops findings without a prompt (the contract is "paste-ready only").
- `apply_top_action(path, run_verify=True)` — apply the structured fix
  the engine pinned and run its verify command. Returns an
  `ApplyResult` so the caller can decide whether to commit.

## Install

```bash
pip install agent-readiness-mcp
```

## Run

Start the server on stdio (the format Claude Desktop and Cursor speak):

```bash
agent-readiness-mcp --transport stdio
```

For a Claude Desktop install, add to `~/.claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "agent-readiness": {
      "command": "agent-readiness-mcp",
      "args": ["--transport", "stdio"]
    }
  }
}
```

## License

MIT.
