"""MCPの起動と、APIキーのローカル設定用エントリーポイント。"""

import argparse
import getpass
import json
import os
import sys

from . import __version__, client


def main() -> int:
    parser = argparse.ArgumentParser(description="setlogの非公式ローカルMCPサーバー")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("command", nargs="?", choices=("serve", "configure", "status"), default="serve")
    args = parser.parse_args()
    try:
        if args.command == "configure":
            client._protect(b"configuration-check")
            value = os.environ.get("SETLOG_API_KEY") or getpass.getpass(
                "アプリ用APIキー（入力は表示されません）: "
            )
            if not value.strip():
                raise client.SetlogError("空のAPIキーは保存できません。")
            with client.operation_lock():
                client.save_private("config.bin", {"app_api_key": value.strip()})
            print("APIキーを現在のWindowsユーザーのDPAPIで暗号化して保存しました。")
        elif args.command == "status":
            from .server import local_status

            print(json.dumps(local_status(), ensure_ascii=False, indent=2))
        else:
            from .server import run

            run()
        return 0
    except (client.SetlogError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
