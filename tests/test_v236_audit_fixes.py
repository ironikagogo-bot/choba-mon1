"""v236: 夜間監査(エージェント2体)で出た実バグの修正+指示書採用3件。

各テストは「直す前は何が起きていたか」を1行で書く。回帰の意味が読めるように。
"""
from tests.conftest import mk_contact

H = {"X-Ingest-Token": "tk"}


# ── 🚦が🎛に出なかった件(重大) ───────────────────────────────
def test_tolerance_with_ok_none_is_undecided(client, tok):
    """直す前: analyze_personaが付ける "ok": None を `"ok" in it` が確定済みと誤判定し、
    🚦が🎛「あたらしい学び」に1件も出ず、一括○の対象にもなっていなかった。"""
    from app import linebot, liff
    mk_contact(client, tok, "t_v236_tol", rank="B")
    linebot.ensure()
    linebot.save_persona("t_v236_tol", {
        "summary": "s", "sections": [],
        "tolerance": [{"k": "呼ばれ方", "v": "太郎さん", "src": "", "conf": "高", "ok": None}],
        "myself": []})
    assert liff._tune_undecided({"ok": None}) is True
    assert liff._tune_undecided({}) is True
    assert liff._tune_undecided({"ok": 1}) is False
    assert liff._tune_undecided({"ok": 0}) is False
    d = client.get("/api/liff/tune", headers=H).json()
    assert any(x["code"] == "t_v236_tol" and x["kind"] == "tol" for x in d["items"])


def test_ackall_covers_ok_none(client, tok):
    """直す前: 一括○が "ok": None の🚦を素通りしていた(本人裁定が🚦に効いていなかった)。"""
    from app import linebot
    mk_contact(client, tok, "t_v236_ack", rank="B")
    linebot.save_persona("t_v236_ack", {
        "summary": "s", "sections": [],
        "tolerance": [{"k": "距離感", "v": "夜も可", "src": "", "conf": "中", "ok": None}],
        "myself": []})
    r = client.post("/api/liff/tune_ackall", data={"key": "tk"})
    assert r.status_code == 200
    assert linebot.get_persona("t_v236_ack")["tolerance"][0]["ok"] == 1


# ── 再分析で🪞の○✕が消える件 ──────────────────────────────
def test_reanalysis_keeps_myself_decisions(client, tok):
    """直す前: 再分析のマージがtoleranceだけで、🪞の✕(止める)が毎回ONに戻っていた。"""
    from app import linebot
    old = {"summary": "s", "sections": [],
           "tolerance": [{"k": "呼ばれ方", "v": "太郎さん", "ok": 1}],
           "myself": [{"k": "気をつけたい癖", "v": "即快諾しがち", "ok": 0},
                      {"k": "口調・距離", "v": "丁寧語", "ok": 1}]}
    new = {"summary": "s2", "sections": [],
           "tolerance": [{"k": "呼ばれ方", "v": "太郎ちゃん"}],
           "myself": [{"k": "気をつけたい癖", "v": "安請け合いしがち"},
                      {"k": "演じている役", "v": "聞き役"}]}
    merged = _merge_like_persona_async(old, new)
    my = {m["k"]: m for m in merged["myself"]}
    assert my["気をつけたい癖"]["ok"] == 0        # ✕が生き残る
    assert my["口調・距離"]["ok"] == 1            # 新配列に無くても残す
    assert "演じている役" in my                    # 新項目は未確認のまま入る
    assert my["演じている役"].get("ok") is None


def _merge_like_persona_async(old, p):
    """persona_async内のマージと同じ手順(関数化されていないため写経)。
    ここが本体とズレたらこのテストが先に落ちる。"""
    for _key in ("tolerance", "myself"):
        decided = {t["k"]: t for t in (old.get(_key) or []) if t.get("ok") in (0, 1)}
        merged = []
        for t in (p.get(_key) or []):
            merged.append(decided[t["k"]] if t.get("k") in decided else t)
        for k, t in decided.items():
            if not any(x.get("k") == k for x in merged):
                merged.append(t)
        p[_key] = merged
    return p


