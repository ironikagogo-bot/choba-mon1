"""app/news.py (ネタ帳 v183/v191) のテスト。

観点:
- _canon 正規化(_STOP/_SUFFIX/_SYN・数字拒否・長さ・NFKC)
- _interest_index が 趣味・関心/好きなお酒/好きな食べ物 から引ける(NG話題除外・非customer除外)
- _ng_hit(正規形照合・埋め込み語) / _neg(_NEG_WORDS)
- 会社ネタのネガ見出し = caution=1(見せるが送らせない) + hash重複ガード
- k>=2 ルールと solo 条件(rank S/A or 31日以内の来店)
- フェーズ予算 _BUDGETS / _fetch_budgeted(枠切れ=None・失敗も1消費)
- クールダウン(mark_used=3日 / dismiss=7日, max延長)
- list_items の3日失効(paper=7日)+30日DELETE+◯◯プレースホルダ非表示化
- who dict形式 / 旧list形式の両読み(who_codes)
- last_day 判定に kwフェーズの成否を含める(v191 #17) → 別プロセス(fresh DB)で検証

規約: _fetch_rss は必ずモック。アプリコードは変更しない。契約者コードは t_news_* 固定。
"""
import hashlib
import json
import time

import pytest

from tests.conftest import mk_contact, run_in_mode


def _news():
    from app import news
    return news


def _item(title, ts=None, link="https://example.com/a"):
    return {"title": title, "link": link,
            "ts": time.time() if ts is None else ts, "src_url": "https://www.nikkei.com/x"}


def _ins_item(title, kw="", who="", tier="", created_ts=None, dismissed=0,
              opener="", contact="", used_ts=0, caution=0):
    """news_items へ直接1行入れる(ヘルパ。hashはタイトル+kwで一意化)。戻り=id"""
    news = _news()
    news.ensure()
    from app import db
    h = hashlib.sha1(("t_news|" + kw + "|" + title).encode("utf-8")).hexdigest()
    with db.conn() as c:
        c.execute(
            "INSERT INTO news_items(contact,company,title,link,opener,hash,created_ts,"
            "dismissed,kw,who,tier,used_ts,caution) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (contact, "", title, "https://example.com/x", opener, h,
             time.time() if created_ts is None else created_ts,
             dismissed, kw, who, tier, used_ts, caution))
        return c.execute("SELECT id FROM news_items WHERE hash=?", (h,)).fetchone()["id"]


# ---------- _canon ----------

def test_canon_normalization(client):
    news = _news()
    # _SUFFIX 剥がし
    assert news._canon("野球観戦") == "野球"
    assert news._canon("ワイン巡り") == "ワイン"
    # _SUFFIX 剥がし → _SYN 併合の連鎖
    assert news._canon("大相撲観戦") == "相撲"
    # _SYN(表記ゆれ併合・1文字趣味の救済は長さ判定より先)
    assert news._canon("打ちっぱなし") == "ゴルフ"
    assert news._canon("獺祭") == "日本酒"
    assert news._canon("車") == "クルマ"
    assert news._canon("鰻") == "うなぎ"
    # _STOP
    assert news._canon("好き") == ""
    assert news._canon("なんでも") == ""
    assert news._canon("グルメ") == ""
    # 数字入りトークン=頻度メモと判定して棄却(全角数字もNFKCで拾う)
    assert news._canon("週3") == ""
    assert news._canon("ゴルフ月1") == ""
    assert news._canon("週３") == ""
    # 長さ境界: 1文字(辞書外)と13文字は棄却
    assert news._canon("山") == ""
    assert news._canon("あ" * 13) == ""
    assert news._canon("あ" * 12) == "あ" * 12
    # 空・NFKC(半角カナ)
    assert news._canon("") == ""
    assert news._canon("ｺﾞﾙﾌ") == "ゴルフ"


# ---------- _ng_hit / _neg ----------

