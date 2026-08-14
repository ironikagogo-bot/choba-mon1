"""v204: ダッシュボード可視化の拡張。
本人報告(2026-08-11)「返信何回かしてるがダッシュボードに反映されていない」
→ ✓対応した(本文なし)とコピー送信が集計に見えないのが正体。statsに両方を出す。
+ 「何人分のtxtを読ませたか」(linebot_talks=1人1行)も表示する。
"""


def _incoming(client, contact, text):
    r = client.post("/api/incoming", json={"contact": contact, "text": text})
    assert r.status_code == 200
    return r.json()


def test_stats_txt_imported_contacts(client, tok):
    from app import linebot
    linebot.save_talk("t_v204_txtA", "[LINE] t_v204_txtA とのトーク履歴\nこんにちは")
    linebot.save_talk("t_v204_txtB", "[LINE] t_v204_txtB とのトーク履歴\nやあ")
    linebot.save_talk("t_v204_txtA", "[LINE] t_v204_txtA とのトーク履歴\n再取り込み")  # 上書き=人数不変
    d = client.get("/api/stats?token=tk").json()
    assert d["txt_imported"]["contacts"] >= 2
    assert d["txt_imported"]["last_ts"] is not None


def test_stats_days_done_counted(client, tok):
    """✓対応した(action=done)が日次に出る(sent_repliesには乗らない従来仕様は不変)。"""
    r = _incoming(client, "t_v204_done", "きょう空いてる?")
    d0 = client.get("/api/stats?token=tk").json()
    before = d0["days"][-1]["done"]
    rr = client.post("/api/liff/reply/act", headers=tok,
                     json={"mid": r["id"], "action": "done"})
    assert rr.status_code == 200
    d = client.get("/api/stats?token=tk").json()
    today = d["days"][-1]
    assert today["done"] == before + 1
    # 本文なしの対応済みは sent_replies に乗らない(定義どおり)
    assert "sent_verbatim" in today and "copies" in today


def test_stats_days_replied_acts(client, tok):
    """返信の「回数」(actedログ)。1回の返信でラリー数通が閉じても回数は1。"""
    r1 = _incoming(client, "t_v204_racts", "きのうはありがとう")
    r2 = _incoming(client, "t_v204_racts", "そういえば来週あいてる?")
    d0 = client.get("/api/stats?token=tk").json()
    before = d0["days"][-1]["replied_acts"]
    rr = client.post("/api/liff/reply/act", headers=tok,
                     json={"mid": r1["id"], "action": "replied",
                           "text": "こちらこそ! 来週いいよ〜", "mids": [r1["id"], r2["id"]]})
    assert rr.status_code == 200
    d = client.get("/api/stats?token=tk").json()
    assert d["days"][-1]["replied_acts"] == before + 1   # 2通閉じても回数は1


def test_liff_track_copy_send(client, tok):
    d0 = client.get("/api/stats?token=tk").json()
    before = d0["days"][-1]["copies"]
    r = client.post("/api/liff/track", headers=tok, json={"ev": "copy_send"})
    assert r.status_code == 200 and r.json()["ok"]
    d = client.get("/api/stats?token=tk").json()
    assert d["days"][-1]["copies"] == before + 1


def test_liff_track_rejects_unknown_and_unauth(client, tok):
    assert client.post("/api/liff/track", headers=tok, json={"ev": "hack"}).status_code == 400
    assert client.post("/api/liff/track", json={"ev": "copy_send"}).status_code == 401
