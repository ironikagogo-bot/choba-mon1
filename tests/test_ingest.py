"""app/deskservice.py ingest (v190/v191) のテスト。

観点:
- staff発は通知無音(キューには残る)
- 同一相手への緊急通知は15分デデュープ(linebot_meta の upush_{code})
- 通知タイトルは匿名化「帳場｜1件届いています」・本文に原文を載せない
- _BOT_SIGS(bot共鳴フィルタ)に「1件届いています」が入っている(照合句の同時更新=a-4)
- notified_ts が messages に記録される(v191 #18 計測)

規約: 直接 deskservice.ingest() を呼ぶ(=/api/incoming と同一パイプライン)。
push.notify_async / push.notify / linebot.push_urgent は monkeypatch で捕捉し
実ネットワーク・スレッドノイズを封じる。契約者コードは t_ingest_<n> 固有。
"""
import inspect
import threading
import time

import pytest

from tests.conftest import mk_contact

SECRET = "今夜9時に3名で行きたい"   # 原文(通知に漏れてはいけない文字列)
ANON_TITLE = "帳場｜1件届いています"


@pytest.fixture
def push_spy(client, monkeypatch):
    """push.notify_async / push.notify / linebot.push_urgent を記録に差し替える。
    notify_async は ingest(predraft=False) から同期で呼ばれるのでスレッド待ち不要。"""
    from app import push, linebot
    calls = {"async": [], "sync": [], "line": [], "sync_evt": threading.Event()}

    def fake_async(title, body, url="/", tag=None):
        calls["async"].append({"title": title, "body": body, "url": url, "tag": tag})

    def fake_sync(title, body, url="/", tag=None):
        calls["sync"].append({"title": title, "body": body, "url": url, "tag": tag})
        calls["sync_evt"].set()
        return 0

    monkeypatch.setattr(push, "notify_async", fake_async)
    monkeypatch.setattr(push, "notify", fake_sync)
    monkeypatch.setattr(linebot, "push_urgent", lambda contact, reason: calls["line"].append(contact) or False)
    return calls


def _notified_ts(mid):
    from app import db
    with db.conn() as c:
        r = c.execute("SELECT notified_ts FROM messages WHERE id=?", (mid,)).fetchone()
    return r["notified_ts"] if r else None


def _upush_meta(code):
    from app import linebot
    return linebot._meta_get(f"upush_{code}")


# ---------- 匿名化通知 ----------

def test_urgent_notify_title_anonymized(client, tok, push_spy):
    """即対応の通知タイトルは「帳場｜1件届いています」固定(客名を載せない)。"""
    from app import deskservice
    mk_contact(client, tok, "t_ingest_1")
    mid, cat, reason = deskservice.ingest("t_ingest_1", "至急、" + SECRET)
    assert cat == "urgent"
    assert len(push_spy["async"]) == 1
    n = push_spy["async"][0]
    assert n["title"] == ANON_TITLE
    assert n["tag"] == f"msg-{mid}"


def test_urgent_notify_body_has_no_raw_text(client, tok, push_spy):
    """通知の本文・タイトルに受信原文も客名も載せない(ロック画面覗き見対策)。"""
    from app import deskservice
    mk_contact(client, tok, "t_ingest_2")
    deskservice.ingest("t_ingest_2", "至急!" + SECRET)
    n = push_spy["async"][0]
    blob = (n["title"] or "") + (n["body"] or "")
    assert SECRET not in blob
    assert "t_ingest_2" not in blob
    assert n["body"] == "タップして確認"


def test_predraft_notify_is_also_anonymized(client, tok, push_spy, monkeypatch):
    """デスク経由(predraft=True)は下書き生成→push.notify。こちらも匿名タイトル・原文なし。"""
    from app import deskservice, drafts
    gen = []
    monkeypatch.setattr(drafts, "generate", lambda mid: gen.append(mid) or ["案1"])
    mk_contact(client, tok, "t_ingest_3")
    mid, cat, _ = deskservice.ingest("t_ingest_3", "至急お願い、" + SECRET, predraft=True)
    assert cat == "urgent"
    assert push_spy["sync_evt"].wait(10), "predraftスレッドからの通知が来ない"
    n = push_spy["sync"][0]
    assert n["title"] == ANON_TITLE
    assert SECRET not in (n["title"] + n["body"])
    assert n["tag"] == f"msg-{mid}"
    assert gen == [mid]   # 通知の前に下書きが生成されている


