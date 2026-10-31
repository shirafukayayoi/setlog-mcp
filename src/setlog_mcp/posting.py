"""一括準備・順次送信と、暗号化して永続化する予約キュー。"""

from __future__ import annotations

import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from . import client, config, crypto, upload

MAX_BATCH = 50
ACTIVE = {"queued", "running"}


class VideoInput(BaseModel):
    """一括準備する動画1本と、その投稿先・コメント。"""

    model_config = ConfigDict(extra="forbid")
    video_path: str = Field(description="MP4動画の絶対パス")
    collection_id: str = Field(description="本人の投稿先ID")
    comment: str = Field(default="", description="動画に添える任意のコメント")


def _check_count(items: list) -> None:
    if not 1 <= len(items) <= MAX_BATCH:
        raise client.SetlogError("一度に指定できる件数は1〜50件です。")


def _safe_error(error: Exception) -> str:
    if isinstance(error, (client.SetlogError, crypto.CryptoError)):
        return str(error)
    return "処理を完了できませんでした。保存状態と通信を確認してください。自動再送はしていません。"


def _user_id() -> str:
    return client.load_private("session.bin")["user"]["userId"]


def _jobs(job_ids: list[str]) -> list[tuple[str, dict]]:
    _check_count(job_ids)
    try:
        normalized = [str(uuid.UUID(identifier)) for identifier in job_ids]
    except (ValueError, AttributeError):
        raise client.SetlogError("準備IDの形式が正しくありません。") from None
    if len(set(normalized)) != len(normalized):
        raise client.SetlogError("同じ準備IDを重複して指定できません。")
    owner = _user_id()
    jobs = [(identifier, upload.load_job(identifier)) for identifier in normalized]
    if any(job["userId"] != owner for _, job in jobs):
        raise client.SetlogError("準備した動画と現在のログインアカウントが一致しません。")
    return jobs


def prepare_video_batch(videos: list[VideoInput]) -> dict:
    _check_count(videos)
    paths, seen = [], set()
    # 入力に誤りがある場合、途中まで準備してから停止しないよう先に全件確認する。
    for video in videos:
        path = Path(video.video_path).expanduser()
        if not path.is_absolute():
            raise client.SetlogError("video_pathには動画の絶対パスを指定してください。")
        try:
            path = path.resolve(strict=True)
            valid = (
                path.is_file()
                and path.suffix.lower() == ".mp4"
                and 0 < path.stat().st_size <= config.max_video_bytes()
            )
        except OSError:
            raise client.SetlogError("指定された動画ファイルを読み取れませんでした。") from None
        if not valid:
            raise client.SetlogError(
                "空でない通常のMP4ファイルを、設定されたサイズ上限以内で指定してください。"
            )
        marker = (path, video.collection_id)
        if marker in seen:
            raise client.SetlogError("同じ動画と投稿先の組み合わせが重複しています。")
        seen.add(marker)
        paths.append(path)
    previews = []
    for index, (video, path) in enumerate(zip(videos, paths, strict=True)):
        try:
            previews.append(upload.prepare(path, video.collection_id, video.comment))
        except Exception as error:
            return {
                "status": "partial",
                "prepared": previews,
                "job_ids": [p["準備ID"] for p in previews],
                "failed_index": index,
                "error": _safe_error(error),
            }
    return {"status": "prepared", "prepared": previews, "job_ids": [p["準備ID"] for p in previews]}


def _load_queue() -> dict:
    if not client.private_path("queue.bin").exists():
        return {"version": 1, "items": []}
    queue = client.load_private("queue.bin")
    if (
        queue.get("version") != 1
        or not isinstance(queue.get("items"), list)
        or any(not isinstance(item, dict) for item in queue["items"])
    ):
        raise client.SetlogError("予約データの形式が不正です。自動送信せず停止しました。")
    return queue


def ensure_unscheduled(job_id: str) -> None:
    if any(item["job_id"] == job_id and item["status"] in ACTIVE for item in _load_queue()["items"]):
        raise client.SetlogError(
            "この動画には有効な予約があります。手動で送信する場合は、先に予約を取り消してください。"
        )


def _deliver(identifier: str) -> dict:
    job = upload.load_job(identifier)
    if job["userId"] != _user_id():
        raise client.SetlogError("準備した動画と現在のログインアカウントが一致しません。")
    if job["phase"] == "verified":
        return {
            "準備ID": identifier,
            "投稿ID": job.get("serverMessageId") or job["message"]["messageId"],
            "状態": "投稿の照合は完了済みです。再送していません。",
        }
    if job["phase"] in {"publishing", "published"}:
        # 応答が失われた投稿は読み戻す。見つからなくてもPOSTし直さない。
        return upload.verify_job(identifier)
    return upload.send(identifier)


def send_prepared_videos(job_ids: list[str]) -> dict:
    jobs = _jobs(job_ids)
    for identifier, _ in jobs:
        ensure_unscheduled(identifier)
    results = []
    for index, (identifier, _) in enumerate(jobs):
        try:
            results.append({"job_id": identifier, "result": _deliver(identifier)})
        except Exception as error:
            return {
                "status": "blocked",
                "results": results,
                "failed_job_id": identifier,
                "remaining_job_ids": [key for key, _ in jobs[index:]],
                "error": _safe_error(error),
            }
    return {"status": "completed", "results": results, "remaining_job_ids": []}


def send_one(job_id: str) -> dict:
    jobs = _jobs([job_id])
    ensure_unscheduled(jobs[0][0])
    return upload.send(jobs[0][0])


