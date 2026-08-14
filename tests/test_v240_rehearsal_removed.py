"""v240: 🎬リハーサルの撤去と、残した「受信案の生成」の保全。

本人裁定(2026-08-14)「リハーサル2回出る。このリハーサルしょぼいから全部一回消して。
受信案は取っておいて」。
- UI・ルートは全撤去(入口の重複もこれで消える)
- app/rehearsal.py の「その人が送ってきそうな1通を作る」部分は資産として保全
- 撤去前に作られた練習用データは**消していない**ので、記録に混ざらない番人だけ残す
"""
from tests.conftest import mk_contact

H = {"X-Ingest-Token": "tk"}
TALK = ("2026/08/01(土)\n"
        "21:00\t宝条\t今夜9時から3名で行けるかな\n"
        "21:05\tまりあ\tぜひぜひ！お待ちしてます\n"
        "21:10\t宝条\t例の玉響まだある？飲みたいんだけど\n"
        "21:12\tまりあ\tありますよ〜\n"
        "21:14\t宝条\t[スタンプ]\n"
        "21:15\t宝条\tうん\n"
        "23:40\t宝条\t今日はありがとう。また来週寄るね\n")


def _seed(code):
    from app import db, linebot
    linebot.ensure()
    db.save_profile("_selfname", {"name": "まりあ"})
    with db.conn() as c:
        c.execute("INSERT INTO linebot_talks(contact,text) VALUES(?,?) "
                  "ON CONFLICT(contact) DO UPDATE SET text=excluded.text", (code, TALK))


# ── 撤去できているか ────────────────────────────────────
def test_routes_are_gone(client, tok):
    for path in ("/api/liff/rehearsal/candidates",):
        assert client.get(path, headers=H).status_code == 404, path
    for path in ("/api/liff/rehearsal/start", "/api/liff/rehearsal/clear"):
        assert client.post(path, headers=H, json={}).status_code == 404, path


def test_home_has_no_rehearsal_flag(client, tok):
    assert "rehearsal" not in client.get("/api/liff/home", headers=H).json()


def test_client_has_no_entry_and_no_screen():
    """入口が0箇所(2箇所出ていた回帰)・画面も残骸も無い。"""
    import os
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "app", "static", "liff.html")
    html = open(p, encoding="utf-8").read()
    assert html.count("nav('#demo')") == 0
    for token in ("vDemo", "demoStart", "demoSheet", "dmsheet", '"#demo"', "リハーサル"):
        assert token not in html, token


# ── 受信案の生成は残っているか(資産の保全) ──────────────────
def test_incoming_lines_partner_only(client, tok):
    """相手が送ってきた行だけを拾う(自分の発言を相手の受信として出さない)。"""
    from app import rehearsal
    mk_contact(client, tok, "t_v240_a", rank="A")
    _seed("t_v240_a")
    lines = rehearsal._incoming_lines("t_v240_a")
    assert lines
    assert all("お待ちしてます" not in x and "ありますよ" not in x for x in lines)
    assert any("玉響" in x for x in lines)


def test_incoming_lines_filters_noise(client, tok):
    """スタンプ・短すぎる相槌は「来そうな1通」にならないので落とす。"""
    from app import rehearsal
    mk_contact(client, tok, "t_v240_n", rank="A")
    _seed("t_v240_n")
    lines = rehearsal._incoming_lines("t_v240_n")
    assert not any(x.startswith("[スタンプ]") for x in lines)
    assert "うん" not in lines            # 6字未満は落とす


def test_pick_replay_is_always_a_real_line(client, tok):
    """選ばれる1通は必ず実在の行(創作しない)。"""
    from app import rehearsal
    mk_contact(client, tok, "t_v240_p", rank="A")
    _seed("t_v240_p")
    lines = set(rehearsal._incoming_lines("t_v240_p"))
    for seed in range(8):
        assert rehearsal._pick_replay("t_v240_p", seed=seed) in lines


def test_pick_replay_empty_when_no_material(client, tok):
    from app import rehearsal
    mk_contact(client, tok, "t_v240_empty", rank="A")
    assert rehearsal._pick_replay("t_v240_empty") == ""


def test_candidates_need_material(client, tok):
    from app import rehearsal
    mk_contact(client, tok, "t_v240_empty2", rank="A")
    assert "t_v240_empty2" not in {c["code"] for c in rehearsal.candidates(limit=50)}
    mk_contact(client, tok, "t_v240_f", rank="A")
    _seed("t_v240_f")
    assert "t_v240_f" in {c["code"] for c in rehearsal.candidates(limit=50)}


# ── 残った練習データの扱い(消さない・でも混ぜない) ──────────────
def test_leftover_practice_data_never_reaches_records(client, tok):
    """撤去前に作られた練習用の受信が残っていても、送信記録・文体学習に入らない。"""
    from app import db, rehearsal
    mk_contact(client, tok, "t_v240_left", rank="A")
    mid = db.add_message("t_v240_left", "むかしの練習用", category="batch", reason="")
    with db.conn() as c:
        c.execute("UPDATE messages SET status='rehearsal' WHERE id=?", (mid,))
    assert rehearsal.is_rehearsal(mid) is True
    r = client.post("/api/liff/reply/act", headers=H,
                    json={"mid": mid, "action": "done", "text": "送ったことにする"})
    assert r.status_code == 400
    with db.conn() as c:
        n = c.execute("SELECT COUNT(*) FROM sent_replies WHERE contact=?",
                      ("t_v240_left",)).fetchone()[0]
    assert n == 0


def test_leftover_practice_data_is_invisible(client, tok):
    """受信箱・ホームの件数のどこにも出ない(残っていても邪魔にならない)。"""
    from app import db
    mk_contact(client, tok, "t_v240_inv", rank="A")
    before = client.get("/api/liff/home", headers=H).json()["queue"]
    mid = db.add_message("t_v240_inv", "むかしの練習用", category="batch", reason="")
    with db.conn() as c:
        c.execute("UPDATE messages SET status='rehearsal' WHERE id=?", (mid,))
    assert client.get("/api/liff/home", headers=H).json()["queue"] == before
    inbox = client.get("/api/liff/inbox", headers=H).json()
    assert not any(x["contact"] == "t_v240_inv" for x in inbox["items"])


def test_app_boots_without_the_module(client, tok, monkeypatch):
    """app/rehearsal.py を配布から外しても、番人がImportErrorを畳んで動く。"""
    import sys

    import app as _app
    monkeypatch.delattr(_app, "rehearsal", raising=False)
    monkeypatch.setitem(sys.modules, "app.rehearsal", None)
    from app import db
    mk_contact(client, tok, "t_v240_norm", rank="B")
    mid = db.add_message("t_v240_norm", "ふつうの受信", category="batch", reason="")
    r = client.post("/api/liff/reply/act", headers=H, json={"mid": mid, "action": "skipped"})
    assert r.status_code == 200, r.text
