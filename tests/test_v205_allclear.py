"""v205: 🔥push後、急ぎ全消化で「✓対応済み」を1通だけ送る(トーク一覧の残像対策)。
本人報告(2026-08-11)「全て処理済みなのに1件届いてるとずっと表示」。
"""
from tests.conftest import mk_contact


def _incoming(client, contact, text):
    r = client.post("/api/incoming", json={"contact": contact, "text": text})
    assert r.status_code == 200
    return r.json()


def _quiet_urgents():
    """他テストが残した未対応の急ぎを閉じて、この検証の前提(全消化)を作る。"""
    from app import db
    with db.conn() as c:
        c.execute("UPDATE messages SET status='replied' "
                  "WHERE status IN ('open','deferred') AND category='urgent'")


def _sent():
    box = []
    def send(msgs):
        box.extend(msgs)
        return True
    return box, send


def test_all_clear_sent_once_after_urgent_cleared(client, tok):
    from app import linebot
    linebot.ensure()
    _quiet_urgents()
    mk_contact(client, tok, "t_v205_a", rank="B")
    r = _incoming(client, "t_v205_a", "今から向かっていい?席ある?")   # urgent
    linebot._meta_set("upush_pending", "1")   # 🔥pushが出た体
    box, send = _sent()
    # まだ急ぎが残っている間は送らない
    assert linebot.maybe_push_all_clear(send=send) is False and not box
    rr = client.post("/api/liff/reply/act", headers=tok,
                     json={"mid": r["id"], "action": "done"})
    assert rr.status_code == 200
    assert linebot.maybe_push_all_clear(send=send) is True
    assert len(box) == 1 and "対応済みになりました" in box[0]["text"]
    # 2回目は送らない(1🔥push=最大1通)
    assert linebot.maybe_push_all_clear(send=send) is False
    assert len(box) == 1


def test_all_clear_not_sent_without_pending(client, tok):
    from app import linebot
    linebot._meta_set("upush_pending", "0")
    box, send = _sent()
    assert linebot.maybe_push_all_clear(send=send) is False and not box


def test_all_clear_waits_for_deferred_urgent(client, tok):
    """↷あとでにした急ぎが残っている間は「対応済み」と言わない。"""
    from app import linebot
    _quiet_urgents()
    mk_contact(client, tok, "t_v205_d", rank="B")
    r = _incoming(client, "t_v205_d", "予約とれる?今夜急ぎで")
    client.post("/api/liff/reply/act", headers=tok,
                json={"mid": r["id"], "action": "deferred"})
    linebot._meta_set("upush_pending", "1")
    box, send = _sent()
    assert linebot.maybe_push_all_clear(send=send) is False and not box
    # 消化したら送る
    client.post("/api/liff/reply/act", headers=tok,
                json={"mid": r["id"], "action": "skipped"})
    assert linebot.maybe_push_all_clear(send=send) is True
    linebot._meta_set("upush_pending", "0")


def test_staff_urgent_does_not_block_all_clear(client, tok):
    """店内の急ぎ語彙は🔥通知対象外なので、残っていても✓は出る(通知条件と同じ判定)。"""
    from app import linebot
    _quiet_urgents()
    mk_contact(client, tok, "t_v205_s", rank="B", kind="staff")
    _incoming(client, "t_v205_s", "シフト急ぎで確認して!")
    linebot._meta_set("upush_pending", "1")
    box, send = _sent()
    assert linebot.maybe_push_all_clear(send=send) is True
    assert len(box) == 1
    linebot._meta_set("upush_pending", "0")
