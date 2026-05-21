"""Smoke tests for the MCP server's tool entry points.

These tests exercise the wrappers, not the MCP transport — the
transport is provided by the official ``mcp`` SDK and is well-tested
upstream. We check that ``scan_repo`` returns a recognisable report
and that ``apply_top_action`` honours its preconditions.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agent_readiness_mcp.server import (
    MultiRepoWorkspaceError,
    apply_top_action,
    detect_workspace,
    list_friction,
    scan_repo,
    scan_workspace,
)


def test_scan_repo_returns_report_envelope():
    with TemporaryDirectory() as td:
        repo = Path(td)
        (repo / "README.md").write_text("# repo\n")
        report = scan_repo(str(repo))
        assert "overall_score" in report
        assert isinstance(report["overall_score"], (int, float))
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


def test_scan_repo_raises_on_multi_repo_workspace():
    """scan_repo refuses multi-repo paths with the structured payload."""
    with TemporaryDirectory() as td:
        root = Path(td)
        for name in ("alpha", "beta"):
            (root / name).mkdir()
            _make_git(root / name)
        with pytest.raises(MultiRepoWorkspaceError) as info:
            scan_repo(str(root))
        payload = info.value.payload
        assert payload["error"] == "multi_repo_workspace"
        assert payload["version"] == "detect_v1"
        assert "alpha" in payload["detected_repos"]
        assert "beta" in payload["detected_repos"]


def test_scan_workspace_fans_out_to_all_detected():
    """scan_workspace returns one envelope with N scan reports."""
    with TemporaryDirectory() as td:
        root = Path(td)
        for name in ("alpha", "beta"):
            (root / name).mkdir()
            _make_git(root / name)
            (root / name / "README.md").write_text(f"# {name}\n")
        result = scan_workspace(str(root))
        assert result["classification"] == "multi_repo_workspace"
        assert {s["name"] for s in result["scanned"]} == {"alpha", "beta"}
        for entry in result["scanned"]:
            assert "report" in entry
            assert "overall_score" in entry["report"]


def test_scan_workspace_select_subset():
    """scan_workspace honours `select` and surfaces unmatched names."""
    with TemporaryDirectory() as td:
        root = Path(td)
        for name in ("alpha", "beta", "gamma"):
            (root / name).mkdir()
            _make_git(root / name)
            (root / name / "README.md").write_text(f"# {name}\n")
        result = scan_workspace(str(root), select=["alpha", "nope"])
        scanned_names = {s["name"] for s in result["scanned"]}
        assert scanned_names == {"alpha"}
        skip_by_reason = {s["reason"]: s for s in result["skipped"]}
        assert "not in select list" in skip_by_reason
        assert "not detected" in skip_by_reason
        assert skip_by_reason["not detected"]["name"] == "nope"


def test_scan_workspace_on_single_repo_returns_single_entry():
    """Calling scan_workspace on a single-repo path still works.

    The skill can branch on classification alone and reuse the same
    tool for both single-repo and multi-repo paths.
    """
    with TemporaryDirectory() as td:
        repo = Path(td)
        _make_git(repo)
        (repo / "README.md").write_text("# repo\n")
        result = scan_workspace(str(repo))
        assert result["classification"] == "single_repo"
        assert len(result["scanned"]) == 1
        assert result["scanned"][0]["report"]["overall_score"] >= 0


def test_scan_workspace_passes_through_drift_warnings():
    """AGENTS.md drift surfaces in scan_workspace, not just detect."""
    with TemporaryDirectory() as td:
        root = Path(td)
        _make_git(root / "alpha")
        (root / "AGENTS.md").write_text(
            "# Workspace\n\n## Repos\n\n"
            "| Repo | One-line | Lang |\n"
            "|---|---|---|\n"
            "| [`alpha`](./alpha) | Alpha | Python |\n"
            "| [`beta`](./beta) | Missing | Rust |\n"
        )
        # alpha alone -> single_repo, not multi_repo_workspace, so
        # drift logic doesn't apply. Add a second repo to keep it
        # multi_repo.
        _make_git(root / "gamma")
        result = scan_workspace(str(root))
        kinds = {w["kind"] for w in result["drift_warnings"]}
        # Both kinds expected: AGENTS.md lists beta (missing on disk),
        # and gamma is on disk but not in AGENTS.md.
        assert "missing_from_disk" in kinds
        assert "missing_from_agents" in kinds
