"""端末リンク(①のQR)。あなたが安全に発行できる側。

フロー:
  1. create_link_token() でワンタイムトークン発行 → QR化してPWAに表示
  2. 本人のiPhoneがトークン付きURLを開く → claim_link_token() で端末を紐付け
  3. 以後この端末に Web プッシュを送れる

②のLINEログインQRとは別物(あちらはLINEが発行、desk.py 側で扱う)。
"""
import secrets
import time
import sqlite3
from contextlib import contextmanager

from .. import config

_LINK_TTL = 300  # 秒。QRの有効期限


@contextmanager
def _conn():
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS link_tokens(
          token TEXT PRIMARY KEY, user_id TEXT NOT NULL,
          created_ts REAL NOT NULL, claimed INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS devices(
          device_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
          push_endpoint TEXT, linked_ts REAL NOT NULL
        );
        """)


def create_link_token(user_id: str) -> dict:
    token = secrets.token_urlsafe(16)
    with _conn() as c:
        c.execute("INSERT INTO link_tokens(token,user_id,created_ts) VALUES(?,?,?)",
                  (token, user_id, time.time()))
    # PWA はこのURLをQRにする。本人のiPhoneで開くと claim される。
    return {"token": token, "link_url": f"/link?token={token}", "expires_in": _LINK_TTL}


def claim_link_token(token: str, push_endpoint: str | None = None) -> dict:
    with _conn() as c:
        r = c.execute("SELECT * FROM link_tokens WHERE token=?", (token,)).fetchone()
        if not r:
            raise ValueError("unknown token")
        if r["claimed"]:
            raise ValueError("already used")
        if time.time() - r["created_ts"] > _LINK_TTL:
            raise ValueError("expired")
        device_id = secrets.token_urlsafe(12)
        c.execute("UPDATE link_tokens SET claimed=1 WHERE token=?", (token,))
        c.execute("INSERT INTO devices(device_id,user_id,push_endpoint,linked_ts) VALUES(?,?,?,?)",
                  (device_id, r["user_id"], push_endpoint, time.time()))
        return {"device_id": device_id, "user_id": r["user_id"]}


def get_devices(user_id: str) -> list[dict]:
    with _conn() as c:
        return [dict(x) for x in c.execute("SELECT * FROM devices WHERE user_id=?", (user_id,))]
