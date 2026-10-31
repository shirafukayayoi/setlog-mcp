"""本人のコレクションの投稿一覧と、日付範囲を指定した検索。"""

from __future__ import annotations

import base64
import json
import math
import re
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import client, upload


def date_bounds(start_date: str, end_date: str, zone_name: str, day_start_hour: int) -> tuple[int, int]:
    try:
        if not all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) for value in (start_date, end_date)):
            raise ValueError
        first, last = date.fromisoformat(start_date), date.fromisoformat(end_date)
        if not 0 <= (last - first).days < 366 or not 0 <= day_start_hour <= 23:
            raise ValueError
        zone = ZoneInfo(zone_name)
        bounds = [
            datetime(day.year, day.month, day.day, day_start_hour, tzinfo=zone)
            for day in (first, last + timedelta(days=1))
        ]
        for boundary in bounds:
            restored = boundary.astimezone(timezone.utc).astimezone(zone)
            if restored.replace(tzinfo=None) != boundary.replace(tzinfo=None):
                raise ValueError
            if boundary.utcoffset() != boundary.replace(fold=1).utcoffset():
                raise ValueError
        return tuple(int(boundary.timestamp() * 1000) for boundary in bounds)
    except (ValueError, OverflowError, OSError, ZoneInfoNotFoundError):
        raise client.SetlogError(
            "日付はYYYY-MM-DD、期間は順序を守って366日以内、区切り時刻は0〜23時、"
            "タイムゾーンは有効なIANA名を指定してください。夏時間で曖昧な区切り時刻は指定できません。"
        ) from None


def _stamp(message: dict) -> int:
    value = message.get("timestamp", message.get("clientTimestamp"))
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise client.SetlogError("取得した投稿の日時を確認できませんでした。")
    return int(value)


def _summarize(message: dict, zone: ZoneInfo) -> dict:
    media = message.get("media") or []
    if isinstance(media, dict):
        media = [media] if "path" in media or "mimeType" in media else list(media.values())
    if not isinstance(media, list) or any(not isinstance(item, dict) for item in media):
        raise client.SetlogError("取得した投稿のメディア形式を確認できませんでした。")
    captions = []
    for item in media:
        captions.extend(
            caption["text"]
            for caption in item.get("captions", []) or []
            if isinstance(caption, dict) and isinstance(caption.get("text"), str)
        )
    stamp = _stamp(message)
    return {
        "message_id": message.get("messageId") or message.get("id"),
        "sender_id": message.get("senderId") or message.get("userId"),
        "timestamp_ms": stamp,
        "posted_at": datetime.fromtimestamp(stamp / 1000, zone).isoformat(),
        "content_type": message.get("contentType"),
        "captions": captions,
        "media_count": len(media),
    }


def _cursor(value: dict) -> str:
    return "v1." + base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()


def _page(collection_id: str, limit: int, cursor: str, scope: dict, zone: ZoneInfo) -> dict:
    if not 1 <= limit <= 100:
        raise client.SetlogError("limitは1〜100を指定してください。")
    query = {"limit": limit, "beforeTimestamp": scope["end"]}
    previous = None
    if cursor:
        try:
            if not cursor.startswith("v1.") or len(cursor) > 16384:
                raise ValueError
            previous = json.loads(base64.b64decode(cursor[3:], altchars=b"-_", validate=True))
            if previous.get("scope") != scope:
                raise ValueError
            if isinstance(previous.get("cursor"), str) and previous["cursor"]:
                query = {"limit": limit, "cursor": previous["cursor"]}
            else:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise client.SetlogError("検索条件に対応するnext_cursorを、そのまま指定してください。") from None
    session = client.load_private("session.bin")
    if not any(room.get("id") == collection_id for room in upload.collections()):
        raise client.SetlogError("指定された投稿先が本人のコレクション一覧にありません。")
    path = "chat/" + urllib.parse.quote(collection_id, safe="") + "/messages?" + urllib.parse.urlencode(query)
    response = client.api("GET", path, token=session["token"])
    messages = response.get("messages")
    if not isinstance(messages, list) or any(not isinstance(item, dict) for item in messages):
        raise client.SetlogError("投稿一覧の形式を確認できませんでした。")
    if len(messages) > limit:
        raise client.SetlogError("投稿一覧の件数が指定した上限を超えています。")
    posts, seen = [], set()
    reached_start = False
    for message in messages:
        if message.get("conversationId", collection_id) != collection_id:
            raise client.SetlogError("取得した投稿の投稿先が指定内容と一致しません。")
        stamp = _stamp(message)
        reached_start |= stamp < scope["start"]
        identifier = message.get("messageId") or message.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise client.SetlogError("取得した投稿のIDを確認できませんでした。")
        if message.get("isDeleted") or identifier in seen or not scope["start"] <= stamp < scope["end"]:
            continue
        seen.add(identifier)
        posts.append(_summarize(message, zone))
    has_more = bool(response.get("hasMore")) and not reached_start
    next_cursor = None
    if has_more:
        following = {"scope": scope}
        raw_cursor = response.get("nextCursor")
        if isinstance(raw_cursor, str) and raw_cursor:
            following["cursor"] = raw_cursor
        else:
            raise client.SetlogError("次のページを取得する情報が不足しています。")
        if following == previous or not messages:
            raise client.SetlogError("投稿一覧のページ送りが進まないため停止しました。")
        next_cursor = _cursor(following)
    return {"posts": posts, "next_cursor": next_cursor, "has_more": has_more}


def list_posts(
    collection_id: str,
    limit: int = 50,
    cursor: str = "",
    before_timestamp_ms: int | None = None,
    timezone_name: str = "Asia/Tokyo",
) -> dict:
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise client.SetlogError("timezone_nameには有効なIANAタイムゾーン名を指定してください。") from None
    # 続きの取得では、初回に固定した上限をカーソルから引き継ぐ。
    end = before_timestamp_ms if before_timestamp_ms is not None else int(time.time() * 1000) + 1
    if cursor and before_timestamp_ms is None:
        try:
            if len(cursor) > 16384 or not cursor.startswith("v1."):
                raise ValueError
            end = json.loads(base64.b64decode(cursor[3:], altchars=b"-_", validate=True))["scope"]["end"]
        except (ValueError, TypeError, KeyError):
            raise client.SetlogError("next_cursorの形式が正しくありません。") from None
    if isinstance(end, bool) or not isinstance(end, int) or end < 0:
        raise client.SetlogError("before_timestamp_msには0以上のミリ秒時刻を指定してください。")
    scope = {"kind": "list", "collection": collection_id, "start": 0, "end": end, "zone": timezone_name}
    return _page(collection_id, limit, cursor, scope, zone)


def search_posts_by_date(
    collection_id: str,
    start_date: str,
    end_date: str = "",
    timezone_name: str = "Asia/Tokyo",
    day_start_hour: int = 4,
    limit: int = 50,
    cursor: str = "",
) -> dict:
    end_date = end_date or start_date
    start, end = date_bounds(start_date, end_date, timezone_name, day_start_hour)
    scope = {"kind": "date", "collection": collection_id, "start": start, "end": end, "zone": timezone_name}
    result = _page(collection_id, limit, cursor, scope, ZoneInfo(timezone_name))
    return {
        **result,
        "start_date": start_date,
        "end_date": end_date,
        "timezone": timezone_name,
        "day_start_hour": day_start_hour,
    }
