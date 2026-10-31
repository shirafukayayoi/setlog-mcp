"""APK内のpb3・ge3で確認した暗号化形式の処理。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ALGORITHM = "X25519-HKDF-SHA256+CHACHA20-POLY1305"


class CryptoError(ValueError):
    """鍵や暗号文の内容を含めず表示する検証エラー。"""


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def unb64(value: str) -> bytes:
    if not isinstance(value, str):
        raise CryptoError("Base64の値が文字列ではありません。")
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, UnicodeError):
        raise CryptoError("Base64の形式を検証できませんでした。") from None


def public_key(private: bytes) -> bytes:
    if len(private) != 32:
        raise CryptoError("秘密鍵の長さが正しくありません。")
    return (
        X25519PrivateKey.from_private_bytes(private)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )


def key_id(public: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(public).digest()[:16]).decode("ascii").rstrip("=")


def generate_pair() -> tuple[bytes, bytes]:
    private = os.urandom(32)
    return private, public_key(private)


def derive(private: bytes, public: bytes, salt: bytes, info: bytes) -> bytes:
    try:
        shared = X25519PrivateKey.from_private_bytes(private).exchange(
            X25519PublicKey.from_public_bytes(public)
        )
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt or None, info=info).derive(shared)
    except ValueError:
        raise CryptoError("X25519の鍵交換を検証できませんでした。") from None


def encrypt(key: bytes, data: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(12)
    return nonce + ChaCha20Poly1305(key).encrypt(nonce, data, aad)


def decrypt(key: bytes, combined: bytes, aad: bytes) -> bytes:
    if len(combined) < 28:
        raise CryptoError("暗号文が短すぎます。")
    try:
        return ChaCha20Poly1305(key).decrypt(combined[:12], combined[12:], aad)
    except (InvalidTag, ValueError):
        raise CryptoError("暗号文と付随データの整合性を確認できませんでした。") from None


def message_aad(conversation: str, message: str, sender: str, content_type: str, version: int) -> bytes:
    return (
        f"msg:enc:v{version}|conv:{conversation}|msg:{message}|sender:{sender}|type:{content_type}"
    ).encode("utf-8")


def media_aad(conversation: str, message: str, sender: str, content_type: str, version: int = 2) -> bytes:
    if version >= 2:
        return message_aad("media-v2", "shared-media", sender, content_type, version)
    return message_aad(conversation, message, sender, content_type, 1)


def wrap_content_key(
    identity: bytes,
    content_key: bytes,
    conversation: str,
    sender: str,
    recipients: list[dict],
    version: int = 2,
) -> list[dict]:
    if len(content_key) != 32:
        raise CryptoError("動画の暗号化キーの長さが正しくありません。")
    public = public_key(identity)
    aad = message_aad(conversation, "media-key", sender, "application/octet-stream", 1)
    wrapped = []
    for recipient in recipients:
        uid = recipient.get("userId")
        peer = unb64(recipient.get("publicKey"))
        if not uid or len(peer) != 32:
            raise CryptoError("受信者の公開鍵情報が不足しています。")
        kid = recipient.get("kid") or key_id(peer)
        if kid != key_id(peer):
            raise CryptoError("受信者の公開鍵と鍵IDが一致しません。")
        salt = os.urandom(32)
        wrapping_key = derive(identity, peer, salt, aad)
        wrapped.append(
            {
                "uid": uid,
                "kid": kid,
                "v": version,
                "alg": ALGORITHM,
                "saltB64": b64(salt),
                "wrappedB64": b64(encrypt(wrapping_key, content_key, aad)),
                "senderKid": key_id(public),
                "senderPublicKey": b64(public),
            }
        )
    if not wrapped:
        raise CryptoError("動画を復号できる受信者がありません。")
    return wrapped


def import_transfer(
    envelope: dict, transfer: dict, ephemeral_private: bytes, user: dict, device: dict
) -> dict:
    if envelope.get("v") != 1 or envelope.get("alg") != ALGORITHM:
        raise CryptoError("端末引き継ぎの暗号化形式が未対応です。")
    kid = user.get("kid")
    public_b64 = user.get("publicKey")
    old_device_id = (transfer.get("oldDevice") or {}).get("deviceId")
    if not all((user.get("userId"), kid, public_b64, transfer.get("sessionId"), device.get("deviceId"))):
        raise CryptoError("本人・端末・引き継ぎセッションの情報が不足しています。")
    aad = unb64(envelope.get("aadB64") or envelope.get("aad"))
    prefix = f"key-transfer:v1|transfer:{transfer['sessionId']}|user:{user['userId']}|old:".encode("utf-8")
    suffix = f"|new:{device['deviceId']}|kid:{kid}".encode("utf-8")
    binding = re.fullmatch(re.escape(prefix) + rb"([^|]+)" + re.escape(suffix), aad)
    # 実サービスは旧端末の名前だけを返す場合がある。識別子が返る場合はAADとも照合する。
    if binding is None or (old_device_id and binding.group(1) != old_device_id.encode("utf-8")):
        raise CryptoError("鍵の引き継ぎ先またはアカウントが一致しません。")
    if envelope.get("kid") != kid or envelope.get("publicKeyB64") != public_b64:
        raise CryptoError("引き継ぎ対象の鍵がアカウントに登録された鍵と一致しません。")
    key = derive(
        ephemeral_private,
        unb64(envelope.get("senderEphemeralPubB64")),
        unb64(envelope.get("saltB64") or envelope.get("salt")),
        aad,
    )
    plaintext = decrypt(key, unb64(envelope.get("ctB64") or envelope.get("ct")), aad)
    try:
        imported = json.loads(plaintext)
        private = unb64(imported["identityPrivateKeyB64"])
    except (ValueError, KeyError, TypeError):
        raise CryptoError("引き継いだ鍵のデータ形式を確認できませんでした。") from None
    public = public_key(private)
    if (
        imported.get("v") != 1
        or key_id(public) != kid
        or b64(public) != public_b64
        or imported.get("kid") != kid
        or imported.get("publicKeyB64") != public_b64
    ):
        raise CryptoError("引き継いだ秘密鍵の正当性を確認できませんでした。")
    return {
        "userId": user["userId"],
        "kid": kid,
        "publicKey": public_b64,
        "identityPrivateKeyB64": b64(private),
    }
