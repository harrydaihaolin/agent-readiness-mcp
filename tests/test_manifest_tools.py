"""Tests for the manifest_validate MCP tool.

Asserts the MCP tool's JSON output is byte-identical to the CLI
``--json`` output for the same inputs (the contract that makes a
fixture cover both surfaces).

These tests are skipped automatically when the installed
``agent-readiness`` wheel predates the manifest module (M1 ships in
v2.7.0); CI re-runs once the engine release cascades to PyPI.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from agent_readiness_mcp.server import manifest_validate


_ENGINE_HAS_MANIFEST = (
    importlib.util.find_spec("agent_readiness.manifest") is not None
)

pytestmark = pytest.mark.skipif(
    not _ENGINE_HAS_MANIFEST,
    reason=(
        "installed agent-readiness wheel predates the manifest module; "
        "tests run once the engine release cascades."
    ),
)


REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNER_SRC = REPO_ROOT / "agent-readiness" / "src"


def _write_minimal_manifest(root: Path) -> None:
    (root / "manifest.yaml").write_text(yaml.safe_dump({
        "apiVersion": "agent-readiness.io/v1",
        "kind": "WorkspaceManifest",
        "metadata": {
            "name": "t",
            "version": "1.0.0",
            "compatibleScanner": ">=0.5.0,<1.0.0",
        },
        "spec": {"exemplar": "x", "repos": [], "answers": []},
    }))
    (root / "glossary.yaml").write_text(yaml.safe_dump({
        "apiVersion": "agent-readiness.io/v1",
        "kind": "Glossary",
        "spec": {"terms": [], "exceptions": []},
    }))
    (root / "boundaries.yaml").write_text(yaml.safe_dump({
        "apiVersion": "agent-readiness.io/v1",
        "kind": "Boundaries",
        "spec": {"tagAxes": {}, "rules": [{"name": "default", "effect": "allow"}]},
    }))
    (root / "rules").mkdir(exist_ok=True)
    (root / ".agent-readiness-version").write_text(">=0.5.0,<1.0.0\n")


def test_manifest_validate_returns_envelope(tmp_path: Path) -> None:
    _write_minimal_manifest(tmp_path)
    envelope = manifest_validate(path=str(tmp_path))
    assert envelope["apiVersion"] == "agent-readiness.io/v1"
    assert envelope["kind"] == "ManifestValidationResult"
    assert envelope["summary"]["valid"] is True
    assert envelope["summary"]["manifest_name"] == "t"
    assert envelope["issues"] == []


def test_manifest_validate_byte_identical_to_cli(tmp_path: Path) -> None:
    _write_minimal_manifest(tmp_path)
    env = {**os.environ}
    if SCANNER_SRC.is_dir():
        env["PYTHONPATH"] = str(SCANNER_SRC)
    cli_raw = subprocess.check_output(
        [sys.executable, "-m", "agent_readiness.cli",
         "manifest", "validate", str(tmp_path), "--json"],
        env=env,
    ).decode()
    cli_envelope = json.loads(cli_raw)
    mcp_envelope = manifest_validate(path=str(tmp_path))
    assert cli_envelope == mcp_envelope


def test_manifest_validate_invalid_manifest_returns_failure_envelope(
    tmp_path: Path,
) -> None:
    _write_minimal_manifest(tmp_path)
    (tmp_path / "boundaries.yaml").write_text(yaml.safe_dump({
        "apiVersion": "agent-readiness.io/v1",
        "kind": "Boundaries",
        "spec": {
            "tagAxes": {},
            "rules": [
                {"name": "bad", "from": {"tier": "edge"},
                 "to": {"tier": "internal"}, "effect": "deny"},
                {"name": "default", "effect": "allow"},
            ],
        },
    }))
    envelope = manifest_validate(path=str(tmp_path))
    assert envelope["summary"]["valid"] is False
    assert envelope["summary"]["errors"] >= 1


def test_manifest_validate_missing_directory_returns_error_envelope(
    tmp_path: Path,
) -> None:
    envelope = manifest_validate(path=str(tmp_path / "does-not-exist"))
    assert envelope["summary"]["valid"] is False
    assert envelope["summary"]["errors"] >= 1
