"""v209: たまった受信の一括片づけ(本人裁定2026-08-12・モック承認)。
保護=S客・ピン・非店内の急ぎ / auto(swept)で成績除外 / actedログで一括undo。
"""
from tests.conftest import mk_contact


def _incoming(client, contact, text):
    r = client.post("/api/incoming", json={"contact": contact, "text": text})
    assert r.status_code == 200
    return r.json()


def test_sweep_skips_and_protects(client, tok):
    from app import db
    mk_contact(client, tok, "t_v209_a", rank="B")
    mk_contact(client, tok, "t_v209_s", rank="S")
    mk_contact(client, tok, "t_v209_u", rank="B")
    r1 = _incoming(client, "t_v209_a", "こないだはどうも〜")            # batch=対象
    r2 = _incoming(client, "t_v209_s", "ひさしぶり!")                  # S客=保護
    r3 = _incoming(client, "t_v209_u", "今から向かっていい?席ある?")   # urgent=保護
    rr = client.post("/api/liff/reply/sweep", headers=tok,
                     json={"mids": [r1["id"], r2["id"], r3["id"]]})
    assert rr.status_code == 200
    d = rr.json()
    assert d["contacts"] == 1 and d["messages"] == 1 and len(d["act_ids"]) == 1
    m1 = db.get_message(r1["id"]); m2 = db.get_message(r2["id"]); m3 = db.get_message(r3["id"])
    assert m1["status"] == "skipped" and int(m1.get("swept") or 0) == 1   # 成績除外の印
    assert m2["status"] == "open" and m3["status"] == "open"              # 保護された


def test_sweep_undo_restores(client, tok):
    from app import db
    mk_contact(client, tok, "t_v209_b", rank="B")
    r1 = _incoming(client, "t_v209_b", "週末どうしてた?")
    rr = client.post("/api/liff/reply/sweep", headers=tok, json={"mids": [r1["id"]]}).json()
    assert rr["messages"] == 1
    for aid in rr["act_ids"]:
        u = client.post("/api/liff/reply/undo", headers=tok, json={"act_id": aid})
        assert u.status_code == 200 and not u.json().get("error")
    assert db.get_message(r1["id"])["status"] == "open"


def test_sweep_does_not_touch_stats(client, tok):
    """一括片づけは skipped 集計(swept=0のみ)に乗らない。"""
    mk_contact(client, tok, "t_v209_c", rank="B")
    r1 = _incoming(client, "t_v209_c", "またごはん行こうね")
    before = client.get("/api/stats?token=tk").json()["days"][-1]["skipped"]
    client.post("/api/liff/reply/sweep", headers=tok, json={"mids": [r1["id"]]})
    after = client.get("/api/stats?token=tk").json()["days"][-1]["skipped"]
    assert after == before


def test_sweep_rejects_bad_input(client, tok):
    assert client.post("/api/liff/reply/sweep", headers=tok, json={"mids": []}).status_code == 400
    assert client.post("/api/liff/reply/sweep", json={"mids": [1]}).status_code == 401


def test_export_requires_key_and_returns_log(client, tok):
    """v210: 生ログ書き出し(owner専用key口)。"""
    from app import linebot
    mk_contact(client, tok, "t_v210_ex", rank="B")
    _incoming(client, "t_v210_ex", "きのうはどうも!")
    linebot.save_talk("t_v210_ex", "[LINE] t_v210_ex とのトーク履歴\n10:00\t自分\tやあ")
    assert client.get("/api/liff/export/t_v210_ex").status_code == 401
    r = client.get("/api/liff/export/t_v210_ex?key=tk")
    assert r.status_code == 200
    assert "取り込みtxt原文" in r.text and "きのうはどうも" in r.text
    j = client.get("/api/liff/export/t_v210_ex?key=tk&fmt=json").json()
    assert j["contact"] == "t_v210_ex" and len(j["received"]) >= 1


def test_export_flag_bulk(client, tok):
    """v213: フラグ一括書き出し(ガチ恋等)。"""
    from app import crm, linebot
    mk_contact(client, tok, "t_v213_koi", rank="B")
    mk_contact(client, tok, "t_v213_nokoi", rank="B")
    crm.update_contact("t_v213_koi", {"flag_koi": 1})
    linebot.save_talk("t_v213_koi", "[LINE] t_v213_koi とのトーク履歴\n10:00\t自分\tやあ")
    r = client.get("/api/liff/export_flag?key=tk&flag=koi")
    assert r.status_code == 200
    assert "t_v213_koi" in r.text and "t_v213_nokoi" not in r.text
    j = client.get("/api/liff/export_flag?key=tk&flag=koi&fmt=json").json()
    assert "t_v213_koi" in j["contacts"]
    assert client.get("/api/liff/export_flag?key=tk&flag=bad").status_code == 400
    assert client.get("/api/liff/export_flag?flag=koi").status_code == 401
