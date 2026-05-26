"""Tests for the gap-aware MCP tools (Bundle B / B1 v0.6.0).

Verifies that the four new tools — ``record_gap``,
``ask_clarification``, ``log_assumption``, and ``confirm_apply`` —
are byte-thin wrappers around the engine's
``agent_readiness.gaps`` module, writing to the expected JSONL file
and returning the documented envelope shape.

The ``confirm_apply`` tests double-check the two branches: approved
→ apply path is exercised (with confidence forcibly upgraded to
``"high"``); declined → a Gap is recorded with
``kind="user_declined_medium_confidence"``.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_ENGINE_HAS_GAPS = importlib.util.find_spec("agent_readiness.gaps") is not None

pytestmark = pytest.mark.skipif(
    not _ENGINE_HAS_GAPS,
    reason=(
        "installed agent-readiness wheel predates the gaps module "
        "(v3.2.0); tests run once the engine release cascades."
    ),
)


from agent_readiness_mcp.server import (  # noqa: E402 -- after skip-marker
    ask_clarification,
    confirm_apply,
    log_assumption,
    record_gap,
)


def _read_rows(jsonl: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in jsonl.read_text().splitlines()
        if line.strip()
    ]


def test_record_gap_writes_jsonl_and_returns_envelope(tmp_path: Path) -> None:
    result = record_gap(
        str(tmp_path),
        kind="ambiguous_object_type",
        detail="two candidates match this finding",
        severity="medium",
    )
    assert result["recorded"] is True
    assert result["id"].startswith("gap-")
    assert result["gap_kind"] == "ambiguous_object_type"
    jsonl = tmp_path / ".agent-readiness" / "gaps.jsonl"
    assert jsonl.exists()
    assert result["path"] == str(jsonl)

    rows = _read_rows(jsonl)
    assert len(rows) == 1
    assert rows[0]["kind"] == "gap"
    assert rows[0]["gap_kind"] == "ambiguous_object_type"
    assert rows[0]["resolved"] is False


def test_record_gap_with_candidate_resolutions(tmp_path: Path) -> None:
    result = record_gap(
        str(tmp_path),
        kind="missing_manifest_field",
        detail="pyproject.toml has no [project.scripts]",
        candidate_resolutions=["add scripts.x = 'pkg.cli:main'"],
        agent_session="claude-abc",
    )
    assert result["recorded"] is True
    rows = _read_rows(tmp_path / ".agent-readiness" / "gaps.jsonl")
    assert rows[0]["candidate_resolutions"] == [
        "add scripts.x = 'pkg.cli:main'"
    ]
    assert rows[0]["agent_session"] == "claude-abc"


def test_ask_clarification_writes_jsonl(tmp_path: Path) -> None:
    result = ask_clarification(
        str(tmp_path),
        question="Which interface should this Repo satisfy?",
        options=["Library", "Protocol"],
        context_path="ontology/instances/Repo/x.yaml",
    )
    assert result["recorded"] is True
    assert result["kind"] == "clarification"
    rows = _read_rows(tmp_path / ".agent-readiness" / "gaps.jsonl")
    assert rows[0]["kind"] == "clarification"
    assert rows[0]["question"].startswith("Which interface")
    assert rows[0]["options"] == ["Library", "Protocol"]


def test_log_assumption_writes_jsonl(tmp_path: Path) -> None:
    result = log_assumption(
        str(tmp_path),
        assumption="Treat ontology/ as authoritative",
        justification="Workspace declared has_ontology: true",
        expires_after="2026-12-31T00:00:00Z",
    )
    assert result["recorded"] is True
    assert result["kind"] == "assumption"
    rows = _read_rows(tmp_path / ".agent-readiness" / "gaps.jsonl")
    assert rows[0]["kind"] == "assumption"
    assert rows[0]["expires_after"] == "2026-12-31T00:00:00Z"


def test_confirm_apply_declined_records_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """approved=False → record a Gap with kind='user_declined_medium_confidence'."""
    from agent_readiness_mcp import server as srv

    fake_top = {
        "check_id": "stub.medium_confidence",
        "pillar": "flow",
        "severity": "warn",
        "message": "stub finding",
        "weight": 1.0,
        "rationale": "stub",
        "confidence": "medium",
        "fix_hint": "do the thing",
        "action": {
            "kind": "create_file",
            "path": "stub.txt",
            "template": "hi",
        },
        "verify": {"command": "true", "description": ""},
    }

    def _fake_scan_repo(path: str) -> dict:
        assert Path(path) == tmp_path
        return {"top_action": fake_top}

    monkeypatch.setattr(srv, "scan_repo", _fake_scan_repo)

    result = confirm_apply(
        str(tmp_path), approved=False, agent_session="claude-abc"
    )
    assert result["applied"] is False
    assert result["approved"] is False
    assert result["recorded_gap"] is True
    assert result["check_id"] == "stub.medium_confidence"
    assert result["id"].startswith("gap-")

    rows = _read_rows(tmp_path / ".agent-readiness" / "gaps.jsonl")
    assert len(rows) == 1
    assert rows[0]["gap_kind"] == "user_declined_medium_confidence"
    assert rows[0]["candidate_resolutions"] == ["do the thing"]
    assert rows[0]["agent_session"] == "claude-abc"
    assert not (tmp_path / "stub.txt").exists()


def test_confirm_apply_approved_runs_apply_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """approved=True → forces confidence=high and re-invokes apply_top_action."""
    from agent_readiness_mcp import server as srv

    fake_top = {
        "check_id": "stub.medium_confidence",
        "pillar": "flow",
        "severity": "warn",
        "message": "stub finding",
        "weight": 1.0,
        "rationale": "stub",
        "confidence": "medium",  # ← would be confirm_required without override
        "fix_hint": "do the thing",
        "action": {
            "kind": "create_file",
            "path": "stub.txt",
            "template": "hi\n",
        },
        "verify": {"command": "test -f stub.txt", "description": ""},
    }

    def _fake_scan_repo(path: str) -> dict:
        return {"top_action": fake_top}

    monkeypatch.setattr(srv, "scan_repo", _fake_scan_repo)

    result = confirm_apply(str(tmp_path), approved=True, run_verify=True)

    assert result["approved"] is True
    assert result["applied"] is True
    assert result["check_id"] == "stub.medium_confidence"
    assert (tmp_path / "stub.txt").exists()
    assert (tmp_path / "stub.txt").read_text() == "hi\n"
    # The original payload's confidence is mutated for the apply call
    # — that's deliberate.
    assert fake_top["confidence"] == "high"


def test_confirm_apply_no_top_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_readiness_mcp import server as srv

    monkeypatch.setattr(srv, "scan_repo", lambda _: {"top_action": None})

    result = confirm_apply(str(tmp_path), approved=True)
    assert result["applied"] is False
    assert result["skipped_reason"] == "no_top_action"
