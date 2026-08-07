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
        # v121: 属性の記録日時(昔のアポを今の予定と誤用しないための材料)
        try:
            c.execute("ALTER TABLE contact_attrs ADD COLUMN updated_ts REAL")
        except Exception:
            pass
    _READY = True


# ---------- 受信の解決(取り込み経路から呼ぶ) ----------

# v145: 表示名の表記ゆれ吸収。グループ着信は同一人物でも「空白・絵文字・敬称・
# グループ印・大文字小文字」が1:1と揺れるため、完全一致だけでは既存カードに繋がらなかった
import unicodedata as _ud
_HON_TAIL = None  # 遅延コンパイル


def _norm_name(s: str) -> str:
    """名前照合用の正規化: NFKC→小文字→末尾敬称除去→空白・記号・絵文字を除去。"""
    global _HON_TAIL
    if _HON_TAIL is None:
        _HON_TAIL = _re_g.compile(r"(さん|様|さま|ちゃん|くん|君|先生)$")
    s = _ud.normalize("NFKC", (s or "")).strip().lower()
    s = _HON_TAIL.sub("", s)
    return "".join(ch for ch in s if ch.isalnum())


def find_candidates(display_name: str, limit: int = 3) -> list:
    """表示名から既存カードの候補を探す。戻り: [{code, why, strong}]。
    strong=正規化後の完全一致(自動紐付けに使える) / 弱=部分一致(UIで提案のみ)。"""
    ensure()
    g, p = group_split(display_name or "")
    nb = _norm_name(p if g else (display_name or ""))
    if not nb:
        return []
    out, seen = [], set()
    _self = (display_name or "").strip()

    def hit(code, why, strong):
        # 自分自身(同名の仮カード)は候補として無意味なので出さない
        if code and code != _self and code not in seen:
            seen.add(code)
            out.append({"code": code, "why": why, "strong": strong})

    with db.conn() as c:
        # 未紐付けの仮カード(linked=0)は候補にしない(仮カード同士を繋いでも意味がない)
        codes = [r["code"] for r in c.execute(
            "SELECT code FROM contacts WHERE COALESCE(linked,1)!=0")]
        aliases = [(r["line_name"], r["contact"]) for r in
                   c.execute("SELECT line_name, contact FROM contact_aliases")]
    for ln, ct in aliases:
        if _norm_name(ln) == nb:
            hit(ct, "別名が一致", True)
    for code in codes:
        keys = [(code, "表示名")]
        try:
            a = get_attrs(code) or {}
            if a.get("呼び名"):
                keys.append((a["呼び名"], "呼び名"))
            if a.get("本名"):
                keys.append((a["本名"], "本名"))
        except Exception:
            pass
        for k, lab in keys:
            nk = _norm_name(k)
            if not nk:
                continue
            if nk == nb:
                hit(code, f"{lab}が一致", True)
                break
            if len(nb) >= 3 and len(nk) >= 3 and (nb in nk or nk in nb):
                hit(code, f"{lab}に近い", False)
                break
    strong = [x for x in out if x["strong"]]
    weak = [x for x in out if not x["strong"]]
    return (strong + weak)[:limit]


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
    # v145: 表記ゆれの「強一致」がちょうど1件なら自動で既存カードに紐付ける
    # (グループ着信の別表記で毎回未登録に落ちる問題)。次回からは別名で即一致。
    # 部分一致(弱)は自動にせず、仕分けUIの候補提案に回す(誤紐付け防止)
    try:
        strong = [x for x in find_candidates(name) if x["strong"]]
        if len(strong) == 1:
            add_alias(name, strong[0]["code"])
            print(f"[resolve] 自動紐付け: {name!r} → {strong[0]['code']!r} ({strong[0]['why']})",
                  flush=True)
            return {"action": "known", "contact": strong[0]["code"]}
    except Exception as e:
        print(f"[resolve cands] {e}", flush=True)
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
        # "LINE"はシステム通知の誤取り込みで湧く偽名(人名としてありえない)。表示せず掃除する
        c.execute("DELETE FROM pending_links WHERE line_name='LINE'")
        return [dict(r) for r in c.execute(
            "SELECT * FROM pending_links ORDER BY last_ts DESC")]


