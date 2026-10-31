# setlog-mcp

本人のsetlogアカウントへ、暗号化したMP4動画を添付する**非公式のローカルMCPサーバー**です。

メールコード認証、スマホ承認による既存の暗号鍵の引き継ぎ、投稿先一覧、動画の準備・添付・読み戻しに対応します。公式の[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)を使い、stdioで接続します。

現在の対応環境は **Windows / Python 3.11〜3.14 / MP4** です。認証情報と暗号鍵の保管にはWindowsのDPAPIを使用します。macOSとLinuxでのアカウント操作は未対応です。

## できること

- 本人の登録メールへの確認コード送信とログイン。
- スマホの「別の端末に移行」で、既存の暗号鍵を本人承認のもとで引き継ぐ。
- 投稿先の名前・ID・本人以外の参加者数を確認する。
- MP4を暗号化し、送信せずに投稿先・受信者数・準備IDを確認する。
- 動画に添えるコメントを指定する。日本語・改行・絵文字に対応する。
- 指定された準備IDの動画を添付し、投稿を読み戻して照合する。
- 投稿要求後に応答が失われた場合、自動再送せず既存の投稿を確認する。
- 複数の動画をまとめて準備し、指定した順番で一括投稿する。
- 時差付きの日時と投稿間隔を指定して予約し、別プロセスのワーカーで実行する。
- 投稿履歴を閲覧し、日付範囲・タイムゾーン・1日の区切り時刻を指定して検索する。

暗号鍵の引き継ぎでは `deactivateOldDevice: false` を使用します。アカウントの公開鍵を作り直す処理はありません。

## インストール

FFmpegに含まれる `ffprobe` をインストールし、PATHに追加してください。

```powershell
git clone https://github.com/shirafukayayoi/setlog-mcp.git
cd setlog-mcp
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
```

Python 3.12〜3.14を使う場合は、`py -3.11` のバージョンを変更できます。

### アプリ用APIキー

このリポジトリには、setlogのアプリ用APIキー、APK、逆コンパイルしたソース、個人の認証データを含めていません。

自分で用意したアプリ用APIキーを、次のいずれかで設定してください。

1. `SETLOG_API_KEY` 環境変数をMCPサーバープロセスへ渡す。
2. 次のコマンドで入力する。入力内容は表示されず、DPAPIで暗号化して保存されます。

```powershell
.\.venv\Scripts\setlog-mcp.exe configure
.\.venv\Scripts\setlog-mcp.exe status
```

APIキーの値はコマンドライン引数、Issue、スクリーンショット、リポジトリへ貼り付けないでください。

## MCPクライアントへの登録

stdioサーバーとして、インストールした実行ファイルを登録します。

```text
コマンド: C:\path\to\setlog-mcp\.venv\Scripts\setlog-mcp.exe
引数: serve
```

`mcpServers` 形式を使うクライアントの設定例です。設定ファイルの場所や項目名は利用するクライアントに合わせてください。

```json
{
  "mcpServers": {
    "setlog": {
      "command": "C:\\path\\to\\setlog-mcp\\.venv\\Scripts\\setlog-mcp.exe",
      "args": ["serve"]
    }
  }
}
```

サーバーはHTTPポートを開きません。標準出力はMCP通信に使います。

## 初回の本人認証

1. `get_status` でAPIキーと `ffprobe` の設定を確認する。
2. `start_email_login` に登録メールを渡す。
3. 届いた確認コードを `verify_email_login` に渡す。
4. スマホのsetlogで「アカウント → 別の端末に移行」を開く。
5. 表示された8桁コードを `claim_key_transfer` に渡す。
6. スマホで `setlog-mcp (Windows)` への引き継ぎを承認する。
7. `check_key_transfer` で鍵の受信と登録鍵との一致を確認する。

認証と鍵の引き継ぎは、保存先を同じにしている限り通常の添付ごとに繰り返す必要はありません。認証の有効性は `get_profile` で確認できます。コード認証の結果が新規登録を要求する場合、アカウントを作らず停止します。

## 動画を添付する

1. `list_collections` で投稿先のIDと参加者数を確認する。
2. `prepare_video` に動画の絶対パスと投稿先IDを渡す。コメントを添える場合は `comment` に本文を指定する。
3. 返された投稿先・受信者数・動画情報・コメント本文を確認する。
4. ユーザーから依頼された送信に対して、準備IDを `send_prepared_video` に渡す。
5. 結果が不明なときは、同じ動画を再送せず `verify_video` で確認する。

`prepare_video` はファイルを暗号化してローカルに準備するだけで、投稿しません。`send_prepared_video` は実際に投稿します。動画の切り取りや再圧縮は自動では行いません。

コメント付きの準備例です。

```json
{
  "video_path": "C:\\Videos\\clip.mp4",
  "collection_id": "投稿先ID",
  "comment": "今日の記録\n楽しかった！😀"
}
```

