"""型ルーティング検疫(v187 §10/§11)のテスト。

対象:
- app/linebot.py: rel_confirmed / quarantine_add / quarantine_release(_async) / apply_fact(🔖)
- app/liff.py: _run_import_job の検疫分岐、fixup/save・fixup/bulk のrelease(検疫解放)フック
- §10: koi/ero等の「客向けモード」がcustomer限定(build_queue の koiフラグ / _template_drafts)

規約: DB共有(session client)。契約者コードは t_quar_<n> で自分専用。
LLMはAPIキー除去でスタブ経路(classify/extract はNone/[]を返すので、取り込み
ジョブのテストでは monkeypatch で決定論の抽出結果を注入する)。
"""
import json
import time

from tests.conftest import mk_contact


# ---------- helpers ----------

def _fact(k, v, conf="高", src="トーク引用"):
    return {"k": k, "v": v, "src": src, "conf": conf, "alts": []}


def _marker(code):
    from app import linebot
    return linebot._meta_get(f"quarantine_{code}")


def _wait(cond, timeout=8.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return bool(cond())


def _fact_rows(code, k=None):
    from app import db
    with db.conn() as c:
        if k is None:
            return [dict(r) for r in c.execute(
                "SELECT * FROM linebot_facts WHERE contact=? ORDER BY id", (code,))]
        return [dict(r) for r in c.execute(
            "SELECT * FROM linebot_facts WHERE contact=? AND k=? ORDER BY id", (code, k))]


def _mk_job(code, fname=None):
    from app import db, liff
    liff._jobs_ensure()
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO liff_import_jobs(fname,contact,status,detail,ts) VALUES(?,?,?,?,?)",
            (fname or f"[LINE] {code}とのトーク.txt", code, "queued", "", time.time()))
        return cur.lastrowid


def _job(jid):
    from app import db
    with db.conn() as c:
        return dict(c.execute("SELECT * FROM liff_import_jobs WHERE id=?", (jid,)).fetchone())


def _talk(code):
    return (f"[LINE] {code}とのトーク履歴\n保存日時:2026/08/01 12:00\n\n"
            "2026/07/01(火)\n"
            f"12:00\t{code}\tこんにちは、週末は空いてる？\n"
            "12:01\tみさき\tありがとう、また連絡するね\n"
            f"12:02\t{code}\tまた飲みに行こうよ\n")


# ---------- rel_confirmed ----------

def test_rel_confirmed_true_for_old_card_without_rel_fact(client, tok):
    """🔖ファクト自体が無い旧カードは確定扱い(従来挙動を変えない)。"""
    from app import linebot
    mk_contact(client, tok, "t_quar_1")
    assert linebot.rel_confirmed("t_quar_1") is True


def test_rel_confirmed_false_while_rel_pending(client, tok):
    """pending🔖あり=未確定(検疫対象)。"""
    from app import linebot
    mk_contact(client, tok, "t_quar_2")
    linebot.save_facts("t_quar_2", [_fact(linebot._REL_KEY, "顧客（対等）", conf="低")],
                       status="pending")
    assert linebot.rel_confirmed("t_quar_2") is False


def test_rel_confirmed_true_when_confirmed(client, tok):
    """confirmedの🔖があれば確定(pendingが残っていてもconfirmed優先)。"""
    from app import linebot
    mk_contact(client, tok, "t_quar_3")
    linebot.save_facts("t_quar_3", [_fact(linebot._REL_KEY, "顧客（対等）")],
                       status="confirmed")
    assert linebot.rel_confirmed("t_quar_3") is True
    # confirmedとpendingが併存してもconfirmed優先で確定扱い
    linebot.save_facts("t_quar_3", [_fact(linebot._REL_KEY, "店内・スタッフ・対等")],
                       status="pending")
    assert linebot.rel_confirmed("t_quar_3") is True


# ---------- quarantine_add ----------

def test_quarantine_add_stores_meta_and_dedups(client, tok):
    """保留箱は linebot_meta の quarantine_{code}。(k,v)重複は積まない。"""
    from app import linebot
    mk_contact(client, tok, "t_quar_4")
    linebot.quarantine_add("t_quar_4", [_fact("好きなお酒", "獺祭"), _fact("趣味・関心", "ゴルフ")])
    linebot.quarantine_add("t_quar_4", [_fact("好きなお酒", "獺祭"), _fact("家族", "娘が一人")])
    cur = json.loads(_marker("t_quar_4"))
    assert [(f["k"], f["v"]) for f in cur] == [
        ("好きなお酒", "獺祭"), ("趣味・関心", "ゴルフ"), ("家族", "娘が一人")]


