"""MCP server core.

The server exposes three tools backed by the agent-readiness engine:

* ``scan_repo(path)``                 -> ReadinessReport JSON envelope
* ``apply_top_action(path, verify)``  -> ApplyResult JSON envelope
* ``list_friction(path)``             -> list of {rule_id, severity,
                                         message, fix_prompt, verify}

``apply_top_action`` acts on the single highest-impact item;
``list_friction`` returns every actionable item with its paste-ready
agent prompt so an in-IDE caller can iterate item-by-item instead of
chaining one top-action at a time.

All calls are synchronous and bounded by the engine's own runtime
budget; we don't wrap them in extra background tasks because the MCP
client surfaces a long-running indicator natively.

Implementation notes:

* We deliberately import ``agent_readiness`` lazily from inside the
  tool functions so the MCP package starts even when the engine wheel
  is missing (the user gets a clean error on the first tool call
  rather than a stack trace at import time).
* The MCP request schema for the tools is intentionally narrow —
  one required ``path`` argument plus an optional ``verify`` boolean.
  Adding more knobs (rule filters, weight overrides, json vs rich
  rendering) gates future per-client demand.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def scan_repo(path: str) -> dict[str, Any]:
    """Scan ``path`` and return the JSON-serialisable readiness report.

    Includes ``overall_score``, ``pillar_scores``, every check result,
    and the ``top_action`` pin (the single highest-priority structured
    fix the engine recommends). Callers chain ``apply_top_action`` to
    actually land the recommended fix.
    """
    from agent_readiness.context import RepoContext
    from agent_readiness.rules_eval import evaluate_rules
    from agent_readiness.rules_runtime import load_default_rules
    from agent_readiness.scorer import score as score_results

    repo = Path(path).expanduser().resolve()
    if not repo.is_dir():
        raise ValueError(f"path is not a directory: {repo}")

    rules = load_default_rules()
    if not rules:
        raise RuntimeError(
            "agent-readiness rules pack is missing; reinstall agent-readiness."
        )
    ctx = RepoContext(root=repo)
    results = []
    for rule in rules:
        results.extend(evaluate_rules([rule], ctx))
    report = score_results(repo, results)
    report.languages = ctx.detected_languages
    return report.to_dict()


def apply_top_action(path: str, run_verify: bool = True) -> dict[str, Any]:
    """Apply the report's pinned ``top_action`` to ``path`` on disk.

    Returns the ``ApplyResult`` envelope (``applied``, ``verified``,
    ``written``, optional ``verify`` block). The caller decides
    whether to commit the change, revert it, or escalate.
    """
    from agent_readiness.apply_action import apply_top_action as _apply_top_action

    report = scan_repo(path)
    top = report.get("top_action")
    result = _apply_top_action(top, Path(path).expanduser().resolve(), run_verify=run_verify)
    return result.to_dict()


def list_friction(path: str) -> list[dict[str, Any]]:
    """Return every actionable friction item with its paste-ready prompt.

    Output is a list of ``{rule_id, pillar, severity, message,
    fix_prompt, verify, score_impact}`` dicts, sorted by ``score_impact``
    descending so the highest-leverage items come first. Items without
    a ``fix_prompt`` populated (v1 rules, synthetic low-score rows) are
    omitted — the contract of this tool is "paste-ready prompts only".

    Only WARN- and ERROR-severity findings are returned. INFO findings
    describe the situation rather than friction the maintainer should
    fix and would inflate the list with noise.
    """
    report = scan_repo(path)
    items: list[dict[str, Any]] = []
    for pillar in report.get("pillars", []):
        for check in pillar.get("checks", []):
            impact = check.get("score_impact") or 0.0
            for finding in check.get("findings", []):
                sev = finding.get("severity")
                if sev not in ("warn", "error"):
                    continue
                prompt = finding.get("fix_prompt")
                if not prompt:
                    continue
                items.append({
                    "rule_id": check.get("check_id"),
                    "pillar": pillar.get("pillar"),
                    "severity": sev,
                    "message": finding.get("message"),
                    "fix_prompt": prompt,
                    "verify": finding.get("verify"),
                    "score_impact": impact,
                })
    items.sort(key=lambda it: -it["score_impact"])
    return items


# ---------- MCP transport layer -------------------------------------------


def serve(transport: str = "stdio") -> None:
    """Start the MCP server on the requested transport.

    Currently only ``stdio`` is supported (the format Claude Desktop
    and Cursor speak). The official ``mcp`` Python SDK provides the
    transport plumbing; we register two tools and hand off.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise ImportError(
            "MCP support requires the 'mcp' package. Install with: "
            "pip install agent-readiness-mcp"
        ) from exc

    server = FastMCP("agent-readiness")

    @server.tool()
    def scan_repo_tool(path: str) -> str:
        """Scan a repository and return the readiness report JSON."""
        return json.dumps(scan_repo(path), indent=2)

    @server.tool()
    def apply_top_action_tool(path: str, run_verify: bool = True) -> str:
        """Apply the top_action pinned by a fresh scan of ``path``."""
        return json.dumps(apply_top_action(path, run_verify=run_verify), indent=2)

    @server.tool()
    def list_friction_tool(path: str) -> str:
        """List every WARN/ERROR friction item with its paste-ready prompt.

        Use when the caller wants to iterate item-by-item rather than
        chaining ``apply_top_action`` after each fix. Items are sorted
        by ``score_impact`` descending; items without a ``fix_prompt``
        are excluded.
        """
        return json.dumps(list_friction(path), indent=2)

    if transport != "stdio":
        raise ValueError(f"unsupported transport: {transport!r}")
    server.run()


__all__ = ["scan_repo", "apply_top_action", "list_friction", "serve"]
