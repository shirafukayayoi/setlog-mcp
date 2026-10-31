"""日付境界、ページ送り、取得結果の情報制限を外部通信なしで確認する。"""

import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from setlog_mcp import client, history, upload


def test_tokyo_day_bounds_and_inclusive_end_date():
    start, end = history.date_bounds("2026-10-30", "2026-10-31", "Asia/Tokyo", 4)
    assert datetime.fromtimestamp(start / 1000, timezone.utc).isoformat() == "2026-10-29T19:00:00+00:00"
    assert datetime.fromtimestamp(end / 1000, timezone.utc).isoformat() == "2026-10-31T19:00:00+00:00"
    calendar_start, _ = history.date_bounds("2026-10-30", "2026-10-30", "Asia/Tokyo", 0)
    assert start - calendar_start == 4 * 3600 * 1000


@pytest.mark.parametrize(("day", "hours"), [("2026-03-08", 23), ("2026-11-01", 25)])
def test_day_bounds_follow_daylight_saving(day, hours):
    start, end = history.date_bounds(day, day, "America/New_York", 0)
    assert end - start == hours * 3600 * 1000


@pytest.mark.parametrize(
    "arguments",
    [
        ("20261030", "2026-10-30", "Asia/Tokyo", 4),
        ("2026-10-31", "2026-10-30", "Asia/Tokyo", 4),
        ("2026-01-01", "2027-01-02", "Asia/Tokyo", 4),
        ("2026-10-30", "2026-10-30", "Invalid/Zone", 4),
        ("2026-10-30", "2026-10-30", "Asia/Tokyo", 24),
        ("2026-03-08", "2026-03-08", "America/New_York", 2),
        ("2026-11-01", "2026-11-01", "America/New_York", 1),
    ],
)
def test_invalid_or_ambiguous_date_boundaries_are_rejected(arguments):
    with pytest.raises(client.SetlogError):
        history.date_bounds(*arguments)


@pytest.fixture
def account(monkeypatch):
    client.save_private("session.bin", {"token": "synthetic-test-token", "user": {"userId": "self"}})
    monkeypatch.setattr(upload, "collections", lambda: [{"id": "room"}])


def post(identifier, stamp):
    return {
        "messageId": identifier,
        "conversationId": "room",
        "senderId": "self",
        "timestamp": stamp,
        "contentType": "video/mp4",
        "keys": [{"wrappedB64": "must-not-appear"}],
        "envelope": "must-not-appear",
        "media": {"0": {"path": "must-not-appear", "captions": [{"text": "今日の記録😀"}]}},
    }


def test_date_filter_excludes_end_deleted_and_other_sensitive_fields(account, monkeypatch):
    start, end = history.date_bounds("2026-10-31", "2026-10-31", "Asia/Tokyo", 4)
    messages = [
        post("at-end", end),
        post("inside", end - 1),
        post("at-start", start),
        post("older", start - 1),
    ]
    messages.insert(2, {**post("deleted", end - 2), "isDeleted": True})
    monkeypatch.setattr(
        client, "api", lambda *args, **kwargs: {"messages": messages, "hasMore": True, "nextCursor": "next"}
    )
    result = history.search_posts_by_date("room", "2026-10-31")
    assert [item["message_id"] for item in result["posts"]] == ["inside", "at-start"]
    assert result["has_more"] is False
    assert result["next_cursor"] is None
    assert result["posts"][0]["captions"] == ["今日の記録😀"]
    assert "must-not-appear" not in json.dumps(result)


def test_paging_preserves_scope_and_rejects_mismatched_dates(account, monkeypatch):
    start, end = history.date_bounds("2026-10-31", "2026-10-31", "Asia/Tokyo", 4)
    calls = []

    def api(method, path, **kwargs):
        query = parse_qs(urlsplit(path).query)
        calls.append(query)
        if "cursor" not in query:
            assert query["beforeTimestamp"] == [str(end)]
            return {"messages": [post("first", end - 1)], "hasMore": True, "nextCursor": "native&cursor=/"}
        assert query["cursor"] == ["native&cursor=/"]
        return {"messages": [post("second", start)], "hasMore": False}

    monkeypatch.setattr(client, "api", api)
    first = history.search_posts_by_date("room", "2026-10-31", limit=1)
    second = history.search_posts_by_date("room", "2026-10-31", limit=1, cursor=first["next_cursor"])
    assert second["posts"][0]["message_id"] == "second"
    with pytest.raises(client.SetlogError, match="next_cursor"):
        history.search_posts_by_date("room", "2026-10-30", cursor=first["next_cursor"])
    assert len(calls) == 2


def test_list_cursor_keeps_first_query_upper_bound(account, monkeypatch):
    monkeypatch.setattr(history.time, "time", lambda: 1000)
    calls = []

    def api(method, path, **kwargs):
        query = parse_qs(urlsplit(path).query)
        calls.append(query)
        return {"messages": [post("first", 900000)], "hasMore": True, "nextCursor": "cursor"}

    monkeypatch.setattr(client, "api", api)
    first = history.list_posts("room", limit=1)
    monkeypatch.setattr(history.time, "time", lambda: 2000)
    with pytest.raises(client.SetlogError, match="ページ送り"):
        history.list_posts("room", limit=1, cursor=first["next_cursor"])
    assert calls[0]["beforeTimestamp"] == ["1000001"]
    assert calls[1]["cursor"] == ["cursor"]


def test_collection_membership_is_checked_before_read(account, monkeypatch):
    monkeypatch.setattr(
        client, "api", lambda *args, **kwargs: pytest.fail("本人の投稿先確認前に通信しました")
    )
    with pytest.raises(client.SetlogError, match="コレクション"):
        history.list_posts("other-room")


def test_timestamp_only_paging_is_rejected_to_avoid_skipping_tied_posts(account, monkeypatch):
    monkeypatch.setattr(
        client,
        "api",
        lambda *args, **kwargs: {
            "messages": [post("first", 1000)],
            "hasMore": True,
            "nextBeforeTimestamp": 1000,
        },
    )
    with pytest.raises(client.SetlogError, match="次のページ"):
        history.list_posts("room", limit=1, before_timestamp_ms=2000)
