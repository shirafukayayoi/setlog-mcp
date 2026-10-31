"""保存先の制限、暗号化された保管と同時書き込みの制御を検証する。"""

import io
import json
import sys
import urllib.error

import pytest

from setlog_mcp import client


def test_private_record_roundtrip_and_no_temporary_file():
    record = {"token": "synthetic-local-value"}
    client.save_private("record.bin", record)
    assert client.load_private("record.bin") == record
    assert not list(client.STATE.glob("*.tmp"))
    if sys.platform == "win32":
        assert record["token"].encode() not in client.private_path("record.bin").read_bytes()


@pytest.mark.parametrize(
    "name", ["../record.bin", "sub/record.bin", r"sub\record.bin", "/record.bin", "record."]
)
def test_private_record_rejects_path_traversal(name):
    with pytest.raises(client.SetlogError):
        client.private_path(name)


def test_operation_lock_rejects_concurrent_writer_and_releases():
    with client.operation_lock():
        with pytest.raises(client.SetlogError, match="書き込み処理"):
            with client.operation_lock():
                pytest.fail("同時の書き込みを許可しています")
    with client.operation_lock():
        pass


def test_http_error_hides_credentials(monkeypatch):
    application_key, token, code, email = "synthetic-app-key", "synthetic-token", "123456", "you@example.com"
    body = json.dumps({"message": " ".join([application_key, token, code, email])}).encode()

    class Opener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, 401, "unauthorized", {}, io.BytesIO(body))

    monkeypatch.setattr(client, "app_key", lambda: application_key)
    monkeypatch.setattr(client.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(client.SetlogError) as error:
        client.api("POST", "auth/email/login/verify", {"email": email, "code": code}, token)
    assert "HTTP 401" in str(error.value)
    assert all(value not in str(error.value) for value in (application_key, token, code, email))


def test_pending_transfer_waits_for_payload(monkeypatch):
    records = {
        "session.bin": {"token": "synthetic-token", "user": {"userId": "self"}},
        "transfer.bin": {"userId": "self", "sessionId": "session"},
    }
    calls = []

    def api(method, path, **kwargs):
        calls.append((method, path))
        return {"state": "claimed", "pollAfterMs": 1000}

    monkeypatch.setattr(client, "load_private", lambda name: records[name])
    monkeypatch.setattr(client, "api", api)
    result = client.check_transfer()
    assert result["鍵の引き継ぎ完了"] is False
    assert result["状態"] == "claimed"
    assert calls == [("GET", "key-transfer/sessions/session")]
