"""Unit tests for the Plan 3 live-scan MCP tools.

These tests use ``HOME`` overrides + ``PYTHONUSERBASE``/``PYTHONPATH``
shims so the spawned ``agent-readiness scan-and-view`` subprocess
can find both ``click`` (user site-packages) and the in-tree
``agent_readiness`` package — without contaminating the user's real
``~/.agent-readiness/`` directory.
"""
from __future__ import annotations

import json
import os
import signal
import site
import time
from pathlib import Path

import pytest

from agent_readiness_mcp.server import (
    get_scan_status,
    list_scans,
    render_workspace_report,
    scan_workspace_async,
    stop_scan,
)


def _subprocess_env_patch(monkeypatch, tmp_home: Path) -> None:
    """Make the spawned CLI inherit the right paths under a fake HOME."""
    monkeypatch.setenv("HOME", str(tmp_home))
    monkeypatch.setenv(
        "PYTHONUSERBASE",
        os.environ.get("PYTHONUSERBASE", str(Path(site.getuserbase()).resolve())),
    )
    ar_src = (Path(__file__).resolve().parents[2] / "agent-readiness/src").resolve()
    monkeypatch.setenv("PYTHONPATH", str(ar_src))


def _make_fixture(tmp_path: Path, repos: list[str]) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    for r in repos:
        d = ws / r
        d.mkdir()
        (d / "AGENTS.md").write_text(f"# {r}\n")
    return ws


def _kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.3)
    except ProcessLookupError:
        pass


# ---------- scan_workspace_async ----------

@pytest.mark.skip(reason="scan_workspace_async_tool is deprecated by the onboarding wizard (plan-5 SKILL.md migration); spawn path was rewritten in agent-readiness 4.0.0 and these integration tests against the legacy live.json shape no longer apply. Tool itself retained for direct callers until v0.9.0.")
def test_scan_workspace_async_returns_dashboard_url(tmp_path, monkeypatch):
    _subprocess_env_patch(monkeypatch, tmp_path)
    ws = _make_fixture(tmp_path, ["r1"])

    result = scan_workspace_async(str(ws), children=[str(ws / "r1")])
    try:
        assert result["status"] == "started"
        assert result["dashboard_url"].startswith("http://")
        # v0.7.1 regression: the dashboard_url must point at the
        # LivePage (`/#/live/<scan_id>`), not the bare base URL.
        # The bare URL renders the legacy WorkspacesPage which gets
        # stuck on "Loading workspaces…" inside a live scan dir.
        assert f"/#/live/{result['scan_id']}" in result["dashboard_url"]
        assert result["scan_id"].startswith("ws-")
        assert result["children_total"] in (0, 1)  # may not be written yet
        assert isinstance(result["pid"], int) and result["pid"] > 0
        assert "guidance" in result
    finally:
        _kill(result["pid"])


@pytest.mark.skip(reason="scan_workspace_async_tool is deprecated by the onboarding wizard (plan-5 SKILL.md migration); spawn path was rewritten in agent-readiness 4.0.0 and these integration tests against the legacy live.json shape no longer apply. Tool itself retained for direct callers until v0.9.0.")
def test_scan_workspace_async_returns_existing_url_for_duplicate(tmp_path, monkeypatch):
    _subprocess_env_patch(monkeypatch, tmp_path)
    ws = _make_fixture(tmp_path, ["r1"])

    first = scan_workspace_async(str(ws), children=[str(ws / "r1")])
    try:
        second = scan_workspace_async(str(ws), children=[str(ws / "r1")])
        assert second["dashboard_url"] == first["dashboard_url"]
        assert second["pid"] == first["pid"]
    finally:
        _kill(first["pid"])


def test_scan_workspace_async_rejects_non_directory(tmp_path):
    bogus = tmp_path / "does-not-exist"
    with pytest.raises(ValueError):
        scan_workspace_async(str(bogus))


# ---------- get_scan_status ----------

