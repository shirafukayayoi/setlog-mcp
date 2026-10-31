"""機密情報をリポジトリ外へ保存するための設定。"""

import os
from datetime import datetime
from pathlib import Path

REGIONS = {"ap-northeast-2", "us-east-1"}


def state_directory() -> Path:
    override = os.environ.get("SETLOG_MCP_STATE_DIR")
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise ValueError("SETLOG_MCP_STATE_DIRには絶対パスを指定してください。")
        return path
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "setlog-mcp"


def media_region() -> str:
    region = os.environ.get("SETLOG_MCP_REGION", "ap-northeast-2")
    if region not in REGIONS:
        raise ValueError("SETLOG_MCP_REGIONはap-northeast-2またはus-east-1を指定してください。")
    return region


def capture_offset_seconds() -> int:
    offset = datetime.now().astimezone().utcoffset()
    return int(offset.total_seconds()) if offset is not None else 0


def capture_country() -> str | None:
    country = os.environ.get("SETLOG_MCP_COUNTRY")
    if country is None:
        return None
    country = country.strip().upper()
    if len(country) != 2 or not country.isascii() or not country.isalpha():
        raise ValueError("SETLOG_MCP_COUNTRYには2文字の国コードを指定してください。")
    return country


def ffprobe_command() -> str:
    return os.environ.get("SETLOG_MCP_FFPROBE", "ffprobe")


def max_video_bytes() -> int:
    value = int(os.environ.get("SETLOG_MCP_MAX_VIDEO_BYTES", str(256 * 1024 * 1024)))
    if value <= 0:
        raise ValueError("SETLOG_MCP_MAX_VIDEO_BYTESには正のバイト数を指定してください。")
    return value
