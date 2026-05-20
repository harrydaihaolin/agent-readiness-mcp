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

## Reporting issues

When something in this file is wrong, file a PR that updates AGENTS.md
*first* and the code change second.
