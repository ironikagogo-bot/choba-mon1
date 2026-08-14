"""/api/stats・/api/web/contacts・/api/web/contact/{code} (app/main.py v184/191) のテスト。

観点:
- /api/stats のトークン認証(無し/誤り→拒否、?token=tk→200)
- /api/stats 応答の triage ブロック(urgent_notified_7d 等のキー)と全体形状
- /api/web/contacts の一覧(private除外・並び順・エントリキー)
- /api/web/contact/{code} が LIFF の contact_payload と同等キー(persona等)
- act()(POST /api/messages/{mid}/action action=deferred)で deferred_ts が記録される
- sent_replies.message_id 列の存在と record_act による紐付け

注意: 仕様書上は「トークン無しで403」だが、実コード(app/main.py:_require_ingest_token)は
INGEST_TOKEN 設定済み環境では 401 "bad token" を返す(403はトークン未設定+STRICT_AUTH時のみ)。
本テスト環境は CHOUBA_INGEST_TOKEN=tk 設定済みのため 401 を検証する。
"""
import time

from tests.conftest import mk_contact

# contact_payload(app/liff.py:586)が返すキー(= /api/web/contact の応答キー)
PAYLOAD_KEYS = {
    "ok", "code", "name", "gname", "pname", "rank", "kind", "stand", "birthday",
    "note", "flag_ero", "flag_koi", "aliases", "attrs", "profile_keys", "now_keys",
    "persona", "persona_stat", "has_talk", "arc", "dyn_block", "pstats", "rel",
    "enrich", "news", "history", "pending_facts", "review_facts", "gap_days",
}

TRIAGE_KEYS = {
    "urgent_notified_7d", "notify_reply_median_min", "notify_replied_within60m",
    "sa_neglect_max_min", "deferred_contacts", "deferred_oldest_days", "skipped_7d",
}


def _ensure_cols():
    """本番では起動時/各エンドポイントで走る linebot.ensure()(後付け列の保証)を明示的に呼ぶ。"""
    from app import linebot
    linebot.ensure()


def _incoming(client, code, text):
    r = client.post("/api/incoming", json={"contact": code, "text": text})
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ---------- /api/stats 認証 ----------

def test_stats_without_token_rejected(client):
    r = client.get("/api/stats")
    # INGEST_TOKEN設定済み環境では 401(仕様書の403はトークン未設定+STRICT_AUTH時のみ)
    assert r.status_code == 401


def test_stats_wrong_token_rejected(client):
    r = client.get("/api/stats", params={"token": "wrong"})
    assert r.status_code == 401


def test_stats_header_token_not_accepted(client):
    """/api/stats はクエリパラメータ token のみ。X-Ingest-Token ヘッダでは通らない。"""
    r = client.get("/api/stats", headers={"X-Ingest-Token": "tk"})
    assert r.status_code == 401


def test_stats_ok_with_token(client):
    r = client.get("/api/stats", params={"token": "tk"})
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    for k in ("now", "last_ingest_ts", "contacts_total", "contacts_by_kind",
              "learning_examples", "days", "triage", "hourly", "actions",
              "neglected_over_24h", "latency", "by_rank", "by_category"):
        assert k in j, f"missing top-level key: {k}"


# ---------- /api/stats 応答形状 ----------

def test_stats_triage_block_keys(client):
    j = client.get("/api/stats", params={"token": "tk"}).json()
    tri = j["triage"]
    assert TRIAGE_KEYS <= set(tri.keys()), f"triage keys missing: {TRIAGE_KEYS - set(tri.keys())}"


def test_stats_days_14_entries(client):
    j = client.get("/api/stats", params={"token": "tk"}).json()
    days = j["days"]
    assert len(days) == 14
    for k in ("date", "received", "drafted", "sent_verbatim", "sent_edited", "skipped"):
        assert k in days[-1]


def test_stats_counts_contact_and_received(client, tok):
    code = "t_statsweb_1"
    mk_contact(client, tok, code, rank="A")
    _incoming(client, code, "今日空いてる?")
    j = client.get("/api/stats", params={"token": "tk"}).json()
    assert j["contacts_total"] >= 1
    # 今日(days末尾)の受信が1件以上
    assert j["days"][-1]["received"] >= 1
    assert j["last_ingest_ts"] is not None


def test_stats_urgent_notified_counted(client, tok):
    """notified_ts が付いた受信は triage.urgent_notified_7d に数えられる。"""
    from app import db
    _ensure_cols()
    code = "t_statsweb_2"
    mk_contact(client, tok, code, rank="S")
    mid = _incoming(client, code, "至急!今日行くから席お願い")
    with db.conn() as c:
        c.execute("UPDATE messages SET notified_ts=? WHERE id=?", (time.time(), mid))
    j = client.get("/api/stats", params={"token": "tk"}).json()
    assert j["triage"]["urgent_notified_7d"] >= 1


# ---------- /api/web/contacts ----------

def test_web_contacts_lists_customer_hides_private(client, tok):
    from app import crm
    cust = "t_statsweb_3"
    priv = "t_statsweb_4"
    mk_contact(client, tok, cust, rank="A")
    mk_contact(client, tok, priv, rank="B")
    # /api/contacts/{code}/kind は private を受けない(LIFF編集経由の crm.set_kind が正規経路)
    crm.set_kind(priv, "private")
    r = client.get("/api/web/contacts")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["mode"] == "mizu"
    codes = [x["code"] for x in j["contacts"]]
    assert cust in codes
    assert priv not in codes, "kind=private は web一覧に出してはいけない"


