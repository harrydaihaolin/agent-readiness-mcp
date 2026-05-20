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

from agent_readiness_mcp.server import apply_top_action, list_friction, scan_repo


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
