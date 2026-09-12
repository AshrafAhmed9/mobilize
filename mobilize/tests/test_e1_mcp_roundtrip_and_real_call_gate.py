"""E1: proves an MCP request actually round-trips through the server to a
tool response (not just that the underlying function works), that the same
real-call confirmation policy is enforced identically by the MCP tool, the
CLI, and the dashboard, and that none of the three will place a real call
without explicit authorization.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.mark.asyncio
async def test_mcp_server_exposes_fixture_and_real_tools():
    """Round-trips an actual MCP protocol call through the running server
    object (mcp.server.fastmcp's FastMCP), not just calling the underlying
    Python function -- proving the tool is really registered and reachable
    the way an MCP client (Claude Code, Codex, Cursor) would reach it."""
    from mobilize.mcp.server import mcp

    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert "mobilize_fixture" in names
    assert "mobilize_real" in names
    assert "mobilize_simulated" in names

    result = await mcp.call_tool("mobilize_fixture", {"scenario": "success"})
    assert result.is_error is False
    assert len(result.content) >= 1


@pytest.mark.asyncio
async def test_mcp_call_tool_fixture_scenario_matches_direct_call():
    """The value returned through the actual MCP call_tool round-trip must
    match calling the underlying tool function directly -- proving the
    server layer doesn't silently transform or drop fields."""
    import json

    from mobilize.mcp.server import mcp, mobilize_fixture

    direct = await mobilize_fixture("success")
    raw = await mcp.call_tool("mobilize_fixture", {"scenario": "success"})
    # FastMCP's call_tool returns a CallToolResult whose .content is a list
    # of content blocks; the tool's dict return value is JSON-serialized
    # into the first TextContent block's .text.
    assert raw.is_error is False
    structured = json.loads(raw.content[0].text)
    assert structured.get("scenario") == direct["scenario"]
    assert structured.get("expected_outcome") == direct["expected_outcome"]


@pytest.mark.asyncio
async def test_mcp_real_call_refuses_without_confirm():
    """confirm defaults to False; the tool must return a preview and place
    no call, not raise, not require a caught exception to stay safe."""
    from mobilize.mcp.server import mobilize_real

    result = await mobilize_real(
        need_label="test", phones=["+15550101234"], timezones=["America/New_York"],
    )
    assert result["preview"] is True
    assert "would_call" in result
    assert "No calls placed" in result["message"]


@pytest.mark.asyncio
async def test_mcp_real_call_requires_calle_api_key_even_with_confirm(monkeypatch):
    """Even with confirm=true, a missing CALLE_API_KEY must refuse rather
    than attempt a call with no credentials."""
    import os
    monkeypatch.delenv("CALLE_API_KEY", raising=False)
    from mobilize.mcp.server import mobilize_real

    result = await mobilize_real(
        need_label="test", phones=["+15550101234"], timezones=["America/New_York"], confirm=True,
    )
    assert "error" in result
    assert "CALLE_API_KEY" in result["error"]


@pytest.mark.asyncio
async def test_dashboard_ws_refuses_real_dispatch_without_confirm(tmp_path, monkeypatch):
    """Same policy, enforced server-side by the dashboard's WebSocket
    handler: confirm:false (or omitted) with simulate:false must refuse and
    place no call, even though the browser's own confirmation dialog is
    only a UX nicety."""
    import mobilize.app.dashboard as dashboard_module
    monkeypatch.setattr(dashboard_module, "REGISTRY_STATE_PATH", tmp_path / "registry.json")
    monkeypatch.setattr(dashboard_module, "REGISTRY_SOURCE_MARKER_PATH", tmp_path / "registry_source.txt")
    monkeypatch.setattr(dashboard_module, "GOVERNANCE_STATE_PATH", tmp_path / "governance.json")

    client = TestClient(dashboard_module.app)
    with client.websocket_connect(
        "/ws/run", headers={"origin": "http://127.0.0.1:8731"}
    ) as ws:
        ws.send_json({"need_label": "test", "need_count": 1, "max_calls": 1,
                       "simulate": False, "confirm": False})
        msg = ws.receive_json()
    assert msg["event"] == "error"
    assert "confirm" in msg["data"]["message"].lower()
    assert "no calls were placed" in msg["data"]["message"].lower()


@pytest.mark.asyncio
async def test_cli_real_path_refuses_without_typed_yes(monkeypatch, capsys):
    """The CLI's --real path requires an interactive typed 'yes'; anything
    else must abort before placing a call. Simulate stdin answering 'no'."""
    monkeypatch.setattr("builtins.input", lambda *_: "no")
    monkeypatch.setenv("CALLE_API_KEY", "fake-key-not-used")

    from mobilize.app.cli import run_real
    await run_real(phones=["+15550101234"], timezones=["America/New_York"],
                    need_count=1, need_label="test")
    captured = capsys.readouterr()
    assert "abort" in (captured.out + captured.err).lower()