def _iso(stamp_ms: int) -> str:
    return datetime.fromtimestamp(stamp_ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def _scheduled_time(value: str) -> datetime:
    try:
        if not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})", value
        ):
            raise ValueError
        moment = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        if moment.timestamp() <= time.time():
            raise ValueError
        return moment
    except (ValueError, OverflowError, OSError):
        raise client.SetlogError(
            "publish_atには未来の日時を、2026-11-01T20:00:00+09:00のように時差付きで指定してください。"
        ) from None


def _task_summary(item: dict) -> dict:
    return {
        key: item[key]
        for key in ("schedule_id", "group_id", "job_id", "publish_at", "status", "error", "completed_at")
        if key in item
    }


def schedule_videos(job_ids: list[str], publish_at: str, interval_seconds: int = 0) -> dict:
    start = _scheduled_time(publish_at)
    if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int) or interval_seconds < 0:
        raise client.SetlogError("interval_secondsには0以上の秒数を指定してください。")
    jobs = _jobs(job_ids)
    queue = _load_queue()
    active = {item["job_id"] for item in queue["items"] if item["status"] in ACTIVE}
    if any(identifier in active or job["phase"] not in {"prepared", "uploaded"} for identifier, job in jobs):
        raise client.SetlogError(
            "予約済み・投稿要求済みの動画は追加できません。予約一覧と投稿状態を確認してください。"
        )
    group = str(uuid.uuid4())
    items = []
    try:
        for index, (identifier, job) in enumerate(jobs):
            moment = start + timedelta(seconds=index * interval_seconds)
            stamp = int(moment.timestamp() * 1000)
            items.append(
                {
                    "schedule_id": str(uuid.uuid4()),
                    "group_id": group,
                    "job_id": identifier,
                    "user_id": job["userId"],
                    "publish_at": _iso(stamp),
                    "due_ms": stamp,
                    "order": index,
                    "status": "queued",
                }
            )
    except (OverflowError, ValueError, OSError):
        raise client.SetlogError("予約日時と投稿間隔の組み合わせが扱える範囲を超えています。") from None
    queue["items"].extend(items)
    client.save_private("queue.bin", queue)
    return {
        "group_id": group,
        "scheduled": [_task_summary(item) for item in items],
        "worker_required": True,
        "状態": "予約を保存しました。同じWindowsユーザーでsetlog-workerを起動している間に実行します。",
    }


def list_scheduled_posts(include_finished: bool = False, limit: int = 100) -> dict:
    if not 1 <= limit <= 500:
        raise client.SetlogError("limitは1〜500を指定してください。")
    items = sorted(_load_queue()["items"], key=lambda item: (item["due_ms"], item["order"]))
    if not include_finished:
        items = [item for item in items if item["status"] not in {"completed", "cancelled"}]
    heartbeat = client.load_private("worker.bin") if client.private_path("worker.bin").exists() else None
    return {
        "scheduled": [_task_summary(item) for item in items[:limit]],
        "total": len(items),
        "truncated": len(items) > limit,
        "worker_last_seen": heartbeat.get("last_seen") if heartbeat else None,
    }


def cancel_scheduled_post(schedule_id: str) -> dict:
    queue = _load_queue()
    item = next((item for item in queue["items"] if item["schedule_id"] == schedule_id), None)
    if item is None:
        raise client.SetlogError("指定された予約が見つかりません。")
    if item["status"] == "completed":
        return {
            **_task_summary(item),
            "cancelled": False,
            "状態": "実行済みの予約です。投稿の削除は行いません。",
        }
    interrupted = item["status"] == "running"
    item["status"] = "cancelled"
    client.save_private("queue.bin", queue)
    result = {**_task_summary(item), "cancelled": True}
    if interrupted:
        result["状態"] = (
            "中断された予約の自動実行を取り消しました。投稿要求済みの場合があるためverify_videoで結果を確認してください。"
        )
    return result


def _heartbeat() -> None:
    client.save_private("worker.bin", {"last_seen": _iso(int(time.time() * 1000)), "pid": os.getpid()})


def run_once(now: float | None = None, max_jobs: int = MAX_BATCH) -> dict:
    """期限に達した予約を順に実行する。呼び出し側は操作ロックを保持しない。"""
    if not 1 <= max_jobs <= MAX_BATCH:
        raise client.SetlogError("max_jobsは1〜50を指定してください。")
    results = []
    busy = False
    while len(results) < max_jobs:
        try:
            with client.operation_lock():
                _heartbeat()
                queue = _load_queue()
                stamp = int((time.time() if now is None else now) * 1000)
                due = sorted(
                    (item for item in queue["items"] if item["status"] in ACTIVE and item["due_ms"] <= stamp),
                    key=lambda item: (item["due_ms"], item["order"]),
                )
                if not due:
                    break
                item = due[0]
                item["status"] = "running"
                client.save_private("queue.bin", queue)
                try:
                    if _user_id() != item["user_id"]:
                        raise client.SetlogError("予約時と現在のログインアカウントが異なるため停止しました。")
                    _deliver(item["job_id"])
                    item["status"] = "completed"
                    item["completed_at"] = _iso(int(time.time() * 1000))
                    item.pop("error", None)
                except Exception as error:
                    try:
                        phase = upload.load_job(item["job_id"])["phase"]
                    except Exception:
                        phase = "unknown"
                    item["status"] = (
                        "needs_review" if phase in {"publishing", "published", "unknown"} else "failed"
                    )
                    item["error"] = _safe_error(error)
                    for remaining in queue["items"]:
                        if remaining["group_id"] == item["group_id"] and remaining["status"] == "queued":
                            remaining.update(
                                status="blocked",
                                error="同じ一括予約内の先行投稿が停止したため、自動送信を止めました。",
                            )
                client.save_private("queue.bin", queue)
                _heartbeat()
                results.append(_task_summary(item))
        except client.StateBusyError:
            busy = True
            break
    return {"processed": len(results), "busy": busy, "results": results}
