"""Unit tests for the intent-bridge MCP tools (Plan 2).

These wrap the engine's ``agent_readiness.live_scan.intents`` store, so the
tests drive that store directly (under a fake HOME) and assert the MCP
wrappers report the right shapes."""
from __future__ import annotations

from agent_readiness_mcp.server import (
    ack_intent,
    claim_intent,
    get_pending_intents,
)


def test_get_pending_intents_lists_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from agent_readiness.live_scan.intents import create_intent
    a = create_intent("start", path="/a")
    out = get_pending_intents()
    assert any(r["id"] == a["id"] and r["status"] == "pending" for r in out["intents"])


def test_claim_intent_returns_claimed_then_none(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from agent_readiness.live_scan.intents import create_intent
    rec = create_intent("start", path="/a")
    claimed = claim_intent(rec["id"])
    assert claimed["ok"] is True
    assert claimed["intent"]["status"] == "claimed"
    again = claim_intent(rec["id"])
    assert again["ok"] is False


def test_ack_intent_marks_terminal(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from agent_readiness.live_scan.intents import claim_intent as _claim
    from agent_readiness.live_scan.intents import create_intent
    rec = create_intent("start", path="/a")
    _claim(rec["id"])
    out = ack_intent(rec["id"], "done", result={"dashboard_url": "http://x"})
    assert out["ok"] is True
    assert out["intent"]["status"] == "done"


def test_ack_intent_unknown_returns_not_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    out = ack_intent("int-nope00", "done")
    assert out["ok"] is False
