"""v217: 配信文面の呼びかけは呼び名(本人指摘2026-08-13: Yasuhiro Yamamotoさんと登録名で
呼びかけていた)+カードAPIのneeds_fixup(通知→カード動線とホームの宿題一貫性)。
"""
from tests.conftest import mk_contact


def test_ann_fallback_uses_yobina(client, tok):
    """AIキー無しフォールバックの定型文でも呼び名で呼ぶ。"""
    from app import crm, campaign
    mk_contact(client, tok, "Yasuhiro Yamamoto", rank="A")
    crm.add_def("呼び名"); crm.set_attr("Yasuhiro Yamamoto", "呼び名", "山本さん")
    r = campaign.generate(mode="greeting", ranks=["S", "A", "B"], codes=["Yasuhiro Yamamoto"])
    text = r["items"][0]["text"]
    assert "山本さん" in text
    assert "Yasuhiro Yamamotoさん" not in text


def test_force_yobina_guard(client):
    """AI出力に登録名呼びかけが残っても決定論で呼び名へ。部分文字列関係は触らない。"""
    from app import campaign
    v = {"code": "Yasuhiro Yamamoto", "yobina": "山本さん"}
    out = campaign._force_yobina("Yasuhiro Yamamotoさん こんにちは。", v)
    assert out.startswith("山本さん こんにちは")
    out2 = campaign._force_yobina("Yasuhiro Yamamoto、お久しぶりです", v)
    assert out2.startswith("山本さん、")
    # 山本/山本さんのような包含関係は二重敬称の危険があるため無変換
    v2 = {"code": "山本", "yobina": "山本さん"}
    assert campaign._force_yobina("山本さん こんにちは", v2) == "山本さん こんにちは"
    # 呼び名なしは無変換
    assert campaign._force_yobina("A子さん やあ", {"code": "A子", "yobina": ""}) == "A子さん やあ"


def test_card_needs_fixup_flag(client, tok):
    """呼び名未設定→needs_fixup=true。呼び名+種別・立場確定→false。"""
    from app import crm, db
    from urllib.parse import quote
    mk_contact(client, tok, "t_v217_fx", rank="B")
    with db.conn() as c:
        c.execute("UPDATE contacts SET stand='' WHERE code=?", ("t_v217_fx",))
    r = client.get(f"/api/liff/contact/{quote('t_v217_fx')}", headers=tok)
    assert r.status_code == 200 and r.json()["needs_fixup"] is True
    crm.add_def("呼び名"); crm.set_attr("t_v217_fx", "呼び名", "てすとさん")
    with db.conn() as c:
        c.execute("UPDATE contacts SET kind='customer', stand='even' WHERE code=?", ("t_v217_fx",))
    r2 = client.get(f"/api/liff/contact/{quote('t_v217_fx')}", headers=tok)
    assert r2.status_code == 200 and r2.json()["needs_fixup"] is False
