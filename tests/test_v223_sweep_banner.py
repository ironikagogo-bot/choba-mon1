"""v223(裁定3・段階投入1): ホームに🧹たまりバナー。5日以上前のopenを持つ相手数をhome APIへ。"""
import time
from tests.conftest import mk_contact


def test_home_sweep_old_count(client, tok):
    from app import db
    for i in range(6):
        mk_contact(client, tok, f"t_v223_{i}", rank="B")
        mid = db.add_message(f"t_v223_{i}", "むかしの連絡", "batch", "",
                             ts=time.time() - 6 * 86400)
    r = client.get("/api/liff/home", headers=tok)
    assert r.status_code == 200
    assert r.json()["sweep_old"] >= 6


def test_home_sweep_old_ignores_recent(client, tok):
    from app import db
    mk_contact(client, tok, "t_v223_new", rank="B")
    db.add_message("t_v223_new", "きょうの連絡", "batch", "")
    r = client.get("/api/liff/home", headers=tok)
    codes_counted = r.json()["sweep_old"]
    db.add_message("t_v223_new", "続き", "batch", "")
    r2 = client.get("/api/liff/home", headers=tok)
    assert r2.json()["sweep_old"] == codes_counted   # 新しい受信では増えない