def test_get_scan_status_reads_live_when_present(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from agent_readiness.live_scan.paths import scans_root

    sd = scans_root() / "ws-aaaaaa"
    sd.mkdir(parents=True)
    (sd / "live.json").write_text(json.dumps({
        "status": "in_progress",
        "progress": {"completed": 2, "total": 5, "in_flight": ["/x/r"]},
        "overall_score": None,
        "completed_at": None,
    }))
    (sd / "server.url").write_text("http://localhost:54712\n")
    result = get_scan_status("ws-aaaaaa")
    assert result["status"] == "in_progress"
    assert result["progress"]["completed"] == 2
    # v0.7.1 regression: dashboard_url points at the LivePage
    # (HashRouter route), not the bare base URL.
    assert result["dashboard_url"] == (
        "http://localhost:54712/#/live/ws-aaaaaa"
    )


def test_get_scan_status_falls_back_to_latest(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from agent_readiness.live_scan.paths import scans_root

    sd = scans_root() / "ws-bbbbbb"
    sd.mkdir(parents=True)
    (sd / "latest.json").write_text(json.dumps({
        "status": "completed",
        "progress": {"completed": 5, "total": 5, "in_flight": []},
        "overall_score": 70.0,
        "completed_at": "2026-05-25T00:00:00Z",
    }))
    result = get_scan_status("ws-bbbbbb")
    assert result["status"] == "completed"
    assert result["overall_score"] == 70.0
    assert result["dashboard_url"] == ""


def test_get_scan_status_raises_when_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        get_scan_status("nonexistent-scan-id")


# ---------- Bundle D enrichment: sse_url / snapshot_url /
# prompts_pending_count / mode_exit_requested ----------


def test_get_scan_status_emits_bundle_d_fields_when_server_running(
    tmp_path, monkeypatch,
):
    """Server URL is known → derive sse_url + snapshot_url from it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from agent_readiness.live_scan.paths import scans_root

    sd = scans_root() / "ws-dddd01"
    sd.mkdir(parents=True)
    (sd / "live.json").write_text(json.dumps({
        "status": "in_progress",
        "progress": {"completed": 1, "total": 3, "in_flight": []},
        "overall_score": None,
        "completed_at": None,
    }))
    (sd / "server.url").write_text("http://localhost:55001\n")

    result = get_scan_status("ws-dddd01")
    assert result["sse_url"] == "http://localhost:55001/sse/scans/ws-dddd01"
    assert result["snapshot_url"] == (
        "http://localhost:55001/api/scans/ws-dddd01/snapshot"
    )
    assert result["prompts_pending_count"] == 0
    assert result["mode_exit_requested"] is False


def test_get_scan_status_dashboard_url_is_live_page_not_bare_url(
    tmp_path, monkeypatch,
):
    """v0.7.1 regression: the chat-side `dashboard_url` must be the
    LivePage URL (`<base>/#/live/<scan_id>`), not the bare base URL.

    The bare base URL renders the legacy WorkspacesPage which polls
    `/data/index.json` (not present in a live scan_dir) and sits on
    "Loading workspaces…" forever. The skill prints this URL straight
    to the user, so the regression is user-visible.

    Also verifies that `sse_url` / `snapshot_url` continue to use the
    *bare* base URL (since they append path segments — if they used
    the `#/...` fragment they'd be broken).
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    from agent_readiness.live_scan.paths import scans_root

    sd = scans_root() / "ws-dddd99"
    sd.mkdir(parents=True)
    (sd / "live.json").write_text(json.dumps({
        "status": "in_progress",
        "progress": {"completed": 0, "total": 2, "in_flight": []},
        "overall_score": None,
        "completed_at": None,
    }))
    (sd / "server.url").write_text("http://127.0.0.1:9000\n")

    result = get_scan_status("ws-dddd99")
    # The dashboard URL is the live route.
    assert result["dashboard_url"] == (
        "http://127.0.0.1:9000/#/live/ws-dddd99"
    )
    # The API URLs are NOT prefixed with `#/live/…`; they append paths
    # to the bare base URL.
    assert result["sse_url"] == "http://127.0.0.1:9000/sse/scans/ws-dddd99"
    assert result["snapshot_url"] == (
        "http://127.0.0.1:9000/api/scans/ws-dddd99/snapshot"
    )
    assert "#" not in result["sse_url"]
    assert "#" not in result["snapshot_url"]


def test_get_scan_status_bundle_d_fields_empty_when_no_server_url(
    tmp_path, monkeypatch,
):
    """Older scan (no live server) → sse_url / snapshot_url empty,
    but the keys are still present so callers can rely on them."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from agent_readiness.live_scan.paths import scans_root

    sd = scans_root() / "ws-dddd02"
    sd.mkdir(parents=True)
    (sd / "latest.json").write_text(json.dumps({
        "status": "completed",
        "progress": {"completed": 3, "total": 3, "in_flight": []},
        "overall_score": 85.0,
        "completed_at": "2026-05-25T00:00:00Z",
    }))

    result = get_scan_status("ws-dddd02")
    assert result["sse_url"] == ""
    assert result["snapshot_url"] == ""
    assert result["prompts_pending_count"] == 0
    assert result["mode_exit_requested"] is False


def test_get_scan_status_counts_pending_prompts_only(tmp_path, monkeypatch):
    """1 requested + 1 requested→answered + 1 requested→expired = 1 pending."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from agent_readiness.live_scan.paths import scans_root

    sd = scans_root() / "ws-dddd03"
    sd.mkdir(parents=True)
    (sd / "live.json").write_text(json.dumps({
        "status": "in_progress",
        "progress": {"completed": 0, "total": 1, "in_flight": []},
        "overall_score": None,
        "completed_at": None,
    }))
    (sd / "server.url").write_text("http://x:8080\n")
    (sd / "prompts.jsonl").write_text("\n".join([
        json.dumps({"seq": 0, "event": "requested", "prompt_id": "p-still-pending",
                    "at": "2026-01-01T00:00:00Z"}),
        json.dumps({"seq": 1, "event": "requested", "prompt_id": "p-answered",
                    "at": "2026-01-01T00:00:01Z"}),
        json.dumps({"seq": 2, "event": "answered", "prompt_id": "p-answered",
                    "at": "2026-01-01T00:00:02Z"}),
        json.dumps({"seq": 3, "event": "requested", "prompt_id": "p-expired",
                    "at": "2026-01-01T00:00:03Z"}),
        json.dumps({"seq": 4, "event": "expired", "prompt_id": "p-expired",
                    "at": "2026-01-01T00:00:04Z"}),
    ]) + "\n")

    result = get_scan_status("ws-dddd03")
    assert result["prompts_pending_count"] == 1


def test_get_scan_status_handles_torn_prompts_tail(tmp_path, monkeypatch):
    """A torn-tail line (crashed mid-write) is ignored, not crashed-on."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from agent_readiness.live_scan.paths import scans_root

    sd = scans_root() / "ws-dddd04"
    sd.mkdir(parents=True)
    (sd / "live.json").write_text(json.dumps({
        "status": "in_progress",
        "progress": {"completed": 0, "total": 1, "in_flight": []},
        "overall_score": None,
        "completed_at": None,
    }))
    (sd / "prompts.jsonl").write_text(
        json.dumps({"seq": 0, "event": "requested", "prompt_id": "p-1",
                    "at": "2026-01-01T00:00:00Z"}) + "\n"
        + '{"seq": 1, "event": "requ'  # torn — no newline, no closing brace
    )

    result = get_scan_status("ws-dddd04")
    assert result["prompts_pending_count"] == 1


def test_get_scan_status_reports_mode_exit_when_flag_present(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from agent_readiness.live_scan.paths import scans_root

    sd = scans_root() / "ws-dddd05"
    sd.mkdir(parents=True)
    (sd / "live.json").write_text(json.dumps({
        "status": "in_progress",
        "progress": {"completed": 1, "total": 2, "in_flight": []},
        "overall_score": None,
        "completed_at": None,
    }))
    (sd / "exit_requested").write_text(json.dumps({"source": "button"}))

    result = get_scan_status("ws-dddd05")
    assert result["mode_exit_requested"] is True


# ---------- stop_scan ----------

def test_stop_scan_all_with_no_scans_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    result = stop_scan("all")
    assert result["ok"] is True
    assert result.get("killed") == []
    assert result.get("skipped") == []


def test_stop_scan_returns_not_found_for_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    result = stop_scan("nonexistent")
    assert result["ok"] is False
    assert result.get("reason") == "not_found"


@pytest.mark.skip(reason="scan_workspace_async_tool is deprecated by the onboarding wizard (plan-5 SKILL.md migration); spawn path was rewritten in agent-readiness 4.0.0 and these integration tests against the legacy live.json shape no longer apply. Tool itself retained for direct callers until v0.9.0.")
def test_stop_scan_kills_live_daemon(tmp_path, monkeypatch):
    _subprocess_env_patch(monkeypatch, tmp_path)
    # 8 children gives the scan enough work to still be running when we call
    # stop_scan; 1-repo fixtures finish in <50ms and race the test.
    ws = _make_fixture(tmp_path, [f"r{i}" for i in range(8)])
    children = [str(ws / f"r{i}") for i in range(8)]

    started = scan_workspace_async(str(ws), children=children)
    scan_id = started["scan_id"]
    try:
        # Don't sleep — race the worker before it finishes.
        result = stop_scan(scan_id)
        # Either we caught it live (ok=True with killed) or it raced ahead
        # of us and finished cleanly (ok=False, reason=not_found). Both
        # outcomes prove the pidfile lifecycle works.
        assert isinstance(result["ok"], bool)
        if result["ok"]:
            assert scan_id in result["killed"]
        else:
            assert result["reason"] in ("not_found", "stale", "recycled")
    finally:
        _kill(started["pid"])


# ---------- list_scans ----------

def test_mcp_list_scans_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    result = list_scans()
    assert result.get("active") == []
    assert result.get("recent") == []
    assert result.get("total_disk_bytes") == 0


def test_mcp_list_scans_returns_recent(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from agent_readiness.live_scan.paths import scans_root

    sd = scans_root() / "ws-cccccc"
    sd.mkdir(parents=True)
    (sd / "latest.json").write_text(json.dumps({
        "status": "completed",
        "repo_path": "/x/ws",
        "overall_score": 80.0,
        "completed_at": "2026-05-25T00:00:00Z",
    }))
    # discovery.list_scans requires meta.json with the workspace info.
    (sd / "meta.json").write_text(json.dumps({
        "schema": 1,
        "workspace_path": "/x/ws",
        "workspace_hash": "ws-cccccc",
        "scans": [{
            "ts": "2026-05-25T00:00:00Z",
            "status": "completed",
            "overall": 80.0,
        }],
    }))
    result = list_scans()
    assert isinstance(result.get("recent"), list)


# ---------- render_workspace_report ----------

def test_render_workspace_report_returns_index_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from agent_readiness.live_scan.paths import scan_dir

    ws = tmp_path / "ws"
    ws.mkdir()
    sd = scan_dir(ws)
    sd.mkdir(parents=True)
    (sd / "latest.json").write_text(json.dumps({
        "schema": 1,
        "status": "completed",
        "overall_score": 50.0,
        "repo_path": str(ws),
        "progress": {"completed": 0, "total": 0, "in_flight": []},
        "started_at": "2026-05-25T00:00:00Z",
        "completed_at": "2026-05-25T00:00:01Z",
        "pillar_scores": {},
        "children": [],
        "coordination_findings": [],
        "top_action": None,
        "stats": {
            "scan_duration_ms": 1000,
            "children_failed_paths": [],
        },
        "safety_caps_applied": [],
    }))
    out = tmp_path / "report"
    result = render_workspace_report(str(ws), output_dir=str(out))
    assert result["status"] == "rendered"
    assert Path(result["index_path"]).exists()
    assert result["source_status"] == "completed"


def test_inspect_tool_returns_inspect_result_json(tmp_path, monkeypatch):
    """`inspect_tool(path)` shells out to `agent-readiness inspect` and
    returns the JSON envelope verbatim."""
    from agent_readiness_mcp.server import inspect as inspect_callable

    target = tmp_path / "demo"
    target.mkdir()
    (target / ".git").mkdir()

    result = inspect_callable(str(target))
    assert "enumeration" in result
    assert "classification" in result
    assert result["classification"]["suggested_type"] == "single_repo"


def test_scan_repo_tool_returns_onboarding_required_envelope(tmp_path, monkeypatch):
    """The new scan_repo_tool opens the dashboard wizard rather than
    returning a sync scan result."""
    monkeypatch.setenv("HOME", str(tmp_path))

    from agent_readiness_mcp.server import scan_repo as scan_repo_callable

    target = tmp_path / "demo"
    target.mkdir()
    (target / ".git").mkdir()

    result = scan_repo_callable(str(target))
    assert result["status"] == "onboarding_required"
    assert result["type"] == "single_repo"
    assert "/onboarding/" in result["dashboard_url"]


def test_scan_monorepo_tool_returns_committed_type_monorepo(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    from agent_readiness_mcp.server import scan_monorepo as scan_monorepo_callable

    target = tmp_path / "demo"
    target.mkdir()
    (target / ".git").mkdir()
    (target / "pkg-a").mkdir()
    (target / "pkg-a" / ".git").mkdir()

    result = scan_monorepo_callable(str(target))
    assert result["status"] == "onboarding_required"
    assert result["type"] == "monorepo"


def test_scan_workspace_tool_returns_onboarding_required(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    from agent_readiness_mcp.server import scan_workspace as scan_workspace_callable

    target = tmp_path / "demo"
    target.mkdir()
    (target / "alpha").mkdir()
    (target / "alpha" / ".git").mkdir()
    (target / "beta").mkdir()
    (target / "beta" / ".git").mkdir()

    result = scan_workspace_callable(str(target))
    assert result["status"] == "onboarding_required"
    assert result["type"] == "workspace"
