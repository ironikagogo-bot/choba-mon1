"""顧客CRM拡張: LINE表示名の紐付け(エイリアス)、私用アカウント除外、
未紐付けトレイ、ユーザー定義のカスタム属性。既存 db.py を壊さず追加する層。

- contact_aliases: LINE表示名 → 顧客code (多対一)
- muted_names:     私用として取り込まない表示名
- pending_links:   未知の表示名(未紐付けトレイに隔離)
- attr_defs:       ユーザー定義属性の定義(型: choice/text/number/date)
- contact_attrs:   顧客ごとの属性値
"""
import time

from . import db

_READY = False

_SCHEMA = """
CREATE TABLE IF NOT EXISTS contact_aliases(
  line_name TEXT PRIMARY KEY,
  contact   TEXT NOT NULL,
  created_ts REAL
);
CREATE TABLE IF NOT EXISTS muted_names(
  line_name TEXT PRIMARY KEY,
  created_ts REAL
);
CREATE TABLE IF NOT EXISTS snoozed_names(
  line_name TEXT PRIMARY KEY,
  until_ts  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_links(
  line_name TEXT PRIMARY KEY,
  last_text TEXT DEFAULT '',
  last_ts   REAL,
  count     INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS attr_defs(
  key       TEXT PRIMARY KEY,
  atype     TEXT NOT NULL DEFAULT 'text',   -- choice / text / number / date
  options   TEXT NOT NULL DEFAULT '',       -- choice型の選択肢(カンマ区切り)
  created_ts REAL
);
CREATE TABLE IF NOT EXISTS contact_attrs(
  contact TEXT NOT NULL,
  akey    TEXT NOT NULL,
  value   TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(contact, akey)
);
"""


def ensure():
    global _READY
    if _READY:
        return
    with db.conn() as c:
        c.executescript(_SCHEMA)
        # 顧客に呼び方(nickname)・距離感(register)列を後付け(既にあれば無視)
        for ddl in ("nickname TEXT DEFAULT ''", "register TEXT DEFAULT ''", "real_name TEXT DEFAULT ''", "phone TEXT DEFAULT ''", "note_pos TEXT DEFAULT ''", "note_neg TEXT DEFAULT ''", "linked INTEGER DEFAULT 1", "kind TEXT DEFAULT 'customer'", "stand TEXT DEFAULT ''", "kids_bday TEXT DEFAULT ''", "founding TEXT DEFAULT ''", "flag_ero INTEGER DEFAULT 0", "flag_koi INTEGER DEFAULT 0", "company TEXT DEFAULT ''", "company_url TEXT DEFAULT ''", "company_note TEXT DEFAULT ''"):
            try:
                c.execute(f"ALTER TABLE contacts ADD COLUMN {ddl}")
            except Exception:
                pass
    _READY = True


# ---------- 受信の解決(取り込み経路から呼ぶ) ----------
def resolve_incoming(display_name: str) -> dict:
    """LINE表示名 → {action, contact}。
      action: 'muted'(破棄) / 'known'(取り込む・contactに解決) / 'unknown'(トレイへ)
    """
    ensure()
    name = (display_name or "").strip()
    if not name:
        return {"action": "unknown", "contact": None}
    with db.conn() as c:
        if c.execute("SELECT 1 FROM muted_names WHERE line_name=?", (name,)).fetchone():
            return {"action": "muted", "contact": None}
        _sn = c.execute("SELECT until_ts FROM snoozed_names WHERE line_name=?", (name,)).fetchone()
        if _sn and _sn["until_ts"] > time.time():
            return {"action": "muted", "contact": None}
        r = c.execute("SELECT contact FROM contact_aliases WHERE line_name=?", (name,)).fetchone()
        if r:
            return {"action": "known", "contact": r["contact"]}
    # エイリアス未登録でも、表示名がそのまま既存顧客codeなら既知扱い
    if db.get_contact(name):
        return {"action": "known", "contact": name}
    return {"action": "unknown", "contact": None}


