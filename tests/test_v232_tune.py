"""v232: 🎛返信の調整ハブ — あたらしい学び(相手横断の未確認🚦🪞)+配信つまみ既定。
位置づけ(本人裁定2026-08-13): 確認は宿題ではない(既定ONで効いている)。ここは✕で止める場所。"""
from tests.conftest import mk_contact

H = {"X-Ingest-Token": "tk"}


def _seed(code, tol_unconfirmed=True, my_unconfirmed=True):
    from app import linebot
    linebot.ensure()
    tol = [{"k": "呼ばれ方", "v": "太郎さん", "src": "「太郎さんって呼んでよ」", "conf": "高"}]
    if not tol_unconfirmed:
        tol[0]["ok"] = 1
    my = [{"k": "口調・距離", "v": "丁寧語ベース", "src": "", "conf": "高"}]
    if not my_unconfirmed:
        my[0]["ok"] = 0
    linebot.save_persona(code, {"summary": "s", "sections": [],
                                "tolerance": tol, "myself": my})


def test_tune_lists_unconfirmed_only(client, tok):
    """未確認(okキー無し)だけが並ぶ。○✕確定済みは出ない。"""
    mk_contact(client, tok, "t_v232_a", rank="B")
    mk_contact(client, tok, "t_v232_b", rank="B")
    _seed("t_v232_a", tol_unconfirmed=True, my_unconfirmed=True)
    _seed("t_v232_b", tol_unconfirmed=False, my_unconfirmed=False)
    d = client.get("/api/liff/tune", headers=H).json()
    assert d["ok"]
    codes = {(x["code"], x["kind"]) for x in d["items"]}
    assert ("t_v232_a", "tol") in codes and ("t_v232_a", "my") in codes
    assert not any(c == "t_v232_b" for c, _ in codes)


def test_tune_act_via_persona_edit_removes(client, tok):
    """既存の persona/edit で○すると学びリストから消える(indexは不変=安全)。"""
    mk_contact(client, tok, "t_v232_c", rank="B")
    _seed("t_v232_c")
    d0 = client.get("/api/liff/tune", headers=H).json()
    it = next(x for x in d0["items"] if x["code"] == "t_v232_c" and x["kind"] == "tol")
    r = client.post("/api/liff/persona/edit", headers=H,
                    json={"code": "t_v232_c", "action": "tolok", "index": it["index"]})
    assert r.json()["ok"]
    d1 = client.get("/api/liff/tune", headers=H).json()
    assert not any(x["code"] == "t_v232_c" and x["kind"] == "tol" for x in d1["items"])
    # ○=採用として下書き注入対象に入る
    from app import linebot
    p = linebot.get_persona("t_v232_c")
    assert p["tolerance"][it["index"]]["ok"] == 1


def test_plevel_default_roundtrip(client, tok):
    """つまみ既定の保存→tune・homeの両方に反映。範囲外は丸める。"""
    r = client.post("/api/liff/tune/plevel", headers=H, json={"plevel": 2})
    assert r.json()["plevel_default"] == 2
    assert client.get("/api/liff/tune", headers=H).json()["plevel_default"] == 2
    assert client.get("/api/liff/home", headers=H).json()["plevel_default"] == 2
    assert client.post("/api/liff/tune/plevel", headers=H,
                       json={"plevel": 9}).json()["plevel_default"] == 2
    client.post("/api/liff/tune/plevel", headers=H, json={"plevel": 1})   # 後始末(既定に戻す)


def test_home_tune_count(client, tok):
    """ホームに未確認件数(tune_n)が乗る。"""
    mk_contact(client, tok, "t_v232_d", rank="B")
    _seed("t_v232_d")
    d = client.get("/api/liff/home", headers=H).json()
    assert d["tune_n"] >= 2   # t_v232_dのtol+myが最低限
