"""v215: txt取り込み→カード反映の点検(card_audit)と自動アップデート(card_backfill)。
検疫は尊重(未確定は触らない)・重要項目は通常の○✕関門を通る。owner専用key口。
"""
from tests.conftest import mk_contact


def _talk(header, lines):
    return f"[LINE] {header}とのトーク履歴\n" + "\n".join(lines)


def _seed_talk(code, text):
    from app import linebot
    linebot.ensure()
    linebot.save_talk(code, text)


def test_card_audit_lists_imported(client, tok):
    from app import db
    mk_contact(client, tok, "t_v215_a", rank="B")
    _seed_talk("t_v215_a", _talk("t_v215_a", ["2026/01/01(木)", "10:00\t自分\tやあ"]))
    r = client.get("/api/liff/card_audit?fmt=json", headers=tok)
    assert r.status_code == 200
    items = {x["contact"]: x for x in r.json()["items"]}
    assert "t_v215_a" in items
    it = items["t_v215_a"]
    assert it["chars"] > 0 and it["kind"] == "customer"


def test_card_audit_key_query_and_text(client):
    """ブラウザ用: key=INGEST_TOKENのクエリだけで開ける(ヘッダなし)。text/plainで返る。"""
    r = client.get("/api/liff/card_audit?key=tk")
    assert r.status_code == 200
    assert "カード反映点検" in r.text


def test_card_audit_denies_wrong_key(client):
    r = client.get("/api/liff/card_audit?key=wrong")
    assert r.status_code in (401, 403)


def test_backfill_fills_yobina_via_pending(client, tok):
    """確定済み顧客で呼び名が無い相手: 決定論抽出→pending facts(自動確定しない)。"""
    from app import db, crm, liff
    mk_contact(client, tok, "宮澤将史", rank="B")
    _seed_talk("宮澤将史", _talk("宮澤将史", [
        "2026/01/01(木)", "10:00\t自分\t宮澤くん、おつ",
        "2026/01/03(土)", "10:00\t自分\t宮澤君どう?",
        "2026/01/05(月)", "10:00\t自分\t宮澤くん、飲む?"]))
    liff._backfill215("自分")   # 同期呼び(テストではスレッド化しない)
    with db.conn() as c:
        row = c.execute("SELECT v, status FROM linebot_facts WHERE contact=? AND k='呼び名'",
                        ("宮澤将史",)).fetchone()
    assert row and row["v"] == "宮澤くん"
    assert row["status"] in ("pending", "applied")   # 関門を通る(勝手に属性確定しない)
    assert not (crm.get_attrs("宮澤将史") or {}).get("呼び名") or row["status"] == "applied"


def test_backfill_skips_quarantined(client, tok):
    """検疫マーカーが残る相手(仕分け待ち)は自動アップデート対象外。"""
    from app import db, linebot, liff
    db.upsert_contact("t_v215_q", "B")
    with db.conn() as c:
        c.execute("UPDATE contacts SET linked=1 WHERE code=?", ("t_v215_q",))
    _seed_talk("t_v215_q", _talk("t_v215_q", [
        "2026/01/01(木)", "10:00\t自分\tみほちゃん、おつ",
        "2026/01/03(土)", "10:00\t自分\tみほちゃん元気?"]))
    linebot.quarantine_add("t_v215_q", [{"k": "仕事・会社", "v": "?", "src": "", "conf": "低",
                                         "alts": []}])
    liff._backfill215("自分")
    with db.conn() as c:
        row = c.execute("SELECT 1 FROM linebot_facts WHERE contact=? AND k='呼び名'",
                        ("t_v215_q",)).fetchone()
    assert row is None   # 触っていない


def test_backfill_confirm_page_and_run(client):
    """GET=確認ページ(実行しない)/POST=開始。規約: 重いURLのGET直実行禁止。"""
    import os
    from app import linebot
    g = client.get("/api/liff/card_backfill?key=tk")
    assert g.status_code == 200 and "実行する" in g.text
    os.environ["CHOUBA_BF215_SLEEP"] = "0"   # 全走時は他テストの相手が乗るため待ち0で
    try:
        p = client.post("/api/liff/card_backfill", data={"key": "tk"})
        assert p.status_code == 200 and "開始" in p.text
        import time as _t
        for _ in range(150):   # 裏スレッド完了を待つ(AIキー無し=決定論のみ)
            if (linebot._meta_get("backfill215") or "").startswith("完了"):
                break
            _t.sleep(0.2)
        assert (linebot._meta_get("backfill215") or "").startswith("完了")
    finally:
        os.environ.pop("CHOUBA_BF215_SLEEP", None)


def test_v224_backfill_never_overwrites_existing_attrs(client, tok, monkeypatch):
    """v224: 自動アップデートは追加専用 — 手入力済みの属性はAI抽出と衝突しても不変。"""
    import json as _j
    from app import linebot, crm, db, liff, config
    mk_contact(client, tok, "t_v224_a", rank="B")
    crm.add_def("仕事・会社"); crm.set_attr("t_v224_a", "仕事・会社", "商社(手入力)")
    crm.add_def("呼び名"); crm.set_attr("t_v224_a", "呼び名", "てすとさん")
    _seed_talk("t_v224_a", _talk("t_v224_a", [
        "2026/01/01(木)", "10:00\tt_v224_a\tうちIT企業なんですよ", "10:01\t自分\tそうなんですね!"]))

    def _fake_extract(text, contact, self_name):
        return ([{"k": "仕事・会社", "v": "IT企業", "src": "うちIT企業なんですよ",
                  "conf": "高", "alts": []}], None)

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(linebot, "extract_facts", _fake_extract)
    monkeypatch.setattr(linebot, "maybe_auto_persona", lambda c: None)
    monkeypatch.setattr(linebot, "persona_async", lambda c: None)
    import app.situations as _sit, app.dynamics as _dyn
    monkeypatch.setattr(_sit, "harvest_and_save", lambda *a: None)
    monkeypatch.setattr(_dyn, "analyze_and_save", lambda *a: False)
    liff._backfill215("自分")
    assert (crm.get_attrs("t_v224_a") or {}).get("仕事・会社") == "商社(手入力)"   # 不変


def test_v229_liff_page_etag(client):
    """v229: 画面HTMLはno-cache+ETag。同一ETag再要求は304(デプロイ後は必ず新画面)。"""
    r = client.get("/liff/")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-cache"
    et = r.headers.get("etag")
    assert et
    r2 = client.get("/liff/", headers={"If-None-Match": et})
    assert r2.status_code == 304
