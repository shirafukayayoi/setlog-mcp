# 開発への参加

READMEの手順で仮想環境を作成し、`python -m pip install -e ".[dev]"` を実行してください。

変更後は次の確認を実行します。

```text
python -m ruff check .
python -m ruff format --check .
python -m pytest -q
python -m build
```

テストには合成データを使用してください。実アカウントへのメール送信や投稿は自動テストに加えないでください。Windows以外のCIでは、DPAPIを使用する保存処理を合成データ用に置き換えて検証します。これはmacOS・Linuxの実アカウント操作をサポートすることを意味しません。

アプリのAPK、逆コンパイルしたソース、第三者のAPIキー、個人の認証情報や動画はコミットしないでください。

MCPツールの追加時は、入力スキーマ、構造化した戻り値、読み取り・書き込みの注釈を定義し、stdioでの呼び出しをテストしてください。
