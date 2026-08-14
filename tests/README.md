# 帳場 tests/ ハーネス規約

## 実行

```
cd /home/claude/work/v177 && python -m pytest tests/ -x -q
```

依存: `pip install pytest httpx`(アプリ本体は requirements.txt)。

## 共通規約 (COMMON)

- pytest。テストは `tests/` 直下に `test_<module>.py`。
- DB: `CHOUBA_DB` = 一時ディレクトリの db ファイル(conftest が import 時に設定)。
- `ANTHROPIC_API_KEY` は環境から除去済み → LLM 呼び出しはスタブ/テンプレート経路。
- `CHOUBA_INGEST_TOKEN=tk`。保護 API は `headers={"X-Ingest-Token": "tk"}` で叩く
  (認証はヘッダ。cookie ではない)。`CHOUBA_PASSWORD` 未設定 = 玄関ミドルウェアは素通し。
- news: import 後に `news._SLEEP=False`、`news._fetch_rss` をモック(実ネットワーク禁止)。
  conftest の `client` fixture が適用済み。テストで独自に news を触るときも実ネット禁止。
- モード分岐: `config.MODE` は import 時 determined。既定 mizu で走らせ、general 固有の
  検証は `run_in_mode("general", code_str)` で subprocess 起動して行う。
- テストはアプリコード(`app/`)を変更しない。実バグを疑ったら修正せず報告に載せる。
- 自分の担当ファイル以外(他の test_*.py)は書き換えない。
- 各テストは自分専用の契約者コード(例: `t_<module>_<n>`)を使い、他テストと衝突させない
  (DB はセッションで共有される)。

## conftest.py が提供するもの

### fixtures

- `client` (session scope): `fastapi.testclient.TestClient`。一時 DB + トークン設定 +
  news モック適用済みの `app.main:app`。
- `tok`: `{"X-Ingest-Token": "tk"}` を返す(保護 API 用ヘッダ)。

### ヘルパ(fixture ではなく通常関数。`from tests.conftest import ...` で使う)

- `run_in_mode(mode, code_str, extra_env=None) -> (returncode, stdout, stderr)`
  `CHOUBA_MODE=mode`・新規 tmp DB・`CHOUBA_INGEST_TOKEN=tk`・API キーなしの env で
  `python -c code_str` を `/home/claude/work/v177` で実行。general モード検証用。
- `mk_contact(client, tok, code, rank="B", cycle_days=None, note="", tags="",
  birthday="", kind=None, **fields) -> dict`
  契約者カードを最短で作成。経路: `POST /api/contacts` → kind 指定時
  `POST /api/contacts/{code}/kind` → その他 `fields`(`stand`/`flag_ero`/`flag_koi`/
  `note_neg` 等)は `crm.update_contact` 直呼び。戻り値は `db.get_contact(code)`。

### 注意

- 環境変数は conftest の import 時に確定する(app.config は import 時に読む)。
  テスト内で `os.environ` を変えても既 import のモジュールには効かない。
- `client` は session scope = DB は全テストで共有。契約者コードの名前空間を守ること。
- 受信の投入は `POST /api/incoming` (`{"contact": .., "text": ..}`) が最短
  (deskservice.ingest と同じパイプライン)。
