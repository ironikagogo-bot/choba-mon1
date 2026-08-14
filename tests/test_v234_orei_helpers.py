"""v234: お礼のヘルプ候補 — 店内0人でも過去に手入力したヘルプ名を候補チップに再利用。
本人報告2026-08-13「ヘルプの時タップできる人が一切出てこない」(aki-test=店内分類0人)。"""
from tests.conftest import mk_contact

H = {"X-Ingest-Token": "tk"}


def test_past_typed_helper_becomes_candidate(client, tok, monkeypatch):
    """店内契約者ゼロ想定: 手入力ヘルプ名で記録→次回prefillのstaff候補に載る。
    共有DBの他テスト製staffに14枠を食われないよう、この検証中はlist_contactsを絞る。"""
    mk_contact(client, tok, "t_v234_主賓", rank="A")
    r = client.post("/api/liff/orei/record", headers=H,
                    json={"main": "t_v234_主賓", "stype": "in", "day": "today",
                          "dohan_venue": "", "after_venue": "",
                          "helpers": [{"contact": "れいなちゃん", "role": "help",
                                       "stand": "equal"}]})
    assert r.json()["ok"]
    from app import db as _db
    real = _db.list_contacts
    monkeypatch.setattr(_db, "list_contacts",
                        lambda: [c for c in real() if c["code"].startswith("t_v234_")])
    d = client.get("/api/liff/orei/prefill", headers=H).json()
    names = {s["name"] for s in d["staff"]}
    assert "れいなちゃん" in names


def test_staff_contact_still_first(client, tok, monkeypatch):
    """店内分類の相手は従来どおり候補に出る(手入力名より前)。"""
    mk_contact(client, tok, "t_v234_黒服", rank="B")
    r = client.post("/api/liff/classify", headers=H,
                    json={"contact": "t_v234_黒服", "kind": "staff"})
    assert r.status_code == 200
    from app import db as _db
    real = _db.list_contacts
    monkeypatch.setattr(_db, "list_contacts",
                        lambda: [c for c in real() if c["code"].startswith("t_v234_")])
    d = client.get("/api/liff/orei/prefill", headers=H).json()
    codes = [s["code"] for s in d["staff"]]
    assert "t_v234_黒服" in codes
    # 店内(kind=staff)は手入力名(れいなちゃん)より前に並ぶ
    assert "れいなちゃん" in codes
    assert codes.index("t_v234_黒服") < codes.index("れいなちゃん")
