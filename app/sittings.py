"""お席（同卓）＝一次オブジェクト。1席を記録すると御礼リスト・実績が同じ記録を共有する。

- sittings:        席（日付・主賓客）
- sitting_members: 席のメンバー（役割 role・立場 stand・送信済み sent）
御礼は役割×立場のテンプレで即時生成（解散直後の速さ優先。AI増強は将来）。
"""
import time

from . import db

_READY = False
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sittings(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date_label TEXT DEFAULT '',
  main_contact TEXT DEFAULT '',
  stype TEXT DEFAULT '',       -- ''=店内お席あり / gaiso=店外のみ(食事・ゴルフ等)
  venue TEXT DEFAULT '',       -- 店外のみの行き先(任意)
  dohan_venue TEXT DEFAULT '', -- 同伴のお店(空=同伴なし)
  after_venue TEXT DEFAULT '', -- アフターのお店(空=アフターなし)
  created_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sitting_members(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sitting_id INTEGER NOT NULL,
  contact TEXT NOT NULL,
  role TEXT NOT NULL,          -- customer/intro/peer/after/help/report
  stand TEXT DEFAULT 'equal',  -- senior/equal/junior
  sent INTEGER NOT NULL DEFAULT 0,
  sent_ts REAL
);
"""

def ensure():
    global _READY
    if _READY:
        return
    with db.conn() as c:
        c.executescript(_SCHEMA)
        for ddl in ("stype TEXT DEFAULT ''", "venue TEXT DEFAULT ''",
                    "dohan_venue TEXT DEFAULT ''", "after_venue TEXT DEFAULT ''"):
            try:
                c.execute(f"ALTER TABLE sittings ADD COLUMN {ddl}")
            except Exception:
                pass
    _READY = True


ROLE_LABEL = {"customer": "主賓客", "intro": "紹介者", "guest": "同席顧客", "peer": "同業者",
              "after": "アフター先のお店", "help": "ヘルプ", "report": "担当ママへ共有"}


def create_sitting(date_label: str, main: str, members: list,
                   stype: str = "", venue: str = "",
                   dohan_venue: str = "", after_venue: str = "") -> int:
    """members = [{contact, role, stand}]。主賓客は role=customer で含める。
    stype: ''=店内お席あり / gaiso=店外のみ。venue=店外のみの行き先。
    dohan_venue/after_venue: 同伴・アフターのお店(空=なし)。同じ夜に両方あってよい。
    来店/同伴/紹介の実績イベントも自動記録。"""
    ensure()
    now = time.time()
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO sittings(date_label, main_contact, stype, venue, dohan_venue, after_venue, created_ts) "
            "VALUES(?,?,?,?,?,?,?)",
            (date_label or "", main or "", stype or "", venue or "",
             dohan_venue or "", after_venue or "", now))
        sid = cur.lastrowid
        for m in members or []:
            code = (m.get("contact") or "").strip()
            if not code:
                continue
            c.execute("INSERT INTO sitting_members(sitting_id,contact,role,stand,sent) "
                      "VALUES(?,?,?,?,0)",
                      (sid, code, m.get("role", "peer"), m.get("stand", "equal")))
    # 実績イベント(入力ゼロ): 主賓客の来店/同伴 + 紹介
    if main:
        try:
            if stype != "gaiso":   # 店内お席ありは来店扱い
                db.set_last_visit(main)
            if (dohan_venue or "").strip():
                db.add_event(main, "dohan", f"{main} 同伴({dohan_venue})", "confirmed")
        except Exception:
            pass
    for m in members or []:
        if m.get("role") == "intro" and m.get("contact"):
            try:
                db.add_event(main or "", "intro", f"{m['contact']} → {main} 紹介", "confirmed")
            except Exception:
                pass
    return sid


def list_sittings() -> list:
    ensure()
    with db.conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM sittings ORDER BY created_ts DESC")]
        for s in rows:
            ms = [dict(r) for r in c.execute(
                "SELECT * FROM sitting_members WHERE sitting_id=?", (s["id"],))]
            s["members"] = ms
            s["member_count"] = len(ms)
            s["sent_count"] = sum(1 for x in ms if x["sent"])
    return rows


def get_sitting(sid: int) -> dict | None:
    ensure()
    with db.conn() as c:
        r = c.execute("SELECT * FROM sittings WHERE id=?", (sid,)).fetchone()
        if not r:
            return None
        s = dict(r)
        s["members"] = [dict(x) for x in c.execute(
            "SELECT * FROM sitting_members WHERE sitting_id=? ORDER BY id", (sid,))]
        return s


def mark_sent(sid: int, contact: str):
    ensure()
    with db.conn() as c:
        c.execute("UPDATE sitting_members SET sent=1, sent_ts=? WHERE sitting_id=? AND contact=?",
                  (time.time(), sid, contact))


def _name(code: str) -> str:
    c = db.get_contact(code) or {}
    return (c.get("nickname") or "").strip() or code


def orei_text(role: str, stand: str, name: str, main: str,
              stype: str = "", venue: str = "",
              dohan_venue: str = "", after_venue: str = "") -> str:
    """役割×立場のテンプレ御礼。同伴・アフターは独立要素として文に織り込む(同じ夜に両方可)。"""
    _dv = (dohan_venue or "").strip()
    _av = (after_venue or "").strip()
    _gv = (venue or "").strip()
    if role == "customer":
        parts = []
        if _dv:
            parts.append(f"同伴の{_dv}、とても美味しかったです！")
        if stype == "gaiso":
            parts.append(f"今日は{_gv}、ご一緒させていただきありがとうございました！とても楽しかったです。"
                         if _gv else "今日はご一緒させていただきありがとうございました！とても楽しかったです。")
        elif stand == "senior":
            parts.append(f"{'そのあとのお席まで、' if _dv else '本日はお越しくださり誠に'}ありがとうございました。")
        else:
            parts.append(f"{'そのあとのお席も楽しかったです！' if _dv else '本日はありがとうございました。'}")
        if _av:
            parts.append(f"{_av}のアフターまでお付き合いいただき、嬉しかったです。")
        # 締め
        if stype == "gaiso":
            parts.append("今度はお店にもぜひ。")
        elif stand == "senior":
            parts.append("またお目にかかれますよう、心よりお待ちしております。")
        else:
            parts.append("またお会いできる日を楽しみにしています。")
        return f"{name}、" + "".join(parts)
    if role == "intro":
        return f"{name}、本日は{main}をご紹介いただきありがとうございました！おかげさまで良いお席になりました、感謝です。"
    if role == "guest":
        if stand == "senior":
            return f"{name}、本日はご一緒させていただきありがとうございました。おかげさまで楽しいお席になりました。またお目にかかれますように。"
        return f"{name}、今日はご一緒できて嬉しかったです！ありがとうございました。またぜひ。"
    if role == "after":
        if _av:
            return f"{name}、今夜はありがとうございました！{_av}、居心地がよくて素敵な時間でした。また寄らせてくださいね。"
        return f"{name}、今夜はお伺いできて嬉しかったです！ありがとうございました。また寄らせてくださいね。"
    if role == "peer":
        if stand == "senior":
            return f"{name}、今日はご一緒できて嬉しかったです！近いうちにお店伺いますね。"
        if stand == "junior":
            return f"{name}、今日は楽しかった〜！また一緒にやろうね。"
        return f"{name}、今日はありがとう！また近いうち会お〜。"
    if role == "help":
        return f"{name}、今日はヘルプありがとう！すごく助かった。"
    if role == "report":
        return f"{main}さんご来店、御礼を一巡します。共有まで。"
    return f"{name}、今日はありがとうございました。"


def generate_orei(sid: int) -> list:
    """席のメンバー全員の御礼を生成。先輩→対等→後輩の順で返す。"""
    s = get_sitting(sid)
    if not s:
        return []
    main = s.get("main_contact", "")
    # 旧v56形式(stype=dohan/after)は新フィールドに読み替え
    _sty = s.get("stype", "") or ""
    _ven = s.get("venue", "") or ""
    _dv = s.get("dohan_venue", "") or ""
    _av = s.get("after_venue", "") or ""
    if _sty == "dohan":
        _dv, _sty = (_dv or _ven), ""
    elif _sty == "after":
        _av, _sty = (_av or _ven), ""
    order = {"senior": 0, "equal": 1, "junior": 2}
    out = []
    for m in s["members"]:
        nm = _name(m["contact"])
        out.append({
            "contact": m["contact"], "name": nm,
            "role": m["role"], "role_label": ROLE_LABEL.get(m["role"], m["role"]),
            "stand": m.get("stand", "equal"),
            "sent": m.get("sent", 0),
            "text": orei_text(m["role"], m.get("stand", "equal"), nm, main,
                              _sty, _ven, _dv, _av),
        })
    out.sort(key=lambda x: order.get(x["stand"], 1))
    return out