# ---------- notified_ts 記録(v191 #18 計測) ----------

def test_notified_ts_recorded_on_urgent(client, tok, push_spy):
    from app import deskservice
    mk_contact(client, tok, "t_ingest_4")
    t0 = time.time()
    mid, cat, _ = deskservice.ingest("t_ingest_4", "至急連絡ください")
    assert cat == "urgent"
    nts = _notified_ts(mid)
    assert nts is not None
    assert t0 - 1 <= nts <= time.time() + 1


def test_notified_ts_absent_on_batch(client, tok, push_spy):
    """batch(通知しない)では notified_ts は残らない。"""
    from app import deskservice
    mk_contact(client, tok, "t_ingest_5")
    mid, cat, _ = deskservice.ingest("t_ingest_5", "こんにちは")
    assert cat == "batch"
    assert _notified_ts(mid) is None
    assert push_spy["async"] == [] and push_spy["sync"] == []


# ---------- 15分デデュープ(upush_{code} meta) ----------

def test_upush_meta_set_on_first_urgent(client, tok, push_spy):
    from app import deskservice
    mk_contact(client, tok, "t_ingest_6")
    deskservice.ingest("t_ingest_6", "至急です")
    v = _upush_meta("t_ingest_6")
    assert v, "upush_{code} メタが記録されていない"
    assert abs(float(v) - time.time()) < 10


def test_dedup_second_urgent_within_15min_is_silent(client, tok, push_spy):
    """15分以内の同一相手の2通目urgentは通知しない(枠を守る)。キュー・分類はそのまま。"""
    from app import deskservice
    mk_contact(client, tok, "t_ingest_7")
    logs = []
    mid1, cat1, _ = deskservice.ingest("t_ingest_7", "至急です", log=logs.append)
    meta_after_first = _upush_meta("t_ingest_7")
    mid2, cat2, _ = deskservice.ingest("t_ingest_7", "至急!早く返事を", log=logs.append)
    assert cat1 == "urgent" and cat2 == "urgent"   # ラリー内でもurgent昇格(v190 #11)
    assert len(push_spy["async"]) == 1              # 通知は1通目だけ
    assert any("15分以内に通知済み" in m for m in logs)
    assert _notified_ts(mid2) is None               # 通知していない2通目には計測時刻なし
    assert _upush_meta("t_ingest_7") == meta_after_first  # metaは1通目のまま(上書きしない)


def test_dedup_releases_after_900_seconds(client, tok, push_spy):
    """upush メタが900秒より古ければ再び通知する(メタも更新される)。"""
    from app import deskservice, linebot
    mk_contact(client, tok, "t_ingest_8")
    deskservice.ingest("t_ingest_8", "至急です")
    assert len(push_spy["async"]) == 1
    old = time.time() - 901
    linebot._meta_set("upush_t_ingest_8", str(old))
    deskservice.ingest("t_ingest_8", "至急、もう一度")
    assert len(push_spy["async"]) == 2
    assert float(_upush_meta("t_ingest_8")) > old + 900 - 60   # 現在時刻で更新


# ---------- staff無音(v190 #12) ----------

def test_staff_urgent_is_silent(client, tok, push_spy):
    """店内(staff)発は urgent 判定でも通知しない。"""
    from app import deskservice
    mk_contact(client, tok, "t_ingest_9", kind="staff")
    logs = []
    mid, cat, _ = deskservice.ingest("t_ingest_9", "至急、店に電話して", log=logs.append)
    assert cat == "urgent"
    assert push_spy["async"] == [] and push_spy["sync"] == []
    assert any("店内の連絡は無音" in m for m in logs)
    assert _notified_ts(mid) is None
    assert _upush_meta("t_ingest_9") == ""   # デデュープ枠も消費しない