def test_ng_hit(client):
    news = _news()
    # 素の包含
    assert news._ng_hit("ゴルフ", "ゴルフの話") is True
    # 正規形同士の照合: 「阪神」NG × 正規形「野球」(_SYN埋め込み語照合)
    assert news._ng_hit("野球", "阪神の話はNG") is True
    # 分割トークンの正規化照合
    assert news._ng_hit("ワイン", "赤ワイン、政治") is True
    # 無関係は素通り
    assert news._ng_hit("寿司", "政治、宗教") is False
    assert news._ng_hit("野球", "") is False
    assert news._ng_hit("", "野球") is False


def test_neg_words(client):
    news = _news()
    assert news._neg("A社の社長が退任へ") is True
    assert news._neg("B社で食中毒が発生") is True
    assert news._neg("C社が新製品を発表") is False
    assert news._neg("") is False


# ---------- _interest_index ----------

def test_interest_index_fields_and_exclusions(client, tok):
    news = _news()
    from app import crm
    mk_contact(client, tok, "t_news_i1", rank="S")
    crm.set_attr("t_news_i1", "趣味・関心", "薪割り、打ちっぱなし")
    mk_contact(client, tok, "t_news_i2")
    crm.set_attr("t_news_i2", "好きなお酒", "獺祭")
    crm.set_attr("t_news_i2", "好きな食べ物", "薪窯ピザ")
    # NG話題該当者は個人単位で who から除外
    mk_contact(client, tok, "t_news_i3")
    crm.set_attr("t_news_i3", "趣味・関心", "薪割り")
    crm.set_attr("t_news_i3", "NG話題", "薪割り")
    # customer以外はインデックス対象外
    mk_contact(client, tok, "t_news_i4", kind="peer")
    crm.set_attr("t_news_i4", "趣味・関心", "薪割り")

    idx = news._interest_index()
    # 趣味・関心: 正規形キー + who={code: 元の記載語}
    assert idx["薪割り"]["who"].get("t_news_i1") == "薪割り"
    assert idx["薪割り"]["field"] == "趣味・関心"
    assert idx["ゴルフ"]["who"].get("t_news_i1") == "打ちっぱなし"
    # 好きなお酒(_SYNで正規化) / 好きな食べ物
    assert idx["日本酒"]["who"].get("t_news_i2") == "獺祭"
    assert idx["薪窯ピザ"]["who"].get("t_news_i2") == "薪窯ピザ"
    assert idx["薪窯ピザ"]["field"] == "好きな食べ物"
    # 除外の確認
    assert "t_news_i3" not in idx["薪割り"]["who"]   # NG話題
    assert "t_news_i4" not in idx["薪割り"]["who"]   # kind=peer


# ---------- _solo_ok(rank S/A or 31日以内の来店) ----------

def test_solo_ok_rank_and_sitting(client, tok):
    news = _news()
    from app import db, sittings
    sittings.ensure()
    mk_contact(client, tok, "t_news_s1", rank="S")
    mk_contact(client, tok, "t_news_s2", rank="A")
    mk_contact(client, tok, "t_news_s3", rank="B")
    mk_contact(client, tok, "t_news_s4", rank="B")
    mk_contact(client, tok, "t_news_s5", rank="B")
    mk_contact(client, tok, "t_news_s6", rank="B")
    assert news._solo_ok("t_news_s1") is True
    assert news._solo_ok("t_news_s2") is True
    assert news._solo_ok("t_news_s3") is False   # rank B・来店なし
    now = time.time()
    with db.conn() as c:
        # s4: 最近の席の主賓
        c.execute("INSERT INTO sittings(date_label, main_contact, created_ts) VALUES(?,?,?)",
                  ("t", "t_news_s4", now - 5 * 86400))
        # s5: 最近の席のメンバー
        c.execute("INSERT INTO sittings(date_label, main_contact, created_ts) VALUES(?,?,?)",
                  ("t", "t_news_zz", now - 5 * 86400))
        sid = c.execute("SELECT id FROM sittings WHERE main_contact='t_news_zz'").fetchone()["id"]
        c.execute("INSERT INTO sitting_members(sitting_id, contact, role) VALUES(?,?,?)",
                  (sid, "t_news_s5", "customer"))
        # s6: 31日より古い席の主賓 → 対象外
        c.execute("INSERT INTO sittings(date_label, main_contact, created_ts) VALUES(?,?,?)",
                  ("t", "t_news_s6", now - 40 * 86400))
    assert news._solo_ok("t_news_s4") is True
    assert news._solo_ok("t_news_s5") is True
    assert news._solo_ok("t_news_s6") is False