コメントはsetlog標準のキャプションとして動画に表示されます。動画ファイルへの文字の焼き込みや、投稿後の返信コメントとは別の機能です。本文は動画の暗号化領域には含まれず、`captions` メタデータとしてAPIへ送信します。コメントを省略した場合や空白だけの場合は、動画だけを準備します。投稿の読み戻しではコメント本文の一致も確認します。

| ツール | 用途 |
| --- | --- |
| `get_status` | 通信せずに設定と保存データの有無を確認 |
| `get_profile` | 本人プロフィールと認証を確認 |
| `list_collections` | 投稿先のID・名前・参加者数を取得 |
| `start_email_login` | 本人の登録メールへ確認コードを送信 |
| `verify_email_login` | メールコードを検証し、認証を保存 |
| `claim_key_transfer` | スマホからの鍵の引き継ぎを要求 |
| `check_key_transfer` | 承認状態と鍵の受信を確認 |
| `prepare_video` | 動画と任意のコメントを送信前の状態に準備 |
| `send_prepared_video` | 準備した動画を実際に添付 |
| `verify_video` | 投稿を読み戻して照合 |
| `prepare_video_batch` | 動画・投稿先・コメントを最大50件まとめて準備 |
| `send_prepared_videos` | 指定順に一括送信。失敗時は後続を停止 |
| `schedule_videos` | 準備IDを日時・投稿間隔付きで予約 |
| `list_scheduled_posts` | 予約一覧・停止理由・ワーカー最終確認日時を取得 |
| `cancel_scheduled_post` | 予約の自動実行を取り消す |
| `list_posts` | 投稿履歴を新しい順に取得 |
| `search_posts_by_date` | 日付・タイムゾーン・区切り時刻で投稿を検索 |

## 一括投稿

`prepare_video_batch` に `videos` を渡します。動画ごとに投稿先とコメントを指定できます。

```json
{
  "videos": [
    {"video_path": "C:\\Videos\\morning.mp4", "collection_id": "投稿先ID", "comment": "朝の記録"},
    {"video_path": "C:\\Videos\\evening.mp4", "collection_id": "投稿先ID", "comment": "夜の記録😀"}
  ]
}
```

返された全件のプレビューを確認し、送信する準備IDを `send_prepared_videos` の `job_ids` 配列へ順番に指定してください。1回に1〜50件を扱えます。同じ準備IDの重複指定は拒否します。

準備途中に失敗した場合、`status: partial` と、準備できた分の `job_ids`、失敗した位置を返します。一括送信の途中で失敗した場合は `status: blocked` と未完了の `remaining_job_ids` を返し、後続を停止します。

状態を確認してから同じID一覧で送信を再実行すると、照合済みの投稿は飛ばします。投稿要求済みで結果不明のものは読み戻しだけを行い、POSTし直しません。読み戻しにも失敗した場合は再び停止します。

## 予約投稿

動画は先に単体または一括で準備します。`schedule_videos` へ準備IDと予約日時を渡すと、その時点で将来の送信を予約します。

```json
{
  "job_ids": ["準備ID1", "準備ID2"],
  "publish_at": "2030-01-01T20:00:00+09:00",
  "interval_seconds": 300
}
```

この例では1本目を日本時間20時、2本目を20時5分に予約します。1本だけの予約も同じツールで指定できます。時差のない日時、過去の日時、負の投稿間隔は受け付けません。予約一覧の `publish_at` はUTCの `Z` 形式で返します。

**予約の実行には、同じWindowsユーザーでワーカーを起動しておく必要があります。** MCPクライアントを閉じても、ワーカーが動いていれば実行を続けます。

```powershell
.\.venv\Scripts\setlog-worker.exe
```

既定で15秒ごとに確認します。確認間隔は `--poll-seconds 30` のように変更できます。通信や前の動画の処理時間もあるため、指定秒ちょうどの投稿は保証しません。PCの停止・スリープ中は実行されず、再開後に期限の過ぎた予約を順に実行します。投稿日時には実際に投稿要求を行う時刻を使用します。

必要なときだけ期限に達した予約を実行する場合は次のコマンドを使用します。

```powershell
.\.venv\Scripts\setlog-worker.exe --once
```

Windowsのタスクスケジューラで定期実行する場合は、同じWindowsユーザーの「ログオンしているときのみ実行」を選び、仮想環境の `setlog-worker.exe` と引数 `--once` を登録できます。複数のワーカーやMCPが同じ保存先へ書き込む場合は、操作ロックで重複実行を防ぎます。競合時は次回の確認へ回します。

`list_scheduled_posts` で状態を確認し、返された `schedule_id` を `cancel_scheduled_post` へ渡すと取り消せます。`worker_last_seen` はワーカーが最後に確認した日時であり、現在の稼働を保証する値ではありません。

| 予約の状態 | 意味 |
| --- | --- |
| `queued` | 実行待ち |
| `running` | 実行中、または前回中断された処理 |
| `completed` | 投稿の照合まで完了 |
| `failed` | 投稿要求前に失敗。保存状態を確認して手動で再予約可能 |
| `needs_review` | 投稿要求後の結果が不明。`verify_video` で確認 |
| `blocked` | 同じ一括予約内の先行投稿が停止したため、後続の実行も停止 |
| `cancelled` | 自動実行を取り消し済み |

