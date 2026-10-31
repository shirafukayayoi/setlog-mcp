"""外部送信を行わず、暗号化データの受け渡しと二重投稿防止を確認する。"""

import hashlib
import json
import uuid

import pytest

from setlog_mcp import client, crypto, upload


@pytest.fixture
def prepared(tmp_path, monkeypatch, request):
    monkeypatch.setattr(client, "STATE", tmp_path)
    private, public = crypto.generate_pair()
    user = {"userId": "self", "publicKey": crypto.b64(public), "kid": crypto.key_id(public)}
    room = {"id": "room", "name": "本人のみ", "participants": []}
    metadata = {"mime": "video/mp4", "width": 320, "height": 180, "duration": 2}
    identifier = str(uuid.uuid4())
    message, encrypted = upload.make_message(
        room,
        user,
        private,
        b"sample-mp4",
        metadata,
        identifier,
        123456,
        comment=getattr(request, "param", ""),
    )
    (tmp_path / "media.enc").write_bytes(encrypted)
    client.save_private("session.bin", {"token": "synthetic-test-token", "user": user})
    client.save_private(
        f"job-{identifier}.bin",
        {
            "phase": "prepared",
            "userId": "self",
            "collection": "room",
            "collectionName": "本人のみ",
            "message": message,
            "encryptedFile": "media.enc",
            "encryptedSha256": hashlib.sha256(encrypted).hexdigest(),
            "recipientKeys": upload.recipient_keys(room, user),
        },
    )
    monkeypatch.setattr(upload, "collections", lambda: [room])
    return identifier, message, encrypted, room, user, private


def test_sender_can_decrypt_own_video(prepared):
    identifier, message, encrypted, room, user, private = prepared
    wrapped = message["keys"][0]
    aad = crypto.message_aad("room", "media-key", "self", "application/octet-stream", 1)
    wrapping_key = crypto.derive(
        private, crypto.unb64(wrapped["senderPublicKey"]), crypto.unb64(wrapped["saltB64"]), aad
    )
    content_key = crypto.decrypt(wrapping_key, crypto.unb64(wrapped["wrappedB64"]), aad)
    assert (
        crypto.decrypt(content_key, encrypted, crypto.media_aad("room", identifier, "self", "video/mp4"))
        == b"sample-mp4"
    )


@pytest.mark.parametrize("prepared", ["", "日本語のコメント\n2行目😀"], indirect=True)
def test_upload_and_readback_without_leaking_authentication(prepared, monkeypatch):
    identifier, message, encrypted, room, user, private = prepared
    calls = []
    put_requests = []

    def api(method, path, body=None, token=None, **kwargs):
        calls.append((method, path, body))
        if path == "user/profile":
            return {"user": user}
        if path == "media/getUploadURL":
            assert body["encrypted"] is True
            assert body["contentType"] == "application/octet-stream"
            return {
                "body": json.dumps(
                    {
                        "uploadUrl": "https://test.s3.ap-northeast-2.amazonaws.com/item?signature=test",
                        "key": "stored-key",
                        "region": "ap-northeast-2",
                    }
                )
            }
        if method == "POST":
            assert body["media"]["path"] == "stored-key"
            assert body["messageId"] == identifier
            return {"messageId": identifier}
        # APKが受け付けている、mediaが番号付きオブジェクトの応答形式も検証する。
        return {
            "message": {
                "id": identifier,
                "conversationId": "room",
                "userId": "self",
                "keys": message["keys"],
                "media": {"0": {"path": "stored-key", "captions": message["media"].get("captions", [])}},
            }
        }

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class Opener:
        def open(self, request, timeout):
            put_requests.append(request)
            assert request.get_method() == "PUT"
            assert request.data == encrypted
            assert not any(
                key.lower() in ("jwt-key", "authorization", "x-api-key") for key in request.headers
            )
            return Response()

    monkeypatch.setattr(client, "api", api)
    monkeypatch.setattr(upload.urllib.request, "build_opener", lambda *args: Opener())
    result = upload.send(identifier)
    assert result["投稿ID"] == identifier
    if "captions" in message["media"]:
        assert result["コメント"] == "日本語のコメント\n2行目😀"
        posted = next(
            body for method, path, body in calls if method == "POST" and path == "chat/room/message"
        )
        assert posted["media"]["captions"] == [
            {"text": "日本語のコメント\n2行目😀", "time": "", "translation": "{0, 0}", "style": 0}
        ]
    else:
        assert "コメント" not in result
    assert len(put_requests) == 1
    assert client.load_private(f"job-{identifier}.bin")["phase"] == "verified"
    with pytest.raises(client.SetlogError):
        upload.send(identifier)
    assert len([call for call in calls if call[0] == "POST" and call[1] == "chat/room/message"]) == 1


def test_changed_recipients_stop_before_upload(prepared, monkeypatch):
    identifier, message, encrypted, room, user, private = prepared
    changed = dict(user)
    other_private, other_public = crypto.generate_pair()
    changed.update({"kid": crypto.key_id(other_public), "publicKey": crypto.b64(other_public)})
    monkeypatch.setattr(client, "api", lambda *args, **kwargs: {"user": changed})
    with pytest.raises(client.SetlogError, match="暗号鍵が変わって"):
        upload.send(identifier)
    assert client.load_private(f"job-{identifier}.bin")["phase"] == "prepared"


@pytest.mark.parametrize("comment", [" 今日の記録\n楽しかった😀 ", "", " \n\t"])
def test_prepare_saves_comment_and_returns_preview(prepared, tmp_path, monkeypatch, comment):
    _, _, _, room, user, private = prepared
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"sample-mp4")
    client.save_private(
        "identity.bin",
        {"userId": user["userId"], "kid": user["kid"], "identityPrivateKeyB64": crypto.b64(private)},
    )
    monkeypatch.setattr(client, "api", lambda *args, **kwargs: {"user": user})
    monkeypatch.setattr(
        upload, "probe_video", lambda _: {"mime": "video/mp4", "width": 320, "height": 180, "duration": 2}
    )
    preview = upload.prepare(path, room["id"], comment)
    job = upload.load_job(preview["準備ID"])
    assert job["phase"] == "prepared"
    if comment.strip():
        assert preview["コメント"] == comment
        assert job["message"]["media"]["captions"][0]["text"] == comment
    else:
        assert "コメント" not in preview
        assert "captions" not in job["message"]["media"]


@pytest.mark.parametrize("prepared", ["確認対象のコメント"], indirect=True)
@pytest.mark.parametrize("returned_captions", [None, [{"text": "別の本文"}], [None]])
def test_missing_or_changed_comment_is_not_marked_verified(prepared, monkeypatch, returned_captions):
    identifier, message, _, _, _, _ = prepared
    job = upload.load_job(identifier)
    job["phase"] = "published"
    job["message"]["media"]["path"] = "stored-key"
    client.save_private(f"job-{identifier}.bin", job)
    monkeypatch.setattr(
        client,
        "api",
        lambda *args, **kwargs: {
            "message": {
                "id": identifier,
                "conversationId": "room",
                "userId": "self",
                "keys": message["keys"],
                "media": [{"path": "stored-key", "captions": returned_captions}],
            }
        },
    )
    with pytest.raises(client.SetlogError, match="コメント"):
        upload.verify_job(identifier)
    assert upload.load_job(identifier)["phase"] == "published"
