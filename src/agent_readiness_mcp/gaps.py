"""MCP wrappers for the engine's ``agent_readiness.gaps`` module.

Bundle B / B1 of the 2026-05-26 ontology-driven-agent design. Thin
adapters that:

1. Translate the typed MCP tool arguments into kwargs for the
   engine module's ``record_gap`` / ``record_clarification`` /
   ``record_assumption`` helpers.
2. Run the call against ``Path(path)`` as the workspace root (the
   engine module writes to ``Path.cwd() / ".agent-readiness/gaps.jsonl"``
   by default; we pass ``root=`` explicitly so the MCP client can
   target an arbitrary repo without ``cd``-ing).
3. Return JSON envelopes shaped like the engine's other tool returns:
   ``{"recorded": True, "id": "...", "path": "..."}`` on success,
   ``{"error": "...", "message": "..."}`` on failure.

Also exposes ``confirm_apply`` — the round-trip companion to the
engine's medium-confidence apply branch. When ``apply_top_action_tool``
returns ``{"confirm_required": true}`` the agent should ask the user;
on approval, ``confirm_apply_tool(path, approved=True)`` re-invokes
apply with the rule's confidence overridden to ``high`` (forcing the
apply path); on rejection (``approved=False``), it records a Gap so
the unresolved ambiguity surfaces on the next scan via
``ontology.gaps_unresolved``.

Imports of ``agent_readiness.*`` are lazy (inside the wrapper
functions) so importing this module never fails when the engine wheel
is missing — same pattern as the rest of ``server.py``.

Added in agent-readiness-mcp v0.6.0.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = [
    "confirm_apply",
    "log_assumption",
    "record_gap",
    "ask_clarification",
]


def _root(path: str) -> Path:
    return Path(path).expanduser().resolve()


def record_gap(
    path: str,
    *,
    kind: str,
    detail: str,
    severity: str = "medium",
    candidate_resolutions: list[str] | None = None,
    agent_session: str | None = None,
) -> dict[str, Any]:
    """Append a new Gap row under ``<path>/.agent-readiness/gaps.jsonl``.

    Returns ``{"recorded": True, "id": "...", "path": "...",
    "gap_kind": "..."}``. The MCP client uses the returned id to refer
    to the gap later (e.g. when calling
    ``agent-readiness gap resolve <id>`` on the workspace).
    """
    try:
        from agent_readiness.gaps import (  # noqa: PLC0415 -- lazy
            record_gap as _record_gap,
        )
    except ImportError as exc:
        return {
            "error": "engine_not_installed",
            "message": (
                "agent-readiness>=3.2.0 is required for gap-aware tools "
                f"({exc})"
            ),
        }
    root = _root(path)
    new_id = _record_gap(
        kind=kind,
        detail=detail,
        severity=severity,
        candidate_resolutions=candidate_resolutions,
        agent_session=agent_session,
        root=root,
    )
    return {
        "recorded": True,
        "id": new_id,
        "gap_kind": kind,
        "path": str(root / ".agent-readiness" / "gaps.jsonl"),
    }


def ask_clarification(
    path: str,
    *,
    question: str,
    options: list[str] | None = None,
    context_path: str | None = None,
    agent_session: str | None = None,
) -> dict[str, Any]:
    """Append a new Clarification row.

    Fire-and-forget in v1 — the row is informational; the design
    defers blocking semantics to a later revision.
    """
    try:
        from agent_readiness.gaps import (  # noqa: PLC0415 -- lazy
            record_clarification as _record_clarification,
        )
    except ImportError as exc:
        return {
            "error": "engine_not_installed",
            "message": (
                "agent-readiness>=3.2.0 is required for gap-aware tools "
                f"({exc})"
            ),
        }
    root = _root(path)
    new_id = _record_clarification(
        question=question,
        options=options,
        context_path=context_path,
        agent_session=agent_session,
        root=root,
    )
    return {
        "recorded": True,
        "id": new_id,
        "kind": "clarification",
        "path": str(root / ".agent-readiness" / "gaps.jsonl"),
    }


def log_assumption(
    path: str,
    *,
    assumption: str,
    justification: str,
    expires_after: str | None = None,
    agent_session: str | None = None,
) -> dict[str, Any]:
    """Append a new Assumption row.

    Assumptions are audit-only — they don't cost score, but they
    surface in ``agent-readiness gap list`` so a reviewer can rebut
    any that no longer hold.
    """
    try:
        from agent_readiness.gaps import (  # noqa: PLC0415 -- lazy
            record_assumption as _record_assumption,
        )
    except ImportError as exc:
        return {
            "error": "engine_not_installed",
            "message": (
                "agent-readiness>=3.2.0 is required for gap-aware tools "
                f"({exc})"
            ),
        }
    root = _root(path)
    new_id = _record_assumption(
        assumption=assumption,
        justification=justification,
        expires_after=expires_after,
        agent_session=agent_session,
        root=root,
    )
    return {
        "recorded": True,
        "id": new_id,
        "kind": "assumption",
        "path": str(root / ".agent-readiness" / "gaps.jsonl"),
    }


def confirm_apply(
    path: str,
    *,
    approved: bool,
    run_verify: bool = True,
    agent_session: str | None = None,
) -> dict[str, Any]:
    """Round-trip companion to the medium-confidence apply path.

    Pair with ``apply_top_action_tool``: when the latter returns
    ``{"confirm_required": true, "skipped_reason": "confirm_required"}``,
    the agent should ask the user, then call this tool with
    ``approved=True/False``.

    * ``approved=True`` — re-runs the apply path with the top action's
      confidence overridden to ``"high"`` so the engine's gating lets
      the handler run. Returns the wrapped ``ApplyResult.to_dict()``.
    * ``approved=False`` — records a Gap whose ``kind`` is
      ``"user_declined_medium_confidence"`` so the unresolved
      ambiguity surfaces on the next scan. Returns the
      ``record_gap`` envelope.

    A bare ``apply_top_action`` call here would re-trigger the same
    ``confirm_required`` short-circuit because the engine reads the
    confidence off the (unchanged) top_action; we have to forcibly
    upgrade the payload's ``confidence`` field before invoking apply.
    """
    try:
        from agent_readiness.apply_action import (  # noqa: PLC0415 -- lazy
            apply_top_action as _apply_top_action,
        )
        from agent_readiness.gaps import (  # noqa: PLC0415 -- lazy
            record_gap as _record_gap,
        )
    except ImportError as exc:
        return {
            "error": "engine_not_installed",
            "message": (
                "agent-readiness>=3.2.0 is required for confirm_apply "
                f"({exc})"
            ),
        }

    # We re-import here rather than at module top to keep the lazy
    # contract — and we deliberately use the MCP server's own
    # ``scan_repo`` so the workspace-detect / monorepo behaviour is
    # consistent with ``apply_top_action_tool``.
    from agent_readiness_mcp.server import (  # noqa: PLC0415 -- lazy
        MultiRepoWorkspaceError,
        scan_repo,
    )

    root = _root(path)
    try:
        report = scan_repo(str(root))
    except MultiRepoWorkspaceError as exc:
        return exc.payload

    top = report.get("top_action")
    if top is None:
        return {
            "applied": False,
            "skipped_reason": "no_top_action",
            "message": "Repo has no actionable top_action; nothing to confirm.",
        }

    if not approved:
        # User said no — convert the declined medium-confidence apply
        # into a Gap so the unresolved ambiguity surfaces on the next
        # scan. Severity defaults to "medium" to mirror the rule's
        # confidence; agents can override later via the CLI if needed.
        new_id = _record_gap(
            kind="user_declined_medium_confidence",
            detail=(
                f"User declined to apply top_action {top.get('check_id')!r}: "
                f"{top.get('message', '')}"
            ),
            severity="medium",
            candidate_resolutions=(
                [top.get("fix_hint")] if top.get("fix_hint") else []
            ),
            agent_session=agent_session,
            root=root,
        )
        return {
            "applied": False,
            "approved": False,
            "recorded_gap": True,
            "id": new_id,
            "check_id": top.get("check_id"),
        }

    # Approved — bypass the gate by forcibly upgrading confidence.
    top["confidence"] = "high"
    result = _apply_top_action(top, root, run_verify=run_verify)
    out = result.to_dict()
    out.setdefault("applied", False)
    out.setdefault("written", [])
    out["approved"] = True
    out["check_id"] = top.get("check_id")
    return out
