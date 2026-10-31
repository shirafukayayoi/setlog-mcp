"""テストは実アカウントや開発者の保存先に接続しない。"""

import sys

import pytest

from setlog_mcp import client


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(client, "STATE", tmp_path / "state")
    monkeypatch.delenv("SETLOG_API_KEY", raising=False)
    monkeypatch.delenv("SETLOG_MCP_COUNTRY", raising=False)
    monkeypatch.delenv("SETLOG_MCP_REGION", raising=False)
    if sys.platform != "win32":
        # DPAPIを持たないCIでは、合成データ専用の保管処理に置き換える。
        monkeypatch.setattr(client, "_protect", lambda raw, decrypt=False: raw)


@pytest.fixture
def anyio_backend():
    return "asyncio"