def test_quarantine_add_empty_keeps_marker_and_caps_at_200(client, tok):
    """hold空でもマーカーは残す(後段分析の実行予約)。保留は末尾200件でキャップ。"""
    from app import linebot
    mk_contact(client, tok, "t_quar_5")
    linebot.quarantine_add("t_quar_5", [])
    assert _marker("t_quar_5") == "[]"   # 空でもマーカー自体は存在
    linebot.quarantine_add("t_quar_5", [_fact(f"k{i}", f"v{i}") for i in range(250)])
    cur = json.loads(_marker("t_quar_5"))
    assert len(cur) == 200
    assert cur[0]["k"] == "k50" and cur[-1]["k"] == "k249"   # 末尾=最新を保持


# ---------- quarantine_release ----------

def test_release_without_marker_is_noop(client, tok):
    """マーカーが無ければ何もしない(旧カードの確定で走らない)。"""
    from app import linebot
    mk_contact(client, tok, "t_quar_6")
    linebot.quarantine_release("t_quar_6")   # 例外なし・factsも増えない
    assert _fact_rows("t_quar_6") == []


def test_release_customer_applies_hold_and_clears_marker(client, tok):
    """客確定→保留適用: 重要(本名)はpending、自動(好きなお酒)はカード反映(applied)。"""
    from app import crm, linebot
    mk_contact(client, tok, "t_quar_7")   # kind既定=customer
    linebot.quarantine_add("t_quar_7", [_fact("本名", "田中太郎"), _fact("好きなお酒", "山崎")])
    linebot.quarantine_release("t_quar_7")
    assert _marker("t_quar_7") == ""
    assert [r["status"] for r in _fact_rows("t_quar_7", "本名")] == ["pending"]
    assert [r["status"] for r in _fact_rows("t_quar_7", "好きなお酒")] == ["applied"]
    assert (crm.get_attrs("t_quar_7") or {}).get("好きなお酒") == "山崎"


def test_release_is_idempotent_after_marker_deleted(client, tok):
    """2回目のreleaseは無害(DELETE勝者だけが適用する競合ガードの単発版)。"""
    from app import linebot
    mk_contact(client, tok, "t_quar_8")
    linebot.quarantine_add("t_quar_8", [_fact("好きなお酒", "白州")])
    linebot.quarantine_release("t_quar_8")
    linebot.quarantine_release("t_quar_8")
    assert len(_fact_rows("t_quar_8", "好きなお酒")) == 1


def test_release_noncustomer_discards_hold(client, tok):
    """店内(staff)等の非顧客確定→保留は破棄・カードに何も書かない。"""
    from app import crm, linebot
    mk_contact(client, tok, "t_quar_9", kind="staff")
    linebot.quarantine_add("t_quar_9", [_fact("本名", "佐藤花子"), _fact("好きなお酒", "レモンサワー")])
    linebot.quarantine_release("t_quar_9")
    assert _marker("t_quar_9") == ""
    assert _fact_rows("t_quar_9") == []
    assert not (crm.get_attrs("t_quar_9") or {}).get("好きなお酒")


# ---------- apply_fact(🔖) → release フック ----------

def test_apply_fact_rel_customer_sets_kind_and_releases(client, tok):
    """✅整備/チャット○での🔖確定(客)→ kind/stand反映+検疫解放(保留適用)。"""
    from app import crm, db, linebot
    mk_contact(client, tok, "t_quar_10")
    linebot.quarantine_add("t_quar_10", [_fact("好きなお酒", "モエ")])
    linebot.apply_fact("t_quar_10", linebot._REL_KEY, "顧客（先輩・目上）")
    ct = db.get_contact("t_quar_10")
    assert ct["kind"] == "customer" and ct["stand"] == "senior"
    assert _wait(lambda: (crm.get_attrs("t_quar_10") or {}).get("好きなお酒") == "モエ"), \
        "async releaseで保留が適用されるはず"
    assert _marker("t_quar_10") == ""


def test_apply_fact_rel_staff_discards_hold(client, tok):
    """🔖確定(店内)→ kind/stand反映+保留は破棄。"""
    from app import crm, db, linebot
    mk_contact(client, tok, "t_quar_11")
    linebot.quarantine_add("t_quar_11", [_fact("好きなお酒", "ビール")])
    linebot.apply_fact("t_quar_11", linebot._REL_KEY, "店内・スタッフ・後輩・目下")
    ct = db.get_contact("t_quar_11")
    assert ct["kind"] == "staff" and ct["stand"] == "junior"
    assert _wait(lambda: not _marker("t_quar_11"))
    time.sleep(0.3)   # 破棄パスはDELETE後に何も書かないことの確認猶予
    assert not (crm.get_attrs("t_quar_11") or {}).get("好きなお酒")
    assert _fact_rows("t_quar_11", "好きなお酒") == []


# ---------- LIFF fixup/save ----------

