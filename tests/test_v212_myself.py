"""v212: ペルソナに「この人へのわたし」(本人がこの相手にどんな顔で接しているか)を独立セクションで追加。
本人指示(2026-08-12)「対応している時の自分はどんな感じかも分析して項目を分けて表示」。
"""
from tests.conftest import mk_contact


def test_myself_in_payload_and_editable(client, tok):
    from app import linebot
    mk_contact(client, tok, "t_v212_p", rank="B")
    linebot.save_persona("t_v212_p", {
        "summary": "テスト", "sections": [{"k": "価値観の核", "v": "x", "src": "", "conf": "高"}],
        "tolerance": [],
        "myself": [{"k": "口調・距離", "v": "タメ口・絵文字多め", "src": "りょ!またね〜", "conf": "高"},
                    {"k": "演じている役", "v": "聞き役", "src": "", "conf": "中"}]})
    d = client.get("/api/liff/contact/t_v212_p", headers=tok).json()
    assert len(d["persona"]["myself"]) == 2
    # myfix
    r = client.post("/api/liff/persona/edit", headers=tok,
                    json={"code": "t_v212_p", "action": "myfix", "index": 0, "value": "敬語寄り"})
    assert r.status_code == 200 and r.json()["persona"]["myself"][0]["v"] == "敬語寄り"
    assert r.json()["persona"]["myself"][0]["conf"] == "中"   # 人手修正=中
    # mydel
    r = client.post("/api/liff/persona/edit", headers=tok,
                    json={"code": "t_v212_p", "action": "mydel", "index": 1})
    assert len(r.json()["persona"]["myself"]) == 1


def test_myself_prompt_keys_defined(client):
    from app import linebot
    assert linebot.MYSELF_KEYS == ("口調・距離", "演じている役", "盛り上げ方の癖", "気をつけたい癖")


def test_persona_without_myself_still_renders(client, tok):
    """旧ペルソナ(myself無し)は従来どおり=後方互換。"""
    from app import linebot
    mk_contact(client, tok, "t_v212_old", rank="B")
    linebot.save_persona("t_v212_old", {"summary": "旧", "sections": [
        {"k": "効く話題", "v": "y", "src": "", "conf": "中"}], "tolerance": []})
    d = client.get("/api/liff/contact/t_v212_old", headers=tok).json()
    assert d["persona"].get("myself") in (None, [])
