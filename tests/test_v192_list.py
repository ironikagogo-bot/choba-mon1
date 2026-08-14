"""v192: トリアージ一覧(案A)のサーバ側 — グループ「自分宛てだけ」フィルタ / 🔥ピン留め。

裁定(2026-08-11): 済み表示=上の琥珀ライン(クライアント)、グループ発言=自分宛てだけ、
🔥いま返す=用件(urgent)/ランクS/📌ピン留め(選別はクライアント、素材はサーバのpin/rank/urgent)。
"""
import json

from tests.conftest import mk_contact


def _selfname(name):
    from app import db
    db.save_profile("_selfname", {"name": name})


def _incoming(client, contact, text):
    r = client.post("/api/incoming", json={"contact": contact, "text": text})
    assert r.status_code == 200
    return r.json()


# ---------- group_visible 単体 ----------

def test_group_visible_nongroup_always_shown(client):
    from app import linebot
    _selfname("大山")
    assert linebot.group_visible("今夜あいてる?", "t_v192_solo") is True
    # 名前を含まなくても非グループは見せる
    assert linebot.group_visible("Eriが写真を送信しました…という話", "t_v192_solo") is True


def test_group_visible_system_lines_hidden(client):
    from app import linebot
    _selfname("大山")
    for t in ["【ママ友会】Eriが写真を送信しました",
              "【ママ友会】Eriがスタンプを送信しました",
              "【ママ友会】Aさんがメッセージの送信を取り消しました",
              "【ママ友会】Bが退会しました"]:
        assert linebot.group_visible(t, "t_v192_g") is False, t


def test_group_visible_self_addressed_only(client):
    from app import linebot
    _selfname("大山")
    assert linebot.group_visible("【ママ友会】大山さんもお盆こっち来ます?", "x") is True
    assert linebot.group_visible("【ママ友会】椅子だと思われる傷ができた", "x") is False
    # グループコード形式(「グループ名: 人名」)でも同じ判定
    assert linebot.group_visible("フェルトは買わない?", "ママ友会: えり") is False
    assert linebot.group_visible("大山ちゃんはどう思う?", "ママ友会: えり") is True


def test_group_visible_without_selfname_keeps_nonsystem(client):
    """名前未学習の間は消しすぎない(システム行だけ隠す)。"""
    from app import linebot
    _selfname("")
    try:
        assert linebot.group_visible("【ママ友会】椅子だと思われる傷ができた", "x") is True
        assert linebot.group_visible("【ママ友会】Eriが写真を送信しました", "x") is False
    finally:
        _selfname("大山")


# ---------- build_queue / inbox 連動 ----------

def test_inbox_group_thread_filters_to_self_addressed(client, tok):
    from app import linebot
    _selfname("大山")
    code = "t_v192_グループA: えり"
    _incoming(client, code, "【グループA】Eriが写真を送信しました")
    _incoming(client, code, "【グループA】椅子だと思われる傷ができた")
    _incoming(client, code, "【グループA】大山さんもお盆こっち来ます?")
    _incoming(client, code, "【グループA】だね")
    r = client.get("/api/liff/inbox", headers=tok).json()
    cards = [x for x in r["items"] if x["contact"] == code]
    assert len(cards) == 1
    c = cards[0]
    assert c["count"] == 1 and len(c["mids"]) == 1
    assert "大山さんもお盆" in c["text"]
    assert "写真を送信" not in c["text"] and "椅子" not in c["text"]
    assert c["grp_total"] == 4   # 間引き前の総通数
    assert c["grp"] == 1


def test_build_queue_drops_contact_when_no_self_addressed(client, tok):
    from app import linebot
    _selfname("大山")
    code = "t_v192_グループB: みか"
    _incoming(client, code, "【グループB】今日の集まりどうする?")
    _incoming(client, code, "【グループB】Eriが写真を送信しました")
    q = linebot.build_queue()
    assert not any(it["contact"] == code for it in q)
    r = client.get("/api/liff/inbox", headers=tok).json()
    assert not any(x["contact"] == code for x in r["items"])


def test_nongroup_contact_untouched_by_filter(client, tok):
    _selfname("大山")
    mk_contact(client, tok, "t_v192_plain", rank="B")
    _incoming(client, "t_v192_plain", "昨日はありがとう〜")
    r = client.get("/api/liff/inbox", headers=tok).json()
    c = [x for x in r["items"] if x["contact"] == "t_v192_plain"][0]
    assert c["count"] == 1 and c["grp_total"] == 0


# ---------- 🔥ピン留め ----------

