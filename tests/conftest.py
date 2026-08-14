"""帳場 v177+ テストハーネス共通部。

規約(必読・tests/README.md にも要約あり):
- 環境変数はこのファイルのimport時(=どのappモジュールのimportより前)に確定させる。
  app.config はimport時に環境変数を読むため、後から変えても効かない。
- DB: 一時ディレクトリの db ファイル(CHOUBA_DB)。セッション終了時に破棄。
- ANTHROPIC_API_KEY は除去 → LLM呼び出しはスタブ/テンプレート経路に落ちる。
- CHOUBA_INGEST_TOKEN=tk。保護APIは headers={"X-Ingest-Token": "tk"} で叩く。
  (認証はヘッダ。cookieではない。CHOUBA_PASSWORD は設定しない=玄関ミドルウェアは素通し)
- news は実ネットワーク禁止: _SLEEP=False、_fetch_rss はno-opモック。
- config.MODE はimport時determined。既定は mizu。generalモード固有の検証は
  run_in_mode("general", code_str) で別プロセス起動して行う。
"""
import os
import subprocess
import sys
import tempfile

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---- 環境の確定(あらゆる app import より前・conftest import 時に実行) ----
_TMPDIR = tempfile.mkdtemp(prefix="chouba_test_")
os.environ["CHOUBA_DB"] = os.path.join(_TMPDIR, "chouba_test.db")
os.environ["CHOUBA_INGEST_TOKEN"] = "tk"
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("CHOUBA_PASSWORD", None)   # 玄関認証オフ(素通し)
os.environ.pop("CHOUBA_DEMO", None)       # デモシード無効(本番相当)
os.environ.setdefault("CHOUBA_MODE", "mizu")

if REPO not in sys.path:
    sys.path.insert(0, REPO)


@pytest.fixture(scope="session")
def client():
    """TestClient(app.main:app)。news の実ネットワークをimport前に封じる。"""
    # app.main のimportで news.start_scheduler() が走るため、先に news を封じる
    from app import news
    news._SLEEP = False
    news._fetch_rss = lambda query, require_in_title="": []   # no-op(実ネット禁止)
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def tok():
    """保護API用ヘッダ。client.get(url, headers=tok) のように使う。"""
    return {"X-Ingest-Token": "tk"}


def run_in_mode(mode: str, code_str: str, extra_env: dict | None = None):
    """CHOUBA_MODE=mode の別プロセスで 'python -c code_str' を実行。
    generalモード固有の検証用(config.MODEはimport時決定のため同一プロセスでは変えられない)。
    DBは呼び出しごとに新規tmp。ANTHROPIC_API_KEYなし・CHOUBA_INGEST_TOKEN=tk。
    戻り値: (returncode, stdout, stderr)"""
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("CHOUBA_PASSWORD", None)
    env.pop("CHOUBA_DEMO", None)
    env["CHOUBA_MODE"] = mode
    env["CHOUBA_DB"] = os.path.join(tempfile.mkdtemp(prefix=f"chouba_{mode}_"), "m.db")
    env["CHOUBA_INGEST_TOKEN"] = "tk"
    if extra_env:
        env.update(extra_env)
    p = subprocess.run([sys.executable, "-c", code_str], cwd=REPO, env=env,
                       capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout, p.stderr


def mk_contact(client, tok, code, rank="B", cycle_days=None, note="", tags="",
               birthday="", kind=None, **fields):
    """契約者(顧客カード)を最短で作る。
    経路: POST /api/contacts(玄関ミドルウェアはPASSWORD未設定で素通し)
          → kind指定時は POST /api/contacts/{code}/kind
          → その他fields(stand/flag_ero/flag_koi/note_neg等)は crm.update_contact 直呼び。
    戻り値: 作成後のカードdict(db.get_contact相当)。"""
    r = client.post("/api/contacts", json={
        "code": code, "rank": rank, "cycle_days": cycle_days,
        "note": note, "tags": tags, "birthday": birthday}, headers=tok)
    assert r.status_code == 200, f"contact create failed: {r.status_code} {r.text}"
    if kind and kind != "customer":
        r2 = client.post(f"/api/contacts/{code}/kind", json={"kind": kind}, headers=tok)
        assert r2.status_code == 200, f"set_kind failed: {r2.status_code} {r2.text}"
    if fields:
        from app import crm
        crm.ensure()
        crm.update_contact(code, fields)
    from app import db
    return db.get_contact(code)