def test_web_contacts_entry_keys_and_sort(client, tok):
    cust = "t_statsweb_5"
    staff = "t_statsweb_6"
    mk_contact(client, tok, cust, rank="B")
    mk_contact(client, tok, staff, rank="S", kind="staff")
    j = client.get("/api/web/contacts").json()
    by_code = {x["code"]: x for x in j["contacts"]}
    assert {"code", "name", "rank", "kind", "linked", "yobina", "company"} <= set(by_code[cust].keys())
    # 並び: customer が staff より先(ランクに関わらず kind 優先)
    codes = [x["code"] for x in j["contacts"]]
    assert codes.index(cust) < codes.index(staff)


# ---------- /api/web/contact/{code} ----------

def test_web_contact_not_found_404(client):
    r = client.get("/api/web/contact/t_statsweb_nonexistent")
    assert r.status_code == 404
    assert r.json()["error"] == "not found"


def test_web_contact_payload_keys(client, tok):
    code = "t_statsweb_7"
    mk_contact(client, tok, code, rank="A", note="テスト用")
    _incoming(client, code, "こんばんは")
    r = client.get(f"/api/web/contact/{code}")
    assert r.status_code == 200
    j = r.json()
    assert PAYLOAD_KEYS <= set(j.keys()), f"missing keys: {PAYLOAD_KEYS - set(j.keys())}"
    assert j["code"] == code
    # persona キーが存在(未生成なら None でよい)
    assert "persona" in j and "persona_stat" in j
    # history は received/sent/seki の3系列
    assert {"received", "sent", "seki"} <= set(j["history"].keys())
    assert any(m["text"] == "こんばんは" for m in j["history"]["received"])


def test_web_contact_same_keys_as_liff(client, tok):
    """web閲覧ビュー(v184)は LIFF カード(/api/liff/contact)と同等キー。"""
    code = "t_statsweb_8"
    mk_contact(client, tok, code, rank="B")
    rw = client.get(f"/api/web/contact/{code}")
    rl = client.get(f"/api/liff/contact/{code}", headers=tok)
    assert rw.status_code == 200 and rl.status_code == 200
    assert set(rw.json().keys()) == set(rl.json().keys())


# ---------- act() 経由の deferred_ts ----------

def test_act_deferred_records_deferred_ts(client, tok):
    from app import db
    _ensure_cols()
    code = "t_statsweb_9"
    mk_contact(client, tok, code, rank="B")
    mid = _incoming(client, code, "また今度飲みに行こうよ")
    t0 = time.time()
    r = client.post(f"/api/messages/{mid}/action", json={"action": "deferred"})
    assert r.status_code == 200 and r.json()["ok"] is True
    with db.conn() as c:
        row = c.execute("SELECT status, deferred_ts FROM messages WHERE id=?", (mid,)).fetchone()
    assert row["status"] == "deferred"
    assert row["deferred_ts"] is not None and row["deferred_ts"] >= t0 - 1


def test_act_deferred_mids_batch_all_stamped(client, tok):
    """mids で渡した同一相手の他メッセージにも deferred_ts が付く(v191#18)。"""
    from app import db
    _ensure_cols()
    code = "t_statsweb_10"
    mk_contact(client, tok, code, rank="B")
    m1 = _incoming(client, code, "1通目")
    m2 = _incoming(client, code, "2通目")
    r = client.post(f"/api/messages/{m1}/action", json={"action": "deferred", "mids": [m1, m2]})
    assert r.status_code == 200
    with db.conn() as c:
        rows = {row["id"]: row for row in c.execute(
            "SELECT id, status, deferred_ts FROM messages WHERE id IN (?,?)", (m1, m2))}
    assert rows[m1]["deferred_ts"] is not None
    assert rows[m2]["deferred_ts"] is not None
    assert rows[m2]["status"] == "deferred"


def test_stats_deferred_contact_counted(client, tok):
    """↷あとで にした相手は /api/stats triage.deferred_contacts に数えられる。"""
    _ensure_cols()
    code = "t_statsweb_11"
    mk_contact(client, tok, code, rank="B")
    mid = _incoming(client, code, "落ち着いたら返して")
    client.post(f"/api/messages/{mid}/action", json={"action": "deferred"})
    j = client.get("/api/stats", params={"token": "tk"}).json()
    assert j["triage"]["deferred_contacts"] >= 1
    assert j["triage"]["deferred_oldest_days"] >= 0


# ---------- sent_replies.message_id ----------

def test_sent_replies_has_message_id_column(client):
    from app import db
    _ensure_cols()
    with db.conn() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(sent_replies)")}
    assert "message_id" in cols
    # 学習用の後付け列もあわせて存在
    assert {"edited", "edit_ratio"} <= cols


def test_record_act_links_sent_reply_to_message(client, tok):
    """linebot.record_act(v191#18)が直近の sent_reply に message_id を紐付ける。"""
    from app import db, linebot
    _ensure_cols()
    code = "t_statsweb_12"
    mk_contact(client, tok, code, rank="A")
    mid = _incoming(client, code, "今週どこかで会える?")
    before = {mid: "open"}
    db.add_sent_reply(code, "金曜なら行けるよ〜")
    db.set_status(mid, "replied")
    linebot.record_act(mid, code, "replied", before, sr0=0, ev0=0)
    with db.conn() as c:
        row = c.execute("SELECT message_id FROM sent_replies WHERE contact=? "
                        "ORDER BY id DESC LIMIT 1", (code,)).fetchone()
    assert row["message_id"] == mid