def record_pending(display_name: str, text: str = "", ts: float = None):
    ensure()
    name = (display_name or "").strip()
    if not name:
        return
    ts = ts or time.time()
    with db.conn() as c:
        c.execute(
            "INSERT INTO pending_links(line_name,last_text,last_ts,count) VALUES(?,?,?,1) "
            "ON CONFLICT(line_name) DO UPDATE SET last_text=excluded.last_text, "
            "last_ts=excluded.last_ts, count=count+1",
            (name, text, ts),
        )


def list_pending() -> list:
    ensure()
    with db.conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM pending_links ORDER BY last_ts DESC")]


def resolve_pending(line_name: str, action: str, contact: str = None,
                    rank: str = "B") -> dict:
    """未紐付けトレイの1件を仕分ける。
      action: 'link'(既存客に紐付け) / 'new'(新規客) / 'private'(私用=除外)
    戻り値に、取りこぼし防止用の last_text/contact を含める。
    """
    ensure()
    name = (line_name or "").strip()
    if not name:
        return {"ok": False, "error": "empty name"}
    with db.conn() as c:
        row = c.execute("SELECT last_text FROM pending_links WHERE line_name=?", (name,)).fetchone()
        last_text = row["last_text"] if row else ""
    target = None
    if action == "private":
        mute(name)
    elif action == "link":
        if not contact:
            return {"ok": False, "error": "contact required for link"}
        add_alias(name, contact)
        link_contact(contact)
        target = contact
    elif action == "new":
        code = (contact or name).strip()
        db.upsert_contact(code, rank)
        add_alias(name, code)
        link_contact(code)
        target = code
    else:
        return {"ok": False, "error": "bad action"}
    with db.conn() as c:
        c.execute("DELETE FROM pending_links WHERE line_name=?", (name,))
    return {"ok": True, "action": action, "contact": target, "last_text": last_text}


# ---------- エイリアス / ミュート ----------
def add_alias(line_name: str, contact: str):
    ensure()
    with db.conn() as c:
        c.execute(
            "INSERT INTO contact_aliases(line_name,contact,created_ts) VALUES(?,?,?) "
            "ON CONFLICT(line_name) DO UPDATE SET contact=excluded.contact",
            ((line_name or "").strip(), contact, time.time()),
        )


def remove_alias(line_name: str, contact: str):
    """LINE表示名の紐付けを1件解除する(顧客本体は消さない)。"""
    ensure()
    with db.conn() as c:
        c.execute("DELETE FROM contact_aliases WHERE line_name=? AND contact=?", (line_name, contact))
    return {"ok": True, "aliases": aliases_for(contact)}


def aliases_for(contact: str) -> list:
    ensure()
    with db.conn() as c:
        return [r["line_name"] for r in c.execute(
            "SELECT line_name FROM contact_aliases WHERE contact=?", (contact,))]


def mute(line_name: str):
    ensure()
    name = (line_name or "").strip()
    with db.conn() as c:
        c.execute("INSERT OR IGNORE INTO muted_names(line_name,created_ts) VALUES(?,?)",
                  (name, time.time()))
        c.execute("DELETE FROM pending_links WHERE line_name=?", (name,))


def unmute(line_name: str):
    ensure()
    with db.conn() as c:
        c.execute("DELETE FROM muted_names WHERE line_name=?", ((line_name or "").strip(),))


def snooze(line_name: str, hours: float = 24):
    """その相手を一定時間だけ無視(通知/受信箱に出さない)。分類はしない・恒久muteでもない。"""
    ensure()
    name = (line_name or "").strip()
    if not name:
        return
    with db.conn() as c:
        c.execute("INSERT INTO snoozed_names(line_name,until_ts) VALUES(?,?) "
                  "ON CONFLICT(line_name) DO UPDATE SET until_ts=excluded.until_ts",
                  (name, time.time() + hours * 3600))
        c.execute("DELETE FROM pending_links WHERE line_name=?", (name,))


def list_muted() -> list:
    ensure()
    with db.conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM muted_names ORDER BY created_ts DESC")]