def test_staff_message_stays_in_queue(client, tok, push_spy):
    """staff無音でもメッセージ自体はキュー(open)に残る。"""
    from app import deskservice, db
    mk_contact(client, tok, "t_ingest_10", kind="staff")
    mid, cat, _ = deskservice.ingest("t_ingest_10", "至急、確認お願い")
    opens = db.open_for_contact("t_ingest_10")
    assert [m["id"] for m in opens] == [mid]
    assert opens[0]["category"] == "urgent"


def test_customer_line_push_fires_but_staff_does_not(client, tok, push_spy):
    """LINEチャット経路(push_urgent)も staff では呼ばれず、customer では呼ばれる。"""
    from app import deskservice
    mk_contact(client, tok, "t_ingest_11", kind="staff")
    mk_contact(client, tok, "t_ingest_12")
    deskservice.ingest("t_ingest_11", "至急です")
    deskservice.ingest("t_ingest_12", "至急です")
    deadline = time.time() + 5   # push_urgentは別スレッド
    while time.time() < deadline and "t_ingest_12" not in push_spy["line"]:
        time.sleep(0.05)
    assert "t_ingest_12" in push_spy["line"]
    assert "t_ingest_11" not in push_spy["line"]


# ---------- 未登録相手は通知しない ----------

def test_unknown_contact_urgent_no_push(client, tok, push_spy):
    """未登録(unknown)はurgent判定でも通知せず、notified_tsも残らない。"""
    from app import deskservice
    mid, cat, _ = deskservice.ingest("t_ingest_unknown_zz13", "至急連絡ください")
    assert cat == "urgent"
    assert push_spy["async"] == [] and push_spy["sync"] == []
    assert _notified_ts(mid) is None
    assert _upush_meta("t_ingest_unknown_zz13") == ""


# ---------- _BOT_SIGS 共鳴フィルタ(a-4: 匿名通知文言との同時更新) ----------

def test_bot_sigs_contains_anonymous_phrase():
    """_BOT_SIGS(android_ingest内ローカル定数)に匿名通知文言「1件届いています」が
    含まれている(push_urgent側の文言と同時更新必須=a-4)。ソース照合で検証。"""
    from app import deskservice
    src = inspect.getsource(deskservice.DeskService.android_ingest)
    assert "_BOT_SIGS" in src
    sig_block = src.split("_BOT_SIGS", 1)[1].split(")", 1)[0]
    assert "1件届いています" in sig_block


def test_android_ingest_ignores_bot_echo_message(client, tok, push_spy):
    """「1件届いています」を含む通知(帳場くん自身のpush文の再取り込み)は ignored-bot。"""
    r = client.post("/api/android/notify", json={
        "token": "tk", "package": "jp.naver.line.android",
        "sender": "t_ingest_14", "text": "🔥 1件届いています。"})
    assert r.status_code == 200
    assert r.json()["status"] == "ignored-bot"
    assert push_spy["async"] == [] and push_spy["sync"] == []


def test_android_ingest_ignores_chouba_named_contact(client, tok, push_spy):
    """相手名に「帳場」を含む通知(別インスタンスOA等)は取り込まない。"""
    r = client.post("/api/android/notify", json={
        "token": "tk", "package": "jp.naver.line.android",
        "sender": "Michiの帳場", "text": "おはようございます"})
    assert r.status_code == 200
    assert r.json()["status"] == "ignored-bot"


# ---------- /api/incoming 経由(同一パイプライン)の疎通 ----------

def test_api_incoming_uses_same_pipeline(client, tok, push_spy):
    """/api/incoming → deskservice.ingest(predraft=False)。匿名通知+notified_ts。"""
    mk_contact(client, tok, "t_ingest_15")
    r = client.post("/api/incoming", json={"contact": "t_ingest_15", "text": "至急、" + SECRET})
    assert r.status_code == 200
    body = r.json()
    assert body["category"] == "urgent"
    assert len(push_spy["async"]) == 1
    assert push_spy["async"][0]["title"] == ANON_TITLE
    assert SECRET not in push_spy["async"][0]["body"]
    assert _notified_ts(body["id"]) is not None
