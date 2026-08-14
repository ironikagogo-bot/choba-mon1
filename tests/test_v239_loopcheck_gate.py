"""v239: 🔬実通しテスト(loopcheck)が玄関パスワードで401になっていた件。

実ログ(mon1・2026-08-14)で発覚:
    GET  /api/stats?token=6fmo...            200 OK
    POST /api/loopcheck/start?token=6fmo...  401 Unauthorized
同じ運用トークン・同じサーバー・同じ数秒間で結果が割れていた。原因は endpoint 側の
トークン検査ではなく、その手前の玄関ミドルウェア(_EXEMPT)に loopcheck が入っていなかったこと。
ダッシュボードの「いま生きてるか確認」は、これが直るまで一度も通っていなかった。
"""
import importlib
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_loopcheck_is_exempt_from_password_gate():
    """玄関パスワードを設定したサーバーでも、運用トークンのloopcheckは通る。"""
    code = r'''
import os, sys
os.environ["CHOUBA_DB"] = "/tmp/_v239.db"
os.environ["CHOUBA_INGEST_TOKEN"] = "tk239"
os.environ["CHOUBA_PASSWORD"] = "secret"     # 玄関を有効にする(モニター実機と同じ条件)
os.environ["CHOUBA_MODE"] = "mizu"
os.environ.pop("ANTHROPIC_API_KEY", None)
sys.path.insert(0, REPO_PATH)
from fastapi.testclient import TestClient
from app import news
news._SLEEP = False
news._fetch_rss = lambda *a, **k: []
from app.main import app
c = TestClient(app)
# 玄関が本当に効いていること(免除されていない画面は弾かれる)
r_home = c.get("/", follow_redirects=False)
assert r_home.status_code in (302, 303, 307, 401), r_home.status_code
# statsは従来どおり通る
assert c.get("/api/stats?token=tk239").status_code == 200, "stats"
# loopcheckも通る(401にならない。ひも付け未完了でも200 + ok:false が正)
r = c.post("/api/loopcheck/start?token=tk239")
assert r.status_code == 200, f"loopcheck start -> {r.status_code} {r.text[:120]}"
assert c.get("/api/loopcheck/status?token=tk239").status_code == 200, "loopcheck status"
# 悪いトークンはちゃんと弾く(免除にしても穴は開いていない)
assert c.post("/api/loopcheck/start?token=wrong").status_code == 401, "bad token"
print("OK")
'''
    code = code.replace("REPO_PATH", repr(REPO))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       timeout=180)
    assert "OK" in r.stdout, f"stdout={r.stdout[-800:]}\nstderr={r.stderr[-1500:]}"


def test_exempt_list_contains_loopcheck():
    """一覧そのものの回帰(将来 _EXEMPT を触った時にここで気づける)。"""
    from app import main
    importlib.reload  # noqa: B018  (参照だけ。リロードはしない)
    assert "/api/loopcheck/" in main._EXEMPT