# ---------- カスタム属性 ----------
def list_defs() -> list:
    ensure()
    with db.conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM attr_defs ORDER BY created_ts")]


def add_def(key: str, atype: str = "text", options: str = "") -> dict:
    ensure()
    key = (key or "").strip()
    if not key:
        return {"ok": False, "error": "empty key"}
    if atype not in ("choice", "text", "number", "date"):
        atype = "text"
    with db.conn() as c:
        c.execute(
            "INSERT INTO attr_defs(key,atype,options,created_ts) VALUES(?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET atype=excluded.atype, options=excluded.options",
            (key, atype, options, time.time()),
        )
    return {"ok": True, "key": key, "atype": atype}


def set_attr(contact: str, key: str, value: str):
    ensure()
    with db.conn() as c:
        c.execute(
            "INSERT INTO contact_attrs(contact,akey,value) VALUES(?,?,?) "
            "ON CONFLICT(contact,akey) DO UPDATE SET value=excluded.value",
            (contact, (key or "").strip(), value),
        )


def get_attrs(contact: str) -> dict:
    ensure()
    with db.conn() as c:
        return {r["akey"]: r["value"] for r in c.execute(
            "SELECT akey,value FROM contact_attrs WHERE contact=?", (contact,))}


def contact_detail(code: str) -> dict:
    """顧客カード用: 基本情報＋エイリアス＋属性をまとめて返す。"""
    ensure()
    c = db.get_contact(code)
    if not c:
        return None
    c["aliases"] = aliases_for(code)
    c["attrs"] = get_attrs(code)
    return c


def search_contacts(q: str = "", attr_key: str = "", attr_val: str = "") -> list:
    """名前/メモ＋属性で顧客を検索。attr_key/attr_val 指定時はその属性値で絞る。"""
    ensure()
    q = (q or "").strip()
    attr_key = (attr_key or "").strip()
    attr_val = (attr_val or "").strip()
    with db.conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM contacts ORDER BY rank, code")]
        rows = [r for r in rows if (r.get("kind") or "customer") == "customer"]
        rows = [r for r in rows if r.get("linked") != 0]  # 未紐付け(未分類)は顧客リストに出さない
        if attr_key:
            keep = set(r["contact"] for r in c.execute(
                "SELECT contact FROM contact_attrs WHERE akey=?" +
                (" AND value=?" if attr_val else ""),
                ((attr_key, attr_val) if attr_val else (attr_key,))))
            rows = [x for x in rows if x["code"] in keep]
    if q:
        rows = [x for x in rows if q in (x.get("code") or "") or q in (x.get("note") or "")
                or q in (x.get("tags") or "")]
    # 属性とLINE紐付けも添える(リストの「未紐付け」表示に必要)
    for x in rows:
        x["attrs"] = get_attrs(x["code"])
        x["aliases"] = aliases_for(x["code"])
    return rows


# ---------- 顧客の基本項目更新(編集フォーム用) ----------
_ALLOWED = {"rank", "nickname", "register", "note", "tags", "cycle_days", "real_name", "phone", "note_pos", "note_neg", "stand", "kids_bday", "founding", "birthday", "flag_ero", "flag_koi", "company", "company_url", "company_note"}

def update_contact(code: str, fields: dict) -> dict:
    ensure()
    if not db.get_contact(code):
        return {"ok": False, "error": "contact not found"}
    sets, vals = [], []
    for k, v in (fields or {}).items():
        if k in _ALLOWED:
            sets.append(f"{k}=?"); vals.append(v)
    if sets:
        vals.append(code)
        with db.conn() as c:
            c.execute(f"UPDATE contacts SET {', '.join(sets)} WHERE code=?", vals)
    return {"ok": True}


# ---------- 未登録(unlinked)管理: 受信箱に出す/仕分ける ----------
def mark_unlinked(code: str):
    ensure()
    with db.conn() as c:
        c.execute("UPDATE contacts SET linked=0 WHERE code=?", (code,))

