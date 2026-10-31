"""引き継ぎの再確認で秘密情報や完了処理を扱い直さないことを確認する。"""

import pytest

from setlog_mcp import client, crypto


@pytest.mark.parametrize("changed_identity", [False, True])
def test_completed_transfer_is_checked_without_claim_or_completion(changed_identity, monkeypatch):
    private, public = crypto.generate_pair()
    user = {"userId": "self", "kid": crypto.key_id(public), "publicKey": crypto.b64(public)}
    identity = {"userId": "self", "kid": user["kid"], "identityPrivateKeyB64": crypto.b64(private)}
    records = {
        "transfer.bin": {"sessionId": "session", "userId": "self", "finished": True},
        "session.bin": {"token": "synthetic-token", "user": user},
        "identity.bin": identity,
    }
    if changed_identity:
        private2, public2 = crypto.generate_pair()
        identity["identityPrivateKeyB64"] = crypto.b64(private2)
    calls = []

    def api(method, path, **kwargs):
        calls.append((method, path))
        assert method == "GET" and path == "user/profile"
        return {"user": user}

    monkeypatch.setattr(client, "load_private", lambda name: records[name])
    monkeypatch.setattr(client, "api", api)
    if changed_identity:
        with pytest.raises(client.SetlogError, match="暗号鍵"):
            client.check_transfer()
    else:
        assert client.check_transfer()["鍵の引き継ぎ完了"] is True
    assert calls == [("GET", "user/profile")]