def resolve_pending(line_name: str, action: str, contact: str = None,
                    rank: str = "B") -> dict:
    """未紐付けトレイの1件を仕分ける。
      action: 'link'(既存客に紐付け) / 'new'(新規客) / 'private'(私用=除外)
              / 'staff'(店内=黒服・ママ・ちーふ) / 'peer'(同業=ホステス仲間)  ※v73 ゆみさん要望
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
        # v74: 表示名で自動生成済みの孤児カードが残る問題(ゆみさん「酒井さん2人」)。
        # 過去の受信ごと既存カードへ吸収して重複を残さない。
        if name != contact and db.get_contact(name):
            merge_contact(contact, name)
        target = contact
    elif action in ("new", "staff", "peer"):
        code = (contact or name).strip()
        db.upsert_contact(code, rank)
        add_alias(name, code)
        link_contact(code)
        if action == "staff":
            mark_staff(code)   # 営業対象外・顧客リスト/実績に載せない
        elif action == "peer":
            with db.conn() as c:
                c.execute("UPDATE contacts SET kind='peer', linked=1 WHERE code=?", (code,))
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


# ---------- グループ由来コードの分解(v141) ----------
# 旧リーダー(〜v0.5)のグループ通知はタイトルが「グループ名: 送信者」形式で届き、
# そのままカードのコードになる(実例: 「焼肉大好き: Yuji Tsuboi」2026-08-07 mon1)。
# コードは各テーブルのキーなので変更せず、表示と記載で人名に寄せる。
import re as _re_g
_GROUP_CODE_RE = _re_g.compile(r"^(?P<g>[^:：]{1,24})[:：]\s+(?P<p>.{1,40})$")


def group_split(code: str):
    """「グループ名: 人名」形式のコードを (グループ名, 人名) に分解。
    該当しなければ (None, code)。コロン直後に空白がある形のみ対象
    (時刻「12:30」やURL等の誤検知を避ける)。"""
    m = _GROUP_CODE_RE.match((code or "").strip())
    if not m:
        return None, code
    return m.group("g").strip(), m.group("p").strip()


def annotate_group_origin(code: str):
    """グループ由来カードに「取り込み元」を記載し、人名を別名に登録する。
    別名登録で、以後クリーンな人名で届いた受信が同じカードに紐付く(重複カード防止)。
    既に記載済みなら何もしない。戻り: グループ名 or None"""
    g, p = group_split(code)
    if not g:
        return None
    try:
        attrs = get_attrs(code) or {}
        if not attrs.get("取り込み元"):
            add_def("取り込み元")
            set_attr(code, "取り込み元", f"グループ「{g}」のチャットで取り込み")
        if p and p != code:
            with db.conn() as c:
                # 人名が他カードのコード/別名で既に使われていれば触らない
                used = c.execute("SELECT 1 FROM contact_aliases WHERE line_name=?", (p,)).fetchone()
                used2 = c.execute("SELECT 1 FROM contacts WHERE code=?", (p,)).fetchone()
            if not used and not used2:
                add_alias(p, code)
    except Exception as e:
        print(f"[group annotate] {e}", flush=True)
    return g


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
            "INSERT INTO contact_attrs(contact,akey,value,updated_ts) VALUES(?,?,?,?) "
            "ON CONFLICT(contact,akey) DO UPDATE SET value=excluded.value, "
            "updated_ts=excluded.updated_ts",
            (contact, (key or "").strip(), value, time.time()),
        )


def get_attr_dates(contact: str) -> dict:
    """v121: 属性ごとの記録日時。無ければ含まない(=取り込み時期不明の古いデータ)。"""
    ensure()
    out = {}
    with db.conn() as c:
        for r in c.execute("SELECT akey, updated_ts FROM contact_attrs WHERE contact=?",
                           (contact,)):
            if r["updated_ts"]:
                out[r["akey"]] = r["updated_ts"]
    return out


def get_attrs(contact: str) -> dict:
    ensure()
    with db.conn() as c:
        return {r["akey"]: r["value"] for r in c.execute(
            "SELECT akey,value FROM contact_attrs WHERE contact=?", (contact,))}


# ---------- 顧客カード→生成プロンプト(v101: カードを下書き・配信に実接続) ----------
_SELF_WORDS = ("自分", "本人", "わたし", "私", "me")


def is_self_tanto(tanto: str) -> bool:
    """v122: 担当が自分か。「自分(Aki)」のような表記も自分扱い(前方一致)。
    完全一致だけだと本人名入りの値を他人と誤認→不要な控えめトーン事故になる。"""
    t = (tanto or "").strip()
    return bool(t) and any(t == w or t.startswith(w + "(") or t.startswith(w + "（")
                           for w in _SELF_WORDS)


def card_prompt_block(code: str) -> str:
    """顧客カードの属性を、下書き/配信生成用のプロンプト断片にする。
    - 事実は「自然に1〜2点だけ活かす」指示付き(詰め込み禁止)
    - NG話題は厳守指示
    - 担当が自分以外なら「出しゃばらない」配慮を厳守指示"""
    ensure()
    a = get_attrs(code) or {}
    dates = get_attr_dates(code)
    import re as _re2
    _PERISH = _re2.compile(r"約束|予定|アポ|行こう|行く話|誘われ|今度|来週|来月|近々")

    def _dated(k, label):
        """時限性のある内容には記録日を付ける(いつの話か をAIに判断させる材料)。"""
        v = a[k]
        if k == "進行中の話" or _PERISH.search(v):
            ts = dates.get(k)
            when = time.strftime("%Y/%m/%d", time.localtime(ts)) if ts else "記録日不明=古い可能性"
            return f"- {label}: {v}（記録: {when}）"
        return f"- {label}: {v}"

    lines = []
    if a.get("呼び名"):
        lines.append(f"- 呼び名(この相手をこう呼んでいる): {a['呼び名']}")
    if a.get("進行中の話"):
        lines.append(_dated("進行中の話", "進行中の話"))
    for k in ("関係性メモ", "記念日", "家族", "好きなお酒", "好きな食べ物",
              "趣味・関心", "健康", "仕事・会社", "お気に入りキャスト"):
        if a.get(k):
            lines.append(_dated(k, k))
    block = ""
    if lines:
        today = time.strftime("%Y/%m/%d", time.localtime())
        block = ("この相手の顧客カード(確認済みの事実。全部使わず、今の文脈に合うもの"
                 "1〜2点だけ自然に活かす。羅列・詰め込みは禁止):\n" + "\n".join(lines)
                 + f"\n【日付の扱い・厳守】今日は{today}。カードや過去会話にある約束・予定・アポ"
                 "(ゴルフ・食事・旅行・「今度〜行こう」等)は記録当時のもので、今も有効とは限らない。"
                 "現在の予定として書くのは禁止。記録日が古い/不明のものに触れる場合は"
                 "「そういえば前に話してた〇〇、どうなりました？」のような確認・回想の形だけにする。"
                 "過ぎた日付の予定は過去の出来事として扱う。"
                 "回想に使うのも記録から2ヶ月以内のものまで。それより古い出来事は"
                 "本文に持ち出さない(何ヶ月も前の話を蒸し返すと監視されている印象になる)。")
    if a.get("NG話題"):
        block += ("\n【NG話題・厳守】次の話題には絶対に触れない。関連語も本文に書かない: "
                  f"{a['NG話題']}")
    tanto = (a.get("担当") or "").strip()
    if tanto and not is_self_tanto(tanto):
        block += (f"\n【担当への配慮・厳守】この客の担当キャストは自分ではなく「{tanto}」。"
                  "出しゃばらない・営業をかけすぎない・来店や同伴の強い誘いはしない・"
                  "担当を立てる軽やかなトーンで、あくまで控えめに。")
    return block


_CARD_PROMPT_KEYS = ("呼び名", "進行中の話", "NG話題", "関係性メモ", "記念日", "家族",
                     "好きなお酒", "好きな食べ物", "趣味・関心", "健康", "仕事・会社",
                     "お気に入りキャスト", "担当")


def card_used_keys(code: str) -> list:
    """生成プロンプトに渡ったカード項目名(UIの「カード参照」表示用)。"""
    ensure()
    a = get_attrs(code) or {}
    return [k for k in _CARD_PROMPT_KEYS if a.get(k)]


def contact_detail(code: str) -> dict:
    """顧客カード用: 基本情報＋エイリアス＋属性をまとめて返す。"""
    ensure()
    c = db.get_contact(code)
    if not c:
        return None
    c["aliases"] = aliases_for(code)
    c["attrs"] = get_attrs(code)
    return c


def search_contacts(q: str = "", attr_key: str = "", attr_val: str = "", kinds=None) -> list:
    """名前/メモ＋属性で検索。kinds=None(既定)は従来どおり顧客のみ。
    v132: kinds="all"で全種別、["staff","peer"]等のリストでも絞れる(店内・同業のカードに
    一覧から到達できない問題の解消)。"""
    ensure()
    q = (q or "").strip()
    attr_key = (attr_key or "").strip()
    attr_val = (attr_val or "").strip()
    with db.conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM contacts ORDER BY rank, code")]
        if kinds is None:
            rows = [r for r in rows if (r.get("kind") or "customer") == "customer"]
        elif kinds != "all":
            _ks = set(kinds if isinstance(kinds, (list, tuple, set)) else [kinds])
            rows = [r for r in rows if (r.get("kind") or "customer") in _ks]
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


def delete_contact_full(code: str) -> dict:
    """v145: カードの完全消去(取り消し不可)。本体・受信・送信実績・下書き・イベント・
    属性・別名・事実・トーク原文・ペルソナ・ニュース・ネット補強・文体・お席の同席記録まで
    すべて消す。muted化はしない(=同じ表示名から再受信すれば、また未登録として現れる)。"""
    ensure()
    code = (code or "").strip()
    if not code or not db.get_contact(code):
        return {"ok": False, "error": "not found"}
    deleted = {}
    with db.conn() as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM messages WHERE contact=?", (code,))]
        for mid in ids:
            c.execute("DELETE FROM drafts WHERE message_id=?", (mid,))
        for t, col in [("messages", "contact"), ("sent_replies", "contact"),
                       ("events", "contact"), ("contact_attrs", "contact"),
                       ("contact_aliases", "contact"), ("pending_links", "line_name"),
                       ("linebot_facts", "contact"), ("linebot_talks", "contact"),
                       ("linebot_persona", "contact"), ("news_items", "contact"),
                       ("enrich_suggestions", "contact"), ("style_profile", "contact"),
                       ("sitting_members", "contact"), ("sittings", "main_contact")]:
            try:
                cur = c.execute(f"DELETE FROM {t} WHERE {col}=?", (code,))
                if cur.rowcount:
                    deleted[t] = cur.rowcount
            except Exception as e:
                print(f"[delete {t}] {e}", flush=True)
        for k in (f"lasttalk_{code}", f"pstat_{code}"):
            c.execute("DELETE FROM linebot_meta WHERE k=?", (k,))
        c.execute("DELETE FROM contacts WHERE code=?", (code,))
    print(f"[delete_contact_full] {code!r}: {deleted}", flush=True)
    return {"ok": True, "deleted": deleted}


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
        for tbl, col in (("sittings", "main_contact"), ("sitting_members", "contact")):
            try:
                c.execute(f"UPDATE {tbl} SET {col}=? WHERE {col}=?", (new, old))
            except Exception:
                pass
    # 旧名がLINE表示名だった場合に備えて紐付けを残す(重複はON CONFLICTで吸収)
    add_alias(old, new)
    return {"ok": True, "code": new}


def merge_contact(keep: str, absorb: str) -> dict:
    """重複カードの統合(v74・ゆみさん実運用の「酒井さん2人」問題)。
    absorb側の受信・返信実例・実績・お席・属性・紐付けを keep へ移し、absorb のカードを消す。
    keep側の設定(ランク・メモ等)が優先。absorb側は空欄の穴埋めにだけ使う。
    absorbの名前は紐付け(alias)として残る=その表示名からの受信は以後 keep に入る。"""
    ensure()
    keep = (keep or "").strip()
    absorb = (absorb or "").strip()
    if not keep or not absorb or keep == absorb:
        return {"ok": False, "error": "統合元と統合先が不正です"}
    k = db.get_contact(keep)
    a = db.get_contact(absorb)
    if not k:
        return {"ok": False, "error": "残す側のカードが見つかりません"}
    with db.conn() as c:
        # データの移動(受信・実例・実績・属性・お席)
        for tbl, col in (("messages", "contact"), ("sent_replies", "contact"),
                          ("events", "contact"), ("contact_aliases", "contact"),
                          ("sittings", "main_contact"), ("sitting_members", "contact")):
            try:
                c.execute(f"UPDATE {tbl} SET {col}=? WHERE {col}=?", (keep, absorb))
            except Exception:
                pass
        # 属性は keep側に同キーがあれば keep優先(absorb側は捨てる)
        try:
            c.execute("UPDATE OR IGNORE contact_attrs SET contact=? WHERE contact=?", (keep, absorb))
            c.execute("DELETE FROM contact_attrs WHERE contact=?", (absorb,))
        except Exception:
            pass
        # v133: トーク原文・ペルソナ・ファクト・ネタ・ネット補強も移す
        for tbl in ("linebot_facts", "news_items", "enrich_suggestions"):
            try:
                c.execute(f"UPDATE {tbl} SET contact=? WHERE contact=?", (keep, absorb))
            except Exception:
                pass
        try:
            rs = c.execute("SELECT text FROM linebot_talks WHERE contact=?", (absorb,)).fetchone()
            rd = c.execute("SELECT text FROM linebot_talks WHERE contact=?", (keep,)).fetchone()
            if rs:
                merged = ((rd["text"] + "\n\n") if rd else "") + rs["text"]
                c.execute("INSERT INTO linebot_talks(contact,text) VALUES(?,?) "
                          "ON CONFLICT(contact) DO UPDATE SET text=excluded.text",
                          (keep, merged[-400000:]))
                c.execute("DELETE FROM linebot_talks WHERE contact=?", (absorb,))
        except Exception:
            pass
        try:
            if not c.execute("SELECT 1 FROM linebot_persona WHERE contact=?", (keep,)).fetchone():
                c.execute("UPDATE linebot_persona SET contact=? WHERE contact=?", (keep, absorb))
            c.execute("DELETE FROM linebot_persona WHERE contact=?", (absorb,))
        except Exception:
            pass
        # ランクは高い方・対応フラグはORで引き継ぐ
        try:
            _ro = {"S": 0, "A": 1, "B": 2}
            if a and _ro.get(a.get("rank") or "B", 3) < _ro.get(k.get("rank") or "B", 3):
                c.execute("UPDATE contacts SET rank=? WHERE code=?", (a["rank"], keep))
            for fl in ("flag_ero", "flag_koi"):
                if a and int(a.get(fl) or 0) and not int(k.get(fl) or 0):
                    c.execute(f"UPDATE contacts SET {fl}=? WHERE code=?", (a[fl], keep))
        except Exception:
            pass
        # 文体プロファイルは keepに無い時だけ移す
        try:
            has_k = c.execute("SELECT 1 FROM style_profile WHERE contact=?", (keep,)).fetchone()
            if has_k:
                c.execute("DELETE FROM style_profile WHERE contact=?", (absorb,))
            else:
                c.execute("UPDATE style_profile SET contact=? WHERE contact=?", (keep, absorb))
        except Exception:
            pass
        # keep側の空欄を absorb側で穴埋め(メモ・タグ・誕生日・周期・会社)
        if a:
            for fld in ("note", "tags", "birthday", "cycle_days", "company", "company_url", "company_note",
                        "nickname", "note_pos", "note_neg"):
                try:
                    if not (k.get(fld) or "") and (a.get(fld) or ""):
                        c.execute(f"UPDATE contacts SET {fld}=? WHERE code=?", (a[fld], keep))
                except Exception:
                    pass
        c.execute("UPDATE contacts SET linked=1 WHERE code=?", (keep,))
        c.execute("DELETE FROM contacts WHERE code=?", (absorb,))
        c.execute("DELETE FROM pending_links WHERE line_name=?", (absorb,))
    # absorbの名前は紐付けとして残す(その表示名からの受信が keep に入り続ける)
    add_alias(absorb, keep)
    return {"ok": True, "kept": keep, "absorbed": absorb}


def repair_sitting_names():
    """過去の改名バグ(v44〜v71: sittingsの列名誤りで席記録が旧名のまま)の追い付け修復。
    contacts に存在しないが contact_aliases で現カードに紐付く名前を席記録から更新する。
    起動時に1回呼ぶ。冪等。"""
    ensure()
    fixed = 0
    with db.conn() as c:
        try:
            rows = c.execute(
                "SELECT a.line_name, a.contact FROM contact_aliases a "
                "WHERE a.line_name NOT IN (SELECT code FROM contacts) "
                "AND a.contact IN (SELECT code FROM contacts)").fetchall()
            for r in rows:
                old_name, cur = r["line_name"], r["contact"]
                for tbl, col in (("sittings", "main_contact"), ("sitting_members", "contact")):
                    try:
                        cu = c.execute(f"UPDATE {tbl} SET {col}=? WHERE {col}=?", (cur, old_name))
                        fixed += cu.rowcount or 0
                    except Exception:
                        pass
        except Exception:
            pass
    return {"ok": True, "fixed": fixed}


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
