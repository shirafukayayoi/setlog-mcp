"""実際のMCP初期化とツール呼び出しを、外部通信なしで検証する。"""

import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from setlog_mcp import client, server

EXPECTED_TOOLS = {
    "get_status",
    "get_profile",
    "list_collections",
    "start_email_login",
    "verify_email_login",
    "claim_key_transfer",
    "check_key_transfer",
    "prepare_video",
    "send_prepared_video",
    "verify_video",
}


@pytest.mark.anyio
async def test_tool_schema_and_annotations():
    async with create_connected_server_and_client_session(server.mcp) as session:
        tools = (await session.list_tools()).tools
        assert {tool.name for tool in tools} == EXPECTED_TOOLS
        mapping = {tool.name: tool for tool in tools}
        assert mapping["get_profile"].annotations.readOnlyHint is True
        assert mapping["send_prepared_video"].annotations.readOnlyHint is False
        assert "job_id" in mapping["send_prepared_video"].inputSchema["properties"]
        assert "video_path" in mapping["prepare_video"].inputSchema["properties"]


@pytest.mark.anyio
async def test_read_tools_return_only_selected_fields(monkeypatch):
    monkeypatch.setattr(client, "profile", lambda: {"name": "テスト利用者"})
    monkeypatch.setattr(
        server.upload,
        "collections",
        lambda: [{"id": "room", "name": "テスト投稿先", "participants": [], "pin": "not-for-output"}],
    )
    async with create_connected_server_and_client_session(server.mcp) as session:
        result = await session.call_tool("get_profile", {})
        assert result.isError is False
        assert result.structuredContent["name"] == "テスト利用者"
        rooms = await session.call_tool("list_collections", {})
        assert rooms.structuredContent == {
            "collections": [{"id": "room", "name": "テスト投稿先", "other_participants": 0}]
        }


@pytest.mark.anyio
async def test_error_does_not_expose_internal_exception(monkeypatch):
    def fail():
        raise OSError("private-value-must-not-appear")

    monkeypatch.setattr(client, "profile", fail)
    async with create_connected_server_and_client_session(server.mcp) as session:
        result = await session.call_tool("get_profile", {})
        assert result.isError is True
        assert "private-value-must-not-appear" not in result.model_dump_json()


@pytest.mark.anyio
async def test_stdio_initialize_list_and_call(tmp_path):
    environment = dict(os.environ)
    environment.pop("SETLOG_API_KEY", None)
    environment["SETLOG_MCP_STATE_DIR"] = str(tmp_path / "stdio-state")
    parameters = StdioServerParameters(command=sys.executable, args=["-m", "setlog_mcp"], env=environment)
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "setlog-mcp"
            assert {tool.name for tool in (await session.list_tools()).tools} == EXPECTED_TOOLS
            status = await session.call_tool("get_status", {})
            assert status.isError is False
            assert status.structuredContent["session_present"] is False
            assert status.structuredContent["api_key_configured"] is False
            missing = await session.call_tool("get_profile", {})
            assert missing.isError is True
            assert (await session.call_tool("get_status", {})).isError is False