def link_contact(code: str):
    ensure()
    with db.conn() as c:
        c.execute("UPDATE contacts SET linked=1 WHERE code=?", (code,))

def is_linked(code: str) -> bool:
    ensure()
    with db.conn() as c:
        r = c.execute("SELECT linked FROM contacts WHERE code=?", (code,)).fetchone()
    if not r or r["linked"] is None:
        return True
    return int(r["linked"]) == 1

def discard_unlinked(code: str):
    """私用に仕分けた仮登録相手を、受信ごと消す(顧客・メッセージ・下書き等)。"""
    ensure()
    with db.conn() as c:
        ids = [row["id"] for row in c.execute("SELECT id FROM messages WHERE contact=?", (code,))]
        for mid in ids:
            c.execute("DELETE FROM drafts WHERE message_id=?", (mid,))
        c.execute("DELETE FROM messages WHERE contact=?", (code,))
        c.execute("DELETE FROM events WHERE contact=?", (code,))
        c.execute("DELETE FROM contact_attrs WHERE contact=?", (code,))
        c.execute("DELETE FROM contact_aliases WHERE contact=?", (code,))
        c.execute("DELETE FROM contacts WHERE code=?", (code,))


def rename_contact(old: str, new: str) -> dict:
    """カードの識別名(呼び名)を変更する。LINE表示名で自動作成されたカードを
    本当の呼び名に直すための機能。全テーブルのキーを付け替え、旧名は紐付けとして残す
    (=旧表示名からの受信は引き続きこのカードに入る)。"""
    ensure()
    old = (old or "").strip()
    new = (new or "").strip()
    if not old or not new:
        return {"ok": False, "error": "名前が空です"}
    if old == new:
        return {"ok": True, "code": new}
    if not db.get_contact(old):
        return {"ok": False, "error": "元の連絡先が見つかりません"}
    if db.get_contact(new):
        return {"ok": False, "error": "その名前は既に使われています"}
    with db.conn() as c:
        c.execute("UPDATE contacts SET code=? WHERE code=?", (new, old))
        for tbl, col in (("messages", "contact"), ("contact_aliases", "contact"),
                          ("contact_attrs", "contact"), ("style_profile", "contact"),
                          ("sent_replies", "contact"), ("events", "contact")):
            try:
                c.execute(f"UPDATE {tbl} SET {col}=? WHERE {col}=?", (new, old))
            except Exception:
                pass
        for tbl, col in (("sittings", "host_contact"), ("sitting_members", "contact")):
            try:
                c.execute(f"UPDATE {tbl} SET {col}=? WHERE {col}=?", (new, old))
            except Exception:
                pass
    # 旧名がLINE表示名だった場合に備えて紐付けを残す(重複はON CONFLICTで吸収)
    add_alias(old, new)
    return {"ok": True, "code": new}


def delete_contact(code: str):
    """顧客を完全削除(受信・下書き・実績・属性・別名・本体を全消去)。"""
    discard_unlinked(code)
    return {"ok": True, "deleted": code}


def mark_staff(code: str):
    """店内・業務(黒服/ママ/同僚)として登録。営業対象外・顧客リスト/実績に載せない。"""
    ensure()
    with db.conn() as c:
        c.execute("UPDATE contacts SET kind='staff', linked=1 WHERE code=?", (code,))


def reset_demo():
    """デモ全リセット: CRMの紐付け/私用/未登録/属性を全消去し、顧客も全削除(呼び出し側で再シード)。"""
    ensure()
    with db.conn() as c:
        for t in ("contact_aliases", "muted_names", "pending_links", "contact_attrs"):
            c.execute(f"DELETE FROM {t}")
        c.execute("DELETE FROM contacts")



# ---------- お席のメンバー候補 / 種別変更 / 移籍 ----------
def list_roster(kinds: str = "staff,peer,excolleague") -> list:
    """お席のメンバー候補を種別で絞って返す(店内staff/同業者peer/元同僚excolleague)。"""
    ensure()
    want = set(k.strip() for k in (kinds or "").split(",") if k.strip())
    with db.conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM contacts ORDER BY kind, rank, code")]
    rows = [r for r in rows if (r.get("kind") or "customer") in want]
    for r in rows:
        r["aliases"] = aliases_for(r["code"])
    return rows


