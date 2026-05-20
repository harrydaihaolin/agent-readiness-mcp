"""Model Context Protocol server for agent-readiness.

Exposes three tools to MCP clients (Claude Desktop, Cursor, custom
agents):

* ``scan_repo(path)`` — run agent-readiness against the given repo
  path and return the JSON report (includes the ``top_action``
  pin so the caller can decide whether to escalate to apply).
* ``apply_top_action(path, run_verify=True)`` — apply the structured
  edit attached to the report's ``top_action`` and (optionally) run
  the rule's verify command. Returns the apply result envelope so
  the caller knows whether to commit, revert, or escalate.
* ``list_friction(path)`` — return every WARN/ERROR finding paired
  with its paste-ready ``fix_prompt`` so the caller can iterate
  item-by-item instead of chaining single top-actions.

All tools are thin wrappers over the agent-readiness engine wheel —
no scanning logic lives here. That keeps the MCP server a stable seam
that follows the engine release cadence without requiring its own
deep tests.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
