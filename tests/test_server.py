"""Smoke tests for the MCP server's tool entry points.

These tests exercise the wrappers, not the MCP transport — the
transport is provided by the official ``mcp`` SDK and is well-tested
upstream. The server is headless and prompt-only (v0.9.0+):
``scan_repo`` / ``scan_monorepo`` return the ReadinessReport inline,
and the workspace scan is exposed via ``check_workspace_readiness``
(see ``test_workspace_tools.py``).
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agent_readiness_mcp.server import (
    apply_top_action,
    detect_workspace,
    list_friction,
    scan_monorepo,
    scan_repo,
)


def test_scan_repo_returns_readiness_report(monkeypatch):
    """v0.9.0: scan_repo returns the ReadinessReport inline (no dashboard)."""
    with TemporaryDirectory() as td:
        monkeypatch.setenv("HOME", td)
        repo = Path(td)
        (repo / ".git").mkdir()
        (repo / "README.md").write_text("# repo\n")
        report = scan_repo(str(repo))
        assert "overall_score" in report
        assert "pillars" in report
        assert "top_action" in report
        # Headless: no dashboard handles in the envelope.
        assert "dashboard_url" not in report
        assert report.get("status") != "onboarding_required"


def test_scan_monorepo_returns_readiness_report(monkeypatch):
    """scan_monorepo scores the root as one repo and returns the report."""
    with TemporaryDirectory() as td:
        monkeypatch.setenv("HOME", td)
        repo = Path(td)
        (repo / ".git").mkdir()
        (repo / "README.md").write_text("# monorepo\n")
        report = scan_monorepo(str(repo))
        assert "overall_score" in report
        assert "pillars" in report


def test_scan_repo_rejects_nonexistent_path():
    with pytest.raises(ValueError, match="not a directory"):
        scan_repo("/this/path/does/not/exist/xyz")


def test_apply_top_action_returns_result_envelope():
    """apply_top_action returns the ApplyResult envelope; we don't
    assert ``applied=True`` because the actual behaviour depends on
    which rule wins the pin (some win with run_command actions whose
    shell command intentionally fails on a bare repo).

    Requires ``agent-readiness >= 2.4.0`` (the engine release that
    ships the ``apply_action`` module). On earlier engines the import
    raises ``ModuleNotFoundError``."""
    with TemporaryDirectory() as td:
        repo = Path(td)
        (repo / "README.md").write_text("# repo\n")
        result = apply_top_action(str(repo), run_verify=False)
        assert "applied" in result
        assert "written" in result
        assert isinstance(result["written"], list)


def test_list_friction_returns_paste_ready_items():
    """list_friction returns one entry per WARN/ERROR finding that
    carries a fix_prompt. A bare README-only repo trips multiple
    structural rules (no AGENTS.md, no manifest, no test target);
    each should come back with a non-empty paste-ready prompt."""
    with TemporaryDirectory() as td:
        repo = Path(td)
        (repo / "README.md").write_text("# repo\n")
        items = list_friction(str(repo))
        assert isinstance(items, list)
        # The vendored pack is at rules_version 1 in this checkout; v1
        # rules don't carry fix_prompt, so the list may be empty until
        # the vendored pack is re-cut. The contract we DO assert is the
        # envelope shape — every returned item must have the documented
        # keys with non-empty fix_prompt.
        for item in items:
            assert set(item.keys()) >= {
                "rule_id", "pillar", "severity", "message",
                "fix_prompt", "score_impact",
            }
            assert item["severity"] in ("warn", "error")
            assert item["fix_prompt"], "list_friction must drop items without a prompt"


# ---------- workspace_detect-backed tools ----------------------------


def _make_git(p: Path) -> None:
    (p / ".git").mkdir(parents=True, exist_ok=True)


def test_detect_workspace_single_repo_envelope():
    """A bare git repo classifies as single_repo / high."""
    with TemporaryDirectory() as td:
        repo = Path(td)
        _make_git(repo)
        (repo / "README.md").write_text("# repo\n")
        result = detect_workspace(str(repo))
        assert result["version"] == "detect_v1"
        assert result["classification"] == "single_repo"
        assert result["confidence"] == "high"
        assert len(result["repos"]) == 1
        assert result["repos"][0]["has_git"] is True


def test_detect_workspace_multi_repo_envelope():
    """Parent dir with two child .git dirs → multi_repo_workspace."""
    with TemporaryDirectory() as td:
        root = Path(td)
        for name in ("alpha", "beta"):
            (root / name).mkdir()
            _make_git(root / name)
        result = detect_workspace(str(root))
        assert result["classification"] == "multi_repo_workspace"
        assert result["confidence"] == "high"
        names = sorted(r["name"] for r in result["repos"])
        assert names == ["alpha", "beta"]


def test_detect_workspace_rejects_nonexistent_path():
    with pytest.raises(ValueError, match="not a directory"):
        detect_workspace("/this/path/does/not/exist/xyz")


def test_scan_repo_raises_on_multi_repo_workspace(monkeypatch):
    """scan_repo refuses a multi-repo workspace with a structured payload."""
    from agent_readiness_mcp.server import MultiRepoWorkspaceError

    with TemporaryDirectory() as td:
        monkeypatch.setenv("HOME", td)
        root = Path(td)
        for name in ("alpha", "beta"):
            (root / name).mkdir()
            (root / name / ".git").mkdir()
        with pytest.raises(MultiRepoWorkspaceError) as excinfo:
            scan_repo(str(root))
        assert excinfo.value.payload["error"] == "multi_repo_workspace"
        assert sorted(excinfo.value.payload["detected_repos"]) == ["alpha", "beta"]
