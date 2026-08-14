"""トリアージ本体(v190/191)のテスト。

対象:
- app/liff.py: /api/liff/reply/act (liff_reply_act) / /api/liff/reply/undo / /api/liff/inbox
- app/linebot.py: acted_log(ensure) / build_queue / record_act

観点:
- act(sent=replied / skip=skipped / later=deferred)で acted_log に行が入る(changed JSON・act_id返却)
- undo で status・sent_replies・仮イベント・文体サンプルが巻き戻る
- 後続actがある場合の undo は該当midを巻き戻さない(#6 整合ガード)
- 一括あとで(mids一括)+deferred_ts 記録(#15)
- 非open(裁定済み)への再actが副作用(仮イベント・文体学習)を繰り返さない(#13)
- recent_acted が JST朝5時起点の「同夜」だけを返す(#16)/acted_n キー(#20)
- build_queue が staff を除外しない(v189)

規約: メッセージは db.add_message 直挿しで category/reason を確定させる
(deskservice.ingest のラリー判定・通知に依存しない)。契約者コードは t_tu_<n>。
"""
import json
import time

from tests.conftest import mk_contact


# ---------- ヘルパ ----------

def _add_msg(contact, text, category="batch", reason="", ts=None):
    from app import db
    return db.add_message(contact, text, category, reason, ts=ts)


def _act(client, tok, mid, action, text="", mids=None):
    body = {"mid": mid, "action": action, "text": text}
    if mids is not None:
        body["mids"] = mids
    return client.post("/api/liff/reply/act", json=body, headers=tok)


def _acted_row(act_id):
    from app import db
    with db.conn() as c:
        r = c.execute("SELECT * FROM acted_log WHERE act_id=?", (act_id,)).fetchone()
        return dict(r) if r else None


def _msg_status(mid):
    from app import db
    return (db.get_message(mid) or {}).get("status")


# ---------- act → acted_log ----------

def test_act_replied_records_acted_log_and_links_sent_reply(client, tok):
    """sent(replied)で acted_log に行が入る: changed=[[mid,'open']]・act_id返却・
    sent_replies 行が message_id で受信に紐付く(v191#18)。"""
    from app import db, linebot
    code = "t_tu_1"
    mk_contact(client, tok, code)
    mid = _add_msg(code, "きょうもおつかれさま")
    r = _act(client, tok, mid, "replied", text="ありがとう、また来てね t_tu_1")
    assert r.status_code == 200
    j = r.json()
    assert j.get("ok") is True
    act_id = j.get("act_id")
    assert isinstance(act_id, int)
    row = _acted_row(act_id)
    assert row is not None
    assert row["contact"] == code
    assert row["action"] == "replied"
    assert row["undone"] == 0
    changed = json.loads(row["changed"])
    assert [mid, "open"] in [[int(a), b] for a, b in changed]
    assert _msg_status(mid) == "replied"
    # 文体学習で増えた sent_replies 行が acted_log に紐付き、message_id=mid が入る
    assert row["sent_reply_id"]
    with db.conn() as c:
        sr = c.execute("SELECT * FROM sent_replies WHERE id=?",
                       (row["sent_reply_id"],)).fetchone()
    assert sr is not None
    assert sr["contact"] == code
    assert sr["message_id"] == mid
    assert row["sent_text"] == "ありがとう、また来てね t_tu_1"
    linebot.ensure()  # 存在確認のみ(冪等)


def test_act_skipped_records_acted_log(client, tok):
    """skip(skipped)でも acted_log に行が入り act_id が返る。sent_reply紐付けは無し。"""
    code = "t_tu_2"
    mk_contact(client, tok, code)
    mid = _add_msg(code, "スタンプだけ")
    r = _act(client, tok, mid, "skipped")
    assert r.status_code == 200
    act_id = r.json().get("act_id")
    assert isinstance(act_id, int)
    row = _acted_row(act_id)
    assert row["action"] == "skipped"
    assert not row["sent_reply_id"]
    assert _msg_status(mid) == "skipped"


def test_bulk_later_mids_and_deferred_ts(client, tok):
    """一括あとで(#15): mids 指定で兄弟メッセージも deferred になり、
    全midに deferred_ts が記録される。acted_log.changed は両midを含む。"""
    from app import db
    code = "t_tu_3"
    mk_contact(client, tok, code)
    m1 = _add_msg(code, "1通目", ts=time.time() - 120)
    m2 = _add_msg(code, "2通目")
    r = _act(client, tok, m1, "deferred", mids=[m1, m2])
    assert r.status_code == 200
    act_id = r.json().get("act_id")
    assert isinstance(act_id, int)
    assert _msg_status(m1) == "deferred"
    assert _msg_status(m2) == "deferred"
    with db.conn() as c:
        for m in (m1, m2):
            row = c.execute("SELECT deferred_ts FROM messages WHERE id=?", (m,)).fetchone()
            assert row["deferred_ts"], f"deferred_ts missing for mid={m}"
    changed_mids = {int(a) for a, _ in json.loads(_acted_row(act_id)["changed"])}
    assert {m1, m2} <= changed_mids


