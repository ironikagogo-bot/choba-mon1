"""app/crm.py の重複検出(find_duplicates/dup_dismiss)と統合(merge_contact)・
改名(rename_contact)のテスト。

対象観点:
- 呼び名・LINE検索名・本名・正規化名の一致検出 / 包含は4文字以上のみ /
  「人名+敬称+店情報」型(AMANE型)の先頭人名抽出
- dup_dismiss → linebot_meta['dup_not_same'] に記録され再検出されない
- merge時の検疫メタ quarantine_{code} の移行(v191その2 #7)
- merge/renameが acted_log 等へ波及(v191その2 #10)
- フラグ(flag_koi/flag_ero)のOR継承はkeepがcustomerの時だけ(v191その2 #11)

規約: 契約者コードは本ファイル専用(t_cm_* / tcm○○)。検出テストで使った
名前カードは delete_contact_full で毎回掃除し、包含判定の全域走査を汚さない。
"""
import json
import time

from tests.conftest import mk_contact

from app import crm, db


# ---------------- helpers ----------------

def _pair_for(items, c1, c2):
    """find_duplicates結果から {c1,c2} のペアを探す。"""
    for p in items:
        if {p["a"]["code"], p["b"]["code"]} == {c1, c2}:
            return p
    return None


def _cleanup(*codes):
    for c in codes:
        try:
            crm.delete_contact_full(c)
        except Exception:
            pass
    crm._DUP_CACHE["ts"] = 0.0


def _insert_msg(contact, text="こんばんは", ts=None):
    with db.conn() as c:
        c.execute("INSERT INTO messages(contact,text,ts,category) VALUES(?,?,?,?)",
                  (contact, text, ts or time.time(), "batch"))


def _lb():
    from app import linebot
    linebot.ensure()
    return linebot


# ---------------- 重複検出 ----------------

def test_dup_same_yobina(client, tok):
    a, b = "t_cm_y1", "t_cm_y2"
    mk_contact(client, tok, a)
    mk_contact(client, tok, b)
    crm.set_attr(a, "呼び名", "つる乃てすと")
    crm.set_attr(b, "呼び名", "つる乃てすと")
    try:
        p = _pair_for(crm.find_duplicates(force=True), a, b)
        assert p is not None, "同じ呼び名のペアが検出されない"
        assert p["reason"] == "呼び名が同じ"
        # infos に判断材料(rank/kind/呼び名)が入る
        assert p["a"]["yobina"] == "つる乃てすと"
        assert "last_ts" in p["a"] and "msgs" in p["a"]
    finally:
        _cleanup(a, b)


def test_dup_same_line_search_name(client, tok):
    a, b = "t_cm_l1", "t_cm_l2"
    mk_contact(client, tok, a)
    mk_contact(client, tok, b)
    crm.set_attr(a, "LINE検索名", "kaz.testline")
    crm.set_attr(b, "LINE検索名", "KAZ TESTLINE")   # 正規化(小文字・記号除去)で一致
    try:
        p = _pair_for(crm.find_duplicates(force=True), a, b)
        assert p is not None, "同じLINE検索名のペアが検出されない"
        assert p["reason"] == "LINE検索名が同じ"
    finally:
        _cleanup(a, b)


def test_dup_same_honmyo(client, tok):
    a, b = "t_cm_h1", "t_cm_h2"
    mk_contact(client, tok, a)
    mk_contact(client, tok, b)
    crm.set_attr(a, "本名", "帳場試験太郎")
    crm.set_attr(b, "本名", "帳場 試験太郎")   # 空白ゆれは正規化で吸収
    try:
        p = _pair_for(crm.find_duplicates(force=True), a, b)
        assert p is not None, "同じ本名のペアが検出されない"
        assert p["reason"] == "本名が同じ"
    finally:
        _cleanup(a, b)


def test_dup_normalized_code_equal(client, tok):
    # 「田中さん」と「田中🍸」型: 敬称・絵文字を除いた正規化名が一致
    a, b = "tcm鶴亀さん", "tcm鶴亀🍸"
    mk_contact(client, tok, a)
    mk_contact(client, tok, b)
    try:
        p = _pair_for(crm.find_duplicates(force=True), a, b)
        assert p is not None, "正規化名一致のペアが検出されない"
        assert p["reason"] == "名前がほぼ同じ"
    finally:
        _cleanup(a, b)