def set_kind(code: str, kind: str) -> dict:
    """相手の種別を変更(customer/staff/peer/excolleague)。"""
    ensure()
    with db.conn() as c:
        c.execute("UPDATE contacts SET kind=?, linked=1 WHERE code=?", (kind, code))
    return {"ok": True, "code": code, "kind": kind}


def mark_peer(code: str) -> dict:
    return set_kind(code, "peer")


def reclassify(from_kind: str, to_kind: str) -> dict:
    """移籍など: ある種別を別種別へ一括付け替え(例: staff->excolleague)。非破壊。"""
    ensure()
    with db.conn() as c:
        cur = c.execute("UPDATE contacts SET kind=? WHERE kind=?", (to_kind, from_kind))
        n = cur.rowcount
    return {"ok": True, "moved": n, "from_kind": from_kind, "to_kind": to_kind}


# ---------- 記念日(命日は扱わない) ----------
def _md(s: str):
    s = (s or "").strip()
    if not s:
        return None
    import re as _re
    m = _re.match(r"^(\d{1,2})[-/](\d{1,2})$", s)
    if not m:
        return None
    mm, dd = int(m.group(1)), int(m.group(2))
    if 1 <= mm <= 12 and 1 <= dd <= 31:
        return (mm, dd)
    return None


def _anniv_text(kind: str, name: str, who: str = "") -> str:
    if kind == "self":
        return f"{name}、お誕生日おめでとうございます！素敵な一年になりますように。またお会いできるのを楽しみにしています。"
    if kind == "kid":
        w = who or "お子様"
        return f"{name}、{w}のお誕生日おめでとうございます！健やかなご成長を心よりお祈りしています。"
    if kind == "founding":
        return f"{name}、創立記念日おめでとうございます。益々のご発展を心よりお祈り申し上げます。"
    return f"{name}、おめでとうございます。"


def upcoming_anniversaries(within_days: int = 14, today=None) -> list:
    """今後 within_days 日以内の記念日。本人誕生日/お子様誕生日/創立記念日のみ。命日は扱わない。
    kids_bday は 'なまえ:MM-DD, なまえ:MM-DD' 形式。"""
    ensure()
    import datetime as _dt
    base = today or _dt.date.today()
    out = []
    with db.conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM contacts")]

    def emit(code, name, kind, label, mmdd, who=""):
        md = _md(mmdd)
        if not md:
            return
        mm, dd = md
        cand = []
        for y in (base.year, base.year + 1):
            try:
                d = _dt.date(y, mm, dd)
            except ValueError:
                d = _dt.date(y, mm, 28)
            cand.append(d)
        future = [d for d in cand if d >= base]
        nxt = min(future) if future else min(cand)
        days = (nxt - base).days
        if 0 <= days <= within_days:
            out.append({"code": code, "name": name, "kind": kind, "label": label,
                        "date": f"{mm:02d}-{dd:02d}", "days": days,
                        "draft": _anniv_text(kind, name, who)})

    for r in rows:
        if (r.get("kind") or "customer") not in ("customer", "peer"):
            continue
        code = r.get("code")
        nm = (r.get("nickname") or "").strip() or code
        emit(code, nm, "self", f"{nm} 様のお誕生日", r.get("birthday"))
        emit(code, nm, "founding", f"{nm} 様の創立記念日", r.get("founding"))
        for part in (r.get("kids_bday") or "").split(","):
            part = part.strip()
            if not part:
                continue
            pp = part.replace("：", ":")
            if ":" in pp:
                who, _, dt = pp.partition(":")
                emit(code, nm, "kid", f"{nm} 様の {who.strip()} のお誕生日", dt.strip(), who.strip())
            else:
                emit(code, nm, "kid", f"{nm} 様のお子様のお誕生日", part)
    out.sort(key=lambda x: x["days"])
    return out