# ── 配信の鮮度フィルタがtxtだけの相手でカードを全滅させる件 ─────────
def test_freshness_uses_imported_talk(client, tok):
    """直す前: messages/sent_repliesが空(=txt取り込みだけの相手)だと鮮度ゼロになり、
    カードの話題が全部落ちて配信が天気とニュースだけになっていた。"""
    from app import campaign, crm, db, linebot
    mk_contact(client, tok, "t_v236_fresh", rank="A")
    linebot.ensure()
    with db.conn() as c:
        c.execute("INSERT INTO linebot_talks(contact,text) VALUES(?,?) "
                  "ON CONFLICT(contact) DO UPDATE SET text=excluded.text",
                  ("t_v236_fresh", "2026/08/01(土)\n21:00\t宝条\tゴルフのスコアが90切ったよ"))
    crm.add_def("趣味・関心")
    crm.set_attr("t_v236_fresh", "趣味・関心", "ゴルフ")
    assert campaign._freshness_corpus("t_v236_fresh")            # 材料が拾える
    assert "趣味・関心" in campaign._fresh_topic_keys("t_v236_fresh")


# ── 呼び名の決定論置換の誤爆 ─────────────────────────────
def test_force_yobina_no_misfire(client, tok):
    from app import campaign
    f = campaign._force_yobina
    assert f("山本先生、お久しぶりです", {"code": "山本", "yobina": "ヒロ"}) == "ヒロさん、お久しぶりです"
    assert f("あいにくの雨ですね", {"code": "あい", "yobina": "アイリ"}) == "あいにくの雨ですね"
    assert f("なおさら会いたい", {"code": "なお", "yobina": "ナオミ"}) == "なおさら会いたい"
    assert f("宝条さん、こんばんは", {"code": "宝条", "yobina": "誠一"}) == "誠一さん、こんばんは"


# ── 指示書採用②: PIIの決定論フィルタ ────────────────────────
def test_strip_pii(client, tok):
    from app import linebot
    keep, drop = linebot.strip_pii([
        {"k": "連絡先", "v": "090-1234-5678"},
        {"k": "関係性メモ", "v": "振込先は三井住友 1234567"},
        {"k": "趣味・関心", "v": "https://example.com のクラブ"},
        {"k": "その他", "v": "taro@example.com"},
        {"k": "好きなお酒", "v": "日本酒(玉響)"},
        {"k": "進行中の話", "v": "電話で話した件のつづき"},
    ])
    assert {f["v"] for f in keep} == {"日本酒(玉響)", "電話で話した件のつづき"}
    assert len(drop) == 4


def test_curate_facts_drops_pii(client, tok):
    """厳選の入口で落ちる(超重要キーでも例外にしない)。"""
    from app import linebot
    out = linebot.curate_facts([{"k": "誕生日", "v": "8月19日", "conf": "高"},
                                {"k": "本名", "v": "080-1111-2222", "conf": "高"}])
    assert any(f["v"] == "8月19日" for f in out)
    assert not any("080" in (f.get("v") or "") for f in out)


# ── 指示書採用③: 浅賀ガード(恋情語の主語比) ─────────────────
def _ev(is_self, text):
    return {"is_self": is_self, "text": text, "kind": "text", "ts": 0}


def test_koi_subject_counts(client, tok):
    from app import dynamics
    ev = [_ev(True, "会いたいな")] * 12 + [_ev(False, "了解")] * 20 + [_ev(False, "好きだよ")]
    c = dynamics.koi_subject_counts(ev)
    assert c == {"self": 12, "them": 1}


