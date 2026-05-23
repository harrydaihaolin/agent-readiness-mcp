"""Wire-shape tests for the new workspace MCP tools.

These tests assert that the MCP tool returns the same JSON envelope as
the CLI's --json output — the byte-identity contract per the
CLI -> MCP -> Skills layering principle.

The tests skip when the installed ``agent-readiness`` wheel predates
the workspace-scan modules (``agent_readiness.enumerate`` and
``agent_readiness.workspace_scan``). The engine release that ships
those modules cascades to this repo via the version pin in
``pyproject.toml`` — see AGENTS.md on dependency-ordered rollouts.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_readiness_mcp.server import enumerate_workspace

_ENGINE_HAS_WORKSPACE_SCAN = (
    importlib.util.find_spec("agent_readiness.enumerate") is not None
    and importlib.util.find_spec("agent_readiness.workspace_scan") is not None
)

pytestmark = pytest.mark.skipif(
    not _ENGINE_HAS_WORKSPACE_SCAN,
    reason=(
        "installed agent-readiness wheel predates the workspace-scan "
        "modules; tests run once the engine release cascades."
    ),
)


def _make_git(p: Path) -> None:
    (p / ".git").mkdir(parents=True, exist_ok=True)


def test_enumerate_returns_dict_with_kind(tmp_path: Path) -> None:
    _make_git(tmp_path / "a")
    (tmp_path / "a" / "README.md").write_text("# a")
    result = enumerate_workspace(str(tmp_path))
    assert result["kind"] == "enumeration"
    assert result["schema"] == 1


def test_enumerate_matches_cli_json(tmp_path: Path) -> None:
    """MCP envelope must equal CLI --json output for the same input."""
    _make_git(tmp_path / "a")
    (tmp_path / "a" / "README.md").write_text("# a")
    _make_git(tmp_path / "b")
    (tmp_path / "b" / "README.md").write_text("# b")

    mcp_envelope = enumerate_workspace(str(tmp_path))

    # Run the CLI as a subprocess with the local engine source on path
    # so the test works against the in-development engine, not whatever
    # wheel happens to be installed in site-packages.
    engine_src = (
        Path(__file__).resolve().parents[2]
        / "agent-readiness" / "src"
    )
    env = os.environ.copy()
    py_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{engine_src}:{py_path}" if py_path else str(engine_src)
    )
    cli_output = subprocess.run(
        ["python3", "-m", "agent_readiness.cli", "enumerate",
         str(tmp_path), "--json"],
        check=True, capture_output=True, text=True, env=env,
    )
    cli_envelope = json.loads(cli_output.stdout)
    assert mcp_envelope == cli_envelope


def test_enumerate_rejects_non_directory(tmp_path: Path) -> None:
    f = tmp_path / "regular.txt"
    f.write_text("hi")
    try:
        enumerate_workspace(str(f))
    except (ValueError, NotADirectoryError):
        return
    raise AssertionError("expected an error for non-directory input")


# --- check_workspace_readiness ----------------------------------------

from agent_readiness_mcp.server import check_workspace_readiness  # noqa: E402


def test_check_workspace_readiness_empty_children_raises(tmp_path: Path) -> None:
    try:
        check_workspace_readiness(str(tmp_path), [])
    except ValueError:
        return
    raise AssertionError("expected ValueError for empty children_paths")


def test_check_workspace_readiness_returns_envelope(tmp_path: Path) -> None:
    _make_git(tmp_path / "a")
    (tmp_path / "a" / "README.md").write_text("# a")
    result = check_workspace_readiness(str(tmp_path), [str(tmp_path / "a")])
    assert result["kind"] == "workspace_readiness"
    assert result["schema"] == 1
    pillar_names = {p["pillar"] for p in result["pillars"]}
    assert "coordination" in pillar_names


def test_workspace_mcp_matches_cli_json(tmp_path: Path) -> None:
    """MCP envelope must equal CLI --json for the same input."""
    _make_git(tmp_path / "a")
    (tmp_path / "a" / "README.md").write_text("# a")

    mcp_envelope = check_workspace_readiness(str(tmp_path), [str(tmp_path / "a")])

    engine_src = (
        Path(__file__).resolve().parents[2]
        / "agent-readiness" / "src"
    )
    env = os.environ.copy()
    py_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{engine_src}:{py_path}" if py_path else str(engine_src)
    )
    cli_output = subprocess.run(
        ["python3", "-m", "agent_readiness.cli", "workspace-scan",
         str(tmp_path), f"--children={tmp_path}/a", "--json"],
        check=True, capture_output=True, text=True, env=env,
    )
    cli_envelope = json.loads(cli_output.stdout)

    # scan_duration_ms is timing-sensitive.
    mcp_envelope["stats"].pop("scan_duration_ms", None)
    cli_envelope["stats"].pop("scan_duration_ms", None)
    assert mcp_envelope == cli_envelope
