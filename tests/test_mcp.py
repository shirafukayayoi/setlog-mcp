"""実際のMCP初期化とツール呼び出しを、外部通信なしで検証する。"""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from setlog_mcp import client, posting, server

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
    "list_posts",
    "search_posts_by_date",
    "prepare_video_batch",
    "send_prepared_videos",
    "schedule_videos",
    "list_scheduled_posts",
    "cancel_scheduled_post",
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
        assert mapping["prepare_video"].inputSchema["properties"]["comment"]["default"] == ""
        assert "comment" not in mapping["prepare_video"].inputSchema["required"]
        assert mapping["search_posts_by_date"].annotations.readOnlyHint is True
        assert mapping["schedule_videos"].annotations.readOnlyHint is False
        assert "videos" in mapping["prepare_video_batch"].inputSchema["required"]


@pytest.mark.anyio
async def test_prepare_tool_passes_comment_and_returns_preview(monkeypatch, tmp_path):
    captured = []

    def prepare(path, collection_id, comment):
        captured.append((path, collection_id, comment))
        return {"準備ID": "synthetic-job", "コメント": comment}

    monkeypatch.setattr(server.upload, "prepare", prepare)
    video = str(tmp_path / "clip.mp4")
    comment = "今日の記録\n😀"
    async with create_connected_server_and_client_session(server.mcp) as session:
        result = await session.call_tool(
            "prepare_video", {"video_path": video, "collection_id": "room", "comment": comment}
        )
        assert result.isError is False
        assert result.structuredContent["コメント"] == comment
        assert [(str(path), collection, text) for path, collection, text in captured] == [
            (video, "room", comment)
        ]


@pytest.mark.anyio
async def test_batch_input_and_schedule_cancel_through_mcp(monkeypatch, tmp_path):
    identifier = str(uuid.uuid4())
    client.save_private("session.bin", {"user": {"userId": "self"}})
    client.save_private(f"job-{identifier}.bin", {"userId": "self", "phase": "prepared"})

    def prepare(videos):
        assert isinstance(videos[0], posting.VideoInput)
        assert videos[0].comment == "一括準備😀"
        return {"job_ids": [identifier], "status": "prepared"}

    monkeypatch.setattr(posting, "prepare_video_batch", prepare)
    async with create_connected_server_and_client_session(server.mcp) as session:
        prepared = await session.call_tool(
            "prepare_video_batch",
            {
                "videos": [
                    {
                        "video_path": str(tmp_path / "clip.mp4"),
                        "collection_id": "room",
                        "comment": "一括準備😀",
                    }
                ]
            },
        )
        assert prepared.isError is False
        when = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        scheduled = await session.call_tool("schedule_videos", {"job_ids": [identifier], "publish_at": when})
        assert scheduled.isError is False
        reservation = scheduled.structuredContent["scheduled"][0]["schedule_id"]
        listed = await session.call_tool("list_scheduled_posts", {})
        assert listed.structuredContent["scheduled"][0]["job_id"] == identifier
        cancelled = await session.call_tool("cancel_scheduled_post", {"schedule_id": reservation})
        assert cancelled.structuredContent["cancelled"] is True
        assert (await session.call_tool("list_scheduled_posts", {})).structuredContent["total"] == 0


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
