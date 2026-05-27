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


def scan_and_view(
    path: str,
    treat_as: str | None = None,
) -> dict[str, Any]:
    """The single front-door for scanning anything.

    Auto-enumerates ``path``, reads ``classification_hint``, then
    dispatches:

      - Clear single repo / monorepo / workspace → spawns
        ``agent-readiness scan-and-view`` and returns the same
        ``StartedScan`` envelope ``scan_workspace_async`` does
        (``status="started"``, ``dashboard_url``, etc.). For
        single-repo paths the dashboard renders a workspace-of-one
        (one card scanning).
      - Ambiguous → returns ``{"status": "needs_disambiguation",
        "ambiguity_reason": "...", "ambiguity_options": [...]}``
        WITHOUT spawning anything. The caller (skill) paints the
        prompt into chat, gets the user's pick, then re-calls
        ``scan_and_view(path, treat_as=<option_id>)``.
      - Not a code repo → returns ``{"status": "not_a_code_repo",
        "message": ..., "rationale": ...}``.

    ``treat_as`` is the disambiguation override; valid values are
    ``"workspace"``, ``"monorepo"``, ``"single_repo"``, and ``"skip"``
    (the option IDs the scanner pre-renders for ``ask_user`` cases).
    ``"workspace"`` routes to a workspace scan; everything else routes
    to a single-repo scan; ``"skip"`` returns ``not_a_code_repo``.

    If a live scan already exists for ``path`` (PID-stamp verified),
    returns *that* scan's URL — same idempotency as
    ``scan_workspace_async``.
    """
    from agent_readiness.enumerate import enumerate_workspace as _enumerate

    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        raise ValueError(f"path is not a directory: {p}")

    report = _enumerate(p)
    hint = report.classification_hint

    if treat_as is not None:
        normalized = treat_as.strip().lower()
        if normalized in ("workspace",):
            action = "scan_workspace_async"
        elif normalized in ("monorepo", "single_repo", "single"):
            action = "scan_repo"
        elif normalized in ("skip", "exit"):
            action = "exit"
        else:
            return {
                "status": "invalid_input",
                "error": "invalid_treat_as",
                "message": (
                    f"unknown treat_as={treat_as!r}; valid: "
                    "'workspace', 'monorepo', 'single_repo', 'skip'"
                ),
            }
    else:
        action = hint.recommended_action if hint else "scan_workspace_async"

    if action == "exit":
        return {
            "status": "not_a_code_repo",
            "message": (
                "This path is not a code repository. Tell the user and stop."
            ),
            "rationale": hint.rationale if hint else "no .git, no README",
        }

    if action == "ask_user":
        assert hint is not None  # ask_user only comes from a real hint
        return {
            "status": "needs_disambiguation",
            "ambiguity_reason": hint.ambiguity_reason,
            "ambiguity_options": hint.ambiguity_options,
            "rationale": hint.rationale,
            "guidance": (
                "Paint ambiguity_reason and ambiguity_options into a chat "
                "prompt verbatim. After the user picks, re-call "
                "scan_and_view(path, treat_as=<option.id>). Do not "
                "improvise wording, do not read READMEs to double-check."
            ),
        }

    if action == "scan_workspace_async":
        children: list[str] = [
            str(c.path) for c in report.children if c.has_git
        ]
        if not children:
            # Defensive: classifier said workspace but no children with .git
            # (shouldn't happen given the rubric, but don't strand the user).
            children = [str(p)]
    else:
        # scan_repo: workspace of one — the dashboard renders a single
        # card and the underlying engine scans the root as a child.
        children = [str(p)]

    return scan_workspace_async(str(p), children=children)


