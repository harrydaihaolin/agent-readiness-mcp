"""CLI entry point: ``agent-readiness-mcp [--transport stdio]``."""

from __future__ import annotations

import argparse
import sys

from agent_readiness_mcp import __version__
from agent_readiness_mcp.server import serve


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="agent-readiness-mcp",
        description=(
            "Run the agent-readiness MCP server. Exposes scan_repo, "
            "apply_top_action, and list_friction tools to MCP-aware "
            "clients (Claude Desktop, Cursor, and any custom MCP host)."
        ),
    )
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio"],
        help="MCP transport to listen on (currently only stdio).",
    )
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()

    try:
        serve(transport=args.transport)
    except ImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
