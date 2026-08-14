"""v226: 配信の唐突さ対策 — A案(鮮度フィルタ)・B案(直近のやり取り)・つまみ・検品ヘルパ。"""
import time
from tests.conftest import mk_contact


def _msg(client, contact, text, ts=None):
    from app import db
    return db.add_message(contact, text, "batch", "", ts=ts)


def test_fresh_topic_keys_filters_stale(client, tok):
    """直近30日の会話に出た話題だけ通る(古い話題は落ちる)。"""
    from app import campaign, crm
    mk_contact(client, tok, "t_v226_a", rank="B")
    crm.add_def("趣味・関心"); crm.set_attr("t_v226_a", "趣味・関心", "ゴルフ")
    crm.add_def("好きなお酒"); crm.set_attr("t_v226_a", "好きなお酒", "ワイン")
    _msg(client, "t_v226_a", "週末ゴルフ行ってきました!")                       # 直近
    keys = campaign._fresh_topic_keys("t_v226_a")
    assert "趣味・関心" in keys        # ゴルフは直近会話に登場
    assert "好きなお酒" not in keys    # ワインは会話に出ていない


def test_recent_exchange_block(client, tok):
    from app import campaign, db
    mk_contact(client, tok, "t_v226_b", rank="B")
    _msg(client, "t_v226_b", "こないだの店また行きたいね")
    with db.conn() as c:
        c.execute("INSERT INTO sent_replies(contact, text, ts) VALUES(?,?,?)",
                  ("t_v226_b", "ぜひ!今度こそ🍷", time.time()))
    blk = campaign._recent_exchange_block("t_v226_b")
    assert "こないだの店" in blk and "ぜひ!" in blk
    assert "蒸し返さない" in blk       # ルール文言が入る
    assert not campaign._recent_exchange_block("存在しない相手xx")


def test_topic_hits_counts(client, tok):
    from app import campaign, crm
    mk_contact(client, tok, "t_v226_c", rank="B")
    crm.add_def("趣味・関心"); crm.set_attr("t_v226_c", "趣味・関心", "ゴルフ")
    crm.add_def("仕事・会社"); crm.set_attr("t_v226_c", "仕事・会社", "商社")
    assert campaign._topic_hits("週末はゴルフですか?商社もお忙しそう", "t_v226_c") == 2
    assert campaign._topic_hits("こんばんは!また来てね", "t_v226_c") == 0


def test_card_prompt_block_only_keys_keeps_safety(client, tok):
    """only_keys=空でも NG話題・担当の安全系は残る。話題は消える。"""
    from app import crm
    mk_contact(client, tok, "t_v226_d", rank="B")
    crm.add_def("趣味・関心"); crm.set_attr("t_v226_d", "趣味・関心", "ゴルフ")
    crm.add_def("NG話題"); crm.set_attr("t_v226_d", "NG話題", "家族の話")
    crm.add_def("担当"); crm.set_attr("t_v226_d", "担当", "れい")
    blk = crm.card_prompt_block("t_v226_d", only_keys=set())
    assert "ゴルフ" not in blk
    assert "NG話題" in blk and "れい" in blk


def test_ann_draft_accepts_plevel(client, tok):
    """plevelを渡してもAPIが受ける(キー無し環境=定型文フォールバック)。"""
    from app import crm
    mk_contact(client, tok, "t_v226_e", rank="A")
    crm.add_def("呼び名"); crm.set_attr("t_v226_e", "呼び名", "えーさん")
    r = client.post("/api/liff/ann/draft", headers=tok,
                    json={"code": "t_v226_e", "tone": "cust", "template": "", "plevel": 0})
    assert r.status_code == 200 and "えーさん" in r.json()["text"]
