"""MCP server core.

The server is **headless and prompt-only**: every scan returns its
report inline in chat — there is no browser, no dashboard, and no
background live-scan process. The core tools backed by the
agent-readiness engine are:

* ``inspect(path)``                        -> InspectResult (enumeration
                                              + suggested workspace type)
* ``detect_workspace(path)``               -> detect_v1 envelope
* ``enumerate_workspace(path)``            -> static enumeration envelope
* ``scan_repo(path)``                      -> ReadinessReport JSON envelope
* ``scan_monorepo(path)``                  -> ReadinessReport JSON envelope
* ``check_workspace_readiness(path, sel)`` -> workspace_readiness envelope
                                              (exposed as scan_workspace_tool)
* ``apply_top_action(path, verify)``       -> ApplyResult JSON envelope
* ``list_friction(path)``                  -> list of {rule_id, severity,
                                              message, fix_prompt, verify}
* ``manifest_validate(path)``              -> ManifestValidationResult
                                              JSON envelope (workspace
                                              bible loader + validator)

The typical flow is ``inspect`` → exactly one of ``scan_repo`` /
``scan_monorepo`` / ``scan_workspace_tool(path, children)`` → present
the score and the engine-generated ``fix_prompt``s (via
``list_friction`` / the workspace ``top_action``) in chat → optionally
``apply_top_action``.

``scan_repo`` / ``scan_monorepo`` score one repository in-process and
surface the engine's structured ``multi_repo_workspace`` error rather
than silently scoring a parent dir. ``apply_top_action`` acts on the
single highest-impact item; ``list_friction`` returns every actionable
item with its paste-ready agent prompt so the caller can iterate
item-by-item.

All calls are synchronous and bounded by the engine's own runtime
budget. The workspace scan fans out over children sequentially.

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

from agent_readiness_mcp import gaps as _gaps


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


def check_workspace_readiness(
    path: str,
    children_paths: list[str],
) -> dict[str, Any]:
    """Workspace-level readiness scan.

    Runs the Coordination pack at ``path`` and the per-repo scan on
    each ``children_paths`` entry. Returns the 5-pillar workspace
    envelope (Coordination is workspace-only; the other four are
    aggregated from the children).

    ``children_paths`` is the caller's classification output — the
    LLM decided who belongs to this workspace, and the tool trusts
    that decision (it does not re-enumerate). Raises ``ValueError``
    if ``children_paths`` is empty: the skill should classify before
    calling this tool.
    """
    from agent_readiness.workspace_scan import scan as _scan

    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"path is not a directory: {root}")

    children: list[Path] = []
    for c in children_paths:
        cp = Path(c).expanduser()
        if not cp.is_absolute():
            cp = (root / cp).resolve()
        else:
            cp = cp.resolve()
        children.append(cp)

    return _scan(root, children).to_dict()


def _score_repo(repo: Path) -> dict[str, Any]:
    """Run the rules engine over ``repo`` and return the ReadinessReport dict.

    Headless and in-process — no dashboard, no subprocess. The returned
    dict carries ``overall_score``, ``pillar_scores``, every ``pillars[]``
    entry (with ``checks[].findings[]`` and ``fix_prompt``), and the
    ``top_action`` pin. ``list_friction`` and ``apply_top_action`` read
    those fields directly.
    """
    from agent_readiness.context import RepoContext
    from agent_readiness.rules_eval import evaluate_rules
    from agent_readiness.rules_runtime import load_default_rules
    from agent_readiness.scorer import score as score_results

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


def scan_repo(path: str) -> dict[str, Any]:
    """Scan ``path`` as a single repo and return the readiness report inline.

    Headless: no browser, no dashboard. Returns the JSON-serialisable
    ReadinessReport (``overall_score``, ``pillar_scores``, every check
    result, and the ``top_action`` pin). Callers chain
    ``apply_top_action`` to land the recommended fix or ``list_friction``
    to enumerate every paste-ready ``fix_prompt``.

    Raises :class:`MultiRepoWorkspaceError` when ``path`` is a multi-repo
    workspace — same contract as ``agent-readiness scan`` from the CLI.
    Use :func:`scan_monorepo` for a one-git-root monorepo, or
    :func:`check_workspace_readiness` for a workspace of independents.
    """
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
                "`detect_workspace(path)` to list them, or "
                "`scan_workspace_tool(path, children=[...])` to scan them"
            ),
            "detected_repos": [r.name for r in classification.repos],
            "root": classification.root,
            "version": classification.version,
        })
    return _score_repo(repo)


def scan_monorepo(path: str) -> dict[str, Any]:
    """Scan ``path`` as a monorepo (one .git at root) and return the report.

    Headless, like :func:`scan_repo`, but skips the multi-repo guard:
    the caller has already classified ``path`` as a monorepo (via
    ``inspect``), so the root is scored directly as one repository.
    """
    repo = Path(path).expanduser().resolve()
    if not repo.is_dir():
        raise ValueError(f"path is not a directory: {repo}")
    return _score_repo(repo)


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


def manifest_validate(path: str = ".") -> dict[str, Any]:
    """Validate an agent-readiness manifest directory.

    Returns the ManifestValidationResult JSON envelope (byte-identical
    to ``agent-readiness manifest validate <path> --json``). On unexpected
    failures (e.g. import errors when the engine wheel is missing) the
    function still returns a well-formed envelope with ``valid: false``
    and the underlying exception text in the issues list, so callers
    never see a stack trace.
    """
    try:
        from agent_readiness.manifest import validate_manifest_dir
    except ImportError as exc:
        return {
            "apiVersion": "agent-readiness.io/v1",
            "kind": "ManifestValidationResult",
            "summary": {
                "valid": False, "manifest_name": "",
                "errors": 1, "warnings": 0, "infos": 0,
            },
            "issues": [{
                "severity": "error",
                "message": f"agent_readiness.manifest unavailable: {exc}",
                "location": path,
            }],
        }
    try:
        result = validate_manifest_dir(Path(path))
    except Exception as exc:  # noqa: BLE001
        return {
            "apiVersion": "agent-readiness.io/v1",
            "kind": "ManifestValidationResult",
            "summary": {
                "valid": False, "manifest_name": "",
                "errors": 1, "warnings": 0, "infos": 0,
            },
            "issues": [{
                "severity": "error",
                "message": str(exc),
                "location": path,
            }],
        }
    return result.to_json_envelope()


def ontology(subcmd: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Passthrough to the agent-readiness-ontology-mcp server tools.

    Lets a skill user install only ``agent-readiness-mcp`` and still reach
    the ontology bootstrap (and later runtime) tools.

    ``subcmd`` is one of the tool names registered in
    :mod:`agent_readiness_ontology_mcp.server`. ``arguments`` is forwarded
    as keyword args to that tool.

    ``agent-readiness-ontology-mcp`` is a soft extra; install with
    ``pip install agent-readiness-mcp[ontology]`` to enable this tool.
    """
    try:
        from agent_readiness_ontology_mcp.server import TOOL_REGISTRY
    except ImportError as exc:
        raise ImportError(
            "ontology passthrough requires the optional "
            "'ontology' extra: pip install agent-readiness-mcp[ontology]"
        ) from exc

    if subcmd not in TOOL_REGISTRY:
        raise ValueError(
            f"Unknown ontology subcmd: {subcmd!r}. "
            f"Known: {sorted(TOOL_REGISTRY)}"
        )
    args = arguments or {}
    return TOOL_REGISTRY[subcmd](**args)