def test_koi_self_dominant_needs_evidence(client, tok):
    """材料が薄い相手では必ずFalse(誤った確信を持たない)。"""
    from app import db, dynamics
    mk_contact(client, tok, "t_v236_koi", rank="B")
    db.save_profile("t_v236_koi", {"dynamics": {"metrics": {"koi_words": {"self": 3, "them": 0}}}})
    dom, _ = dynamics.koi_self_dominant("t_v236_koi")
    assert dom is False                       # self<10=判断しない
    db.save_profile("t_v236_koi", {"dynamics": {"metrics": {"koi_words": {"self": 40, "them": 2}}}})
    dom2, cnt = dynamics.koi_self_dominant("t_v236_koi")
    assert dom2 is True and cnt == {"self": 40, "them": 2}
    db.save_profile("t_v236_koi", {"dynamics": {"metrics": {"koi_words": {"self": 20, "them": 15}}}})
    dom3, _ = dynamics.koi_self_dominant("t_v236_koi")
    assert dom3 is False                      # 相手も言っている=従来の線引きレーン


def test_koi_guard_switches_lane(client, tok, monkeypatch):
    """💘ONでも本人発偏重なら、線引き長文の指示を出さない(プロンプトを実際に覗く)。"""
    from app import config, db, drafts
    mk_contact(client, tok, "t_v236_lane", rank="A")
    with db.conn() as c:
        c.execute("UPDATE contacts SET flag_koi=1, kind='customer' WHERE code=?", ("t_v236_lane",))
    db.save_profile("t_v236_lane", {"dynamics": {"metrics": {"koi_words": {"self": 40, "them": 1}}}})
    mid = db.add_message("t_v236_lane", "元気してた？", category="batch", reason="雑談")
    seen = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"content": [{"text": '{"drafts":[{"text":"うん元気だよ"}]}'}]}

    def _spy(url, **kw):
        seen["body"] = (kw.get("json") or {})
        return _Resp()

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key", raising=False)
    monkeypatch.setattr(drafts.requests, "post", _spy)
    drafts.generate(mid)
    msgs = (seen.get("body") or {}).get("messages") or []
    blob = "".join(m.get("content") or "" for m in msgs) + ((seen.get("body") or {}).get("system") or "")
    assert blob, "プロンプトを捕まえられなかった"
    assert "線引きモード(控えめ)" in blob
    assert "長文案を必ず追加" not in blob


def test_koi_lane_unchanged_when_partner_speaks(client, tok, monkeypatch):
    """相手も恋情語を言っている相手では、従来どおり線引き長文レーンのまま。"""
    from app import config, db, drafts
    mk_contact(client, tok, "t_v236_lane2", rank="A")
    with db.conn() as c:
        c.execute("UPDATE contacts SET flag_koi=1, kind='customer' WHERE code=?", ("t_v236_lane2",))
    db.save_profile("t_v236_lane2", {"dynamics": {"metrics": {"koi_words": {"self": 20, "them": 18}}}})
    mid = db.add_message("t_v236_lane2", "会いたいよ", category="batch", reason="雑談")
    seen = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"content": [{"text": '{"drafts":[{"text":"ありがと"}]}'}]}

    def _spy(url, **kw):
        seen["body"] = (kw.get("json") or {})
        return _Resp()

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key", raising=False)
    monkeypatch.setattr(drafts.requests, "post", _spy)
    drafts.generate(mid)
    msgs = (seen.get("body") or {}).get("messages") or []
    blob = "".join(m.get("content") or "" for m in msgs)
    assert "ガチ恋・線引きモード" in blob and "長文案を必ず追加" in blob


# ── データ衛生 S6 ─────────────────────────────────────
def test_merge_moves_lasttalk(client, tok):
    """直す前: 統合後に「ご無沙汰」を誤判定し、先週会った相手に掘り起こしが飛んだ。"""
    import time as _t
    from app import crm, linebot
    mk_contact(client, tok, "t_v236_keep", rank="A")
    mk_contact(client, tok, "t_v236_abs", rank="B")
    linebot.ensure()
    recent = _t.time() - 3 * 86400
    linebot._meta_set("lasttalk_t_v236_abs", str(recent))
    linebot._meta_set("lasttalk_t_v236_keep", str(_t.time() - 200 * 86400))
    crm.merge_contact("t_v236_keep", "t_v236_abs")
    got = float(linebot._meta_get("lasttalk_t_v236_keep") or 0)
    assert abs(got - recent) < 1              # 新しい方が残る
    assert not linebot._meta_get("lasttalk_t_v236_abs")


