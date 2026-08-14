"""v220: グループ受信の根治 — リーダー0.6.x系の「sender=グループ名: 送信者」複合形式を
確定グループとして解釈(2026-08-13 aki-test実ログで形式確定)。
"""


def test_sender_composite_group_marked(client):
    """sender=「焼肉大好き: Kento」group無し → contact=Kento・本文に【焼肉大好き】印。"""
    from app import db
    r = client.post("/api/android/notify", params={
        "token": "tk", "title": "焼肉大好き", "text": "これは子供喜びますね!",
        "sender": "焼肉大好き: Kento"})
    assert r.status_code == 200
    d = r.json()
    assert d.get("status") in ("ingested", "trayed") or d.get("contact") or d.get("ok", True)
    with db.conn() as c:
        row = c.execute("SELECT contact, text FROM messages ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    assert "【焼肉大好き】" in row["text"]
    assert "Kento" in row["contact"] and "焼肉大好き" not in row["contact"]


def test_sender_roster_group_generic_mark(client):
    """メンバー列挙型(名無しグループ)は総称【グループ】(名前が不安定なため)。"""
    from app import db
    r = client.post("/api/android/notify", params={
        "token": "tk", "title": "Eri, Sangeetha Roka, Aki",
        "text": "Ok", "sender": "Eri, Sangeetha Roka, Aki: Sangeetha Roka"})
    assert r.status_code == 200
    with db.conn() as c:
        row = c.execute("SELECT contact, text FROM messages ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    assert row["text"].startswith("【グループ】")
    assert "Sangeetha" in row["contact"]


def test_sender_plain_not_marked(client):
    """コロン無しの通常sender(1対1)は従来どおり無印。"""
    from app import db
    r = client.post("/api/android/notify", params={
        "token": "tk", "title": "山田太郎", "text": "こんばんは", "sender": "山田太郎"})
    assert r.status_code == 200
    with db.conn() as c:
        row = c.execute("SELECT contact, text FROM messages ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None and "【" not in row["text"] and row["contact"] == "山田太郎"


def test_fixup_rank_optional_for_staff(client, tok):
    """v220: 非顧客はランク未指定でも確定できる(既定B)。顧客は必須のまま。"""
    from app import db
    db.upsert_contact("t_v220_st", "B")
    r = client.post("/api/liff/fixup/save", headers=tok,
                    json={"code": "t_v220_st", "呼び名": "みほ", "kind": "staff", "stand": "down"})
    assert r.status_code == 200 and r.json().get("ok")
    assert (db.get_contact("t_v220_st") or {}).get("rank") == "B"
    db.upsert_contact("t_v220_cu", "B")
    r2 = client.post("/api/liff/fixup/save", headers=tok,
                     json={"code": "t_v220_cu", "呼び名": "こう", "kind": "customer",
                           "stand": "even", "rank": ""})
    assert r2.status_code == 400   # 顧客はランク必須のまま


def test_v222_missed_call_becomes_message(client):
    """不在着信(sender=LINE不在着信&text=相手名)→「LINEさん」カードでなく相手カードに📞。"""
    from app import db
    r = client.post("/api/android/notify", params={
        "token": "tk", "sender": "LINE不在着信", "text": "AKO"})
    assert r.status_code == 200
    with db.conn() as c:
        row = c.execute("SELECT contact, text, category FROM messages ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    assert row["contact"] == "AKO"
    assert "不在着信" in row["text"]
    assert row["category"] == "urgent"   # 折り返し待ち=急ぎ
    with db.conn() as c:
        bad = c.execute("SELECT 1 FROM contacts WHERE code IN ('LINE', 'LINE不在着信')").fetchone()
    assert bad is None


def test_v222_ongoing_call_still_ignored(client):
    """着信中・通話中の過渡通知は従来どおり捨てる。"""
    from app import db
    with db.conn() as c:
        n0 = c.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
    client.post("/api/android/notify", params={"token": "tk", "sender": "LINE着信中", "text": "AKO"})
    with db.conn() as c:
        n1 = c.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
    assert n1 == n0