def test_dup_amane_style_head_name(client, tok):
    # 「大山さん🌵AMANE芳美」型: 先頭の人名+敬称を抽出して素の人名カードと照合
    a, b = "tcm百千鳥さん🌵AMANE芳美", "tcm百千鳥"
    mk_contact(client, tok, a)
    mk_contact(client, tok, b)
    try:
        p = _pair_for(crm.find_duplicates(force=True), a, b)
        assert p is not None, "AMANE型表示名と素の人名カードのペアが検出されない"
        assert p["reason"] == "名前がほぼ同じ"
    finally:
        _cleanup(a, b)


def test_dup_inclusion_needs_4chars(client, tok):
    # 包含は正規化名4文字以上でだけ検出(3文字以下は誤爆防止で対象外)
    a, b = "tcm木村孝史さん", "tcm木村孝史別荘会"      # 短い方7文字 ⊂ 長い方
    c1, c2 = "宇久井", "宇久井水産流通"                # 短い方3文字 ⊂ 長い方 → 非検出
    mk_contact(client, tok, a)
    mk_contact(client, tok, b)
    mk_contact(client, tok, c1)
    mk_contact(client, tok, c2)
    try:
        items = crm.find_duplicates(force=True)
        p = _pair_for(items, a, b)
        assert p is not None, "4文字以上の包含ペアが検出されない"
        assert p["reason"] == "名前の一部が一致"
        assert _pair_for(items, c1, c2) is None, "3文字包含が誤検出されている"
    finally:
        _cleanup(a, b, c1, c2)


def test_dup_dismiss_persists_and_suppresses(client, tok):
    a, b = "t_cm_d1", "t_cm_d2"
    mk_contact(client, tok, a)
    mk_contact(client, tok, b)
    crm.set_attr(a, "呼び名", "たか志てすと")
    crm.set_attr(b, "呼び名", "たか志てすと")
    try:
        assert _pair_for(crm.find_duplicates(force=True), a, b) is not None
        crm.dup_dismiss(a, b)
        # linebot_meta の dup_not_same に sorted-join キーで永続記録される
        with db.conn() as c:
            r = c.execute("SELECT v FROM linebot_meta WHERE k='dup_not_same'").fetchone()
        assert r is not None
        assert "|".join(sorted([a, b])) in json.loads(r["v"])
        # 再検出されない(forceあり/なし両方。dismissはキャッシュも無効化する)
        assert _pair_for(crm.find_duplicates(force=True), a, b) is None
        assert _pair_for(crm.find_duplicates(), a, b) is None
    finally:
        _cleanup(a, b)


# ---------------- 統合 merge_contact ----------------

def test_merge_moves_rows_and_keeps_attr_priority(client, tok):
    keep, absorb = "t_cm_m1_keep", "t_cm_m1_abs"
    mk_contact(client, tok, keep)
    mk_contact(client, tok, absorb)
    _insert_msg(absorb, "受信1")
    _insert_msg(absorb, "受信2")
    lb = _lb()
    with db.conn() as c:
        c.execute("INSERT INTO acted_log(contact,action,changed,acted_ts) VALUES(?,?,?,?)",
                  (absorb, "done", "[]", time.time()))
    crm.add_alias("t_cm_m1_旧表示名", absorb)
    crm.set_attr(keep, "呼び名", "K様")
    crm.set_attr(absorb, "呼び名", "A様")          # keep側優先で捨てられる
    crm.set_attr(absorb, "好きなお酒", "山崎")      # keepに無い属性は移る
    r = crm.merge_contact(keep, absorb)
    assert r.get("ok") is True and r.get("kept") == keep and r.get("absorbed") == absorb
    # absorbカードは消え、その名前はkeepの紐付けとして残る
    assert db.get_contact(absorb) is None
    assert absorb in crm.aliases_for(keep)
    assert "t_cm_m1_旧表示名" in crm.aliases_for(keep)
    # 受信・裁定ログ(#10)がkeep名義へ
    with db.conn() as c:
        n = c.execute("SELECT COUNT(*) n FROM messages WHERE contact=?", (keep,)).fetchone()["n"]
        na = c.execute("SELECT COUNT(*) n FROM acted_log WHERE contact=?", (keep,)).fetchone()["n"]
        nz = c.execute("SELECT COUNT(*) n FROM acted_log WHERE contact=?", (absorb,)).fetchone()["n"]
    assert n == 2 and na == 1 and nz == 0
    # 属性: keep優先+absorb側の穴埋め
    attrs = crm.get_attrs(keep)
    assert attrs.get("呼び名") == "K様"
    assert attrs.get("好きなお酒") == "山崎"
    assert crm.get_attrs(absorb) == {}
    assert (db.get_contact(keep) or {}).get("linked") == 1