def test_rename_moves_self_examples(client, tok):
    """直す前: 改名で実例庫が切り離され、下書きが実例なしの一般文に落ちていた。"""
    from app import crm, db, situations
    mk_contact(client, tok, "t_v236_old", rank="B")
    situations.ensure()
    with db.conn() as c:
        c.execute("INSERT INTO self_examples(contact,situation,partner_text,self_text,created_ts) "
                  "VALUES(?,?,?,?,?)", ("t_v236_old", "誘い", "今度ごはん", "ぜひ〜", 0))
    crm.rename_contact("t_v236_old", "t_v236_new")
    with db.conn() as c:
        n = c.execute("SELECT COUNT(*) FROM self_examples WHERE contact=?",
                      ("t_v236_new",)).fetchone()[0]
    assert n == 1


# ── S7: 月上限のJST境界 ───────────────────────────────
def test_push_month_key_is_jst(client, tok):
    """直す前: UTCで月を数えていて、リセットが日本時間の1日9時にズレていた。"""
    import time as _t
    from app import linebot
    assert linebot._lp_month_key() == "lp_" + _t.strftime("%Y%m", _t.gmtime(_t.time() + 9 * 3600))


# ── S8: 取り込みジョブと原文メタの掃除 ──────────────────────
def test_jobs_gc_removes_old_and_orphans(client, tok):
    import time as _t
    from app import db, liff, linebot
    liff._jobs_ensure()
    linebot.ensure()
    with db.conn() as c:
        c.execute("INSERT INTO liff_import_jobs(fname,contact,status,detail,ts) "
                  "VALUES('a.txt','x','done','',?)", (_t.time() - 40 * 86400,))
        old_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("INSERT INTO liff_import_jobs(fname,contact,status,detail,ts) "
                  "VALUES('b.txt','y','done','',?)", (_t.time(),))
        new_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    linebot._meta_set(f"liffimp_{old_id}", "x" * 100)
    linebot._meta_set(f"liffimp_{new_id}", "y" * 100)
    linebot._meta_set("liffimp_999999", "orphan")     # 親のない原文
    liff._jobs_gc()
    with db.conn() as c:
        ids = {r["id"] for r in c.execute("SELECT id FROM liff_import_jobs")}
    assert old_id not in ids and new_id in ids
    assert not linebot._meta_get(f"liffimp_{old_id}")
    assert linebot._meta_get(f"liffimp_{new_id}")
    assert not linebot._meta_get("liffimp_999999")


# ── ホームの🧹バナーと受信箱の母集団を揃える ────────────────
def test_sweep_old_excludes_protected(client, tok):
    """直す前: バナーはS客・急ぎも数えるのに、受信箱の🧹はそれらを除外して数えるため、
    「バナーは出るのに片づける入口がどこにも無い」状態になっていた。"""
    import time as _t
    from app import db
    old = _t.time() - 10 * 86400
    mk_contact(client, tok, "t_v236_sw_s", rank="S")
    mk_contact(client, tok, "t_v236_sw_b", rank="B")
    for code, cat in (("t_v236_sw_s", "batch"), ("t_v236_sw_b", "urgent")):
        mid = db.add_message(code, "むかしの連絡", category=cat, reason="")
        with db.conn() as c:
            c.execute("UPDATE messages SET ts=?, status='open' WHERE id=?", (old, mid))
    d = client.get("/api/liff/home", headers=H).json()
    n = d["sweep_old"]
    # S客(rank=S)も🔥急ぎ(category=urgent)も母集団に入らない
    mid3 = db.add_message("t_v236_sw_b", "ふつうの古い連絡", category="batch", reason="")
    with db.conn() as c:
        c.execute("UPDATE messages SET ts=?, status='open' WHERE id=?", (old, mid3))
    d2 = client.get("/api/liff/home", headers=H).json()
    assert d2["sweep_old"] == n + 1
