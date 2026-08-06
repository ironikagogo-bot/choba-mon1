"""帳場リーダー v0.5 認証 (v102)。

- リーダー専用トークン: 書き込み専用(notify/heartbeatのみ)。端末ごとに発行・失効可。
  DBにはSHA-256ハッシュのみ保存(平文トークンはclaim応答の1回だけ)。
- プロビジョニング: LIFFがワンタイムコード(10分・1回きり)をQRで提示 →
  リーダーが POST /api/reader/claim {code} → トークン受領。
  手動フォールバック: {password}=玄関パスワードでも可(レート制限つき)。
- 旧方式(INGEST_TOKEN直指定)は当面併用(移行の断絶なし)。
"""
import hashlib
import secrets
import time

from . import db


def ensure():
    with db.conn() as c:
        c.execute("CREATE TABLE IF NOT EXISTS reader_tokens("
                  "id INTEGER PRIMARY KEY AUTOINCREMENT, token_hash TEXT UNIQUE, "
                  "label TEXT, created REAL, last_seen REAL, battery TEXT, "
                  "revoked INTEGER DEFAULT 0)")
        c.execute("CREATE TABLE IF NOT EXISTS reader_codes("
                  "id INTEGER PRIMARY KEY AUTOINCREMENT, code_hash TEXT UNIQUE, "
                  "exp REAL, used INTEGER DEFAULT 0)")


def _h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ---- ワンタイムコード(QRの中身・10分・1回きり) ----

CODE_TTL = 600


def make_code() -> str:
    ensure()
    # 紛らわしい文字(0/O/1/I)抜きの8文字。手動打ちの保険にもなる
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    code = "".join(secrets.choice(alphabet) for _ in range(8))
    with db.conn() as c:
        c.execute("DELETE FROM reader_codes WHERE exp < ?", (time.time(),))
        c.execute("INSERT INTO reader_codes(code_hash, exp, used) VALUES(?,?,0)",
                  (_h(code), time.time() + CODE_TTL))
    return code


def claim_code(code: str) -> bool:
    ensure()
    with db.conn() as c:
        r = c.execute("SELECT id, exp, used FROM reader_codes WHERE code_hash=?",
                      (_h((code or "").strip().upper()),)).fetchone()
        if not r or r["used"] or r["exp"] < time.time():
            return False
        c.execute("UPDATE reader_codes SET used=1 WHERE id=?", (r["id"],))
    return True


# ---- リーダートークン ----

def issue(label: str = "") -> str:
    ensure()
    token = "rdr_" + secrets.token_urlsafe(24)
    with db.conn() as c:
        c.execute("INSERT INTO reader_tokens(token_hash,label,created,last_seen,revoked) "
                  "VALUES(?,?,?,?,0)", (_h(token), (label or "リーダー端末")[:40],
                                        time.time(), time.time()))
    return token


def check(token: str, battery=None) -> bool:
    """notify/heartbeat用。有効ならlast_seen更新してTrue。"""
    if not token or not token.startswith("rdr_"):
        return False
    ensure()
    with db.conn() as c:
        r = c.execute("SELECT id, revoked FROM reader_tokens WHERE token_hash=?",
                      (_h(token),)).fetchone()
        if not r or r["revoked"]:
            return False
        c.execute("UPDATE reader_tokens SET last_seen=?, battery=COALESCE(?,battery) WHERE id=?",
                  (time.time(), str(battery) if battery is not None else None, r["id"]))
    return True


def list_readers() -> list:
    ensure()
    with db.conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, label, created, last_seen, battery, revoked "
            "FROM reader_tokens ORDER BY id DESC LIMIT 10")]


def revoke(rid: int):
    ensure()
    with db.conn() as c:
        c.execute("UPDATE reader_tokens SET revoked=1 WHERE id=?", (rid,))
