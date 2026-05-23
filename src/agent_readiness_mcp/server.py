"""MCP server core.

The server exposes seven tools backed by the agent-readiness engine:

* ``detect_workspace(path)``               -> detect_v1 envelope
* ``enumerate_workspace(path)``            -> static enumeration envelope
* ``scan_workspace(path, select)``         -> multi-repo scan envelope
* ``check_workspace_readiness(path, sel)`` -> workspace_readiness envelope
* ``scan_repo(path)``                      -> ReadinessReport JSON envelope
* ``apply_top_action(path, verify)``       -> ApplyResult JSON envelope
* ``list_friction(path)``                  -> list of {rule_id, severity,
                                              message, fix_prompt, verify}

``detect_workspace`` and ``scan_workspace`` ship in v0.2.0 alongside
agent-readiness 2.5.0's workspace-detection module. ``scan_repo`` was
backwards-compatible with single-repo and monorepo paths from day one
and now also surfaces the engine's new structured ``multi_repo_workspace``
error to the caller instead of silently scoring the parent dir.

``apply_top_action`` acts on the single highest-impact item;
``list_friction`` returns every actionable item with its paste-ready
agent prompt so an in-IDE caller can iterate item-by-item instead of
chaining one top-action at a time.

All calls are synchronous and bounded by the engine's own runtime
budget; we don't wrap them in extra background tasks because the MCP
client surfaces a long-running indicator natively. ``scan_workspace``
fans out sequentially in v1 — parallel execution is a follow-up
gated by demand, because 17 sequential scans (our biggest known
workspace) take ~10s end-to-end on the reference cohort.

Implementation notes:

* We deliberately import ``agent_readiness`` lazily from inside the
  tool functions so the MCP package starts even when the engine wheel
  is missing (the user gets a clean error on the first tool call
  rather than a stack trace at import time).
* The MCP request schema for the tools is intentionally narrow —
  one required ``path`` argument plus an optional ``select`` /
  ``verify``. Adding more knobs (rule filters, weight overrides,
  json vs rich rendering) gates future per-client demand.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class MultiRepoWorkspaceError(Exception):
    """Raised by ``scan_repo`` when the path is a multi-repo workspace.

    The structured ``payload`` mirrors the JSON the engine CLI emits on
    stderr so MCP clients can deserialise it once and route through the
    same recovery path.
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(payload.get("hint", "multi_repo_workspace"))
        self.payload = payload


def detect_workspace(path: str) -> dict[str, Any]:
    """Classify ``path`` and return the ``detect_v1`` envelope.

    The envelope is wire-stable; consumers should check the ``version``
    string before parsing. The MCP client / skill drives any HITL
    selection conversation off this output (one of single_repo,
    monorepo, multi_repo_workspace).
    """
    from agent_readiness.workspace_detect import detect

    repo = Path(path).expanduser().resolve()
    if not repo.is_dir():
        raise ValueError(f"path is not a directory: {repo}")
    return detect(repo).to_dict()


def enumerate_workspace(path: str) -> dict[str, Any]:
    """Static depth-1 enumeration of ``path`` for workspace classification.

    Returns the ``EnumerationReport`` envelope as a dict. The skill consumes
    this and decides (in the LLM step) whether ``path`` is a single repo,
    monorepo, or workspace of independents — call ``scan_repo`` or
    ``check_workspace_readiness`` accordingly.

    Unlike :func:`detect_workspace`, this tool does NOT classify. It
    returns raw structure; classification is the LLM's job. Use this
    when the static signals (``detect_workspace``) aren't enough and
    the caller needs a richer view of the tree.
    """
    from agent_readiness.enumerate import enumerate_workspace as _enumerate

    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        raise ValueError(f"path is not a directory: {p}")
    return _enumerate(p).to_dict()


def scan_repo(path: str) -> dict[str, Any]:
    """Scan ``path`` and return the JSON-serialisable readiness report.

    Includes ``overall_score``, ``pillar_scores``, every check result,
    and the ``top_action`` pin (the single highest-priority structured
    fix the engine recommends). Callers chain ``apply_top_action`` to
    actually land the recommended fix.

    Raises :class:`MultiRepoWorkspaceError` when ``path`` is a multi-repo
    workspace — same contract as ``agent-readiness scan`` from the CLI
    (which exits 2 with a structured stderr envelope). Use
    :func:`scan_workspace` for that case, or :func:`detect_workspace`
    to enumerate before deciding.
    """
    from agent_readiness.context import RepoContext
    from agent_readiness.rules_eval import evaluate_rules
    from agent_readiness.rules_runtime import load_default_rules
    from agent_readiness.scorer import score as score_results
    from agent_readiness.workspace_detect import detect

    repo = Path(path).expanduser().resolve()
    if not repo.is_dir():
        raise ValueError(f"path is not a directory: {repo}")

    classification = detect(repo)
    if classification.classification == "multi_repo_workspace":
        raise MultiRepoWorkspaceError({
            "error": "multi_repo_workspace",
            "hint": (
                "this path contains multiple repos; call "
                "`detect_workspace(path)` to list them or "
                "`scan_workspace(path, select=[...])` to scan a subset"
            ),
            "detected_repos": [r.name for r in classification.repos],
            "root": classification.root,
            "version": classification.version,
        })

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