# ---------- フェーズ予算 ----------

def test_budgets_and_fetch_budgeted(client, monkeypatch):
    news = _news()
    assert news._BUDGETS == {"company": 24, "group": 8, "cursor": 5}
    calls = []
    monkeypatch.setattr(news, "_fetch_rss",
                        lambda query, require_in_title="": calls.append(query) or [])
    # 枠切れ = None(fetchは呼ばれない)
    b = {"company": 0}
    assert news._fetch_budgeted(b, "company", "q") is None
    assert calls == []
    # 正常消費
    b = {"company": 2}
    assert news._fetch_budgeted(b, "company", "q") == []
    assert b["company"] == 1
    # 失敗フェッチも予算1として消費(例外は伝播)
    def boom(query, require_in_title=""):
        raise RuntimeError("net down")
    monkeypatch.setattr(news, "_fetch_rss", boom)
    with pytest.raises(RuntimeError):
        news._fetch_budgeted(b, "company", "q")
    assert b["company"] == 0


# ---------- クールダウン ----------

def test_cooldown_mark_used_3days(client):
    news = _news()
    from app import db
    nid = _ins_item("薪窯速報A", kw="t_news_kwA", contact="t_news_cA")   # v202掃除対象にしない
    now = time.time()
    news.mark_used(nid)
    with db.conn() as c:
        r = c.execute("SELECT used_ts FROM news_items WHERE id=?", (nid,)).fetchone()
    assert r["used_ts"] > 0
    st = json.loads(news._meta_get("kw_state"))
    cu = float(st["t_news_kwA"]["cool_until"])
    assert now + 3 * 86400 - 120 <= cu <= now + 3 * 86400 + 120
    assert news._kw_cooled("t_news_kwA", now) is True
    assert news._kw_cooled("t_news_kwA", now + 4 * 86400) is False


def test_cooldown_dismiss_7days_and_max(client):
    news = _news()
    from app import db
    nid = _ins_item("薪窯速報B", kw="t_news_kwB", contact="t_news_cB")   # v202掃除対象にしない
    now = time.time()
    news.dismiss(nid)
    with db.conn() as c:
        r = c.execute("SELECT dismissed FROM news_items WHERE id=?", (nid,)).fetchone()
    assert r["dismissed"] == 1
    st = json.loads(news._meta_get("kw_state"))
    cu = float(st["t_news_kwB"]["cool_until"])
    assert now + 7 * 86400 - 120 <= cu <= now + 7 * 86400 + 120
    # 短いクールダウンで上書きしても縮まない(max)
    news._kw_cool("t_news_kwB", 1)
    st2 = json.loads(news._meta_get("kw_state"))
    assert float(st2["t_news_kwB"]["cool_until"]) >= cu


# ---------- list_items(失効・掃除) ----------

def test_list_items_expiry_and_cleanup(client):
    news = _news()
    from app import db
    now = time.time()
    fresh_id = _ins_item("t_news_list 新しいネタ")
    old_id = _ins_item("t_news_list 4日前のネタ", created_ts=now - 4 * 86400)
    paper_keep_id = _ins_item("t_news_list 紙面5日前", tier="paper", created_ts=now - 5 * 86400)
    paper_old_id = _ins_item("t_news_list 紙面8日前", tier="paper", created_ts=now - 8 * 86400)
    ancient_id = _ins_item("t_news_list 31日前dismissed", created_ts=now - 31 * 86400, dismissed=1)
    ph_id = _ins_item("t_news_list プレースホルダ", opener="◯◯さん、見ました？")

    items = news.list_items(limit=1000)
    ids = {it["id"] for it in items}
    assert fresh_id in ids
    assert old_id not in ids           # 3日失効
    assert paper_keep_id in ids        # paper tierは7日保持
    assert paper_old_id not in ids     # paperも7日で失効
    assert ph_id not in ids            # ◯◯プレースホルダは非表示化
    with db.conn() as c:
        assert c.execute("SELECT dismissed FROM news_items WHERE id=?",
                         (old_id,)).fetchone()["dismissed"] == 1
        assert c.execute("SELECT dismissed FROM news_items WHERE id=?",
                         (ph_id,)).fetchone()["dismissed"] == 1
        # 30日超のdismissed行はDELETE
        assert c.execute("SELECT 1 FROM news_items WHERE id=?", (ancient_id,)).fetchone() is None