def test_fixup_save_customer_confirms_rel_and_releases(client, tok):
    """1人分の確定(客): 🔖pending→confirmed・値は本人確定内容に上書き・検疫解放。"""
    from app import crm, db, linebot
    mk_contact(client, tok, "t_quar_12")
    linebot.save_facts("t_quar_12", [_fact(linebot._REL_KEY, "店内・スタッフ・対等", conf="低")],
                       status="pending")
    linebot.quarantine_add("t_quar_12", [_fact("好きなお酒", "獺祭")])
    assert linebot.rel_confirmed("t_quar_12") is False
    r = client.post("/api/liff/fixup/save", headers=tok,
                    json={"code": "t_quar_12", "呼び名": "たろーさん",
                          "kind": "customer", "stand": "even", "rank": "A"})
    assert r.status_code == 200 and r.json().get("ok") is True
    ct = db.get_contact("t_quar_12")
    assert ct["kind"] == "customer" and ct["stand"] == "even" and ct["rank"] == "A"
    rows = _fact_rows("t_quar_12", linebot._REL_KEY)
    assert rows and all(x["status"] == "confirmed" for x in rows)
    # v191その2(#8): AI予想値のままではなく本人確定の値に上書きされている
    assert rows[0]["v"] == linebot._rel_value("customer", "equal") == "顧客（対等）"
    assert linebot.rel_confirmed("t_quar_12") is True
    assert _wait(lambda: (crm.get_attrs("t_quar_12") or {}).get("好きなお酒") == "獺祭")
    assert _marker("t_quar_12") == ""


def test_fixup_save_staff_discards_quarantine(client, tok):
    """1人分の確定(店内): 検疫は破棄され、保留がカード・factsに漏れない。"""
    from app import crm, db, linebot
    mk_contact(client, tok, "t_quar_13")
    linebot.save_facts("t_quar_13", [_fact(linebot._REL_KEY, "顧客（対等）", conf="低")],
                       status="pending")
    linebot.quarantine_add("t_quar_13", [_fact("好きなお酒", "焼酎"), _fact("本名", "鈴木一郎")])
    r = client.post("/api/liff/fixup/save", headers=tok,
                    json={"code": "t_quar_13", "呼び名": "すーさん",
                          "kind": "staff", "stand": "up", "rank": "B"})
    assert r.status_code == 200 and r.json().get("ok") is True
    assert db.get_contact("t_quar_13")["kind"] == "staff"
    rows = _fact_rows("t_quar_13", linebot._REL_KEY)
    assert rows[0]["status"] == "confirmed"
    assert rows[0]["v"] == linebot._rel_value("staff", "senior")   # 店内・スタッフ・先輩・目上
    assert _wait(lambda: not _marker("t_quar_13"))
    time.sleep(0.3)
    assert not (crm.get_attrs("t_quar_13") or {}).get("好きなお酒")
    assert _fact_rows("t_quar_13", "好きなお酒") == []
    assert _fact_rows("t_quar_13", "本名") == []


# ---------- 📥 取り込みジョブの検疫分岐 ----------

def test_import_job_new_contact_goes_to_quarantine(client, tok, monkeypatch):
    """新規カード=未確定 → 🔖のみsave_split(pending)、他の抽出は保留箱へ。カードは白紙のまま。"""
    from app import crm, db, liff, linebot
    code = "t_quar_14"
    monkeypatch.setattr(linebot, "extract_facts",
                        lambda text, partner, self_name: ([_fact("好きなお酒", "山崎ハイボール")], None))
    monkeypatch.setattr(linebot, "classify_relationship",
                        lambda text, contact, self_name: _fact(linebot._REL_KEY, "顧客（対等）", conf="低"))
    jid = _mk_job(code)
    liff._run_import_job(jid, code, _talk(code))
    assert _job(jid)["status"] == "done"
    ct = db.get_contact(code)
    assert ct is not None
    # v182: 裏予想の暫定適用(kind/stand)は入るが、🔖pendingは残る=未確定
    assert ct["kind"] == "customer" and ct["stand"] == "even"
    rel = _fact_rows(code, linebot._REL_KEY)
    assert rel and rel[0]["status"] == "pending"
    assert linebot.rel_confirmed(code) is False
    held = json.loads(_marker(code) or "[]")
    assert {"好きなお酒", "呼び名"} <= {f["k"] for f in held}   # 呼び名質問も保留箱行き
    # カード(attrs)にもfactsにも顧客抽出は書かれていない
    assert not (crm.get_attrs(code) or {}).get("好きなお酒")
    assert _fact_rows(code, "好きなお酒") == []