def scan_workspace(
    path: str,
    select: list[str] | None = None,
) -> dict[str, Any]:
    """Scan every detected repo in a multi-repo workspace (or a subset).

    On a single-repo or monorepo classification, returns a single-entry
    ``scanned`` list — same wire shape, so the skill can branch only on
    the ``classification`` field instead of dispatching to two tools.

    Selection rules:

    * ``select=None`` → scan everything detected.
    * ``select=[name, ...]`` → scan only the named repos. Names that
      don't match any detected repo land in ``skipped`` with
      ``reason="not detected"`` rather than failing the whole call —
      the skill is the right place to surface the user-facing error.
    """
    from agent_readiness.workspace_detect import detect

    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"path is not a directory: {root}")

    classification = detect(root)
    detected_by_name: dict[str, dict[str, Any]] = {}
    for r in classification.repos:
        detected_by_name[r.name] = {
            "name": r.name,
            "path": r.path,
            "rel_path": r.rel_path,
            "display_name": r.display_name,
        }

    if select is None:
        chosen = list(detected_by_name.keys())
        unmatched: list[str] = []
    else:
        chosen = [n for n in select if n in detected_by_name]
        unmatched = [n for n in select if n not in detected_by_name]

    scanned: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for name in detected_by_name:
        if name not in chosen:
            skipped.append({
                "name": name,
                "rel_path": detected_by_name[name]["rel_path"],
                "reason": "not in select list",
            })
            continue
        repo_path = detected_by_name[name]["path"]
        try:
            report = scan_repo(repo_path)
        except MultiRepoWorkspaceError as exc:
            # Nested multi_repo_workspace under a multi-repo root —
            # spec non-goal but defensive: skip rather than recurse.
            skipped.append({
                "name": name,
                "rel_path": detected_by_name[name]["rel_path"],
                "reason": "nested multi_repo_workspace",
                "detail": exc.payload,
            })
        except Exception as exc:
            skipped.append({
                "name": name,
                "rel_path": detected_by_name[name]["rel_path"],
                "reason": "scan_failed",
                "detail": str(exc),
            })
        else:
            scanned.append({
                "name": name,
                "rel_path": detected_by_name[name]["rel_path"],
                "display_name": detected_by_name[name]["display_name"],
                "report": report,
            })

    for name in unmatched:
        skipped.append({
            "name": name,
            "rel_path": None,
            "reason": "not detected",
        })

    return {
        "version": classification.version,
        "root": classification.root,
        "classification": classification.classification,
        "scanned": scanned,
        "skipped": skipped,
        "drift_warnings": [
            {
                "kind": w.kind,
                "agents_md_path": w.agents_md_path,
                "detected_path": w.detected_path,
                "message": w.message,
            }
            for w in classification.drift_warnings
        ],
    }


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
    out = result.to_dict()
    # The engine's ApplyResult.to_dict() strips None/empty fields for
    # cleanliness, but MCP clients want a stable envelope. Default the
    # two primary outcome fields so consumers can read them
    # unconditionally.
    out.setdefault("applied", False)
    out.setdefault("written", [])
    return out


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
    def detect_workspace_tool(path: str) -> str:
        """Classify PATH as single repo, monorepo, or multi-repo workspace.

        Returns the ``detect_v1`` envelope as JSON. Call this first
        when the user-supplied path is ambiguous; the result tells
        the client whether to chain ``scan_repo_tool`` (single repo /
        monorepo) or ``scan_workspace_tool`` (multi-repo).
        """
        return json.dumps(detect_workspace(path), indent=2)

    @server.tool()
    def enumerate_workspace_tool(path: str) -> str:
        """Enumerate PATH's direct children for workspace classification.

        Returns the ``EnumerationReport`` JSON envelope. Use this before
        any scan call when PATH is unfamiliar — the LLM classifies the
        result and decides whether to call ``scan_repo_tool`` (single
        repo / monorepo) or ``check_workspace_readiness_tool``
        (workspace).
        """
        return json.dumps(enumerate_workspace(path), indent=2)

    @server.tool()
    def scan_workspace_tool(
        path: str,
        select: list[str] | None = None,
    ) -> str:
        """Scan every detected repo in a workspace, or a named subset.

        ``select`` accepts a list of repo names (the ``name`` field
        from ``detect_workspace_tool``'s ``repos`` array). With
        ``select=None`` the tool scans everything detected. Names that
        don't match a detected repo land in ``skipped`` with
        ``reason="not detected"`` so the caller can surface a clean
        error.
        """
        return json.dumps(scan_workspace(path, select=select), indent=2)

    @server.tool()
    def scan_repo_tool(path: str) -> str:
        """Scan a repository and return the readiness report JSON.

        Raises a structured ``multi_repo_workspace`` error when the
        path is a multi-repo workspace — switch to
        ``scan_workspace_tool`` for that case.
        """
        try:
            return json.dumps(scan_repo(path), indent=2)
        except MultiRepoWorkspaceError as exc:
            # Surface the structured envelope as the tool's payload so
            # the client doesn't need exception introspection. The
            # `error` field disambiguates it from a successful report.
            return json.dumps(exc.payload, indent=2)

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


__all__ = [
    "MultiRepoWorkspaceError",
    "apply_top_action",
    "detect_workspace",
    "enumerate_workspace",
    "list_friction",
    "scan_repo",
    "scan_workspace",
    "serve",
]