# ---------- who両読み ----------

def test_who_codes_dict_and_legacy_list(client):
    news = _news()
    assert news.who_codes({"who": json.dumps({"a": "獺祭", "b": "十四代"})}) == ["a", "b"]
    assert news.who_codes({"who": json.dumps(["a", "b"])}) == ["a", "b"]
    assert news.who_codes({"who": ""}) == []
    assert news.who_codes({"who": "not-json"}) == []
    assert news.who_codes({}) == []


# ---------- 会社ネタ refresh(ネガ=caution・重複ガード) ----------

def test_refresh_company_caution_and_dedup(client, tok, monkeypatch):
    news = _news()
    from app import db
    co = "薪川重工"
    mk_contact(client, tok, "t_news_co1", rank="A", company=co)

    def fake(query, require_in_title=""):
        if co in query:
            return [_item(f"{co}社長が退任へ"), _item(f"{co}が新工場を建設")]
        return []
    monkeypatch.setattr(news, "_fetch_rss", fake)
    # 共有DBに他テストの会社契約者が多くても枠切れしないよう予算だけ広げる
    monkeypatch.setattr(news, "_BUDGETS", {"company": 500, "group": 8, "cursor": 5})
    r = news.refresh(force=True)
    assert r["ran"] is True
    with db.conn() as c:
        rows = [dict(x) for x in c.execute(
            "SELECT * FROM news_items WHERE contact='t_news_co1' ORDER BY id")]
    assert len(rows) == 2
    neg = [x for x in rows if "退任" in x["title"]][0]
    ok = [x for x in rows if "新工場" in x["title"]][0]
    # ネガ見出し=「見せるが送らせない」: caution=1・一言なし
    assert neg["caution"] == 1 and neg["opener"] == ""
    # 通常見出し: caution=0(APIキー無しなのでopenerは空)
    assert ok["caution"] == 0 and ok["opener"] == ""
    assert ok["company"] == co
    # 2回目: hash重複ガードで増えない
    news.refresh(force=True)
    with db.conn() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM news_items WHERE contact='t_news_co1'"
                      ).fetchone()["n"]
    assert n == 2


# ---------- 興味ネタ: 群(>=3)tier・who dict・ネガ破棄 ----------

def _ev_now(y, mo, d):
    """JST正午のepoch(行事フェーズの窓判定用)。"""
    import calendar
    return calendar.timegm((y, mo, d, 3, 0, 0, 0, 0, 0))   # 12:00 JST = 03:00 UTC


def test_event_phase_window_and_matching(client, tok):
    """v197: 行事カレンダー。窓内(初日5日前〜最終日)+興味の合う相手がいる時だけ作る。"""
    from app import crm, db
    news = _news()
    mk_contact(client, tok, "t_news_ev1", rank="B")
    crm.set_attr("t_news_ev1", "趣味・関心", "相撲")
    # 窓の外(初日6日前) → 作らない
    assert news._refresh_events(_ev_now(2026, 9, 6)) == 0
    # 窓内(初日3日前) → 九月場所が1件・tier=event・whoに該当者
    n = news._refresh_events(_ev_now(2026, 9, 10))
    assert n == 1
    with db.conn() as c:
        r = c.execute("SELECT * FROM news_items WHERE kw='行事:sumo09'").fetchone()
    assert r is not None and r["tier"] == "event"
    assert "九月場所" in r["title"] and "9/13" in r["title"]
    assert "始まりますね" in r["opener"]
    assert "t_news_ev1" in json.loads(r["who"])
    # 生きている同行事ネタがある間は追い足さない
    assert news._refresh_events(_ev_now(2026, 9, 11)) == 0