def test_merge_rank_and_flag_or_for_customer(client, tok):
    keep, absorb = "t_cm_m2_keep", "t_cm_m2_abs"
    mk_contact(client, tok, keep, rank="B")               # kind=customer(既定)
    mk_contact(client, tok, absorb, rank="S", flag_koi=1, flag_ero=1)
    assert crm.merge_contact(keep, absorb)["ok"] is True
    k = db.get_contact(keep)
    assert k["rank"] == "S", "高い方のランクが継承されない"
    assert int(k["flag_koi"]) == 1 and int(k["flag_ero"]) == 1, \
        "keepがcustomerなのに対応フラグがOR継承されない"


def test_merge_flag_not_inherited_when_keep_not_customer(client, tok):
    # v191その2(#11): koi客を店内カード(staff)へ統合してもフラグは継承しない
    keep, absorb = "t_cm_m3_keep", "t_cm_m3_abs"
    mk_contact(client, tok, keep, kind="staff")
    mk_contact(client, tok, absorb, flag_koi=1, flag_ero=1)
    assert crm.merge_contact(keep, absorb)["ok"] is True
    k = db.get_contact(keep)
    assert int(k["flag_koi"] or 0) == 0 and int(k["flag_ero"] or 0) == 0, \
        "非customerのkeepにflagがOR継承されている(#11違反)"


def test_merge_fills_only_empty_fields(client, tok):
    keep, absorb = "t_cm_m4_keep", "t_cm_m4_abs"
    mk_contact(client, tok, keep, note="keep側メモ")            # 埋まっている→保持
    mk_contact(client, tok, absorb, note="absorb側メモ", tags="VIP", birthday="08-19")
    assert crm.merge_contact(keep, absorb)["ok"] is True
    k = db.get_contact(keep)
    assert k["note"] == "keep側メモ", "keep側の既存値がabsorbで潰された"
    assert k["tags"] == "VIP" and k["birthday"] == "08-19", "空欄の穴埋めがされない"


def test_merge_migrates_quarantine_meta(client, tok):
    # v191その2(#7): quarantine_{absorb} は keep へ移行され、absorb側キーは消える。
    # keepの種別が未確定(pending🔖あり)なら解放されず quarantine_{keep} に残る。
    keep, absorb = "t_cm_m5_keep", "t_cm_m5_abs"
    mk_contact(client, tok, keep)
    mk_contact(client, tok, absorb)
    lb = _lb()
    with db.conn() as c:
        c.execute("INSERT INTO linebot_facts(contact,k,v,status,created_ts) VALUES(?,?,?,?,?)",
                  (keep, "🔖種別・立場", "お客様", "pending", time.time()))
    assert lb.rel_confirmed(keep) is False
    lb.quarantine_add(absorb, [{"k": "好きなお酒", "v": "山崎"}])
    assert crm.merge_contact(keep, absorb)["ok"] is True
    assert lb._meta_get(f"quarantine_{absorb}") == "", "absorb側の検疫キーが残留(#7違反)"
    moved = json.loads(lb._meta_get(f"quarantine_{keep}") or "[]")
    assert {"k": "好きなお酒", "v": "山崎"} in [{"k": f.get("k"), "v": f.get("v")} for f in moved], \
        "検疫中の保留factがkeepへ移行されていない(#7違反)"
    # keep未確定なので pending🔖 は消されない(仕分けキューに残るのが正)
    with db.conn() as c:
        n = c.execute("SELECT COUNT(*) n FROM linebot_facts WHERE contact=? AND k=? "
                      "AND status='pending'", (keep, "🔖種別・立場")).fetchone()["n"]
    assert n == 1


