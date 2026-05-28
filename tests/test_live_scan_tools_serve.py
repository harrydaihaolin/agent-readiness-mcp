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
        "inspect_tool",
        "scan_monorepo_tool",
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


def _capture_tool_docstrings() -> dict[str, str]:
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


def test_docstrings_steer_multi_repo_to_async_tool():
    """v0.7.2 regression: the docstrings the LLM reads when picking a
    workspace tool must steer multi-repo paths to
    ``scan_workspace_async_tool`` (the live dashboard, ~2s return) and
    away from ``check_workspace_readiness_tool`` (synchronous, blocks
    the chat for minutes).

    Before v0.7.2 the ``enumerate_workspace_tool`` docstring recommended
    the synchronous tool for workspaces and did not warn that the sync
    tool blocks. The skill would dutifully pick the sync path on a
    17-repo workspace and stall the conversation for 5+ minutes.

    User report 2026-05-27:

        > "Razzle-dazzling… (5m 8s · almost done thinking)
        >  what's taking so long? shouldn't we get to the prompt
        >  on dashboard first?"
    """
    docs = _capture_tool_docstrings()

    # enumerate_workspace_tool must steer multi-repo to the async tool.
    enum = docs["enumerate_workspace_tool"]
    assert "scan_workspace_async_tool" in enum, (
        "enumerate_workspace_tool docstring must mention "
        "scan_workspace_async_tool so the LLM picks it for workspaces"
    )
    assert "multi-repo workspace" in enum.lower()

    # check_workspace_readiness_tool must warn it is synchronous and
    # recommend the async tool as the default.
    sync_doc = docs["check_workspace_readiness_tool"]
    assert "synchronous" in sync_doc.lower(), (
        "check_workspace_readiness_tool docstring must declare itself "
        "synchronous so the LLM doesn't pick it for chat sessions"
    )
    assert "scan_workspace_async_tool" in sync_doc, (
        "check_workspace_readiness_tool docstring must redirect callers "
        "to scan_workspace_async_tool for normal multi-repo cases"
    )
    assert "block" in sync_doc.lower()

    # scan_workspace_async_tool must announce itself as the default.
    async_doc = docs["scan_workspace_async_tool"]
    assert "default" in async_doc.lower(), (
        "scan_workspace_async_tool docstring must mark itself as the "
        "default workspace path so the LLM picks it first"
    )
    assert "dashboard_url" in async_doc

    # get_scan_status_tool must explicitly say "at most once per chat
    # turn" so the LLM doesn't poll-loop.
    status_doc = docs["get_scan_status_tool"]
    assert "once per chat turn" in status_doc.lower(), (
        "get_scan_status_tool docstring must forbid polling loops"
    )


def test_enumerate_docstring_obeys_classification_hint():
    """v0.7.3 regression: enumerate_workspace_tool must tell the LLM
    to obey ``classification_hint.recommended_action`` verbatim — no
    re-classification, no deliberation, no README reads.

    Before v0.7.3 the docstring asked the LLM to apply the rubric
    itself. For the (root .git AND children .git) case the rubric
    doesn't match cleanly and the LLM would extended-think its way
    to a guess and scan the wrong target. User report 2026-05-27:

        > "I think we are taking a lot of time just to classify
        >  whether it's a workspace, monorepo or single repo, I
        >  think once we captured some signals then we should
        >  immediately jump to prompt and let user select…"
    """
    docs = _capture_tool_docstrings()
    doc = docs["enumerate_workspace_tool"]
    assert "classification_hint" in doc, (
        "enumerate_workspace_tool docstring must reference the new "
        "classification_hint field so the LLM reads it"
    )
    assert "recommended_action" in doc, (
        "enumerate_workspace_tool docstring must instruct the LLM "
        "to act on recommended_action"
    )
    assert "ask_user" in doc, (
        "enumerate_workspace_tool docstring must spell out the "
        "ask_user contract — the case the LLM was getting wrong"
    )
    assert "ambiguity_options" in doc, (
        "enumerate_workspace_tool docstring must tell the LLM that "
        "ambiguity_options is pre-rendered (no improvisation)"
    )


def test_scan_and_view_tool_is_not_registered_in_v0_8_0():
    """The MCP server's expected tool list should not include scan_and_view_tool."""
    import inspect as pyinspect
    from agent_readiness_mcp.server import serve

    src = pyinspect.getsource(serve)
    assert "scan_and_view_tool" not in src, \
        "scan_and_view_tool registration should be removed in v0.8.0"


def test_inspect_tool_docstring_steers_to_typed_scan_tools():
    """`inspect_tool` must teach the LLM to chain into scan_repo /
    scan_monorepo / scan_workspace based on the suggested_type."""
    import inspect as pyinspect
    from agent_readiness_mcp.server import serve

    src = pyinspect.getsource(serve)
    # Find the inspect_tool block specifically.
    idx = src.index("def inspect_tool")
    block = src[idx:idx + 2000]
    assert "scan_repo_tool" in block
    assert "scan_monorepo_tool" in block
    assert "scan_workspace_tool" in block
    assert "BEFORE picking which scan tool" in block


def test_scan_repo_tool_docstring_routes_to_others_on_mismatch():
    import inspect as pyinspect
    from agent_readiness_mcp.server import serve

    src = pyinspect.getsource(serve)
    idx = src.index("def scan_repo_tool")
    block = src[idx:idx + 2000]
    assert "scan_monorepo_tool" in block
    assert "scan_workspace_tool" in block
    assert "inspect_tool" in block


def test_scan_monorepo_tool_docstring_describes_picker_layout():
    import inspect as pyinspect
    from agent_readiness_mcp.server import serve

    src = pyinspect.getsource(serve)
    idx = src.index("def scan_monorepo_tool")
    block = src[idx:idx + 1500]
    assert "grouped" in block.lower()
    assert "/#/onboarding/" in block


def test_scan_workspace_tool_docstring_describes_picker_layout():
    import inspect as pyinspect
    from agent_readiness_mcp.server import serve

    src = pyinspect.getsource(serve)
    idx = src.index("def scan_workspace_tool")
    block = src[idx:idx + 1500]
    assert "flat" in block.lower()
    assert "/#/onboarding/" in block