def test_event_phase_requires_matching_interest(client, tok):
    """興味の合う相手がいない行事は作らない(ノイズを足さない)。"""
    from app import db
    news = _news()
    with db.conn() as c:
        c.execute("DELETE FROM news_items WHERE kw LIKE '行事:%'")
    # 競馬好きが誰もいない状態で有馬記念の窓 → 0件
    with db.conn() as c:
        n_before = c.execute("SELECT COUNT(*) FROM news_items WHERE kw='行事:arima'").fetchone()[0]
    assert n_before == 0
    news._refresh_events(_ev_now(2026, 12, 26))
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM news_items WHERE kw='行事:arima'").fetchone()[0] == 0


def test_event_dismiss_cooldown_blocks_recreation(client, tok):
    """dismissした行事は7日クールダウンで翌日以降も再掲されない。"""
    from app import crm, db
    news = _news()
    mk_contact(client, tok, "t_news_ev2", rank="B")
    crm.set_attr("t_news_ev2", "趣味・関心", "ワイン")
    n = news._refresh_events(_ev_now(2026, 11, 16))   # ボジョレー3日前(十一月場所の窓とも重なる)
    assert n >= 1
    with db.conn() as c:
        nid = c.execute("SELECT id FROM news_items WHERE kw='行事:beaujolais'").fetchone()["id"]
    news.dismiss(nid)
    # (a) dismissがクールダウン(実時刻基準・7日)を書いていること
    import time as _t
    assert news._kw_cooled("行事:beaujolais", _t.time()) is True
    # (b) _refresh_eventsがクールダウンを参照して再掲しないこと
    #     (行事窓=2026年11月の擬似nowと実時刻がズレるため、期間を伸ばして判定面を検証)
    news._kw_cool("行事:beaujolais", 200)
    assert news._refresh_events(_ev_now(2026, 11, 17)) == 0
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM news_items WHERE kw='行事:beaujolais' "
                         "AND dismissed=0").fetchone()[0] == 0


# ---------- last_day 判定に kwフェーズ(v191 #17)。fresh DB の別プロセスで検証 ----------

def test_last_day_company_only_fresh_db():
    """v197: last_day判定は会社フェーズのみ(kw廃止・行事は通信しないため失敗しない)。
    会社ネタありの構成で全滅した日はマークせず次周回で再挑戦。成功したらマーク。"""
    code = r"""
import time
from app import db, crm, news
db.init(); crm.ensure(); news.ensure()
news._SLEEP = False
db.upsert_contact("c1", rank="B")
with db.conn() as c:
    c.execute("UPDATE contacts SET company='テスト商事' WHERE code='c1'")

def boom(query, require_in_title=""):
    raise RuntimeError("net down")
news._fetch_rss = boom
r1 = news.refresh(force=True)
assert r1["ran"] is True, r1
# 会社全滅の日 → last_day を立てない(次周回で再挑戦)
assert news._meta_get("last_day") == "", news._meta_get("last_day")

now = time.time()
news._fetch_rss = lambda query, require_in_title="": [
    {"title": "テスト商事が新製品を発表", "link": "https://example.com", "ts": now, "src_url": ""}]
r2 = news.refresh(force=False)   # マーク無しなので走る
assert r2["ran"] is True, r2
assert r2["added"] >= 1, r2
jst_today = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 9 * 3600))
assert news._meta_get("last_day") == jst_today, news._meta_get("last_day")
r3 = news.refresh(force=False)
assert r3 == {"ran": False, "added": 0}, r3
print("OK17B")
"""
    rc, out, err = run_in_mode("mizu", code)
    assert rc == 0, f"stdout={out}\nstderr={err}"
    assert "OK17B" in out
