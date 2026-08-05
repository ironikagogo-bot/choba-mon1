"""SQLite 永続層。パイロット規模(数名)前提の素直な実装。"""
import json
import sqlite3
import time
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts(
  code TEXT PRIMARY KEY,          -- 表示名(コードネーム推奨)
  rank TEXT NOT NULL DEFAULT 'B', -- S/A/B
  cycle_days INTEGER,             -- 来店周期(日)
  last_visit_ts REAL,
  note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contact TEXT NOT NULL,
  text TEXT NOT NULL,
  ts REAL NOT NULL,
  category TEXT NOT NULL,         -- urgent / rally / batch
  reason TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open',  -- open / replied / stamped / deferred / skipped
  FOREIGN KEY(contact) REFERENCES contacts(code)
);
CREATE TABLE IF NOT EXISTS drafts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id INTEGER NOT NULL,
  text TEXT NOT NULL,
  tone TEXT DEFAULT '',
  FOREIGN KEY(message_id) REFERENCES messages(id)
);
CREATE TABLE IF NOT EXISTS style_profile(
  contact TEXT PRIMARY KEY,       -- '_global' = 本人全体の文体
  profile_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events(  -- 予約・同伴など、成績の元データ
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contact TEXT NOT NULL,
  kind TEXT NOT NULL,             -- visit / dohan / anniversary
  label TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'tentative',  -- tentative / confirmed
  created_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sent_replies(  -- 本人が実際に送った返信(学習用・copygo時に保存)
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contact TEXT NOT NULL,
  text TEXT NOT NULL,
  ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS push_subscriptions(  -- Web Push 購読(本人スマホ)
  endpoint TEXT PRIMARY KEY,
  subscription_json TEXT NOT NULL,
  created_ts REAL NOT NULL
);
"""


@contextmanager
def conn():
    # v72(9-8): 複数スレッド(受信取り込み・事前生成・ニュース・API)からの書き込みが並ぶため
    # WAL + busy_timeout を常時設定。'database is locked' の即死を防ぐ。
    c = sqlite3.connect(config.DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=10000")
    except Exception:
        pass
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        # 後付けカラムの移行(既存DBでも安全に)。既にあれば無視。
        for ddl in ("tags TEXT DEFAULT ''", "birthday TEXT DEFAULT ''"):
            try:
                c.execute(f"ALTER TABLE contacts ADD COLUMN {ddl}")
            except sqlite3.OperationalError:
                pass


def upsert_contact(code: str, rank: str = "B", cycle_days=None, note: str = "",
                   tags: str = "", birthday: str = ""):
    # v72(9-10): 同名で新規登録した時に既存カードのメモ・タグ・誕生日・周期を
    # 空値で上書き消失させない。渡された値が空なら既存値を保持する(CASE式)。
    with conn() as c:
        c.execute(
            "INSERT INTO contacts(code,rank,cycle_days,note,tags,birthday) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET "
            "  rank=excluded.rank, "
            "  note=CASE WHEN excluded.note IS NOT NULL AND excluded.note!='' THEN excluded.note ELSE contacts.note END, "
            "  tags=CASE WHEN excluded.tags IS NOT NULL AND excluded.tags!='' THEN excluded.tags ELSE contacts.tags END, "
            "  birthday=CASE WHEN excluded.birthday IS NOT NULL AND excluded.birthday!='' THEN excluded.birthday ELSE contacts.birthday END, "
            "  cycle_days=CASE WHEN excluded.cycle_days IS NOT NULL THEN excluded.cycle_days ELSE contacts.cycle_days END",
            (code, rank, cycle_days, note, tags, birthday),
        )


def set_tags(code: str, tags: str):
    with conn() as c:
        c.execute("UPDATE contacts SET tags=? WHERE code=?", (tags, code))


def set_last_visit(code: str, ts=None):
    """来店を記録(お礼・ご無沙汰判定の元データ)。eventsにも1件残す。"""
    ts = ts or time.time()
    with conn() as c:
        c.execute("UPDATE contacts SET last_visit_ts=? WHERE code=?", (ts, code))
        c.execute("INSERT INTO events(contact,kind,label,status,created_ts) "
                  "VALUES(?,?,?,?,?)", (code, "visit", "来店", "confirmed", ts))


def set_rank(code: str, rank: str):
    with conn() as c:
        c.execute("UPDATE contacts SET rank=? WHERE code=?", (rank, code))


def set_cycle(code: str, cycle_days: int):
    with conn() as c:
        c.execute("UPDATE contacts SET cycle_days=? WHERE code=?", (cycle_days, code))


def get_contact(code: str):
    with conn() as c:
        r = c.execute("SELECT * FROM contacts WHERE code=?", (code,)).fetchone()
        return dict(r) if r else None


def list_contacts():
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM contacts ORDER BY rank, code")]


def last_message_ts(contact: str):
    with conn() as c:
        r = c.execute(
            "SELECT ts FROM messages WHERE contact=? ORDER BY ts DESC LIMIT 1", (contact,)
        ).fetchone()
        return r["ts"] if r else None


def add_message(contact: str, text: str, category: str, reason: str, ts=None) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO messages(contact,text,ts,category,reason) VALUES(?,?,?,?,?)",
            (contact, text, ts or time.time(), category, reason),
        )
        return cur.lastrowid


def open_messages():
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM messages WHERE status='open' ORDER BY ts ASC")]


def get_message(mid: int):
    with conn() as c:
        r = c.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        return dict(r) if r else None


def set_status(mid: int, status: str, auto: bool = False):
    """auto=True は本人の操作でないシステム都合のクローズ(スレッド一括・残骸整理)。
    v72(9-7): swept=1 の印を付け、応答時間・返信数などの成績集計から除外する。"""
    with conn() as c:
        for ddl in ("acted_ts REAL", "swept INTEGER DEFAULT 0"):
            try:
                c.execute(f"ALTER TABLE messages ADD COLUMN {ddl}")
            except Exception:
                pass
        c.execute("UPDATE messages SET status=?, acted_ts=?, swept=? WHERE id=?",
                  (status, time.time(), 1 if auto else 0, mid))


def save_drafts(mid: int, drafts):
    with conn() as c:
        c.execute("DELETE FROM drafts WHERE message_id=?", (mid,))
        for d in drafts:
            c.execute("INSERT INTO drafts(message_id,text,tone) VALUES(?,?,?)",
                      (mid, d["text"], d.get("tone", "")))


def get_drafts(mid: int):
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM drafts WHERE message_id=?", (mid,))]


def track(name: str, ts=None):
    """機能イベント(名前と時刻のみ・本文なし)。使われた/使われない機能の計測用。"""
    with conn() as c:
        c.execute("CREATE TABLE IF NOT EXISTS feature_events("
                  "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, ts REAL NOT NULL)")
        c.execute("INSERT INTO feature_events(name,ts) VALUES(?,?)", (name, ts or time.time()))


def add_sent_reply(contact: str, text: str, ts=None, edited: int = 0, edit_ratio: int = 100):
    """本人が実際に送った返信を記録(学習用)。
    edited=1なら下書きを直した。edit_ratio=下書きとの一致度0-100(本文は使わず品質の連続指標として)。"""
    with conn() as c:
        for ddl in ("edited INTEGER DEFAULT 0", "edit_ratio INTEGER DEFAULT 100"):
            try:
                c.execute(f"ALTER TABLE sent_replies ADD COLUMN {ddl}")
            except Exception:
                pass
        c.execute("INSERT INTO sent_replies(contact,text,ts,edited,edit_ratio) VALUES(?,?,?,?,?)",
                  (contact, text, ts or time.time(), 1 if edited else 0, int(edit_ratio)))


def recent_dialogue(contact: str, limit: int = 8) -> list:
    """この相手との直近のやり取り(対応済みの受信＋自分の送信)を時系列で返す。
    未対応(open)の受信は含めない=生成側でスレッドとして別に渡すため。"""
    with conn() as c:
        inc = [{"who": "相手", "text": r["text"], "ts": r["ts"]} for r in c.execute(
            "SELECT text,ts FROM messages WHERE contact=? AND status!='open' "
            "ORDER BY ts DESC LIMIT ?", (contact, limit))]
        out = [{"who": "自分", "text": r["text"], "ts": r["ts"]} for r in c.execute(
            "SELECT text,ts FROM sent_replies WHERE contact=? "
            "ORDER BY ts DESC LIMIT ?", (contact, limit))]
    merged = sorted(inc + out, key=lambda x: x["ts"])
    return merged[-limit:]


def save_profile(contact: str, profile: dict):
    with conn() as c:
        c.execute(
            "INSERT INTO style_profile(contact,profile_json) VALUES(?,?) "
            "ON CONFLICT(contact) DO UPDATE SET profile_json=excluded.profile_json",
            (contact, json.dumps(profile, ensure_ascii=False)),
        )


def get_profile(contact: str = "_global"):
    with conn() as c:
        r = c.execute("SELECT profile_json FROM style_profile WHERE contact=?", (contact,)).fetchone()
        return json.loads(r["profile_json"]) if r else None


def clear_demo_messages():
    """デモ再生用: 受信・下書き・実績を消す(顧客とプロファイルは残す)。"""
    with conn() as c:
        c.execute("DELETE FROM drafts")
        c.execute("DELETE FROM messages")
        c.execute("DELETE FROM events")


def save_subscription(sub: dict):
    """Web Push 購読を保存(同一 endpoint は上書き)。"""
    with conn() as c:
        c.execute(
            "INSERT INTO push_subscriptions(endpoint,subscription_json,created_ts) VALUES(?,?,?) "
            "ON CONFLICT(endpoint) DO UPDATE SET subscription_json=excluded.subscription_json",
            (sub["endpoint"], json.dumps(sub, ensure_ascii=False), time.time()),
        )


def delete_subscription(endpoint: str):
    with conn() as c:
        c.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))


def list_subscriptions():
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM push_subscriptions")]


def add_event(contact: str, kind: str, label: str, status: str = "tentative"):
    with conn() as c:
        c.execute("INSERT INTO events(contact,kind,label,status,created_ts) VALUES(?,?,?,?,?)",
                  (contact, kind, label, status, time.time()))


def list_events():
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM events ORDER BY created_ts DESC")]
