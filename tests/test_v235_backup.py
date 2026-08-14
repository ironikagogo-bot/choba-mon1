"""v235: バックアップ体制(既知の最重大課題「バックアップゼロ」)。
自動世代・整合スナップショット・ダウンロード口・検証つき復元。"""
import os

from tests.conftest import mk_contact

H = {"X-Ingest-Token": "tk"}


def test_snapshot_is_consistent_copy(client, tok):
    """スナップショットが読めるSQLiteで、中身が一致する。"""
    from app import backup as bk, db
    mk_contact(client, tok, "t_v235_a", rank="A")
    p = os.path.join(bk.backup_dir(), "_test_snap.db")
    n = bk.snapshot(p)
    assert n > 0 and os.path.exists(p)
    ok, why, info = bk.restore_validate(p)
    assert ok, why
    with db.conn() as c:
        live = c.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    assert info["contacts"] == live
    os.unlink(p)


def test_auto_snapshot_is_daily_and_rotates(client, tok):
    """同じ日には二度取らない(force=Trueなら取り直す)。
    注: app.mainのimportで既に起動時スナップショットが走っているので、
    「1本目が取れたか」ではなく「同日は取らない」を確かめる。"""
    from app import backup as bk
    bk.auto_snapshot()                           # まだ無ければここで作られる
    assert bk.auto_snapshot() is None            # 同日=取らない
    assert bk.auto_snapshot(force=True) is not None
    assert bk.status()["gen_n"] >= 1


def test_rotation_keeps_only_generations(client, tok):
    """世代数を超えたら古いものから消える(手動退避 pre_restore_* は残す)。"""
    from app import backup as bk
    d = bk.backup_dir()
    made = []
    for i in range(bk.GENERATIONS + 3):
        p = os.path.join(d, f"chouba_2000010{i:02d}.db")
        with open(p, "wb") as f:
            f.write(b"SQLite format 3\x00")
        made.append(p)
    keeper = os.path.join(d, "pre_restore_20000101_000000.db")
    with open(keeper, "wb") as f:
        f.write(b"SQLite format 3\x00")
    bk._rotate(d)
    left = [x for x in os.listdir(d) if x.startswith("chouba_")]
    assert len(left) <= bk.GENERATIONS
    assert os.path.exists(keeper)      # 退避は世代整理で消さない
    os.unlink(keeper)


def test_restore_rejects_garbage(client, tok):
    """SQLiteでないもの・別物のDBは復元しない。"""
    from app import backup as bk
    import sqlite3
    d = bk.backup_dir()
    junk = os.path.join(d, "_junk.bin")
    with open(junk, "wb") as f:
        f.write(b"this is not a database")
    ok, why, _ = bk.restore_validate(junk)
    assert not ok and "SQLite" in why
    other = os.path.join(d, "_other.db")
    c = sqlite3.connect(other)
    c.execute("CREATE TABLE hello(x)")
    c.commit()
    c.close()
    ok2, why2, _ = bk.restore_validate(other)
    assert not ok2 and "帳場くんのDB" in why2
    os.unlink(junk)
    os.unlink(other)


def test_backup_page_and_download_need_auth(client, tok):
    """認証なしでは開けない。ヘッダ認証なら開ける。"""
    assert client.get("/api/liff/backup").status_code in (401, 403)
    r = client.get("/api/liff/backup", headers=H)
    assert r.status_code == 200 and "バックアップ" in r.text
    r2 = client.get("/api/liff/backup?dl=1", headers=H)
    assert r2.status_code == 200
    assert r2.content[:15] == b"SQLite format 3"


def test_download_ticket_is_one_shot(client, tok):
    """チケットは1回きり(URLが履歴に残っても再利用できない)。"""
    info = client.get("/api/liff/backup_info", headers=H).json()
    t = info["ticket"]
    assert t
    r1 = client.get(f"/api/liff/backup?dl=1&t={t}")     # ヘッダ認証なしで通る
    assert r1.status_code == 200 and r1.content[:15] == b"SQLite format 3"
    r2 = client.get(f"/api/liff/backup?dl=1&t={t}")     # 2回目は弾く
    assert r2.status_code in (401, 403)


def test_gen_download_cannot_escape_dir(client, tok):
    """世代名にパス片を入れて backups/ の外を取れない。"""
    for bad in ("../chouba.db", "..%2Fchouba.db", "a/b.db", "chouba.txt"):
        r = client.get("/api/liff/backup", headers=H, params={"dl": 1, "gen": bad})
        assert r.status_code in (400, 404), bad


def test_home_exposes_backup_age(client, tok):
    d = client.get("/api/liff/home", headers=H).json()
    assert "backup_age" in d and "backup_warn" in d
