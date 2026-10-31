"""動画を暗号化して準備し、指定されたコレクションへ送信する。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import client, config, crypto


def collections() -> list[dict]:
    session = client.load_private("session.bin")
    items = []
    cursor = None
    seen = set()
    while True:
        path = "collections"
        if cursor:
            encoded_cursor = json.dumps(cursor, separators=(",", ":")) if isinstance(cursor, dict) else cursor
            path += "?" + urllib.parse.urlencode({"lastEvaluatedKey": encoded_cursor})
        result = client.api("GET", path, token=session["token"])
        page = result.get("collections")
        if not isinstance(page, list):
            raise client.SetlogError("コレクション一覧の形式を確認できませんでした。")
        items.extend(page)
        cursor = result.get("lastEvaluatedKey")
        if not cursor:
            break
        marker = json.dumps(cursor, sort_keys=True)
        if marker in seen:
            raise client.SetlogError("一覧のページ送りが進まないため停止しました。")
        seen.add(marker)
    return items


def recipients(room: dict, user: dict) -> list[dict]:
    # 一覧のparticipantsには本人が含まれない。APKのrd.vと同様に本人を補う。
    members = {user["userId"]: user}
    for member in room.get("participants", []):
        if not isinstance(member, dict) or not member.get("userId"):
            raise client.SetlogError("参加者情報を確認できませんでした。")
        members[member["userId"]] = member
    return list(members.values())


def recipient_keys(room: dict, user: dict) -> dict:
    return {
        member["userId"]: {
            "kid": crypto.key_id(crypto.unb64(member.get("publicKey"))),
            "publicKey": member.get("publicKey"),
        }
        for member in recipients(room, user)
    }


def probe_video(path: Path) -> dict:
    try:
        process = subprocess.run(
            [
                config.ffprobe_command(),
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            check=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        result = json.loads(process.stdout)
        stream = next(
            stream
            for stream in result["streams"]
            if stream.get("codec_type") == "video" and not stream.get("disposition", {}).get("attached_pic")
        )
        duration = float(stream.get("duration") or result["format"]["duration"])
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, StopIteration):
        raise client.SetlogError("ffprobeで動画の形式と再生時間を確認できませんでした。") from None
    if "mp4" not in result.get("format", {}).get("format_name", ""):
        raise client.SetlogError("このクライアントではMP4形式の動画を指定してください。")
    if (
        not math.isfinite(duration)
        or duration <= 0
        or stream.get("width", 0) <= 0
        or stream.get("height", 0) <= 0
    ):
        raise client.SetlogError("動画の再生時間または寸法が不正です。")
    width, height = stream["width"], stream["height"]
    rotation = next(
        (item.get("rotation", 0) for item in stream.get("side_data_list", []) if "rotation" in item), 0
    )
    if abs(int(rotation)) % 180 == 90:
        width, height = height, width
    return {
        "width": width,
        "height": height,
        "duration": duration,
        "codec": stream.get("codec_name"),
        "mime": "video/mp4",
    }


def make_message(
    room: dict,
    user: dict,
    identity: bytes,
    video: bytes,
    metadata: dict,
    message_id: str,
    timestamp: int,
    *,
    comment: str = "",
) -> tuple[dict, bytes]:
    content_key = os.urandom(32)
    aad = crypto.media_aad(room["id"], message_id, user["userId"], metadata["mime"], 2)
    encrypted = crypto.encrypt(content_key, video, aad)
    wrapped = crypto.wrap_content_key(
        identity, content_key, room["id"], user["userId"], recipients(room, user), 2
    )
    media = {
        "path": None,
        "region": config.media_region(),
        "mimeType": metadata["mime"],
        "dimensions": {"width": metadata["width"], "height": metadata["height"]},
        "permission": "allow_saving",
        "captureUTCOffsetSeconds": config.capture_offset_seconds(),
    }
    country = config.capture_country()
    if country is not None:
        media["captureCountryCode"] = country
    if comment.strip():
        # setlog標準のキャプションとして添え、動画への焼き込みは行わない。
        media["captions"] = [{"text": comment, "time": "", "translation": "{0, 0}", "style": 0}]
    message = {
        "type": "collection",
        "conversationId": room["id"],
        "messageId": message_id,
        "uuid": str(uuid.uuid4()),
        "refreshesStaleKeys": True,
        "clientTimestamp": timestamp,
        "contentType": metadata["mime"],
        "keys": wrapped,
        "media": media,
        "mediaCipherV": 2,
    }
    return message, encrypted


def prepare(path: Path, collection_id: str, comment: str = "") -> dict:
    path = path.expanduser().resolve(strict=True)
    if not path.is_file() or path.suffix.lower() != ".mp4":
        raise client.SetlogError("通常のMP4ファイルを指定してください。")
    if path.stat().st_size > config.max_video_bytes():
        raise client.SetlogError("動画が設定された上限を超えています。先にサイズを調整してください。")
    identity = client.load_private("identity.bin")
    session = client.load_private("session.bin")
    response = client.api("GET", "user/profile", token=session["token"])
    user = response.get("user", {})
    private = crypto.unb64(identity.get("identityPrivateKeyB64"))
    if (
        identity.get("userId") != user.get("userId")
        or identity.get("kid") != user.get("kid")
        or crypto.b64(crypto.public_key(private)) != user.get("publicKey")
    ):
        raise client.SetlogError("ローカルの暗号鍵が現在の本人アカウントと一致しません。")
    room = next((room for room in collections() if room.get("id") == collection_id), None)
    if room is None:
        raise client.SetlogError("指定された投稿先が本人のコレクション一覧にありません。")
    metadata = probe_video(path)
    video = path.read_bytes()
    if not video:
        raise client.SetlogError("動画ファイルが空です。")
    identifier = str(uuid.uuid4())
    timestamp = int(time.time() * 1000)
    message, encrypted = make_message(
        room, user, private, video, metadata, identifier, timestamp, comment=comment
    )
    encrypted_name = f"video-{identifier}.enc"
    (client.STATE / encrypted_name).write_bytes(encrypted)
    job = {
        "phase": "prepared",
        "userId": user["userId"],
        "collection": room["id"],
        "collectionName": room.get("name"),
        "fileName": path.name,
        "metadata": metadata,
        "message": message,
        "encryptedFile": encrypted_name,
        "encryptedSha256": hashlib.sha256(encrypted).hexdigest(),
        "originalSha256": hashlib.sha256(video).hexdigest(),
        "recipientKeys": recipient_keys(room, user),
    }
    client.save_private(f"job-{identifier}.bin", job)
    preview = {
        "準備ID": identifier,
        "投稿先": room.get("name"),
        "受信者数": len(job["recipientKeys"]),
        "動画": path.name,
        "動画情報": metadata,
        "状態": "暗号化済み。まだアップロード・投稿していません。",
    }
    if "captions" in message["media"]:
        preview["コメント"] = comment
    return preview


def load_job(identifier: str) -> dict:
    try:
        uuid.UUID(identifier)
    except ValueError:
        raise client.SetlogError("準備IDの形式が正しくありません。") from None
    return client.load_private(f"job-{identifier}.bin")


def verify_job(identifier: str) -> dict:
    job = load_job(identifier)
    session = client.load_private("session.bin")
    if job["userId"] != session["user"]["userId"]:
        raise client.SetlogError("動画準備後にログインアカウントが変わっています。")
    message_id = job.get("serverMessageId") or job["message"]["messageId"]
    query = urllib.parse.urlencode({"conversationId": job["collection"]})
    result = client.api(
        "GET", f"chat/message/{urllib.parse.quote(message_id, safe='')}?{query}", token=session["token"]
    )
    message = result.get("message")
    if not isinstance(message, dict):
        raise client.SetlogError("投稿を読み戻せませんでした。送信済みかを確認してから再操作してください。")
    media = message.get("media") or {}
    if isinstance(media, list):
        media = media[0] if media else {}
    elif isinstance(media, dict) and "path" not in media:
        media = media.get("0") or next(iter(media.values()), {})
    if media.get("path") != job["message"]["media"]["path"]:
        raise client.SetlogError("読み戻した動画の保存先が送信した動画と一致しません。")
    returned_id = message.get("messageId") or message.get("id")
    if returned_id != message_id or message.get("conversationId") != job["collection"]:
        raise client.SetlogError("読み戻した投稿IDまたは投稿先が一致しません。")
    remote_version = max(
        [1, message.get("mediaCipherV") or 1] + [key.get("v") or 1 for key in message.get("keys", [])]
    )
    if (message.get("senderId") or message.get("userId")) != job["userId"] or remote_version != job[
        "message"
    ]["mediaCipherV"]:
        raise client.SetlogError("読み戻した投稿の送信者または暗号化形式が一致しません。")
    expected_captions = job["message"]["media"].get("captions")
    if expected_captions:
        returned_captions = media.get("captions")
        if (
            not isinstance(returned_captions, list)
            or any(not isinstance(caption, dict) for caption in returned_captions)
            or [caption.get("text") for caption in returned_captions]
            != [caption["text"] for caption in expected_captions]
        ):
            raise client.SetlogError(
                "読み戻した投稿のコメントが準備した本文と一致しません。再送せず確認してください。"
            )
    job["phase"] = "verified"
    client.save_private(f"job-{identifier}.bin", job)
    verified = {
        "準備ID": identifier,
        "投稿先": job["collectionName"],
        "投稿ID": message_id,
        "状態": "投稿を読み戻し、動画の保存先の一致を確認しました。アプリ内再生は別途確認が必要です。",
    }
    if expected_captions:
        verified["コメント"] = expected_captions[0]["text"]
    return verified


def send(identifier: str) -> dict:
    job = load_job(identifier)
    session = client.load_private("session.bin")
    if job["userId"] != session["user"]["userId"]:
        raise client.SetlogError("動画準備後にログインアカウントが変わっています。")
    if job["phase"] in ("publishing", "published", "verified"):
        raise client.SetlogError("投稿要求済みのため再送しません。verifyで投稿を確認してください。")
    if job["phase"] not in ("prepared", "uploaded"):
        raise client.SetlogError("送信準備の状態を確認できませんでした。")
    room = next((room for room in collections() if room.get("id") == job["collection"]), None)
    if room is None:
        raise client.SetlogError("準備時の投稿先を確認できませんでした。")
    current_user = client.api("GET", "user/profile", token=session["token"]).get("user", {})
    current_keys = recipient_keys(room, current_user)
    if current_keys != job["recipientKeys"]:
        raise client.SetlogError("参加者または暗号鍵が変わっています。動画を準備し直してください。")
    encrypted = (client.STATE / job["encryptedFile"]).read_bytes()
    if hashlib.sha256(encrypted).hexdigest() != job["encryptedSha256"]:
        raise client.SetlogError("準備済み動画の内容が変わっているため停止しました。")
    if job["phase"] == "prepared":
        region = job["message"]["media"]["region"]
        if region not in config.REGIONS:
            raise client.SetlogError("準備した動画の保存地域が未対応です。")
        base = "https://api-kr.newchatapi.com/" if region == "ap-northeast-2" else client.BASE
        result = client.api(
            "POST",
            "media/getUploadURL",
            {"contentType": "application/octet-stream", "region": region, "encrypted": True},
            token=session["token"],
            base=base,
        )
        if isinstance(result.get("body"), str):
            result = json.loads(result["body"])
        url = result.get("uploadUrl") or result.get("url")
        parsed = urllib.parse.urlsplit(url or "")
        if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".amazonaws.com"):
            raise client.SetlogError("取得したアップロード先の形式を確認できませんでした。")
        if not result.get("key") or not result.get("region"):
            raise client.SetlogError("アップロード先の保存キーまたは地域が不足しています。")
        request = urllib.request.Request(
            url, data=encrypted, method="PUT", headers={"Content-Type": "application/octet-stream"}
        )
        with urllib.request.build_opener(client._NoRedirect).open(request, timeout=180) as response:
            if not 200 <= response.status < 300:
                raise client.SetlogError("動画ファイルのアップロードに失敗しました。")
        job["message"]["media"].update({"path": result["key"], "region": result["region"]})
        job["phase"] = "uploaded"
        client.save_private(f"job-{identifier}.bin", job)
    # 投稿開始を先に保存し、応答が失われた場合にも二重投稿しない。
    job["phase"] = "publishing"
    client.save_private(f"job-{identifier}.bin", job)
    result = client.api("POST", f"chat/{job['collection']}/message", job["message"], token=session["token"])
    message_id = result.get("messageId")
    if not isinstance(message_id, str) or not message_id:
        raise client.SetlogError("投稿IDを応答から確認できませんでした。再送せずverifyで確認してください。")
    job.update({"phase": "published", "serverMessageId": message_id})
    client.save_private(f"job-{identifier}.bin", job)
    return verify_job(identifier)


def main() -> int:
    parser = argparse.ArgumentParser(description="setlogへのMP4動画の準備・送信・読み戻し")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("collections", help="投稿先の名前とIDを確認する")
    prepare_parser = commands.add_parser("prepare", help="動画を暗号化し、送信せずに準備する")
    prepare_parser.add_argument("video", type=Path)
    prepare_parser.add_argument("--collection", required=True)
    prepare_parser.add_argument(
        "--comment", default="", help="動画に添えるコメント。改行・絵文字も指定できる"
    )
    for name in ("send", "verify"):
        command = commands.add_parser(name)
        command.add_argument("job_id")
    args = parser.parse_args()
    try:
        if args.command == "collections":
            result = [
                {
                    "id": room.get("id"),
                    "名前": room.get("name"),
                    "本人以外の参加者数": len(room.get("participants", [])),
                }
                for room in collections()
            ]
        elif args.command == "prepare":
            with client.operation_lock():
                result = prepare(args.video, args.collection, args.comment)
        elif args.command == "send":
            with client.operation_lock():
                result = send(args.job_id)
        else:
            with client.operation_lock():
                result = verify_job(args.job_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (client.SetlogError, crypto.CryptoError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError):
        # 署名付きURLや秘密情報を含み得る例外の全体を標準出力へ出さない。
        print(
            "処理を完了できませんでした。投稿要求後の失敗では、自動再送せず読み戻しを確認してください。",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
