"""提供されたsetlog APKから確認した通信形式を使うローカルクライアント。"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path

from . import __version__, config

STATE = config.state_directory()
BASE = "https://api.newchatapi.com/"
_WRITE_LOCK = threading.Lock()


class SetlogError(Exception):
    """機密情報を含めず利用者へ表示できるエラー。"""


class StateBusyError(SetlogError):
    """同じ保存先への別の書き込みが進行している。"""


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _protect(raw: bytes, decrypt: bool = False) -> bytes:
    """現在のWindowsユーザーのDPAPIでセッション情報を保護する。"""
    if sys.platform != "win32":
        raise SetlogError("セッションの保管にはWindowsのDPAPIが必要です。")
    source_buffer = ctypes.create_string_buffer(raw)
    source = _Blob(len(raw), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = _Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    if decrypt:
        operation = crypt.CryptUnprotectData
        operation.restype = wintypes.BOOL
        operation.argtypes = [
            ctypes.POINTER(_Blob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_Blob),
        ]
        success = operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        operation = crypt.CryptProtectData
        operation.restype = wintypes.BOOL
        operation.argtypes = [
            ctypes.POINTER(_Blob),
            wintypes.LPCWSTR,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_Blob),
        ]
        success = operation(
            ctypes.byref(source), "setlog ローカルセッション", None, None, None, 1, ctypes.byref(target)
        )
    if not success:
        raise SetlogError("Windowsによるセッション情報の保護・復号に失敗しました。")
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def private_path(name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name) or ".." in name or name.endswith("."):
        raise SetlogError("保存データの名前が不正です。")
    return STATE / name


@contextmanager
def operation_lock():
    """複数のMCPクライアントから同じ状態へ同時に書き込まない。"""
    if not _WRITE_LOCK.acquire(blocking=False):
        raise StateBusyError("別の書き込み処理が実行中です。完了後に状態を確認してください。")
    stream = None
    locked = False
    try:
        STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
        stream = (STATE / ".operation.lock").open("a+b")
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            raise StateBusyError(
                "同じ保存先を別のプロセスが使用中です。状態を確認してから再操作してください。"
            ) from None
        yield
    finally:
        try:
            if stream is not None:
                try:
                    if locked:
                        if sys.platform == "win32":
                            import msvcrt

                            stream.seek(0)
                            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl

                            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                finally:
                    stream.close()
        finally:
            _WRITE_LOCK.release()


def save_private(name: str, value: dict) -> None:
    destination = private_path(name)
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    encrypted = _protect(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    descriptor, temporary_name = tempfile.mkstemp(prefix="." + name, suffix=".tmp", dir=STATE)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def load_private(name: str) -> dict:
    path = private_path(name)
    if not path.exists():
        raise SetlogError("必要な保存データがありません。statusで認証と鍵の準備状況を確認してください。")
    return json.loads(_protect(path.read_bytes(), decrypt=True))


def app_key() -> str:
    value = os.environ.get("SETLOG_API_KEY")
    if value:
        return value
    if private_path("config.bin").exists():
        value = load_private("config.bin").get("app_api_key")
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise SetlogError(
        "アプリ用APIキーが未設定です。SETLOG_API_KEYまたはsetlog-mcp configureで設定してください。"
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        raise SetlogError("APIが転送を返したため、認証情報を送らず停止しました。")


def api(
    method: str, path: str, body: dict | None = None, token: str | None = None, *, base: str = BASE
) -> dict:
    if base not in (BASE, "https://api-kr.newchatapi.com/"):
        raise SetlogError("アプリのAPI以外には認証情報を送信できません。")
    if path.startswith(("/", "http:", "https:")) or ".." in path:
        raise SetlogError("APIのパスが不正です。")
    application_key = app_key()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-api-key": application_key,
        "User-Agent": "setlog-mcp/" + __version__,
    }
    if token:
        headers["jwt-key"] = token
    raw = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(base + path, data=raw, headers=headers, method=method)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read(8192))
        except (ValueError, UnicodeError):
            error = {}
        # 応答全体やトークンを例外文へ含めない。
        message = str(error.get("message") or error.get("error") or "API呼び出しに失敗しました。")
        secrets = [application_key, token]
        if body:
            secrets.extend(body.get(field) for field in ("code", "email", "token", "password"))
        for secret in secrets:
            if isinstance(secret, str) and secret:
                message = message.replace(secret, "[非表示]")
        raise SetlogError(f"HTTP {exc.code}: {message[:400]}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise SetlogError("通信を確認できませんでした。自動再送はしていません。") from None
    if not isinstance(result, dict):
        raise SetlogError("APIの応答がJSONオブジェクトではありません。")
    return result


def start_email(email: str, backup: bool = False) -> dict:
    email = email.strip()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise SetlogError("メールアドレスの形式を確認してください。")
    pending_path = STATE / "pending.bin"
    if pending_path.exists():
        pending = load_private("pending.bin")
        if pending.get("email") == email and pending.get("retry_at", 0) > time.time():
            raise SetlogError("コードの再送待機時間内です。届いたコードを確認してください。")
    # 暗号化機能の利用可否を、メール送信より前に確認する。
    _protect(b"setlog-session-check")
    prefix = "auth/backup-email/login/" if backup else "auth/email/login/"
    result = api("POST", prefix + "start", {"email": email, "language": "ja"})
    cooldown = max(0, int(result.get("resendAfterMs") or 0)) / 1000
    save_private(
        "pending.bin",
        {"email": email, "prefix": prefix, "started_at": time.time(), "retry_at": time.time() + cooldown},
    )
    return {"状態": "確認コードの送信要求が受理されました", "再送待機秒": cooldown, "ログイン完了": False}


def verify_email(code: str) -> dict:
    pending = load_private("pending.bin")
    code = code.strip()
    if not code or len(code) > 64 or any(character.isspace() for character in code):
        raise SetlogError("確認コードの形式を確認してください。")
    result = api(
        "POST", pending["prefix"] + "verify", {"email": pending["email"], "code": code, "language": "ja"}
    )
    user = result.get("user")
    if not isinstance(user, dict):
        if (
            result.get("status") == "needs_onboarding"
            or result.get("needsOnboarding")
            or "onboarding" in result
        ):
            raise SetlogError(
                "既存アカウントへのログインではなく新規登録が必要と返されました。新規作成せず停止しました。"
            )
        raise SetlogError("ログイン済みユーザーの情報が応答にありません。")
    token = user.get("token")
    if not isinstance(token, str) or not token:
        raise SetlogError("ログイン応答に本人セッションがありません。")
    # 有効な応答を保管してから、別の読み取りで認証の成立を確かめる。
    save_private("session.bin", {"token": token, "user": user, "created_at": time.time()})
    verified = profile()
    (STATE / "pending.bin").unlink(missing_ok=True)
    return {
        "状態": "本人ログインとプロフィール読み取りを確認しました",
        "ログイン完了": True,
        "ユーザー": verified,
    }


def profile() -> dict:
    session = load_private("session.bin")
    response = api("GET", "user/profile", token=session["token"])
    user = response.get("user")
    if not isinstance(user, dict):
        raise SetlogError("プロフィールの読み取り結果を確認できませんでした。")
    # 表示に必要な項目だけを返し、暗号鍵やトークンを表示しない。
    if user.get("userId") != session.get("user", {}).get("userId"):
        raise SetlogError("プロフィールの本人IDが保存されたセッションと一致しません。")
    return {
        field: user[field]
        for field in ("userId", "id", "uid", "name", "userName", "username")
        if field in user
    }


def device_info() -> dict:
    if (STATE / "device.bin").exists():
        return load_private("device.bin")
    device = {
        "deviceId": str(uuid.uuid4()),
        "platform": "windows",
        "name": "setlog-mcp (Windows)",
        "appVersion": __version__,
    }
    save_private("device.bin", device)
    return device


def claim_transfer(code: str) -> dict:
    from . import crypto

    code = re.sub(r"[\s-]", "", code)
    if not re.fullmatch(r"[0-9]{8}", code):
        raise SetlogError("スマホに表示された8桁の引き継ぎコードが必要です。")
    if (STATE / "transfer.bin").exists():
        previous = load_private("transfer.bin")
        if previous.get("sessionId") and not previous.get("finished"):
            existing_session = load_private("session.bin")
            identifier = previous["sessionId"]
            if not re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
                raise SetlogError("保存された引き継ぎセッションの形式が不正です。")
            state = api("GET", f"key-transfer/sessions/{identifier}", token=existing_session["token"])
            if state.get("state") not in ("expired", "cancelled", "canceled", "failed"):
                raise SetlogError(
                    "引き継ぎセッションが既にあります。check_key_transferで現在の状態を確認してください。"
                )
    session = load_private("session.bin")
    private, public = crypto.generate_pair()
    device = device_info()
    pending = {
        "private": crypto.b64(private),
        "device": device,
        "userId": session["user"]["userId"],
        "created_at": time.time(),
    }
    save_private("transfer.bin", pending)
    result = api(
        "POST",
        "key-transfer/sessions/claim",
        {"code": code, "recipientEphemeralPubB64": crypto.b64(public), "device": device},
        token=session["token"],
    )
    if not result.get("sessionId"):
        raise SetlogError("引き継ぎセッションの識別子が応答にありません。")
    pending.update({"sessionId": result["sessionId"], "session": result})
    save_private("transfer.bin", pending)
    return {
        "状態": result.get("state"),
        "スマホ側の操作": "スマホに表示されたsetlog-mcpへの引き継ぎを承認してください。",
        "有効期限": result.get("expiresAt"),
    }


def check_transfer() -> dict:
    from . import crypto

    pending = load_private("transfer.bin")
    identifier = pending.get("sessionId")
    if not identifier or not re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
        raise SetlogError("有効な引き継ぎセッションがありません。")
    session = load_private("session.bin")
    if pending["userId"] != session["user"]["userId"]:
        raise SetlogError("引き継ぎ中にログインアカウントが変わったため停止しました。")
    if pending.get("finished"):
        identity = load_private("identity.bin")
        current_user = api("GET", "user/profile", token=session["token"]).get("user", {})
        private = crypto.unb64(identity.get("identityPrivateKeyB64"))
        if (
            identity.get("userId") != current_user.get("userId")
            or identity.get("kid") != current_user.get("kid")
            or crypto.b64(crypto.public_key(private)) != current_user.get("publicKey")
        ):
            raise SetlogError("保存済みの暗号鍵が現在の本人アカウントと一致しません。")
        return {"状態": "鍵の引き継ぎは既に完了しています", "鍵の引き継ぎ完了": True}
    state = api("GET", f"key-transfer/sessions/{identifier}", token=session["token"])
    if state.get("state") != "payload_uploaded":
        return {
            "状態": state.get("state"),
            "鍵の引き継ぎ完了": False,
            "次回確認までのミリ秒": state.get("pollAfterMs"),
            "有効期限": state.get("expiresAt"),
        }
    result = api("GET", f"key-transfer/sessions/{identifier}/payload", token=session["token"])
    envelope = result.get("envelope")
    if not isinstance(envelope, dict):
        return {
            "状態": result.get("state"),
            "鍵の引き継ぎ完了": False,
            "次回確認までのミリ秒": result.get("pollAfterMs"),
            "有効期限": result.get("expiresAt"),
        }
    transfer = dict(pending["session"])
    transfer.update({key: value for key, value in result.items() if value is not None})
    current_user = api("GET", "user/profile", token=session["token"]).get("user", {})
    if current_user.get("userId") != pending["userId"]:
        raise SetlogError("現在の本人プロフィールが引き継ぎ先と一致しません。")
    imported = crypto.import_transfer(
        envelope, transfer, crypto.unb64(pending["private"]), current_user, pending["device"]
    )
    # アカウントの公開鍵を更新せず、本人が承認した既存の鍵だけをローカルへ保存する。
    save_private("identity.bin", imported)
    session["user"] = current_user
    save_private("session.bin", session)
    api(
        "POST",
        f"key-transfer/sessions/{identifier}/complete",
        {"importedKid": imported["kid"], "deactivateOldDevice": False},
        token=session["token"],
    )
    save_private("transfer.bin", {"sessionId": identifier, "userId": imported["userId"], "finished": True})
    return {"状態": "鍵の引き継ぎと登録済み公開鍵との一致を確認しました", "鍵の引き継ぎ完了": True}


def main() -> int:
    parser = argparse.ArgumentParser(description="setlogの本人ログインを確認するローカルツール")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("login-start", help="登録メール宛ての確認コードを要求する")
    start.add_argument("email")
    start.add_argument("--backup", action="store_true", help="登録済みバックアップメールを使用する")
    commands.add_parser("login-verify", help="標準入力から確認コードを受け取り検証する")
    commands.add_parser("profile", help="本人プロフィールを読み取って認証を確認する")
    commands.add_parser("transfer-claim", help="標準入力の8桁コードでスマホからの鍵引き継ぎを要求する")
    commands.add_parser("transfer-check", help="引き継ぎの承認と鍵の到着を1回確認する")
    options = parser.parse_args()
    try:
        if options.command == "login-start":
            with operation_lock():
                result = start_email(options.email, options.backup)
        elif options.command == "login-verify":
            print("確認コードを入力してください。", flush=True)
            code = sys.stdin.readline()
            with operation_lock():
                result = verify_email(code)
        elif options.command == "transfer-claim":
            print("引き継ぎコードを入力してください。", flush=True)
            code = sys.stdin.readline().strip()
            with operation_lock():
                result = claim_transfer(code)
        elif options.command == "transfer-check":
            with operation_lock():
                result = check_transfer()
        else:
            result = profile()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (SetlogError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
