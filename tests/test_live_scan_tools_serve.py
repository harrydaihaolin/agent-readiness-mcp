"""Registration + docstring tests for the headless (prompt-only) tool set.

The v0.9.0 server is prompt-only: every scan returns its report inline.
The dashboard / live-scan tools (``scan_workspace_async_tool``,
``get_scan_status_tool``, ``stop_scan_tool``, ``list_scans_tool``,
``render_workspace_report_tool``) and the redundant
``check_workspace_readiness_tool`` were removed.

We don't stand up a real MCP transport — instead we inject a FastMCP
instance via the ``_INJECTED_SERVER_FOR_TEST`` hook on the module and
wrap ``server.tool`` to record the names + docstrings of every function
decorator the body of ``serve()`` applies.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

import agent_readiness_mcp.server as srv_mod
from agent_readiness_mcp.server import serve


def _registered_tools() -> dict[str, str]:
    """Run ``serve()`` against an instrumented FastMCP and return a
    {tool_name: docstring} dict for every ``@server.tool()`` decoration.
    """
    server = FastMCP("agent-readiness-test")
    docs: dict[str, str] = {}
    orig_tool = server.tool

    def capture_tool(*args, **kwargs):
        decorator = orig_tool(*args, **kwargs)

        def wrap(fn):
            docs[fn.__name__] = fn.__doc__ or ""
            return decorator(fn)

        return wrap

    server.tool = capture_tool  # type: ignore[assignment]
    srv_mod._INJECTED_SERVER_FOR_TEST = server
    try:
        serve()
    finally:
        delattr(srv_mod, "_INJECTED_SERVER_FOR_TEST")
    return docs


def test_headless_tool_set_registers():
    registered = set(_registered_tools())
    expected = {
        "inspect_tool",
        "detect_workspace_tool",
        "enumerate_workspace_tool",
        "scan_repo_tool",
        "scan_monorepo_tool",
        "scan_workspace_tool",
        "apply_top_action_tool",
        "list_friction_tool",
        "manifest_validate_tool",
        "ontology_tool",
        # Bundle B gap-aware tools
        "record_gap_tool",
        "ask_clarification_tool",
        "log_assumption_tool",
        "confirm_apply_tool",
    }
    assert expected.issubset(registered)


def test_dashboard_and_live_scan_tools_are_gone():
    """The prompt-only server must not register any dashboard/live-scan
    tools or the redundant synchronous workspace tool."""
    registered = set(_registered_tools())
    removed = {
        "scan_workspace_async_tool",
        "get_scan_status_tool",
        "stop_scan_tool",
        "list_scans_tool",
        "render_workspace_report_tool",
        "check_workspace_readiness_tool",
        "scan_and_view_tool",
    }
    assert removed.isdisjoint(registered), (
        f"unexpected dashboard tools still registered: {removed & registered}"
    )


def test_inspect_docstring_steers_to_typed_headless_tools():
    docs = _registered_tools()
    doc = docs["inspect_tool"]
    assert "scan_repo_tool" in doc
    assert "scan_monorepo_tool" in doc
    assert "scan_workspace_tool" in doc
    # Headless contract: report returned inline; no dashboard mechanics.
    assert "inline" in doc.lower()
    for token in ("onboarding", "wizard", "dashboard_url"):
        assert token not in doc.lower(), f"inspect_tool docstring leaks {token!r}"


def test_scan_tool_docstrings_are_headless():
    docs = _registered_tools()
    for name in ("scan_repo_tool", "scan_monorepo_tool", "scan_workspace_tool"):
        doc = docs[name].lower()
        for token in ("onboarding", "wizard", "dashboard_url"):
            assert token not in doc, f"{name} docstring still mentions {token!r}"
        assert "inline" in doc, f"{name} docstring should say it returns inline"


def test_scan_workspace_tool_takes_explicit_children():
    """The workspace scan tool must accept an explicit children list
    (the skill enumerates via inspect and passes them in)."""
    import inspect as pyinspect

    src = pyinspect.getsource(serve)
    idx = src.index("def scan_workspace_tool")
    block = src[idx:idx + 400]
    assert "children" in block