# ---------- undo ----------

def test_undo_rolls_back_status_sent_reply_event_and_style(client, tok):
    """undoで status復帰・sent_replies削除・仮イベント削除・文体サンプル除去・undone=1。"""
    from app import db
    code = "t_tu_4"
    mk_contact(client, tok, code)
    mid = _add_msg(code, "今日これから行っていい?", category="urgent", reason="来店の申し出")
    sent = "うれしい、待ってるね t_tu_4"
    r = _act(client, tok, mid, "replied", text=sent)
    assert r.status_code == 200
    act_id = r.json()["act_id"]
    row = _acted_row(act_id)
    # 仮イベント(来店(仮))と sent_reply が act で生まれている
    assert row["event_id"], "tentative visit event not linked"
    assert row["sent_reply_id"]
    prof = db.get_profile(code) or {}
    assert sent in (prof.get("my_samples_to_them") or [])
    # undo
    r2 = client.post("/api/liff/reply/undo", json={"act_id": act_id}, headers=tok)
    assert r2.status_code == 200
    assert r2.json().get("ok") is True
    assert _msg_status(mid) == "open"
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM sent_replies WHERE id=?",
                         (row["sent_reply_id"],)).fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM events WHERE id=?",
                         (row["event_id"],)).fetchone()[0] == 0
    assert _acted_row(act_id)["undone"] == 1
    prof2 = db.get_profile(code) or {}
    assert sent not in (prof2.get("my_samples_to_them") or [])


def test_undo_unknown_and_double_undo_404(client, tok):
    """存在しない/undo済みの act_id は404。"""
    code = "t_tu_5"
    mk_contact(client, tok, code)
    r = client.post("/api/liff/reply/undo", json={"act_id": 99999999}, headers=tok)
    assert r.status_code == 404
    mid = _add_msg(code, "テスト")
    act_id = _act(client, tok, mid, "skipped").json()["act_id"]
    assert client.post("/api/liff/reply/undo", json={"act_id": act_id},
                       headers=tok).status_code == 200
    # 2回目は undone=1 なので404
    assert client.post("/api/liff/reply/undo", json={"act_id": act_id},
                       headers=tok).status_code == 404


def test_undo_overlap_guard_keeps_later_act(client, tok):
    """#6 整合ガード: ↷→返信 のあとで最初の↷をundoしても、後続裁定に含まれるmidは
    巻き戻らない(返信済みがopen復活しない)。後の裁定をundoすれば通常どおり戻る。"""
    code = "t_tu_6"
    mk_contact(client, tok, code)
    mid = _add_msg(code, "また連絡するね")
    act1 = _act(client, tok, mid, "deferred").json()["act_id"]
    assert _msg_status(mid) == "deferred"
    act2 = _act(client, tok, mid, "replied").json()["act_id"]
    assert _msg_status(mid) == "replied"
    assert act2 > act1
    # act1 の undo: okは返るが status は replied のまま(open復活しない)
    r = client.post("/api/liff/reply/undo", json={"act_id": act1}, headers=tok)
    assert r.status_code == 200
    assert _msg_status(mid) == "replied"
    # act2 の undo: 通常どおり act2 直前の状態(deferred)へ戻る
    r2 = client.post("/api/liff/reply/undo", json={"act_id": act2}, headers=tok)
    assert r2.status_code == 200
    assert _msg_status(mid) == "deferred"


# ---------- 再act(#13) ----------

def test_react_on_closed_message_has_no_side_effects(client, tok):
    """#13: 裁定済みmidへの再act(二重タップ・再送)で仮イベント・文体学習が増えない。
    差分ゼロなので acted_log にも行が増えず act_id は null。"""
    from app import db
    code = "t_tu_7"
    mk_contact(client, tok, code)
    mid = _add_msg(code, "予約できる?", category="urgent", reason="来店・席の確認")
    assert _act(client, tok, mid, "replied", text="お席とっておくね").status_code == 200

    def counts():
        with db.conn() as c:
            ev = c.execute("SELECT COUNT(*) FROM events WHERE contact=?", (code,)).fetchone()[0]
            sr = c.execute("SELECT COUNT(*) FROM sent_replies WHERE contact=?",
                           (code,)).fetchone()[0]
            al = c.execute("SELECT COUNT(*) FROM acted_log WHERE contact=?",
                           (code,)).fetchone()[0]
        return ev, sr, al

    ev1, sr1, al1 = counts()
    assert ev1 == 1 and sr1 == 1 and al1 == 1
    # 同じmidへ再act(別テキストを送っても学習・イベントは繰り返さない)
    r2 = _act(client, tok, mid, "replied", text="こんどは別の文")
    assert r2.status_code == 200
    assert r2.json().get("act_id") is None   # 差分なし=undo対象なし
    ev2, sr2, al2 = counts()
    assert (ev2, sr2, al2) == (ev1, sr1, al1)
    assert _msg_status(mid) == "replied"


