"""v214: 担当を登録フロー(受信後の仕分け/txt後の仕分け)に、恋愛系の線引きをtxt後の仕分けに。
本人裁定2026-08-12(モック承認+Pro Max考察)。担当・flag_koiは顧客のみ(v187§10)。
"""
from tests.conftest import mk_contact


def _incoming(client, contact, text):
    r = client.post("/api/incoming", json={"contact": contact, "text": text})
    assert r.status_code == 200
    return r.json()


def test_classify_work_with_tanto(client, tok):
    """①受信後: ランク登録と一緒に担当が入る。"""
    from app import crm
    _incoming(client, "t_v214_a", "はじめまして!昨日はありがとうございました")
    r = client.post("/api/liff/classify", headers=tok,
                    json={"contact": "t_v214_a", "kind": "work", "rank": "A", "tanto": "れいちゃん"})
    assert r.status_code == 200 and r.json().get("ok")
    assert (crm.get_attrs("t_v214_a") or {}).get("担当") == "れいちゃん"


def test_classify_work_tanto_self_default(client, tok):
    """①受信後: 既定=自分(クライアントは常にtantoを送る)。"""
    from app import crm
    _incoming(client, "t_v214_b", "こんにちは〜")
    r = client.post("/api/liff/classify", headers=tok,
                    json={"contact": "t_v214_b", "kind": "work", "rank": "B", "tanto": "自分"})
    assert r.status_code == 200
    assert (crm.get_attrs("t_v214_b") or {}).get("担当") == "自分"


def test_fixup_save_with_tanto_and_koi(client, tok):
    """②txt後の仕分け: 担当+flag_koiが一緒に確定される(顧客)。"""
    from app import db, crm
    db.upsert_contact("t_v214_c", "B")
    r = client.post("/api/liff/fixup/save", headers=tok,
                    json={"code": "t_v214_c", "呼び名": "こうちゃん", "kind": "customer",
                          "stand": "even", "rank": "A", "担当": "みかちゃん", "flag_koi": 1})
    assert r.status_code == 200 and r.json().get("ok")
    assert (crm.get_attrs("t_v214_c") or {}).get("担当") == "みかちゃん"
    assert int((db.get_contact("t_v214_c") or {}).get("flag_koi") or 0) == 1


def test_fixup_save_koi_not_applied_to_staff(client, tok):
    """②非顧客(staff)にはflag_koi・担当を書かない(v187§10: 非客に客UIを誤爆させない)。"""
    from app import db, crm
    db.upsert_contact("t_v214_d", "B")
    r = client.post("/api/liff/fixup/save", headers=tok,
                    json={"code": "t_v214_d", "呼び名": "みほ", "kind": "staff",
                          "stand": "down", "rank": "B", "担当": "自分", "flag_koi": 1})
    assert r.status_code == 200 and r.json().get("ok")
    assert int((db.get_contact("t_v214_d") or {}).get("flag_koi") or 0) == 0
    assert not (crm.get_attrs("t_v214_d") or {}).get("担当")
