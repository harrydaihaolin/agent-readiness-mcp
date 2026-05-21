# agent-readiness-mcp

MCP server exposing `agent-readiness` to coding agents (Claude Desktop,
Cursor, custom hosts).

Five tools, all backed by the [`agent-readiness`](https://pypi.org/project/agent-readiness/)
engine wheel:

- `detect_workspace(path)` — classify the path as `single_repo` /
  `monorepo` / `multi_repo_workspace`. Returns the engine's
  `detect_v1` envelope (repos list with `AGENTS.md` enrichment,
  drift warnings, signals fired). Call this first when the
  user-supplied path is ambiguous; the result tells you whether
  to chain `scan_repo` or `scan_workspace`. Added in `0.2.0` /
  `agent-readiness 2.5.0`.
- `scan_workspace(path, select=None)` — fan out scans across every
  detected repo in a multi-repo workspace (or a named subset via
  `select`). Returns one envelope with a `scanned` list of reports
  and a `skipped` list of not-selected / failed / unmatched names.
  Also works on single-repo paths (single-entry `scanned`) so the
  caller can branch on classification alone.
- `scan_repo(path)` — scan a repo and return the readiness report
  (overall score, per-pillar scores, every check, and the `top_action`
  pin). On a multi-repo path the tool returns a structured
  `multi_repo_workspace` error payload pointing at `scan_workspace`
  / `detect_workspace`, mirroring `agent-readiness scan`'s CLI
  contract.
- `list_friction(path)` — return every WARN/ERROR finding paired with
  its paste-ready `fix_prompt`, sorted by `score_impact` descending.
  Drops findings without a prompt (the contract is "paste-ready only").
- `apply_top_action(path, run_verify=True)` — apply the structured fix
  the engine pinned and run its verify command. Returns an
  `ApplyResult` so the caller can decide whether to commit.
  Requires `agent-readiness >= 2.4.0` (the engine release that ships
  the `apply_action` module).

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
