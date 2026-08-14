"""v218段3(データ衛生 S1-S5): rename検疫移行・priv破棄・完全消去の消し漏れ・
talk切り詰め上書きガード・quarantine_release退避キー。
"""
import json
from tests.conftest import mk_contact


def _quar(code):
    from app import linebot
    return linebot._meta_get(f"quarantine_{code}") or ""


def test_s1_rename_migrates_quarantine(client, tok):
    from app import linebot, crm
    mk_contact(client, tok, "t_v218_r1", rank="B")
    linebot.ensure()
    linebot.quarantine_add("t_v218_r1", [{"k": "仕事・会社", "v": "商社", "src": "s",
                                          "conf": "中", "alts": []}])
    r = crm.rename_contact("t_v218_r1", "t_v218_r2")
    assert r.get("ok")
    assert not _quar("t_v218_r1")
    assert "商社" in _quar("t_v218_r2")


def test_s2_priv_discards_quarantine(client, tok):
    from app import linebot, db
    db.upsert_contact("t_v218_p", "B")
    linebot.ensure()
    linebot.quarantine_add("t_v218_p", [{"k": "家族", "v": "娘", "src": "s", "conf": "中", "alts": []}])
    r = client.post("/api/liff/fixup/save", headers=tok,
                    json={"code": "t_v218_p", "kind": "priv"})
    assert r.status_code == 200 and r.json().get("discarded")
    assert not _quar("t_v218_p")


def test_s3_delete_full_wipes_all(client, tok):
    from app import linebot, crm, db, situations
    mk_contact(client, tok, "t_v218_d", rank="B")
    linebot.ensure(); situations.ensure()
    linebot.save_talk("t_v218_d", "x" * 300)
    linebot.quarantine_add("t_v218_d", [{"k": "家族", "v": "妻", "src": "s", "conf": "中", "alts": []}])
    with db.conn() as c:
        c.execute("INSERT INTO acted_log(contact, action, changed, sent_text, acted_ts) "
                  "VALUES(?,?,?,?,?)", ("t_v218_d", "replied", "[]", "秘密の返信本文", 1.0))
        c.execute("INSERT INTO self_examples(contact, situation, partner_text, self_text, created_ts) "
                  "VALUES(?,?,?,?,?)", ("t_v218_d", "誘い", "会いたい", "うれしい!", 1.0))
    r = crm.delete_contact_full("t_v218_d")
    assert r.get("ok")
    with db.conn() as c:
        assert not c.execute("SELECT 1 FROM acted_log WHERE contact=?", ("t_v218_d",)).fetchone()
        assert not c.execute("SELECT 1 FROM self_examples WHERE contact=?", ("t_v218_d",)).fetchone()
    assert not _quar("t_v218_d")


def test_s4_save_talk_suffix_guard(client):
    from app import linebot, db
    linebot.ensure()
    full = "冒頭のなれそめ部分\n" + "\n".join(f"10:00\t相手\tメッセージ{i}" for i in range(50))
    linebot.save_talk("t_v218_t", full)
    tail = full[-200:]   # 切り詰め断片(末尾一致)
    linebot.save_talk("t_v218_t", tail)
    with db.conn() as c:
        r = c.execute("SELECT text FROM linebot_talks WHERE contact=?", ("t_v218_t",)).fetchone()
    assert r["text"] == full   # 断片で上書きされない
    new = "全く新しい短いトーク履歴"
    linebot.save_talk("t_v218_t", new)   # 正当な新txt(末尾一致しない)は上書きされる
    with db.conn() as c:
        r = c.execute("SELECT text FROM linebot_talks WHERE contact=?", ("t_v218_t",)).fetchone()
    assert r["text"] == new


def test_s5_release_keeps_backup_on_failure(client, tok, monkeypatch):
    """適用中に落ちても保留事実が消えない(退避キー)。次のreleaseで復元適用される。"""
    from app import linebot, db, crm
    mk_contact(client, tok, "t_v218_q", rank="B")
    with db.conn() as c:
        c.execute("UPDATE contacts SET kind='customer', stand='even' WHERE code=?", ("t_v218_q",))
    linebot.ensure()
    linebot.quarantine_add("t_v218_q", [{"k": "仕事・会社", "v": "銀行", "src": "s", "conf": "中", "alts": []}])
    orig = linebot.save_split
    monkeypatch.setattr(linebot, "save_split", lambda *a: (_ for _ in ()).throw(RuntimeError("死")))
    linebot.quarantine_release("t_v218_q")
    assert "銀行" in (linebot._meta_get("quarantine_bak_t_v218_q") or "")   # 消えていない
    monkeypatch.setattr(linebot, "save_split", orig)
    linebot.quarantine_release("t_v218_q")   # 復元→適用
    with db.conn() as c:
        r = c.execute("SELECT 1 FROM linebot_facts WHERE contact=? AND k='仕事・会社'",
                      ("t_v218_q",)).fetchone()
    assert r
    assert not (linebot._meta_get("quarantine_bak_t_v218_q") or "")


def test_v218r_quarantine_add_merges_bak(client, tok):
    """レビュー指摘#1: bak残留中に新検疫が積まれても旧保留分が消えない(合流する)。"""
    from app import linebot
    linebot.ensure()
    linebot._meta_set("quarantine_bak_t_v218_x", 
                      '[{"k": "家族", "v": "娘がいる", "src": "s", "conf": "中", "alts": []}]')
    linebot.quarantine_add("t_v218_x", [{"k": "仕事・会社", "v": "銀行", "src": "s",
                                         "conf": "中", "alts": []}])
    raw = linebot._meta_get("quarantine_t_v218_x") or ""
    assert "娘がいる" in raw and "銀行" in raw          # 両方が本体に
    assert not (linebot._meta_get("quarantine_bak_t_v218_x") or "")   # bakは解消


def test_v218r_delete_full_wipes_bak(client, tok):
    from app import linebot, crm
    mk_contact(client, tok, "t_v218_y", rank="B")
    linebot.ensure()
    linebot._meta_set("quarantine_bak_t_v218_y", '[{"k": "家族", "v": "妻", "src": "s", "conf": "中", "alts": []}]')
    crm.delete_contact_full("t_v218_y")
    assert not (linebot._meta_get("quarantine_bak_t_v218_y") or "")
