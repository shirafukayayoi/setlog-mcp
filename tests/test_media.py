"""実際のffprobe出力形式に基づく動画メタデータと上限の検証。"""

import json
from types import SimpleNamespace

import pytest

from setlog_mcp import client, config, upload


def test_rotated_video_uses_display_dimensions(tmp_path, monkeypatch):
    output = {
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "2.0"},
        "streams": [
            {
                "codec_type": "video",
                "width": 1920,
                "height": 1080,
                "codec_name": "h264",
                "side_data_list": [{"rotation": -90}],
            }
        ],
    }
    monkeypatch.setattr(
        upload.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(output))
    )
    metadata = upload.probe_video(tmp_path / "clip.mp4")
    assert (metadata["width"], metadata["height"], metadata["duration"]) == (1080, 1920, 2.0)


def test_size_limit_stops_before_authentication(tmp_path, monkeypatch):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"oversized")
    monkeypatch.setattr(config, "max_video_bytes", lambda: 1)
    monkeypatch.setattr(
        client, "load_private", lambda name: pytest.fail("動画サイズの確認前に認証を読み取りました")
    )
    with pytest.raises(client.SetlogError, match="上限"):
        upload.prepare(path, "room")
