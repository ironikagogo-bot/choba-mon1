"""v233: 学び30件超の挙動修正(total=総数・30件ずつ配る)+一括○のURL口(tune_ackall)。
本人裁定2026-08-13「既存txt分は全てOK。モニターに100回以上タップは厳しい」。"""
from tests.conftest import mk_contact

H = {"X-Ingest-Token": "tk"}


def _seed_many(n_contacts=8, items_per=5):
    """8人×5件=40件の未確認学びを作る(30件のページ境界を跨ぐ)。"""
    from app import linebot
    linebot.ensure()
    for ci in range(n_contacts):
        code = f"t_v233_{ci}"
        tol = [{"k": f"項目{i}", "v": f"値{ci}-{i}", "src": "", "conf": "中"}
               for i in range(items_per)]
        linebot.save_persona(code, {"summary": "s", "sections": [],
                                    "tolerance": tol, "myself": []})
    return n_contacts * items_per


def test_total_exceeds_page(client, tok):
    """40件仕込む→ items は30件・total は40件以上(バッジ=総数)。"""
    for ci in range(8):
        mk_contact(client, tok, f"t_v233_{ci}", rank="B")
    n = _seed_many()
    d = client.get("/api/liff/tune", headers=H).json()
    assert d["total"] >= n
    assert len(d["items"]) == 30


def test_ackall_confirm_page_then_run(client, tok):
    """GET=確認ページ(実行しない)→POST=全件ok=1。実行後 total=0。"""
    r = client.get("/api/liff/tune_ackall?key=tk")
    assert r.status_code == 200 and "一括" in r.text
    # GETでは何も変わっていない
    assert client.get("/api/liff/tune", headers=H).json()["total"] > 0
    r = client.post("/api/liff/tune_ackall", data={"key": "tk"})
    assert r.status_code == 200 and "一括確定しました" in r.text
    d = client.get("/api/liff/tune", headers=H).json()
    assert d["total"] == 0 and d["items"] == []
    # ok=1が付いている(値は書き換えない)
    from app import linebot
    p = linebot.get_persona("t_v233_0")
    assert all(it.get("ok") == 1 for it in p["tolerance"])
    assert p["tolerance"][0]["v"] == "値0-0"


def test_ackall_bad_key_denied(client, tok):
    r = client.post("/api/liff/tune_ackall", data={"key": "wrong"})
    assert r.status_code in (401, 403)
