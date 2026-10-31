"""永続化した予約と一括送信の順序・中断・再開を合成データで検証する。"""

import json
import os
import subprocess
import sys
import uuid

import pytest

from setlog_mcp import client, posting, upload

NOW = 1893456000
LATER = "2030-01-01T09:01:00+09:00"


@pytest.fixture
def jobs(monkeypatch):
    monkeypatch.setattr(posting.time, "time", lambda: NOW)
    client.save_private("session.bin", {"token": "synthetic-test-token", "user": {"userId": "self"}})
    identifiers = [str(uuid.uuid4()) for _ in range(3)]
    for identifier in identifiers:
        client.save_private(
            f"job-{identifier}.bin",
            {"userId": "self", "phase": "prepared", "message": {"messageId": identifier, "media": {}}},
        )
    calls = []

    def send(identifier):
        calls.append(identifier)
        job = upload.load_job(identifier)
        job["phase"] = "verified"
        client.save_private(f"job-{identifier}.bin", job)
        return {"投稿ID": identifier}

    monkeypatch.setattr(upload, "send", send)
    return identifiers, calls


def test_batch_is_ordered_and_repeated_call_does_not_repost(jobs):
    identifiers, calls = jobs
    assert posting.send_prepared_videos(identifiers)["status"] == "completed"
    assert posting.send_prepared_videos(identifiers)["status"] == "completed"
    assert calls == identifiers


def test_batch_stops_on_unknown_result_and_recovers_by_readback(jobs, monkeypatch):
    identifiers, calls = jobs
    original = upload.send

    def interrupted(identifier):
        if identifier == identifiers[1]:
            calls.append(identifier)
            job = upload.load_job(identifier)
            job["phase"] = "publishing"
            client.save_private(f"job-{identifier}.bin", job)
            raise OSError("private-signed-url-must-not-appear")
        return original(identifier)

    monkeypatch.setattr(upload, "send", interrupted)
    first = posting.send_prepared_videos(identifiers)
    assert first["status"] == "blocked"
    assert first["remaining_job_ids"] == identifiers[1:]
    assert calls == identifiers[:2]
    assert "private-signed-url" not in json.dumps(first)
    verified = []

    def readback(identifier):
        verified.append(identifier)
        job = upload.load_job(identifier)
        job["phase"] = "verified"
        client.save_private(f"job-{identifier}.bin", job)
        return {"投稿ID": identifier}

    monkeypatch.setattr(upload, "verify_job", readback)
    assert posting.send_prepared_videos(identifiers)["status"] == "completed"
    assert calls == identifiers
    assert verified == [identifiers[1]]


def test_schedule_persists_offsets_spacing_and_does_not_send_early(jobs):
    identifiers, calls = jobs
    scheduled = posting.schedule_videos(identifiers[:2], LATER, 60)
    assert scheduled["scheduled"][0]["publish_at"] == "2030-01-01T00:01:00Z"
    assert scheduled["scheduled"][1]["publish_at"] == "2030-01-01T00:02:00Z"
    assert posting.run_once(now=NOW + 59)["processed"] == 0
    assert calls == []
    assert posting.run_once(now=NOW + 60)["processed"] == 1
    assert calls == identifiers[:1]
    assert posting.run_once(now=NOW + 120)["processed"] == 1
    assert posting.run_once(now=NOW + 120)["processed"] == 0
    assert calls == identifiers[:2]
    assert posting.list_scheduled_posts()["total"] == 0
    assert posting.list_scheduled_posts(True)["total"] == 2


def test_cancellation_and_reserved_manual_send_guard(jobs):
    identifiers, calls = jobs
    scheduled = posting.schedule_videos(identifiers[:1], LATER)
    with pytest.raises(client.SetlogError, match="予約"):
        posting.send_prepared_videos(identifiers)
    with pytest.raises(client.SetlogError, match="予約"):
        posting.send_one(identifiers[0])
    reservation = scheduled["scheduled"][0]["schedule_id"]
    assert posting.cancel_scheduled_post(reservation)["cancelled"] is True
    assert posting.cancel_scheduled_post(reservation)["cancelled"] is True
    assert posting.run_once(now=NOW + 120)["processed"] == 0
    assert calls == []


def test_failure_blocks_same_group_but_other_group_can_proceed(jobs, monkeypatch):
    identifiers, calls = jobs
    posting.schedule_videos(identifiers[:2], LATER)
    posting.schedule_videos(identifiers[2:], LATER)
    original = upload.send

    def send(identifier):
        if identifier == identifiers[0]:
            raise client.SetlogError("合成データの通信失敗")
        return original(identifier)

    monkeypatch.setattr(upload, "send", send)
    posting.run_once(now=NOW + 120)
    statuses = {item["job_id"]: item["status"] for item in posting.list_scheduled_posts(True)["scheduled"]}
    assert statuses == {identifiers[0]: "failed", identifiers[1]: "blocked", identifiers[2]: "completed"}
    assert calls == identifiers[2:]


