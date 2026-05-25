"""Tests that the 5 Plan-3 live-scan tools are registered by ``serve()``.

We don't stand up a real MCP transport — instead we inject a FastMCP
instance via the ``_INJECTED_SERVER_FOR_TEST`` hook on the module and
wrap ``server.tool`` to record the names of every function decorator
the body of ``serve()`` applies.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

import agent_readiness_mcp.server as srv_mod
from agent_readiness_mcp.server import serve


def test_all_live_scan_tools_register():
    server = FastMCP("agent-readiness-test")
    registered: list[str] = []

    orig_tool = server.tool

    def capture_tool(*args, **kwargs):
        decorator = orig_tool(*args, **kwargs)

        def wrap(fn):
            registered.append(fn.__name__)
            return decorator(fn)

        return wrap

    server.tool = capture_tool  # type: ignore[assignment]

    srv_mod._INJECTED_SERVER_FOR_TEST = server
    try:
        serve()
    finally:
        delattr(srv_mod, "_INJECTED_SERVER_FOR_TEST")

    expected = {
        "scan_workspace_async_tool",
        "get_scan_status_tool",
        "stop_scan_tool",
        "list_scans_tool",
        "render_workspace_report_tool",
    }
    assert expected.issubset(set(registered))


def test_legacy_tools_still_register():
    """Plan 3 should not remove the original tool set."""
    server = FastMCP("agent-readiness-test")
    registered: list[str] = []
    orig_tool = server.tool

    def capture_tool(*args, **kwargs):
        decorator = orig_tool(*args, **kwargs)

        def wrap(fn):
            registered.append(fn.__name__)
            return decorator(fn)

        return wrap

    server.tool = capture_tool  # type: ignore[assignment]
    srv_mod._INJECTED_SERVER_FOR_TEST = server
    try:
        serve()
    finally:
        delattr(srv_mod, "_INJECTED_SERVER_FOR_TEST")

    expected_legacy = {
        "detect_workspace_tool",
        "enumerate_workspace_tool",
        "check_workspace_readiness_tool",
        "scan_workspace_tool",
        "scan_repo_tool",
        "apply_top_action_tool",
        "list_friction_tool",
        "manifest_validate_tool",
        "ontology_tool",
    }
    assert expected_legacy.issubset(set(registered))
