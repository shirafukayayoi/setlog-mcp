"""MCPクライアントの接続とは独立して予約を実行する。"""

import argparse
import json
import math
import sys
import time

from . import client, posting


def main() -> int:
    parser = argparse.ArgumentParser(description="暗号化した予約キューを実行するsetlogワーカー")
    parser.add_argument("--once", action="store_true", help="現在期限に達している予約を確認して終了する")
    parser.add_argument("--poll-seconds", type=float, default=15, help="常駐時の確認間隔。既定15秒")
    args = parser.parse_args()
    if not math.isfinite(args.poll_seconds) or not 1 <= args.poll_seconds <= 300:
        parser.error("--poll-secondsは1〜300を指定してください。")
    try:
        while True:
            result = posting.run_once()
            if args.once or result["processed"]:
                print(json.dumps(result, ensure_ascii=False), flush=True)
            if args.once:
                return 1 if any(item["status"] != "completed" for item in result["results"]) else 0
            time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        return 0
    except client.SetlogError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print(
            "予約ワーカーを停止しました。保存状態を確認してください。投稿要求済みの動画は自動再送しません。",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