def test_hotpin_toggle_and_inbox_field(client, tok):
    mk_contact(client, tok, "t_v192_pin", rank="B")
    _incoming(client, "t_v192_pin", "元気?")
    r = client.post("/api/liff/hotpin", json={"code": "t_v192_pin", "on": True}, headers=tok)
    assert r.status_code == 200 and r.json()["pin"] == 1
    inbox = client.get("/api/liff/inbox", headers=tok).json()
    c = [x for x in inbox["items"] if x["contact"] == "t_v192_pin"][0]
    assert c["pin"] == 1
    r = client.post("/api/liff/hotpin", json={"code": "t_v192_pin", "on": False}, headers=tok)
    assert r.json()["pin"] == 0
    inbox = client.get("/api/liff/inbox", headers=tok).json()
    c = [x for x in inbox["items"] if x["contact"] == "t_v192_pin"][0]
    assert c["pin"] == 0


def test_hotpin_requires_token_and_code(client, tok):
    assert client.post("/api/liff/hotpin", json={"code": "x", "on": 1}).status_code == 401
    assert client.post("/api/liff/hotpin", json={"on": 1}, headers=tok).status_code == 400


def test_hotpin_survives_merge_or_inherit(client, tok):
    """flag_hot はマージでOR継承(keep=customer限定・既存フラグと同じ規則)。"""
    from app import crm, db
    mk_contact(client, tok, "t_v192_mkeep", rank="B")
    mk_contact(client, tok, "t_v192_mabs", rank="B")
    client.post("/api/liff/hotpin", json={"code": "t_v192_mabs", "on": True}, headers=tok)
    r = crm.merge_contact("t_v192_mkeep", "t_v192_mabs")
    assert r.get("ok")
    keep = db.get_contact("t_v192_mkeep")
    assert int(keep.get("flag_hot") or 0) == 1


def test_yobina_strips_group_prefix_for_display(client, tok):
    """v192: グループ由来カード(code=「グループ名: 人名」)の表示名は人名だけ。
    本人指摘「グループで初着信した新規メンバーの登録名にグループ名が表示される」。"""
    from app import crm, linebot
    code = "t_v192会: ゆか"
    _incoming(client, code, "【t_v192会】大山さん来ます?")
    r = client.get("/api/liff/inbox", headers=tok).json()
    c = [x for x in r["items"] if x["contact"] == code][0]
    assert c["name"] == "ゆか"          # グループ名は出ない
    assert c["contact"] == code          # 同一性(code)は不変
    # 呼び名抽出後は「呼び名(人名)」形式(グループ名は括弧内にも出ない)
    crm.ensure(); crm.add_def("呼び名"); crm.set_attr(code, "呼び名", "ゆかちゃん")
    assert linebot._yobina(code) == "ゆかちゃん(ゆか)"


def test_home_breakdown_tiles(client, tok):
    """v196: ホーム受信タイルの内訳(緊急=非店内urgent/店内/ふつう/あとで)。"""
    mk_contact(client, tok, "t_v196_urg", rank="B")
    mk_contact(client, tok, "t_v196_stf", rank="B", kind="staff")
    mk_contact(client, tok, "t_v196_nrm", rank="B")
    _incoming(client, "t_v196_urg", "今から向かっていい?席ある?")   # urgent
    _incoming(client, "t_v196_stf", "シフトの件、明日でいい?")        # staff(urgent語彙でも店内枠)
    _incoming(client, "t_v196_nrm", "こないだはどうも〜")             # ふつう
    d = client.get("/api/liff/home", headers=tok).json()
    assert d["hot_urgent_n"] >= 1
    assert d["staff_n"] >= 1
    assert d["normal_n"] >= 1
    assert d["queue"] == d["hot_urgent_n"] + d["staff_n"] + d["normal_n"]


def test_group_guess_mark_tags_but_never_hides(client, tok):
    """v199: 【?名前】=グループ疑い印(sub_text推定)。👥タグは立つが宛先フィルタでは隠さない
    (誤検知で1対1のDMを消す事故の方が重い)。システム行だけは隠す。"""
    from app import linebot
    _selfname("大山")
    # 名指しが無くても visible(タグ専用)
    assert linebot.group_visible("【?テニス会】おめでとう!", "t_v199_g") is True
    # システム行は疑い印でも隠す
    assert linebot.group_visible("【?テニス会】Eriが写真を送信しました", "t_v199_g") is False
    mk_contact(client, tok, "t_v199_nakano", rank="B")
    _incoming(client, "t_v199_nakano", "【?テニス会】おめでとう!")
    r = client.get("/api/liff/inbox", headers=tok).json()
    c = [x for x in r["items"] if x["contact"] == "t_v199_nakano"][0]
    assert c["grp"] == 1                       # 👥タグの根拠
    assert c["count"] == 1                     # 隠されていない
    assert "【テニス会】おめでとう!" in c["text"]   # 表示では?を落とす
