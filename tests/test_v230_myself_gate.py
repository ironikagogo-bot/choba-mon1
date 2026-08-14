"""v230: 🪞のA+Cハイブリッドゲート — 癖=自動ブレーキ(✕で停止)/役・口調=○のみ注入。
✎自由入力はUIから廃止(本人裁定: データが汚れる)。"""
from tests.conftest import mk_contact


def _seed_persona(code):
    from app import linebot
    linebot.ensure()
    linebot.save_persona(code, {
        "summary": "s", "sections": [{"k": "価値観の核", "v": "v", "src": "", "conf": "高"}],
        "tolerance": [],
        "myself": [
            {"k": "口調・距離", "v": "タメ口で短文", "src": "", "conf": "高"},
            {"k": "演じている役", "v": "聞き役", "src": "", "conf": "中"},
            {"k": "気をつけたい癖", "v": "誘われると即快諾しがち", "src": "", "conf": "高"},
        ]})


def test_all_default_on(client, tok):
    """v230改(本人裁定): 全項目が既定ONで下書きに効く。"""
    from app import linebot
    mk_contact(client, tok, "t_v230_a", rank="B")
    _seed_persona("t_v230_a")
    blk = linebot.myself_prompt_block("t_v230_a")
    assert "即快諾" in blk and "タメ口で短文" in blk and "聞き役" in blk
    assert "実例を優先" in blk


def test_ng_disables_ok_restores(client, tok):
    from app import linebot
    mk_contact(client, tok, "t_v230_b", rank="B")
    _seed_persona("t_v230_b")
    r = client.post("/api/liff/persona/edit", headers=tok,
                    json={"code": "t_v230_b", "action": "myng", "index": 0})   # 口調を✕
    assert r.status_code == 200
    blk = linebot.myself_prompt_block("t_v230_b")
    assert "タメ口で短文" not in blk and "聞き役" in blk   # ✕だけ止まる
    r2 = client.post("/api/liff/persona/edit", headers=tok,
                     json={"code": "t_v230_b", "action": "myok", "index": 0})  # ○で復帰
    assert r2.status_code == 200
    assert "タメ口で短文" in linebot.myself_prompt_block("t_v230_b")


def test_empty_when_no_persona(client):
    from app import linebot
    assert linebot.myself_prompt_block("存在しないxx") == ""
