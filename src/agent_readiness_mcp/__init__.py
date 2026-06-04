"""Model Context Protocol server for agent-readiness.

Headless and prompt-only: every scan returns its report inline in chat
— no browser, no dashboard, no background live-scan process. The core
tools exposed to MCP clients (Claude Desktop, Cursor, custom agents):

* ``inspect(path)`` — fast pre-flight: enumerate the path and suggest a
  workspace type (single_repo / monorepo / workspace). Call this first
  to pick which scan tool to invoke.
* ``detect_workspace(path)`` / ``enumerate_workspace(path)`` — richer
  classification / raw enumeration envelopes for callers that want them.
* ``scan_repo(path)`` / ``scan_monorepo(path)`` — score one repository
  in-process and return the JSON ReadinessReport (includes the
  ``top_action`` pin). ``scan_repo`` raises ``MultiRepoWorkspaceError``
  on multi-repo paths, mirroring the engine CLI.
* ``scan_workspace_tool(path, children)`` — score a workspace of
  independent repos and return the 5-pillar WorkspaceReadinessReport
  inline (Coordination is the workspace-only pillar).
* ``apply_top_action(path, run_verify=True)`` — apply the structured
  edit attached to the report's ``top_action`` and (optionally) run
  the rule's verify command.
* ``list_friction(path)`` — return every WARN/ERROR finding paired
  with its paste-ready ``fix_prompt`` so the caller can iterate
  item-by-item.

All tools are thin wrappers over the agent-readiness engine wheel —
no scanning logic lives here. That keeps the MCP server a stable seam
that follows the engine release cadence without requiring its own
deep tests.
"""

from __future__ import annotations

__version__ = "0.9.0"

__all__ = ["__version__"]
