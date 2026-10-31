"""暗号化の互換形式と、アカウントや暗号文の取り違えを検証する。"""

import hashlib
import hmac
import json

import pytest

from setlog_mcp import crypto as c


def test_x25519_rfc7748_and_hkdf():
    private = bytes.fromhex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
    public = bytes.fromhex("8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a")
    peer = bytes.fromhex("de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f")
    shared = bytes.fromhex("4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742")
    assert c.public_key(private) == public
    salt, info = b"setlog-test-salt", b"setlog-test-info"
    extracted = hmac.digest(salt, shared, hashlib.sha256)
    expected = hmac.digest(extracted, info + b"\x01", hashlib.sha256)
    assert c.derive(private, peer, salt, info) == expected


def test_media_key_can_be_opened_by_recipient():
    sender, sender_public = c.generate_pair()
    recipient, recipient_public = c.generate_pair()
    content_key = bytes(range(32))
    wrapped = c.wrap_content_key(
        sender,
        content_key,
        "room",
        "sender",
        [{"userId": "recipient", "publicKey": c.b64(recipient_public), "kid": c.key_id(recipient_public)}],
    )[0]
    aad = c.message_aad("room", "media-key", "sender", "application/octet-stream", 1)
    key = c.derive(recipient, sender_public, c.unb64(wrapped["saltB64"]), aad)
    assert c.decrypt(key, c.unb64(wrapped["wrappedB64"]), aad) == content_key
    encrypted = c.encrypt(content_key, b"test-video", c.media_aad("room", "msg", "sender", "video/mp4"))
    assert (
        c.decrypt(content_key, encrypted, c.media_aad("room", "msg", "sender", "video/mp4")) == b"test-video"
    )
    with pytest.raises(c.CryptoError):
        c.decrypt(content_key, encrypted, c.media_aad("room", "msg", "other-sender", "video/mp4"))


def make_transfer():
    identity, public = c.generate_pair()
    recipient, recipient_public = c.generate_pair()
    sender, sender_public = c.generate_pair()
    kid = c.key_id(public)
    user = {"userId": "user", "kid": kid, "publicKey": c.b64(public)}
    transfer = {"sessionId": "session", "oldDevice": {"deviceId": "old"}}
    device = {"deviceId": "new"}
    aad = f"key-transfer:v1|transfer:session|user:user|old:old|new:new|kid:{kid}".encode()
    salt = bytes(range(32))
    key = c.derive(sender, recipient_public, salt, aad)
    content = {"v": 1, "identityPrivateKeyB64": c.b64(identity), "publicKeyB64": c.b64(public), "kid": kid}
    envelope = {
        "v": 1,
        "alg": c.ALGORITHM,
        "senderEphemeralPubB64": c.b64(sender_public),
        "saltB64": c.b64(salt),
        "aadB64": c.b64(aad),
        "ctB64": c.b64(c.encrypt(key, json.dumps(content).encode(), aad)),
        "kid": kid,
        "publicKeyB64": c.b64(public),
    }
    return envelope, transfer, recipient, user, device, identity


def test_import_transfer_preserves_registered_identity():
    envelope, transfer, private, user, device, identity = make_transfer()
    imported = c.import_transfer(envelope, transfer, private, user, device)
    assert c.unb64(imported["identityPrivateKeyB64"]) == identity
    assert imported["kid"] == user["kid"]


def test_import_transfer_when_old_device_identifier_is_not_returned():
    envelope, transfer, private, user, device, identity = make_transfer()
    transfer["oldDevice"] = {"platform": "ios", "name": "本人のスマホ"}
    imported = c.import_transfer(envelope, transfer, private, user, device)
    assert c.unb64(imported["identityPrivateKeyB64"]) == identity


def test_import_transfer_rejects_explicit_old_device_mismatch():
    envelope, transfer, private, user, device, identity = make_transfer()
    transfer["oldDevice"]["deviceId"] = "other-old-device"
    with pytest.raises(c.CryptoError):
        c.import_transfer(envelope, transfer, private, user, device)


@pytest.mark.parametrize("change", ["user", "device", "session", "ciphertext"])
def test_import_transfer_rejects_mismatch(change):
    envelope, transfer, private, user, device, identity = make_transfer()
    if change == "user":
        user["userId"] = "other-user"
    elif change == "device":
        device["deviceId"] = "other-device"
    elif change == "session":
        transfer["sessionId"] = "other-session"
    else:
        raw = bytearray(c.unb64(envelope["ctB64"]))
        raw[-1] ^= 1
        envelope["ctB64"] = c.b64(raw)
    with pytest.raises(c.CryptoError):
        c.import_transfer(envelope, transfer, private, user, device)