def _live_dashboard_url(base_url: str, scan_id: str) -> str:
    """Build the URL the user should open in a browser.

    The dashboard SPA uses HashRouter, so the live route lives at
    ``<base>/#/live/<scan_id>``. The bare base URL renders the old
    WorkspacesPage which polls a /data/index.json not present in a
    live scan_dir — that path sits on "Loading workspaces…" forever
    (bug reported 2026-05-27, v0.7.1 fix).
    """
    if not base_url:
        return ""
    return f"{base_url}/#/live/{scan_id}"


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
    base_url = url_file.read_text().strip()
    scan_id = pid_data["scan_id"]
    return {
        "status": "started",
        "dashboard_url": _live_dashboard_url(base_url, scan_id),
        "scan_id": scan_id,
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

    Bundle D enrichment (additive — never raises if the new files are
    absent):

      ``sse_url``               URL to subscribe to the SSE event stream.
      ``snapshot_url``          URL of the WorkspaceScanSnapshot JSON.
      ``prompts_pending_count`` count of pending interactive prompts.
      ``mode_exit_requested``   True when the user clicked Exit Dashboard.

    The skill calls this once per chat turn after handing scanning over
    to dashboard mode — it does NOT continuously stream from the SSE
    endpoint, matching the spec § 4 "hands-off skill ↔ dashboard bridge"
    decision.
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

    # Bundle D — derive the new fields. All four are safe-on-missing so
    # this enrichment never breaks callers that hit a scan_dir from a
    # pre-3.4.0 worker (events.jsonl / prompts.jsonl simply absent).
    sse_url = f"{url}/sse/scans/{scan_id}" if url else ""
    snapshot_url = f"{url}/api/scans/{scan_id}/snapshot" if url else ""
    prompts_pending_count = _count_pending_prompts(sd / "prompts.jsonl")
    mode_exit_requested = (sd / "exit_requested").exists()

    return {
        "scan_id": scan_id,
        "status": env.get("status"),
        "progress": env.get("progress"),
        # Bundle D bugfix (v0.7.1): point at the LivePage (HashRouter
        # route) instead of the bare base URL — the bare URL renders
        # the legacy WorkspacesPage which polls a missing
        # /data/index.json and gets stuck on "Loading workspaces…".
        "dashboard_url": _live_dashboard_url(url, scan_id),
        "overall_score": env.get("overall_score"),
        "completed_at": env.get("completed_at"),
        # Bundle D additive fields:
        "sse_url": sse_url,
        "snapshot_url": snapshot_url,
        "prompts_pending_count": prompts_pending_count,
        "mode_exit_requested": mode_exit_requested,
    }


def _count_pending_prompts(prompts_file) -> int:
    """Count prompt_ids in ``prompts.jsonl`` that are still ``pending``.

    A prompt is pending if it has a ``requested`` line and neither an
    ``answered`` nor an ``expired`` line. Resilient to a missing file
    (returns 0) and to a torn-tail line (stops at the first JSON parse
    error, matching the engine's reader contract).
    """
    if not prompts_file.exists():
        return 0
    states: dict[str, str] = {}
    try:
        for raw in prompts_file.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                # Torn line at tail — stop counting, MCP's read isn't
                # the writer so we don't try to recover further.
                break
            pid = obj.get("prompt_id")
            event = obj.get("event")
            if not pid or event not in ("requested", "answered", "expired"):
                continue
            if event == "requested":
                states.setdefault(pid, "pending")
            else:  # answered | expired — both terminate "pending"
                states[pid] = "closed"
    except OSError:
        return 0
    return sum(1 for s in states.values() if s == "pending")


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
        """Enumerate PATH's direct children AND classify the layout.

        Returns the ``EnumerationReport`` JSON envelope. The envelope
        carries a **``classification_hint``** block (added in
        agent-readiness 3.4.3) that the caller MUST obey verbatim
        — do not re-classify in the LLM, do not deliberate, do not
        read READMEs first. The hint is a pure function of the
        signals; LLM judgment cannot improve on it and burns wall-clock
        time.

        Read ``envelope["classification_hint"]["recommended_action"]``
        and act:

          - ``"scan_repo"`` → call ``scan_repo_tool(path)``.
            ``classification`` will be ``single_repo`` or ``monorepo``.
          - ``"scan_workspace_async"`` → call
            ``scan_workspace_async_tool(path, children=...)`` (DASHBOARD
            MODE, the default for multi-repo workspaces). Returns in
            ~2s with a ``dashboard_url`` to share with the user. Do
            NOT use ``check_workspace_readiness_tool`` for this case —
            it is synchronous and blocks the chat for minutes per repo.
          - ``"ask_user"`` → **STOP. Do not scan.** Signals are
            ambiguous from data alone (e.g. root has ``.git`` AND
            children also have ``.git`` — could be a workspace nested
            in a meta-repo, a monorepo with submodules, or a single
            repo with unrelated sub-checkouts). The envelope carries
            pre-rendered ``ambiguity_reason`` and ``ambiguity_options``
            ``[{id, label, route, hint}]`` — paint them into a chat
            prompt verbatim, wait for the user to pick, then chain
            the matching ``route`` (``scan_repo`` or
            ``scan_workspace_async``).
          - ``"exit"`` → not a code repo. Tell the user, do not scan.

        ``classification_hint`` may be absent on payloads from
        agent-readiness < 3.4.3. In that case fall back to the manual
        rubric (root.has_git / children_with_git / manifest_signals)
        — but for new installs this branch is dead code.
        """
        return json.dumps(enumerate_workspace(path), indent=2)

    @server.tool()
    def check_workspace_readiness_tool(
        path: str,
        children_paths: list[str],
    ) -> str:
        """**SYNCHRONOUS workspace scan — blocks the chat for minutes.**
        Prefer ``scan_workspace_async_tool`` for any workspace ≥ 2 repos.

        Runs Coordination checks at PATH and per-repo scans on each
        child sequentially (~30s per repo for a typical Python repo).
        For a 10-repo workspace that is ~5 minutes of blocked chat;
        for a 20-repo workspace, ~10 minutes. Returns the 5-pillar
        ``WorkspaceReadinessReport`` JSON envelope when finally done.

        Use this tool ONLY when:

          - the user explicitly opted out of dashboard mode
            (e.g. ``"don't open the dashboard, just give me the JSON"``),
            OR
          - running headless in CI (no human, no browser),
            OR
          - the workspace has 1-2 children and the user is fine waiting.

        For every other multi-repo case, switch to
        ``scan_workspace_async_tool`` — same scan engine, same
        Coordination findings, but the chat doesn't block and prompts
        are answered inline in the browser instead of stalling the
        conversation.
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

    # ----- Plan 3: live-scan tools ---------------------------------------

    @server.tool()
    def scan_and_view_tool(
        path: str,
        treat_as: str | None = None,
    ) -> str:
        """**THE FRONT-DOOR TOOL — call this first for ANY path.**

        Single-tool entry point. The skill calls this immediately on
        every user-supplied path; the tool auto-enumerates,
        auto-classifies, auto-launches the dashboard, and returns the
        URL within ~2 seconds. The skill makes zero classification
        decisions and zero pre-flight tool calls.

        Possible return shapes:

          1. ``{"status": "started", "dashboard_url": "...", ...}``
             — Same envelope as ``scan_workspace_async_tool``. The
             dashboard is up. Share the URL with the user verbatim
             and stop calling tools. Works for single repos
             (one-card workspace), monorepos, and multi-repo
             workspaces alike.

          2. ``{"status": "needs_disambiguation",
                "ambiguity_reason": "...",
                "ambiguity_options": [{"id", "label", "route", "hint"}, ...]}``
             — Signals are ambiguous (e.g. root has ``.git`` AND
             children also have ``.git``). The scanner already
             pre-rendered the chat prompt. Paint it verbatim, get
             the user's pick, then re-call
             ``scan_and_view_tool(path, treat_as=<option.id>)``.

          3. ``{"status": "not_a_code_repo", "message": ...}``
             — No .git, no README, no children. Tell the user, stop.

          4. ``{"status": "invalid_input", ...}`` — bad ``treat_as``
             value. Surface the error.

        Why this exists: prior to v0.7.4 the skill had to chain
        ``enumerate_workspace_tool`` → think → pick a scan tool →
        call it. Three tool calls and two LLM-thinking turns =
        30+ seconds of latency before the dashboard appears. This
        tool collapses all of that into one call.

        ``treat_as`` is the disambiguation override (option IDs
        the scanner pre-renders): ``"workspace"``, ``"monorepo"``,
        ``"single_repo"``, or ``"skip"``.
        """
        try:
            return json.dumps(scan_and_view(path, treat_as=treat_as), indent=2)
        except (ValueError, RuntimeError) as exc:
            return json.dumps({
                "status": "error",
                "error": "scan_start_failed",
                "message": str(exc),
            })

    @server.tool()
    def scan_workspace_async_tool(
        workspace_path: str,
        children: list[str] | None = None,
    ) -> str:
        """**DEFAULT workspace scan tool — start here for any multi-repo path.**

        Spawns ``agent-readiness scan-and-view`` as a detached subprocess
        and **returns within ~2 seconds** with a JSON envelope:

            {
              "status": "started",
              "scan_id": "<workspace_hash>",
              "dashboard_url": "http://127.0.0.1:<port>/#/live/<scan_id>",
              "children_total": N,
              "eta_minutes_estimate": M,
              ...
            }

        After this returns:

          1. **Share ``dashboard_url`` with the user verbatim** so they
             can open it in a browser. The browser shows the per-repo
             grid, prompts queue, and findings feed updating live over
             SSE — the user does not need to wait in chat.
          2. **Tell the user how to exit dashboard mode** — they can
             click *"Exit dashboard"* in the browser OR ask in chat
             (which POSTs to the exit endpoint). The scan keeps running
             either way.
          3. **Stop calling tools.** Hand off. Don't poll
             ``get_scan_status_tool`` in a loop — that's what the
             dashboard is for. Call ``get_scan_status_tool`` **at most
             once per chat turn**, only when the user sends a new
             message.

        Use this tool for any workspace with ≥ 2 children. Same scan
        engine as ``check_workspace_readiness_tool`` but the chat
        doesn't block and interactive prompts are answered in the
        browser instead of stalling the conversation.

        If a live scan already exists for ``workspace_path`` (verified
        via daemon.pid), returns that scan's URL instead of starting a
        new one.
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
        """Cheap, non-blocking poll of a live or completed scan.

        Returns the status envelope (``status``, ``progress``,
        ``dashboard_url``, ``sse_url``, ``snapshot_url``,
        ``prompts_pending_count``, ``mode_exit_requested``,
        ``overall_score``). Safe to call repeatedly — but the contract
        is **at most once per chat turn**, not in a polling loop. The
        dashboard already shows live progress; this tool exists so the
        agent can briefly answer "how's it going?" when the user asks.

        Read the envelope and respond conversationally:

          - ``status == "completed"`` → summarise + offer to apply.
          - ``status == "running"`` → one-liner: "X of Y repos done".
          - ``prompts_pending_count > 0`` → tell the user to answer
            the pending prompts in the dashboard tab.
          - ``mode_exit_requested == True`` → the user clicked Exit
            Dashboard; revert to chat mode for the next response.
        """
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
    "scan_and_view",
    "scan_repo",
    "scan_workspace",
    "scan_workspace_async",
    "serve",
    "stop_scan",
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