# ---------- inspect (fast pre-flight classifier) --------------------------


def inspect(path: str) -> dict[str, Any]:
    """Run `agent-readiness inspect <path> --json` and return the parsed
    envelope as a dict. Used by the MCP `inspect_tool` wrapper."""
    import subprocess

    proc = subprocess.run(
        ["agent-readiness", "inspect", path, "--json"],
        capture_output=True, text=True, check=False, timeout=30,
    )
    if proc.returncode != 0:
        return {
            "status": "error",
            "error": proc.stderr.strip() or "agent-readiness inspect failed",
            "exit_code": proc.returncode,
        }
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {
            "status": "error",
            "error": f"non-JSON output from inspect: {exc}",
            "raw_stdout": proc.stdout,
        }


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

    injected = globals().get("_INJECTED_SERVER_FOR_TEST")
    server = injected if injected is not None else FastMCP("agent-readiness")

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
        """Enumerate PATH's direct children AND classify the layout.

        Returns the ``EnumerationReport`` JSON envelope (``root``,
        ``children[]``, ``manifest_signals``, ``stats``). Prefer
        ``inspect_tool`` for the fast pre-flight classification used to
        pick a scan tool; this tool exists for callers that want the
        richer raw enumeration.

        Once you know the type, chain the matching **headless** scan
        tool (each returns its report inline, no dashboard):

          - single repo / monorepo → ``scan_repo_tool(path)`` /
            ``scan_monorepo_tool(path)``.
          - workspace of independents →
            ``scan_workspace_tool(path, children=[...])`` with the
            ``.git`` child paths.
        """
        return json.dumps(enumerate_workspace(path), indent=2)

    @server.tool()
    def scan_workspace_tool(path: str, children: list[str]) -> str:
        """Score PATH as a workspace of independent repos — headless.

        Runs the Coordination pack at ``path`` and a per-repo scan on
        each entry in ``children`` (the ``.git`` repo paths from
        ``inspect``'s enumeration), then returns the 5-pillar
        ``WorkspaceReadinessReport`` JSON **inline** — no browser, no
        dashboard. The envelope carries ``overall_score``, the five
        ``pillars`` (the fifth, Coordination, is workspace-only), the
        per-child cards (worst-first), and a single ``top_action``
        whose ``fix_prompt`` is the paste-ready Coordination prompt to
        surface to the user.

        ``children`` is the caller's classification output — the LLM
        decided who belongs to this workspace, and the tool trusts that
        decision (it does not re-enumerate). Pass at least one path;
        an empty list returns an ``invalid_input`` error.

        For single repos call ``scan_repo_tool``; for monorepos call
        ``scan_monorepo_tool``."""
        try:
            envelope = check_workspace_readiness(path, children)
        except ValueError as exc:
            return json.dumps({"error": "invalid_input", "message": str(exc)})
        return json.dumps(envelope, indent=2)

    @server.tool()
    def inspect_tool(path: str) -> str:
        """Fast pre-flight: enumerate PATH and suggest a workspace type.

        Returns ``InspectResult`` JSON:

          ``{
            "enumeration": {
              "root": "...", "root_has_git": bool, "repos": [...],
              "directories_walked": int, "elapsed_ms": int
            },
            "classification": {
              "suggested_type": "single_repo" | "monorepo" | "workspace",
              "confidence": "high" | "medium" | "low",
              "rationale": "..."
            }
          }``

        Call this BEFORE picking which scan tool to invoke. Then:

          - ``classification.suggested_type == "single_repo"`` → call
            ``scan_repo_tool(path)``.
          - ``classification.suggested_type == "monorepo"`` → call
            ``scan_monorepo_tool(path)``.
          - ``classification.suggested_type == "workspace"`` → call
            ``scan_workspace_tool(path, children=[...])`` where children
            is the list of ``.git`` repo paths from
            ``enumeration.repos``.

        Each scan tool runs headlessly and returns the readiness report
        **inline in chat** — no browser, no dashboard. Returns in
        ~200ms for trees under ~5k directories."""
        return json.dumps(inspect(path), indent=2)

    @server.tool()
    def scan_repo_tool(path: str) -> str:
        """Score PATH as a single repository — headless.

        Returns the ``ReadinessReport`` JSON **inline** (no browser, no
        dashboard): ``overall_score``, ``pillar_scores``, every check
        result, and the ``top_action`` pin. Follow with
        ``list_friction_tool(path)`` to surface every paste-ready
        ``fix_prompt``, or ``apply_top_action_tool(path)`` to land the
        top fix.

        Errors with an ``multi_repo_workspace`` payload if PATH actually
        holds several repos — call ``scan_workspace_tool`` for that
        case. For monorepos call ``scan_monorepo_tool``. If you don't
        know, call ``inspect_tool`` first."""
        try:
            return json.dumps(scan_repo(path), indent=2)
        except MultiRepoWorkspaceError as exc:
            return json.dumps(exc.payload, indent=2)
        except ValueError as exc:
            return json.dumps({"status": "error", "error": str(exc)})

    @server.tool()
    def scan_monorepo_tool(path: str) -> str:
        """Score PATH as a monorepo (one .git at root, many packages) — headless.

        Returns the ``ReadinessReport`` JSON **inline** (no browser, no
        dashboard), scoring the root as a single repository. Follow with
        ``list_friction_tool`` / ``apply_top_action_tool`` exactly as
        for ``scan_repo_tool``.

        For single repos call ``scan_repo_tool``; for workspaces of
        independent repos call ``scan_workspace_tool``."""
        try:
            return json.dumps(scan_monorepo(path), indent=2)
        except ValueError as exc:
            return json.dumps({"status": "error", "error": str(exc)})

    @server.tool()
    def apply_top_action_tool(path: str, run_verify: bool = True) -> str:
        """Apply the top_action pinned by a fresh scan of ``path``.

        Note (v0.6.0): with the engine's per-rule confidence gating
        (agent-readiness v3.2.0+), ``apply_top_action`` may return
        ``{"confirm_required": true}`` (rule with confidence=medium)
        or ``{"gap_payload": {...}}`` (rule with confidence=low) and
        leave the working copy untouched. Pair with
        ``confirm_apply_tool`` to round-trip the medium-confidence
        case once the agent has user approval.
        """
        return json.dumps(apply_top_action(path, run_verify=run_verify), indent=2)

    # ----- Bundle B: gap-aware tools + ambiguity-refusing apply ----------

    @server.tool()
    def record_gap_tool(
        path: str,
        kind: str,
        detail: str,
        severity: str = "medium",
        candidate_resolutions: list[str] | None = None,
        agent_session: str | None = None,
    ) -> str:
        """Record a Gap the agent recognised but couldn't confidently resolve.

        Writes to ``<path>/.agent-readiness/gaps.jsonl`` and surfaces
        as a Finding on the next scan via ``ontology.gaps_unresolved``
        until ``agent-readiness gap resolve <id>`` flips it.

        Use when the agent considered multiple resolutions but couldn't
        pick one with high confidence — recording a gap is preferable
        to applying the wrong fix or silently dropping the question.
        """
        return json.dumps(
            _gaps.record_gap(
                path,
                kind=kind,
                detail=detail,
                severity=severity,
                candidate_resolutions=candidate_resolutions,
                agent_session=agent_session,
            ),
            indent=2,
        )

    @server.tool()
    def ask_clarification_tool(
        path: str,
        question: str,
        options: list[str] | None = None,
        context_path: str | None = None,
        agent_session: str | None = None,
    ) -> str:
        """Surface a clarification question for a human reviewer (fire-and-forget).

        Persists to ``.agent-readiness/gaps.jsonl`` alongside Gaps and
        Assumptions, discriminated by ``kind="clarification"``. v1 is
        non-blocking — the agent continues; the clarification is
        visible via ``agent-readiness gap list --all``.
        """
        return json.dumps(
            _gaps.ask_clarification(
                path,
                question=question,
                options=options,
                context_path=context_path,
                agent_session=agent_session,
            ),
            indent=2,
        )

    @server.tool()
    def log_assumption_tool(
        path: str,
        assumption: str,
        justification: str,
        expires_after: str | None = None,
        agent_session: str | None = None,
    ) -> str:
        """Log an assumption the agent made so future agents can audit it.

        Audit-only — assumptions don't cost score, but they surface in
        ``agent-readiness gap list --all`` so a reviewer can rebut any
        that no longer hold.
        """
        return json.dumps(
            _gaps.log_assumption(
                path,
                assumption=assumption,
                justification=justification,
                expires_after=expires_after,
                agent_session=agent_session,
            ),
            indent=2,
        )

    @server.tool()
    def confirm_apply_tool(
        path: str,
        approved: bool,
        run_verify: bool = True,
        agent_session: str | None = None,
    ) -> str:
        """Round-trip the medium-confidence apply path with a user decision.

        Use after ``apply_top_action_tool`` returns
        ``{"confirm_required": true}``: ask the user, then call this
        with ``approved=True`` to apply (forces confidence to high)
        or ``approved=False`` to record a Gap (so the unresolved
        ambiguity surfaces on the next scan via
        ``ontology.gaps_unresolved``).
        """
        return json.dumps(
            _gaps.confirm_apply(
                path,
                approved=approved,
                run_verify=run_verify,
                agent_session=agent_session,
            ),
            indent=2,
        )

    @server.tool()
    def list_friction_tool(path: str) -> str:
        """List every WARN/ERROR friction item with its paste-ready prompt.

        Use when the caller wants to iterate item-by-item rather than
        chaining ``apply_top_action`` after each fix. Items are sorted
        by ``score_impact`` descending; items without a ``fix_prompt``
        are excluded.
        """
        return json.dumps(list_friction(path), indent=2)

    @server.tool()
    def manifest_validate_tool(path: str = ".") -> str:
        """Validate an agent-readiness manifest directory.

        Returns the ManifestValidationResult JSON envelope —
        byte-identical to ``agent-readiness manifest validate <path>
        --json``. Use when the caller hands you the path to a workspace
        bible directory and wants schema + semantic checks (declared-tag
        axes, arch-rule id-vs-filename prefix).
        """
        return json.dumps(manifest_validate(path), indent=2)

    @server.tool()
    def ontology_tool(subcmd: str, arguments: dict[str, Any] | None = None) -> str:
        """Passthrough to agent-readiness-ontology-mcp tools.

        ``subcmd`` is one of the bootstrap tool names (e.g.
        ``bootstrap_init``). ``arguments`` is forwarded as keyword args
        to that tool.
        """
        return json.dumps(ontology(subcmd, arguments=arguments), indent=2)

    if injected is not None:
        return
    if transport != "stdio":
        raise ValueError(f"unsupported transport: {transport!r}")
    server.run()


__all__ = [
    "MultiRepoWorkspaceError",
    "apply_top_action",
    "check_workspace_readiness",
    "detect_workspace",
    "enumerate_workspace",
    "inspect",
    "list_friction",
    "manifest_validate",
    "ontology",
    "scan_monorepo",
    "scan_repo",
    "serve",
    # Bundle B (v0.6.0): gap-aware tools + ambiguity-refusing apply
    # round-trip. Re-exported from agent_readiness_mcp.gaps.
    "ask_clarification",
    "confirm_apply",
    "log_assumption",
    "record_gap",
]


# Re-export gap-aware wrappers at the package's module level so
# downstream callers can ``from agent_readiness_mcp.server import
# record_gap`` without having to know about the ``gaps`` submodule.
ask_clarification = _gaps.ask_clarification
confirm_apply = _gaps.confirm_apply
log_assumption = _gaps.log_assumption
record_gap = _gaps.record_gap
