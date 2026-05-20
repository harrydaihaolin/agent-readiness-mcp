# agent-readiness-mcp

MCP server exposing `agent-readiness` to coding agents (Claude Desktop,
Cursor, custom hosts).

Two tools, both backed by the [`agent-readiness`](https://pypi.org/project/agent-readiness/)
engine wheel:

- `scan_repo(path)` — scan a repo and return the readiness report
  (overall score, per-pillar scores, every check, and the `top_action`
  pin).
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