def test_import_job_confirmed_noncustomer_skips_extraction_permanently(client, tok, monkeypatch):
    """確定済み非顧客(staff)は再取り込みでも顧客抽出を恒久スキップ(検疫にも積まない)。"""
    from app import crm, liff, linebot
    code = "t_quar_15"
    mk_contact(client, tok, code, kind="staff")
    linebot.save_facts(code, [_fact(linebot._REL_KEY, "店内・スタッフ・対等")], status="confirmed")
    monkeypatch.setattr(linebot, "extract_facts",
                        lambda text, partner, self_name: ([_fact("好きなお酒", "生ビール")], None))
    monkeypatch.setattr(linebot, "classify_relationship",
                        lambda text, contact, self_name: None)
    jid = _mk_job(code)
    liff._run_import_job(jid, code, _talk(code))
    assert _job(jid)["status"] == "done"
    assert _marker(code) == ""                      # 検疫マーカーも作らない
    assert _fact_rows(code, "好きなお酒") == []       # 抽出は保存されない
    assert not (crm.get_attrs(code) or {}).get("好きなお酒")


def test_import_job_confirmed_customer_applies_directly(client, tok, monkeypatch):
    """確定済みの客は従来どおり即save_split(検疫を経ずカード反映)。"""
    from app import crm, liff, linebot
    code = "t_quar_16"
    mk_contact(client, tok, code, stand="even")     # kind既定=customer
    linebot.save_facts(code, [_fact(linebot._REL_KEY, "顧客（対等）")], status="confirmed")
    monkeypatch.setattr(linebot, "extract_facts",
                        lambda text, partner, self_name: ([_fact("好きなお酒", "ドンペリ")], None))
    monkeypatch.setattr(linebot, "classify_relationship",
                        lambda text, contact, self_name: None)
    jid = _mk_job(code)
    liff._run_import_job(jid, code, _talk(code))
    assert _job(jid)["status"] == "done"
    assert _marker(code) == ""
    assert (crm.get_attrs(code) or {}).get("好きなお酒") == "ドンペリ"
    assert [r["status"] for r in _fact_rows(code, "好きなお酒")] == ["applied"]


# ---------- §10: koi/ero等のkind分析はcustomer限定 ----------

def test_queue_koi_flag_is_customer_only(client, tok):
    """flag_koi=1でも非顧客(staff)は返信キューのkoi=0(v186/v187: customer限定)。"""
    from app import linebot
    mk_contact(client, tok, "t_quar_17", kind="staff", flag_koi=1)
    mk_contact(client, tok, "t_quar_18", flag_koi=1)   # customer
    for code in ("t_quar_17", "t_quar_18"):
        r = client.post("/api/incoming", json={"contact": code, "text": "今度いつ会える？"})
        assert r.status_code == 200
    items = {it["contact"]: it for it in linebot.build_queue()}
    assert items["t_quar_17"]["koi"] == 0 and items["t_quar_17"]["kind"] == "staff"
    assert items["t_quar_18"]["koi"] == 1


def test_template_drafts_koi_mode_customer_only(client):
    """定型下書き: ガチ恋モードはcustomer限定。staffカードには実務トーン(客UI誤爆なし)。"""
    from app import drafts
    cust = drafts._template_drafts({"flag_koi": 1, "kind": "customer"}, "好きだよ", "")
    assert any("線引き" in d["tone"] for d in cust)
    stf = drafts._template_drafts({"flag_koi": 1, "kind": "staff"}, "好きだよ", "")
    assert not any("線引き" in d["tone"] for d in stf)
    assert all("店" not in d["text"] or "待ってる" not in d["text"] for d in stf)


# ---------- LIFF fixup/bulk (最後に実行: 全体走査エンドポイントのため) ----------

def test_fixup_bulk_releases_customer_but_keeps_noncustomer_quarantine(client, tok):
    """⚡おまかせ: 客予想は検疫解放(適用)、非顧客予想では解放しない(不可逆破棄の防止)。"""
    from app import crm, db, linebot
    mk_contact(client, tok, "t_quar_19")               # kind既定=customer・stand空→仕分け対象
    mk_contact(client, tok, "t_quar_20", kind="staff")  # stand空→仕分け対象・staff予想
    linebot.quarantine_add("t_quar_19", [_fact("好きなお酒", "シャンパン")])
    linebot.quarantine_add("t_quar_20", [_fact("好きなお酒", "ハイボール")])
    r = client.post("/api/liff/fixup/bulk", headers=tok)
    assert r.status_code == 200 and r.json().get("ok") is True
    # 客: 解放されて保留が適用される
    assert _wait(lambda: (crm.get_attrs("t_quar_19") or {}).get("好きなお酒") == "シャンパン")
    assert _marker("t_quar_19") == ""
    # 非顧客予想: 検疫のまま維持(破棄は個別の種別確定タップのみ)
    time.sleep(0.5)
    assert db.get_contact("t_quar_20")["kind"] == "staff"
    held = json.loads(_marker("t_quar_20") or "[]")
    assert [f["v"] for f in held] == ["ハイボール"]
    assert not (crm.get_attrs("t_quar_20") or {}).get("好きなお酒")