# ---------- inbox: recent_acted / acted_n ----------

def _night_start(now=None):
    """liff_inbox と同じ「同夜=直近のJST朝5時」境界。"""
    now = now or time.time()
    jst = now + 9 * 3600
    day0 = (jst // 86400) * 86400
    ns = (day0 + 5 * 3600) - 9 * 3600
    if jst % 86400 < 5 * 3600:
        ns -= 86400
    return ns


def test_recent_acted_same_night_only_and_acted_n(client, tok):
    """#16/#19/#20: recent_acted は同夜(JST朝5時起点)・undone=0 のみ。
    acted_n は同夜の COUNT(DISTINCT contact)(前夜分・undone分は数えない)。"""
    from app import db, linebot
    linebot.ensure()
    code_new = "t_tu_8a"
    code_old = "t_tu_8b"
    code_und = "t_tu_8c"
    for c in (code_new, code_old, code_und):
        mk_contact(client, tok, c)
    n0 = client.get("/api/liff/inbox", headers=tok).json()["acted_n"]
    now = time.time()
    old_ts = _night_start(now) - 3600   # 境界の1時間前=前夜
    with db.conn() as c:
        def ins(contact, ts, undone=0):
            return c.execute(
                "INSERT INTO acted_log(contact,action,changed,undone,acted_ts) "
                "VALUES(?,?,?,?,?)", (contact, "skipped", "[]", undone, ts)).lastrowid
        aid_new1 = ins(code_new, now)
        aid_new2 = ins(code_new, now)          # 同一相手2裁定 → 人数は1
        aid_old = ins(code_old, old_ts)        # 前夜 → 出ない
        aid_und = ins(code_und, now, undone=1) # undo済み → 出ない
    j = client.get("/api/liff/inbox", headers=tok).json()
    ids = {x["act_id"] for x in j["recent_acted"]}
    assert aid_new1 in ids and aid_new2 in ids
    assert aid_old not in ids
    assert aid_und not in ids
    # 完走人数: 同夜の新規1人分だけ増える(DISTINCT contact)
    assert j["acted_n"] == n0 + 1
    # recent_acted の要素形(act_id/name/action/sent/tm)
    mine = next(x for x in j["recent_acted"] if x["act_id"] == aid_new1)
    assert mine["action"] == "skipped"
    assert mine["sent"] is False
    assert code_new in mine["name"]
    assert ":" in mine["tm"]


def test_inbox_response_has_acted_keys(client, tok):
    """inbox 応答に recent_acted / acted_n / items / koi_patterns キーがある。"""
    r = client.get("/api/liff/inbox", headers=tok)
    assert r.status_code == 200
    j = r.json()
    assert j.get("ok") is True
    for k in ("items", "koi_patterns", "recent_acted", "acted_n"):
        assert k in j, f"missing key: {k}"
    assert isinstance(j["recent_acted"], list)
    assert isinstance(j["acted_n"], int)


# ---------- build_queue(v189: staff除外しない) ----------

def test_build_queue_includes_staff(client, tok):
    """v189: 店内(staff)の未対応も build_queue・inbox の両方に出る(消えない)。"""
    from app import linebot
    code = "t_tu_9"
    mk_contact(client, tok, code, kind="staff")
    mid = _add_msg(code, "あしたのシフトの件")
    q = linebot.build_queue()
    it = next((x for x in q if x["contact"] == code), None)
    assert it is not None, "staff contact excluded from build_queue"
    assert it["kind"] == "staff"
    assert mid in it["mids"]
    assert it["koi"] == 0   # koiフラグはcustomer限定
    j = client.get("/api/liff/inbox", headers=tok).json()
    card = next((x for x in j["items"] if x["contact"] == code), None)
    assert card is not None, "staff card missing from inbox"
    assert card["kind"] == "staff"
    # 後片付け(他テストのキュー汚染防止)
    _act(client, tok, mid, "skipped")


# ---------- エラー系・認証 ----------

def test_act_error_codes(client, tok):
    """存在しないmid=404 / 不正action=400(main.actのHTTPExceptionがそのまま返る)。"""
    code = "t_tu_10"
    mk_contact(client, tok, code)
    r = _act(client, tok, 99999999, "replied")
    assert r.status_code == 404
    mid = _add_msg(code, "テスト")
    r2 = _act(client, tok, mid, "banzai")
    assert r2.status_code == 400
    assert _msg_status(mid) == "open"   # 失敗actでは状態が動かない
    _act(client, tok, mid, "skipped")   # 後片付け


def test_liff_triage_endpoints_require_token(client):
    """inbox / act / undo はヘッダ X-Ingest-Token 無しで401。"""
    assert client.get("/api/liff/inbox").status_code == 401
    assert client.post("/api/liff/reply/act",
                       json={"mid": 1, "action": "skipped"}).status_code == 401
    assert client.post("/api/liff/reply/undo", json={"act_id": 1}).status_code == 401