def test_interrupted_worker_reads_back_without_resending(jobs, monkeypatch):
    identifiers, calls = jobs
    posting.schedule_videos(identifiers[:2], LATER)
    queue = client.load_private("queue.bin")
    queue["items"][0]["status"] = "running"
    client.save_private("queue.bin", queue)
    job = upload.load_job(identifiers[0])
    job["phase"] = "publishing"
    client.save_private(f"job-{identifiers[0]}.bin", job)
    monkeypatch.setattr(
        upload, "verify_job", lambda _: (_ for _ in ()).throw(client.SetlogError("投稿結果が不明"))
    )
    result = posting.run_once(now=NOW + 120)
    assert result["results"][0]["status"] == "needs_review"
    assert calls == []
    assert posting.list_scheduled_posts()["scheduled"][1]["status"] == "blocked"


def test_changed_account_and_lock_contention_do_not_post(jobs):
    identifiers, calls = jobs
    posting.schedule_videos(identifiers[:1], LATER)
    with client.operation_lock():
        assert posting.run_once(now=NOW + 120)["busy"] is True
    assert calls == []
    client.save_private("session.bin", {"user": {"userId": "other"}})
    assert posting.run_once(now=NOW + 120)["results"][0]["status"] == "failed"
    assert calls == []


@pytest.mark.parametrize("when", ["2030-01-01T00:01:00", "2020-01-01T00:00:00Z", "not-a-date"])
def test_invalid_schedule_is_atomic(jobs, when):
    identifiers, _ = jobs
    with pytest.raises(client.SetlogError):
        posting.schedule_videos(identifiers, when)
    assert posting.list_scheduled_posts(True)["total"] == 0


def test_duplicate_schedule_and_duplicate_batch_ids_are_rejected(jobs):
    identifiers, calls = jobs
    posting.schedule_videos(identifiers[:1], LATER)
    with pytest.raises(client.SetlogError):
        posting.schedule_videos(identifiers, LATER)
    with pytest.raises(client.SetlogError):
        posting.send_prepared_videos([identifiers[0], identifiers[0]])
    assert posting.list_scheduled_posts()["total"] == 1
    assert calls == []


def test_prepare_batch_reports_partial_work_and_stops(tmp_path, monkeypatch):
    inputs = []
    for index in range(3):
        path = tmp_path / f"clip-{index}.mp4"
        path.write_bytes(b"synthetic-mp4")
        inputs.append(posting.VideoInput(video_path=str(path), collection_id="room", comment=f"本文{index}"))
    calls = []

    def prepare(path, collection_id, comment):
        calls.append(comment)
        if len(calls) == 2:
            raise client.SetlogError("合成データの準備失敗")
        return {"準備ID": "synthetic-job", "コメント": comment}

    monkeypatch.setattr(upload, "prepare", prepare)
    result = posting.prepare_video_batch(inputs)
    assert result["status"] == "partial"
    assert result["job_ids"] == ["synthetic-job"]
    assert result["failed_index"] == 1
    assert calls == ["本文0", "本文1"]


def test_worker_cli_once_with_isolated_empty_state(tmp_path):
    environment = dict(os.environ)
    environment["SETLOG_MCP_STATE_DIR"] = str(tmp_path / "worker-state")
    if sys.platform == "win32":
        command = [sys.executable, "-m", "setlog_mcp.worker", "--once"]
    else:
        # 子プロセスにも合成データ専用の保管処理を設定する。
        command = [
            sys.executable,
            "-c",
            "import sys;from setlog_mcp import client;client._protect=lambda raw,decrypt=False:raw;from setlog_mcp.worker import main;sys.argv=['worker','--once'];raise SystemExit(main())",
        ]
    process = subprocess.run(
        command, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=20
    )
    assert process.returncode == 0
    assert json.loads(process.stdout)["processed"] == 0


def test_worker_process_restart_uses_persisted_queue_without_duplicate_delivery(jobs):
    identifiers, _ = jobs
    posting.schedule_videos(identifiers[:1], LATER)
    environment = dict(os.environ)
    environment["SETLOG_MCP_STATE_DIR"] = str(client.STATE)
    program = f"""
import sys
from setlog_mcp import client, posting, upload
if sys.platform != 'win32':
    client._protect = lambda raw, decrypt=False: raw
posting.time.time = lambda: {NOW + 120}
def send(identifier):
    job = upload.load_job(identifier)
    job['phase'] = 'verified'
    job['delivery_count'] = job.get('delivery_count', 0) + 1
    client.save_private(f'job-{{identifier}}.bin', job)
    return {{'投稿ID': identifier}}
upload.send = send
from setlog_mcp.worker import main
sys.argv = ['worker', '--once']
raise SystemExit(main())
"""
    for expected in (1, 0):
        process = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", program],
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
        )
        assert process.returncode == 0
        assert json.loads(process.stdout)["processed"] == expected
    assert upload.load_job(identifiers[0])["delivery_count"] == 1
