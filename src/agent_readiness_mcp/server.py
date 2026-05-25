"""MCP server core.

The server exposes eight tools backed by the agent-readiness engine:

* ``detect_workspace(path)``               -> detect_v1 envelope
* ``enumerate_workspace(path)``            -> static enumeration envelope
* ``scan_workspace(path, select)``         -> multi-repo scan envelope
* ``check_workspace_readiness(path, sel)`` -> workspace_readiness envelope
* ``scan_repo(path)``                      -> ReadinessReport JSON envelope
* ``apply_top_action(path, verify)``       -> ApplyResult JSON envelope
* ``list_friction(path)``                  -> list of {rule_id, severity,
                                              message, fix_prompt, verify}
* ``manifest_validate(path)``              -> ManifestValidationResult
                                              JSON envelope (workspace
                                              bible loader + validator)

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
import os
import subprocess
import sys
import time as _time
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


# ---------- live scan tools (Plan 3) --------------------------------------


def scan_workspace_async(
    workspace_path: str,
    children: list[str] | None = None,
    *,
    server_url_timeout_s: float = 5.0,
) -> dict[str, Any]:
    """Kick off ``agent-readiness scan-and-view`` as a detached subprocess.

    Returns a ``StartedScan`` envelope as soon as ``<scan-dir>/server.url``
    appears on disk (the CLI writes it once the HTTP server is listening).
    The CLI process then runs the scan independently of the MCP process.

    If a live scan already exists for ``workspace_path`` (PID-stamp
    verified), returns *that* scan's URL instead of starting a new one.
    """
    from agent_readiness.live_scan.paths import scan_dir
    from agent_readiness.live_scan.pidfile import PidStatus, verify_pidfile

    ws = Path(workspace_path).expanduser().resolve()
    if not ws.is_dir():
        raise ValueError(f"path is not a directory: {ws}")
    sd = scan_dir(ws)
    url_file = sd / "server.url"
    pid_file = sd / "daemon.pid"

    if verify_pidfile(pid_file) is PidStatus.LIVE and url_file.exists():
        return _started_envelope(ws, sd, url_file, pid_file)

    children = children or [str(ws)]
    cmd = [
        sys.executable, "-m", "agent_readiness.cli", "scan-and-view",
        str(ws), "--children", ",".join(children), "--no-open",
    ]
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    deadline = _time.monotonic() + server_url_timeout_s
    while _time.monotonic() < deadline:
        if url_file.exists() and pid_file.exists():
            return _started_envelope(ws, sd, url_file, pid_file)
        _time.sleep(0.1)
    raise RuntimeError(
        f"scan-and-view did not write {url_file} within {server_url_timeout_s}s"
    )


def _started_envelope(
    ws: Path, sd: Path, url_file: Path, pid_file: Path,
) -> dict[str, Any]:
    pid_data = json.loads(pid_file.read_text())
    live = sd / "live.json"
    children_total = 0
    if live.exists():
        try:
            children_total = json.loads(live.read_text())["progress"]["total"]
        except (json.JSONDecodeError, KeyError):
            pass
    return {
        "status": "started",
        "dashboard_url": url_file.read_text().strip(),
        "scan_id": pid_data["scan_id"],
        "workspace": str(ws),
        "children_total": children_total,
        "eta_minutes_estimate": max(1, children_total * 30 // 60),
        "pid": pid_data["pid"],
        "log_tail_file": str(sd / "scan.log"),
        "guidance": (
            "Share dashboard_url with the user. "
            "Poll get_scan_status before reading final results."
        ),
    }


def get_scan_status(scan_id: str) -> dict[str, Any]:
    """Return current status for a scan by ``scan_id`` (workspace hash).

    Reads ``live.json`` if present, else ``latest.json``. Cheap and safe
    to call repeatedly. Returns the dashboard URL when the local server
    is still running; empty string otherwise.
    """
    from agent_readiness.live_scan.paths import scans_root

    sd = scans_root() / scan_id
    live = sd / "live.json"
    latest = sd / "latest.json"
    target = live if live.exists() else latest if latest.exists() else None
    if target is None:
        raise FileNotFoundError(f"no scan data for {scan_id}")
    env = json.loads(target.read_text())
    url = ""
    url_file = sd / "server.url"
    if url_file.exists():
        url = url_file.read_text().strip()
    return {
        "scan_id": scan_id,
        "status": env.get("status"),
        "progress": env.get("progress"),
        "dashboard_url": url,
        "overall_score": env.get("overall_score"),
        "completed_at": env.get("completed_at"),
    }


def stop_scan(scan_id: str) -> dict[str, Any]:
    """Stop a running scan by ``scan_id``, or every scan when ``scan_id="all"``.

    Returns ``{"ok": bool, "killed": [...], "skipped": [...]}``. ``ok=False``
    only when a specific scan_id can't be found or its pidfile is
    stale/recycled — never when ``scan_id="all"``.
    """
    import signal as _sig

    from agent_readiness.live_scan import discovery as _discovery
    from agent_readiness.live_scan.paths import scans_root
    from agent_readiness.live_scan.pidfile import (
        PidStatus,
        clear_pidfile,
        verify_pidfile,
    )

    if scan_id == "all":
        result = _discovery.stop_all()
        return {"ok": True, **result}

    sd = scans_root() / scan_id
    pid_file = sd / "daemon.pid"
    status = verify_pidfile(pid_file)
    if status is PidStatus.MISSING:
        return {
            "ok": False,
            "reason": "not_found",
            "killed": [],
            "skipped": [],
        }
    if status is PidStatus.LIVE:
        data = json.loads(pid_file.read_text())
        try:
            os.kill(data["pid"], _sig.SIGTERM)
            return {"ok": True, "killed": [scan_id], "skipped": []}
        except ProcessLookupError:
            clear_pidfile(pid_file)
            return {
                "ok": False,
                "reason": "process_disappeared",
                "killed": [],
                "skipped": [],
            }
    clear_pidfile(pid_file)
    return {
        "ok": False,
        "reason": status.value,
        "killed": [],
        "skipped": [{"scan_id": scan_id, "reason": status.value}],
    }


def list_scans() -> dict[str, Any]:
    """Enumerate active + recent scans across every workspace."""
    from agent_readiness.live_scan import discovery as _discovery
    return _discovery.list_scans()


def render_workspace_report(
    workspace_path: str,
    scan_id: str | None = None,
    output_dir: str | None = None,
) -> dict[str, Any]:
    """Render a scan as a portable static directory via agent_readiness.render."""
    from agent_readiness.render import export_report

    out = Path(output_dir).expanduser().resolve() if output_dir else None
    result = export_report(
        Path(workspace_path).expanduser().resolve(),
        scan_id=scan_id,
        output_dir=out,
    )
    return {
        "status": "rendered",
        "index_path": str(result.index_path),
        "output_dir": str(result.output_dir),
        "scan_id": result.scan_id,
        "scan_ts": result.scan_ts,
        "rendered_at": result.rendered_at,
        "source_status": result.source_status,
        "guidance": (
            "Share index_path with the user. If file:// routing fails, "
            "run `python -m http.server` in output_dir and open "
            "http://localhost:8000."
        ),
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
        """Enumerate PATH's direct children for workspace classification.

        Returns the ``EnumerationReport`` JSON envelope. Use this before
        any scan call when PATH is unfamiliar — the LLM classifies the
        result and decides whether to call ``scan_repo_tool`` (single
        repo / monorepo) or ``check_workspace_readiness_tool``
        (workspace).
        """
        return json.dumps(enumerate_workspace(path), indent=2)

    @server.tool()
    def check_workspace_readiness_tool(
        path: str,
        children_paths: list[str],
    ) -> str:
        """Workspace-level readiness scan.

        Runs Coordination checks at PATH and per-repo scans on each
        child. Returns the 5-pillar ``WorkspaceReadinessReport`` JSON
        envelope. Call ``enumerate_workspace_tool`` first to discover
        children; the LLM classifies which paths to include here.
        """
        try:
            envelope = check_workspace_readiness(path, children_paths)
        except ValueError as exc:
            return json.dumps({"error": "invalid_input", "message": str(exc)})
        return json.dumps(envelope, indent=2)

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

    # ----- Plan 3: live-scan tools ---------------------------------------

    @server.tool()
    def scan_workspace_async_tool(
        workspace_path: str,
        children: list[str] | None = None,
    ) -> str:
        """Kick off a live workspace scan + serve dashboard, return URL immediately.

        Spawns ``agent-readiness scan-and-view`` as a detached subprocess.
        Returns within ~2s with a JSON envelope containing ``dashboard_url``.
        Agent should share that URL with the user, then poll
        ``get_scan_status_tool`` before reading final results.

        If a live scan already exists for ``workspace_path``, returns
        that scan's URL instead of starting a new one.
        """
        try:
            return json.dumps(
                scan_workspace_async(workspace_path, children=children),
                indent=2,
            )
        except (ValueError, RuntimeError) as exc:
            return json.dumps({"error": "scan_start_failed", "message": str(exc)})

    @server.tool()
    def get_scan_status_tool(scan_id: str) -> str:
        """Return current status for a scan by ``scan_id`` (workspace hash)."""
        try:
            return json.dumps(get_scan_status(scan_id), indent=2)
        except FileNotFoundError as exc:
            return json.dumps({"error": "not_found", "message": str(exc)})

    @server.tool()
    def stop_scan_tool(scan_id: str) -> str:
        """Stop one scan, or every running scan with ``scan_id='all'``."""
        return json.dumps(stop_scan(scan_id), indent=2)

    @server.tool()
    def list_scans_tool() -> str:
        """Enumerate active + recent scans across every workspace."""
        return json.dumps(list_scans(), indent=2)

    @server.tool()
    def render_workspace_report_tool(
        workspace_path: str,
        scan_id: str | None = None,
        output_dir: str | None = None,
    ) -> str:
        """Render a scan as a portable static directory (HTML + JSON)."""
        try:
            return json.dumps(
                render_workspace_report(
                    workspace_path,
                    scan_id=scan_id,
                    output_dir=output_dir,
                ),
                indent=2,
            )
        except FileNotFoundError as exc:
            return json.dumps({"error": "not_found", "message": str(exc)})

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
    "get_scan_status",
    "list_friction",
    "list_scans",
    "manifest_validate",
    "ontology",
    "render_workspace_report",
    "scan_repo",
    "scan_workspace",
    "scan_workspace_async",
    "serve",
    "stop_scan",
]