同じ一括予約内で1件が停止すると、そのグループの後続を止めます。他の独立した予約グループは続行します。再起動時には、投稿要求前の処理は再開し、投稿要求済みなら読み戻しで確認します。認証アカウントや受信者の鍵が変わった場合も停止します。

予約が有効な動画は手動送信できません。先に予約を取り消してください。取り消しは投稿の削除を行う機能ではありません。実行中の操作ロックが保持されている場合は、処理後に状態を確認してください。

## 投稿履歴と日付検索

`list_posts` へ `collection_id` と任意の `limit` を渡すと、最新の投稿を取得できます。

`search_posts_by_date` の例です。`end_date` を省略すると1日だけを検索します。終了日も検索対象に含みます。

```json
{
  "collection_id": "投稿先ID",
  "start_date": "2026-10-30",
  "end_date": "2026-10-31",
  "timezone_name": "Asia/Tokyo",
  "day_start_hour": 4,
  "limit": 50
}
```

既定はsetlogに合わせた朝4時区切りです。たとえば10月31日は、日本時間10月31日4時から11月1日4時の直前までを扱います。通常の0時からの暦日で検索する場合は `day_start_hour: 0` を指定します。`timezone_name` はIANA名で指定し、夏時間を考慮します。

日付範囲は最大366日、1ページは最大100件です。結果の `has_more` が `true` の場合は、`next_cursor` を `cursor` に渡し、同じ検索条件で続きを取得してください。上限日時の境界や削除済み投稿を除外するため、件数が指定した `limit` より少なくても続きがある場合があります。

投稿日時・投稿ID・送信者ID・コメントなどを返します。暗号鍵、エンベロープ、配信URLは返しません。日付検索は投稿日時を対象とし、動画の撮影日を解析する機能ではありません。

## 設定と保存先

既定の保存先は `%LOCALAPPDATA%\setlog-mcp` です。トークン、秘密鍵、準備データのJSONはDPAPIで暗号化します。動画本体はアプリ互換の暗号化を行った状態で保存します。

| 環境変数 | 内容 | 既定値 |
| --- | --- | --- |
| `SETLOG_API_KEY` | 自分で用意したアプリ用APIキー | 設定コマンドで保管した値 |
| `SETLOG_MCP_STATE_DIR` | 保存先の絶対パス | `%LOCALAPPDATA%\setlog-mcp` |
| `SETLOG_MCP_REGION` | 動画の保存地域 | `ap-northeast-2` |
| `SETLOG_MCP_COUNTRY` | 撮影国の2文字コード | 未設定なら送信しない |
| `SETLOG_MCP_FFPROBE` | ffprobeの実行ファイル | `ffprobe` |
| `SETLOG_MCP_MAX_VIDEO_BYTES` | 準備できる動画の最大バイト数 | 268435456（256 MiB） |

保存地域は `ap-northeast-2` と `us-east-1` に対応します。撮影時のUTCオフセットは実行環境のタイムゾーンから取得します。動画はメモリー上で暗号化するため、大きい動画ではファイルサイズより多いメモリーを使います。

## CLI

```powershell
.\.venv\Scripts\setlog-auth.exe profile
.\.venv\Scripts\setlog-upload.exe collections
.\.venv\Scripts\setlog-upload.exe prepare "C:\Videos\clip.mp4" --collection "投稿先ID"
.\.venv\Scripts\setlog-upload.exe prepare "C:\Videos\clip.mp4" --collection "投稿先ID" --comment "今日の記録😀"
.\.venv\Scripts\setlog-upload.exe send "準備ID"
.\.venv\Scripts\setlog-upload.exe verify "準備ID"
```

## 開発と検証

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\python.exe -m build
```

自動テストは合成データを使用し、メール送信や実際の投稿は行いません。暗号化の既知値、鍵と端末の不一致、改ざん、二重投稿防止、MCPの初期化・ツール一覧・ツール呼び出しを検証します。

一括投稿・予約は順序、投稿間隔、取消、アカウント変更、競合、中断後の読み戻し、別プロセスでの復旧を検証します。日付検索は朝4時・0時の区切り、夏時間、ページ送り、返却情報の制限を検証します。投稿履歴と日付検索は、実際のstdio MCP経由で本人の既知の投稿を読み取り確認しています。

コメント付き投稿については、キャプションの通信形式、準備データの保存、送信・読み戻し時の本文照合を合成データで検証しています。実サービスへのコメント付き投稿とスマホ上の表示は未確認です。

通信形式はsetlog Android 1.3をもとに実装しました。開発時には本人のみの投稿先で2秒のMP4を添付し、配信先から取得した動画の復号・元データとのSHA-256一致と、スマホでの再生を確認しています。非公開APIを使用するため、setlog側の更新に追従が必要になる場合があります。

## ライセンス

このリポジトリの自作コードは [MIT](LICENSE) です。setlogおよびNew Chat Inc.の公式プロジェクトではありません。