def test_merge_drops_pending_rel_when_keep_confirmed(client, tok):
    # v191その2(#7): 確定済みkeepへabsorb由来のpending🔖を持ち込まない
    # (確定済み客が統合の副作用で仕分けキューに再登場しない)
    keep, absorb = "t_cm_m6_keep", "t_cm_m6_abs"
    mk_contact(client, tok, keep)          # relファクト無し=確定扱い
    mk_contact(client, tok, absorb)
    lb = _lb()
    with db.conn() as c:
        c.execute("INSERT INTO linebot_facts(contact,k,v,status,created_ts) VALUES(?,?,?,?,?)",
                  (absorb, "🔖種別・立場", "お客様", "pending", time.time()))
    assert lb.rel_confirmed(keep) is True
    assert crm.merge_contact(keep, absorb)["ok"] is True
    with db.conn() as c:
        n = c.execute("SELECT COUNT(*) n FROM linebot_facts WHERE contact IN (?,?) AND k=? "
                      "AND status='pending'", (keep, absorb, "🔖種別・立場")).fetchone()["n"]
    assert n == 0, "確定済みkeepにpending🔖が持ち込まれた(#7違反)"
    assert lb.rel_confirmed(keep) is True


def test_merge_concats_talk_texts(client, tok):
    keep, absorb = "t_cm_m7_keep", "t_cm_m7_abs"
    mk_contact(client, tok, keep)
    mk_contact(client, tok, absorb)
    lb = _lb()
    with db.conn() as c:
        c.execute("INSERT INTO linebot_talks(contact,text) VALUES(?,?)", (keep, "KEEP側原文"))
        c.execute("INSERT INTO linebot_talks(contact,text) VALUES(?,?)", (absorb, "ABS側原文"))
        # keepを未確定にして解放スレッド(後段分析)を起動させない=決定的に検証する
        c.execute("INSERT INTO linebot_facts(contact,k,v,status,created_ts) VALUES(?,?,?,?,?)",
                  (keep, "🔖種別・立場", "お客様", "pending", time.time()))
    assert crm.merge_contact(keep, absorb)["ok"] is True
    with db.conn() as c:
        rk = c.execute("SELECT text FROM linebot_talks WHERE contact=?", (keep,)).fetchone()
        ra = c.execute("SELECT text FROM linebot_talks WHERE contact=?", (absorb,)).fetchone()
    assert ra is None, "absorb側のトーク原文が残留"
    assert rk["text"] == "KEEP側原文\n\nABS側原文"


def test_merge_rejects_bad_args(client, tok):
    mk_contact(client, tok, "t_cm_m8_same")
    assert crm.merge_contact("t_cm_m8_same", "t_cm_m8_same")["ok"] is False
    assert crm.merge_contact("", "t_cm_m8_same")["ok"] is False
    # keep側が実在しない
    assert crm.merge_contact("t_cm_m8_nokeep", "t_cm_m8_same")["ok"] is False
    assert db.get_contact("t_cm_m8_same") is not None, "エラー時にabsorbが消された"


# ---------------- 改名 rename_contact の波及(#10) ----------------

def test_rename_propagates_acted_log_and_talks(client, tok):
    old, new = "t_cm_rn_old", "t_cm_rn_new"
    mk_contact(client, tok, old)
    _insert_msg(old, "改名前受信")
    lb = _lb()
    with db.conn() as c:
        c.execute("INSERT INTO acted_log(contact,action,changed,acted_ts) VALUES(?,?,?,?)",
                  (old, "replied", "[]", time.time()))
        c.execute("INSERT INTO linebot_talks(contact,text) VALUES(?,?)", (old, "原文"))
    r = crm.rename_contact(old, new)
    assert r.get("ok") is True and r.get("code") == new
    assert db.get_contact(old) is None and db.get_contact(new) is not None
    with db.conn() as c:
        na = c.execute("SELECT COUNT(*) n FROM acted_log WHERE contact=?", (new,)).fetchone()["n"]
        nz = c.execute("SELECT COUNT(*) n FROM acted_log WHERE contact=?", (old,)).fetchone()["n"]
        nt = c.execute("SELECT COUNT(*) n FROM linebot_talks WHERE contact=?", (new,)).fetchone()["n"]
        nm = c.execute("SELECT COUNT(*) n FROM messages WHERE contact=?", (new,)).fetchone()["n"]
    assert na == 1 and nz == 0, "改名でacted_logが追随しない(#10違反)"
    assert nt == 1 and nm == 1
    # 旧名(LINE表示名だった場合)からの受信が新カードへ入るよう紐付けが残る
    assert old in crm.aliases_for(new)
