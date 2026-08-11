"""顧客CRM拡張: LINE表示名の紐付け(エイリアス)、私用アカウント除外、
未紐付けトレイ、ユーザー定義のカスタム属性。既存 db.py を壊さず追加する層。

- contact_aliases: LINE表示名 → 顧客code (多対一)
- muted_names:     私用として取り込まない表示名
- pending_links:   未知の表示名(未紐付けトレイに隔離)
- attr_defs:       ユーザー定義属性の定義(型: choice/text/number/date)
- contact_attrs:   顧客ごとの属性値
"""
import re
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
        for ddl in ("nickname TEXT DEFAULT ''", "register TEXT DEFAULT ''", "real_name TEXT DEFAULT ''", "phone TEXT DEFAULT ''", "note_pos TEXT DEFAULT ''", "note_neg TEXT DEFAULT ''", "linked INTEGER DEFAULT 1", "kind TEXT DEFAULT 'customer'", "stand TEXT DEFAULT ''", "kids_bday TEXT DEFAULT ''", "founding TEXT DEFAULT ''", "flag_ero INTEGER DEFAULT 0", "flag_koi INTEGER DEFAULT 0", "company TEXT DEFAULT ''", "company_url TEXT DEFAULT ''", "company_note TEXT DEFAULT ''", "flag_hot INTEGER DEFAULT 0"):
            try:
                c.execute(f"ALTER TABLE contacts ADD COLUMN {ddl}")
            except Exception:
                pass
        # v121: 属性の記録日時(昔のアポを今の予定と誤用しないための材料)
        try:
            c.execute("ALTER TABLE contact_attrs ADD COLUMN updated_ts REAL")
        except Exception:
            pass
        # v150: add_alias引数逆バグ(〜v149)の修復。contactが実在せず、line_nameが
        # 既存カードcodeである壊れた紐付けを自己エイリアスに戻す(受信の迷子防止)
        try:
            c.execute("UPDATE contact_aliases SET contact=line_name "
                      "WHERE contact NOT IN (SELECT code FROM contacts) "
                      "AND line_name IN (SELECT code FROM contacts)")
        except Exception as e:
            print(f"[alias repair] {e}", flush=True)
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
    # v150: 記号・絵文字の除去を先に(「田中さん🍸」→敬称が末尾に来てから除去)
    s = _ud.normalize("NFKC", (s or "")).strip().lower()
    s = "".join(ch for ch in s if ch.isalnum())
    return _HON_TAIL.sub("", s)


# v164: 本人要望「LINE名が本名ぽければ本名に最初から入力できないか」。
# 絵文字・記号・数字が一切無く、漢字の姓名らしい並び or 欧文Title Caseの2〜3語に
# 一致する場合だけ「本名候補」とする(過検出より見逃しを優先=本名欄を誤った値で汚さない)。
# あくまで初期値の"候補"提示用で、確定情報として扱わない(仕分け画面で必ず人が確認する)。
_NAME_KANJI_RE = re.compile(r"^[一-龯々〆ヶ]{2,6}(?:[ 　][一-龯々〆ヶ]{1,4})?$")
_NAME_LATIN_RE = re.compile(r"^[A-Z][a-zA-Z'\-]{1,}(?:[ 　][A-Z][a-zA-Z'\-]{1,}){1,2}$")


def looks_like_real_name(name: str) -> bool:
    """LINE表示名が本名らしい形かのおおまかな判定。絵文字・記号・数字・スペース以外の
    非文字が1つでも混じっていたら対象外。グループ由来コードも対象外。"""
    s = (name or "").strip()
    if not s or len(s) > 20:
        return False
    g, _p = group_split(s)
    if g:
        return False
    for ch in s:
        if ch in " 　・":
            continue
        if not _ud.category(ch).startswith("L"):
            return False
    return bool(_NAME_KANJI_RE.match(s) or _NAME_LATIN_RE.match(s))


def find_candidates(display_name: str, limit: int = 3, auto: bool = False) -> list:
    """表示名から既存カードの候補を探す。戻り: [{code, why, strong}]。
    strong=正規化後の完全一致(自動紐付けに使える) / 弱=部分一致(UIで提案のみ)。
    v150 auto=True(自動紐付け用)は条件を絞る: 顧客カードのみ・別名/表示名一致のみstrong
    (呼び名「社長」等の汎用語や店内/同業カードへの誤自動紐付けを防ぐ。UI提案は従来どおり広く)。"""
    ensure()
    g, p = group_split(display_name or "")
    nb = _norm_name(p if g else (display_name or ""))
    if not nb or (auto and len(nb) < 2):
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
        kind_cond = " AND COALESCE(kind,'customer')='customer'" if auto else ""
        codes = [r["code"] for r in c.execute(
            "SELECT code FROM contacts WHERE COALESCE(linked,1)!=0" + kind_cond)]
        aliases = [(r["line_name"], r["contact"]) for r in
                   c.execute("SELECT line_name, contact FROM contact_aliases")]
    code_set = set(codes)
    for ln, ct in aliases:
        if _norm_name(ln) == nb and (not auto or ct in code_set):
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
                # auto時は呼び名/本名一致をstrong扱いしない(汎用呼び名の誤爆防止)
                hit(code, f"{lab}が一致", lab == "表示名" or not auto)
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
        strong = [x for x in find_candidates(name, auto=True) if x["strong"]]
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
        # v166: 本人要望「登録名(索引名)を、自分のLINE上で表示されている名前(=連絡先交換時に
        # フルネーム等へ編集した後の名前)と揃えたい」。仕分けトレイでの紐付け時点のLINE表示名
        # (name)が現在の索引名(contact)と違う場合、それが最新のLINE上の表示である可能性が高い
        # ので索引名自体をnameへ更新する。旧名はrename_contact内でalias化されるので、
        # 引き続きどちらの名前からの受信もこのカードに届く(カード分裂しない)。
        # rename失敗時(何らかの理由でnewが既存等)は従来通りaliasだけで沈黙継続。
        if name != contact:
            try:
                ren = rename_contact(contact, name)
                if ren.get("ok"):
                    target = ren["code"]
            except Exception:
                pass
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
        # v175: 取り込み済みの未対応も掃除。従来は「以後の取り込みを止める」だけだったため、
        # 私用にした相手の既存メッセージが受信箱に居座り続けていた(監査で発見)。
        ids = [r["id"] for r in c.execute(
            "SELECT id FROM messages WHERE contact=? AND status IN ('open','deferred')", (name,))]
    for i in ids:
        try:
            db.set_status(i, "skipped", auto=True)
        except Exception:
            pass


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


# v148: 値の中身の月日から古さを判定(「3/2〜9来日」を8月に🟢進行中と出す事故の根治)。
# txtを取り込んだ日=記録日になるため、記録日だけでは古い話題を検出できない
_MD_PAT = None


def stale_by_content(v: str, days: int = 70) -> bool:
    """値に含まれる月日(3/2・3月など)の「直近の過去の該当日」がdays日より古ければTrue。
    未来の日付(これからの予定)や月日が無い値はFalse(新しい扱い)。"""
    global _MD_PAT
    if _MD_PAT is None:
        # v150: 「5/6人」「3/4くらい」「2026/08」「B1/2F」等の非日付スラッシュへの誤爆を防止
        _MD_PAT = _re_g.compile(
            r"(?<![\d/])(\d{1,2})\s*/\s*(\d{1,2})(?![\d/])(?!\s*(?:人|名|件|個|割|くらい|ぐらい|F|階))"
            r"|(?<!\d)(\d{1,2})月(?!曜)")
    import datetime
    hits = _MD_PAT.findall(v or "")
    if not hits:
        return False
    today = datetime.date.today()
    latest = None
    for a, b, c in hits:
        try:
            mo = int(a or c)
            dy = int(b) if b else 15
        except ValueError:
            continue
        if not (1 <= mo <= 12 and 1 <= dy <= 31):
            continue
        try:
            d1 = datetime.date(today.year, mo, min(dy, 28))
        except ValueError:
            continue
        if d1 > today:
            return False   # 未来の日付あり=これからの予定として新しい扱い
        if latest is None or d1 > latest:
            latest = d1
    return latest is not None and (today - latest).days > days


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
        """時限性のある内容には記録日を付ける(いつの話か をAIに判断させる材料)。
        v148: 中身の月日が2ヶ月超前の時限情報は、生成コンテキストから丸ごと外す。"""
        v = a[k]
        if k == "進行中の話" or _PERISH.search(v):
            if stale_by_content(v):
                print(f"[card prompt] 古い時限情報を除外: {k}={v[:30]!r}", flush=True)
                return ""
            ts = dates.get(k)
            when = time.strftime("%Y/%m/%d", time.localtime(ts)) if ts else "記録日不明=古い可能性"
            return f"- {label}: {v}（記録: {when}）"
        return f"- {label}: {v}"

    lines = []
    if a.get("呼び名"):
        lines.append(f"- 呼び名(この相手をこう呼んでいる): {a['呼び名']}")
    if a.get("進行中の話"):
        _l = _dated("進行中の話", "進行中の話")
        if _l:
            lines.append(_l)
    for k in ("関係性メモ", "記念日", "家族", "好きなお酒", "好きな食べ物",
              "趣味・関心", "健康", "仕事・会社", "お気に入りキャスト"):
        if a.get(k):
            _l = _dated(k, k)
            if _l:
                lines.append(_l)
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
_ALLOWED = {"rank", "nickname", "register", "note", "tags", "cycle_days", "real_name", "phone", "note_pos", "note_neg", "stand", "kids_bday", "founding", "birthday", "flag_ero", "flag_koi", "flag_hot", "company", "company_url", "company_note"}

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
        # v151: 行が無い相手にUPDATEだけして「登録成功」に見える無言失敗の修正
        # (成功トーストが出るのにDBに何も書かれず、翌日また未登録に出る)
        c.execute("INSERT OR IGNORE INTO contacts(code, rank) VALUES(?, 'B')", (code,))
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
        # v150: 席は他の同席者の実績でもあるため、席ごと消さない。
        # この人の同席行だけ消し、主賓だった席は主賓欄を空にして残す(孤児行も残さない)
        try:
            c.execute("UPDATE sittings SET main_contact='' WHERE main_contact=?", (code,))
        except Exception:
            pass
        for t, col in [("messages", "contact"), ("sent_replies", "contact"),
                       ("events", "contact"), ("contact_attrs", "contact"),
                       ("contact_aliases", "contact"), ("pending_links", "line_name"),
                       ("linebot_facts", "contact"), ("linebot_talks", "contact"),
                       ("linebot_persona", "contact"), ("news_items", "contact"),
                       ("enrich_suggestions", "contact"), ("style_profile", "contact"),
                       ("sitting_members", "contact")]:
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
        # v150: linebot_talks/facts/persona/news/enrichが漏れており、改名するとトーク原文・
        # ペルソナ・確認待ち・ネット提案が無言で切り離される実バグを修正
        for tbl, col in (("messages", "contact"), ("contact_aliases", "contact"),
                          ("contact_attrs", "contact"), ("style_profile", "contact"),
                          ("sent_replies", "contact"), ("events", "contact"),
                          ("linebot_facts", "contact"), ("linebot_talks", "contact"),
                          ("linebot_persona", "contact"), ("news_items", "contact"),
                          ("enrich_suggestions", "contact"),
                          ("acted_log", "contact")):   # v191その2(#10): 裁定履歴・undoの名義追随
            try:
                c.execute(f"UPDATE {tbl} SET {col}=? WHERE {col}=?", (new, old))
            except Exception:
                pass
        for tbl, col in (("sittings", "main_contact"), ("sitting_members", "contact")):
            try:
                c.execute(f"UPDATE {tbl} SET {col}=? WHERE {col}=?", (new, old))
            except Exception:
                pass
        for pref in ("lasttalk_", "pstat_"):
            try:
                c.execute("UPDATE OR IGNORE linebot_meta SET k=? WHERE k=?",
                          (pref + new, pref + old))
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
    # v191その2(#7): keepの種別が本人確定済みかをmerge前に判定しておく(統合後はabsorb由来の
    # pending🔖が混ざるため判定が変わってしまう)
    _keep_rel_ok = True
    try:
        from . import linebot as _lb
        _lb.ensure()
        _keep_rel_ok = _lb.rel_confirmed(keep)
    except Exception:
        _keep_rel_ok = True
    with db.conn() as c:
        # データの移動(受信・実例・実績・属性・お席)
        for tbl, col in (("messages", "contact"), ("sent_replies", "contact"),
                          ("events", "contact"), ("contact_aliases", "contact"),
                          ("sittings", "main_contact"), ("sitting_members", "contact"),
                          ("acted_log", "contact")):   # v191その2(#10): 裁定履歴・undoの名義追随
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
        # v191その2(#7): keepが確定済みならabsorb由来のpending🔖を持ち込まない
        # (確定済みS客が統合の副作用で「未確定」化し仕分けキューへ再登場する事故の防止)
        if _keep_rel_ok:
            try:
                c.execute("DELETE FROM linebot_facts WHERE contact=? AND k=? AND status='pending'",
                          (keep, "🔖種別・立場"))
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
            # v191その2(#11): フラグOR継承はkeepが顧客(customer)の時だけ(v187§10)。
            # koi客を店内カードへ統合すると非客にkoi=1が継承され客UIが誤爆していた
            # v192: flag_hot(🔥ピン留め)も同条件でOR継承
            for fl in ("flag_ero", "flag_koi", "flag_hot"):
                if ((k.get("kind") or "customer") == "customer"
                        and a and int(a.get(fl) or 0) and not int(k.get(fl) or 0)):
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
    # v191その2(#7): 検疫メタ(quarantine_{absorb})を迷子にしない。keep側へ統合移行し、
    # keepの種別が確定済みならその場で解放(客=適用/非客=破棄)。従来はabsorbのカード消滅後も
    # 保留事実(機微データ)が linebot_meta に永久残留していた。
    try:
        import json as _json
        from . import linebot as _lb2
        _lb2.ensure()
        _raw_a = _lb2._meta_get(f"quarantine_{absorb}")
        # v191その3: _meta_getはキー不在で""を返す(Noneではない)。旧判定は常に真で、
        # absorbに検疫が無くても毎mergeで空マーカーquarantine_{keep}='[]'が作られ
        # 後段分析予約が無条件に立っていた。マーカーがある時だけ移行する。
        if _raw_a:
            with db.conn() as c:
                c.execute("DELETE FROM linebot_meta WHERE k=?", (f"quarantine_{absorb}",))
            try:
                _fa = _json.loads(_raw_a) if _raw_a else []
            except Exception:
                _fa = []
            _lb2.quarantine_add(keep, _fa)
            if _keep_rel_ok:
                _lb2.quarantine_release_async(keep)
    except Exception as _e:
        print(f"[merge quarantine] {_e}", flush=True)
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
    """v151: UI見本・AI抽出は「8月19日」形式なのにMM-DDしか受けず、誕生日機能が
    全滅していた実バグの修正。8月19日/8/19/08-19/8.19 すべて受ける。"""
    import unicodedata as _u
    s = _u.normalize("NFKC", (s or "")).strip()
    if not s:
        return None
    import re as _re
    m = (_re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日?", s)
         or _re.match(r"^(\d{1,2})[-/.](\d{1,2})$", s))
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
        # v162: 誕生日等の祝い文の宛名も、お礼と同じ根本原因(attrsの呼び名を見ずcontacts.nickname
        # という別の未使用列だけ見ていた)でLINE表示名になっていた。attrsを優先して修正。
        nm = (get_attrs(code).get("呼び名") or "").strip() or (r.get("nickname") or "").strip() or code
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


# ============ 🔀 重複カードの自動検出 (v184) ============
# 本人選択: 「似た名前・同じ呼び名・同じLINE検索名を裏で検出し、同じ人かも?を提示。
# 1タップで統合画面へ」。検出は提示のみで、統合の実行は必ず本人の方向選択+確認を経る。

_DUP_CACHE = {"ts": 0.0, "items": []}
_DUP_TTL = 300   # ホームAPIから毎回呼ばれるため5分キャッシュ


def _dup_key(a: str, b: str) -> str:
    return "|".join(sorted([a, b]))


def _dup_dismissed() -> set:
    """「別人です」の記録(linebot_metaのJSON1キー。新テーブルは作らない)。"""
    import json as _json
    try:
        with db.conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS linebot_meta(k TEXT PRIMARY KEY, v TEXT)")
            r = c.execute("SELECT v FROM linebot_meta WHERE k='dup_not_same'").fetchone()
        return set(_json.loads(r["v"])) if r else set()
    except Exception:
        return set()


def dup_dismiss(a: str, b: str):
    """「別人です」= このペアを今後の検出から永久に外す。"""
    import json as _json
    s = _dup_dismissed()
    s.add(_dup_key(a, b))
    with db.conn() as c:
        c.execute("CREATE TABLE IF NOT EXISTS linebot_meta(k TEXT PRIMARY KEY, v TEXT)")
        c.execute("INSERT INTO linebot_meta(k,v) VALUES('dup_not_same',?) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                  (_json.dumps(sorted(s), ensure_ascii=False),))
    _DUP_CACHE["ts"] = 0.0


def find_duplicates(force: bool = False) -> list:
    """同じ人かもしれないカードのペアを検出する。
    根拠: ①呼び名が同じ ②LINE検索名が同じ ③本名が同じ ④正規化した名前が同じ/包含。
    返り値: [{"a": {...}, "b": {...}, "reason": "..."}] 最大10組。"""
    import time as _time
    now = _time.time()
    if not force and _DUP_CACHE["items"] is not None and now - _DUP_CACHE["ts"] < _DUP_TTL:
        return _DUP_CACHE["items"]
    dismissed = _dup_dismissed()
    rows = [r for r in db.list_contacts() if (r.get("kind") or "customer") != "private"]
    infos = {}
    by_attr = {"呼び名": {}, "LINE検索名": {}, "本名": {}}
    by_norm = {}
    for r in rows:
        code = r["code"]
        try:
            a = get_attrs(code) or {}
        except Exception:
            a = {}
        infos[code] = {"code": code, "rank": r.get("rank") or "B",
                       "kind": r.get("kind") or "customer",
                       "yobina": a.get("呼び名") or "",
                       "company": a.get("仕事・会社") or ""}
        for k in by_attr:
            v = _norm_name(a.get(k) or "")
            if len(v) >= 2:
                by_attr[k].setdefault(v, []).append(code)
        n = _norm_name(code)
        if len(n) >= 2:
            by_norm.setdefault(n, []).append(code)
        # 「大山さん🌵AMANE芳美」型(人名+敬称+店情報のLINE表示名)の先頭人名も照合対象に。
        # 「大山」カードとの重複を拾う(本人が最初に報告した実フォーマット)
        m = _re_g.match(r"^(.{1,8}?)(さん|様|さま|ちゃん|くん|君)", code)
        if m:
            h = _norm_name(m.group(1))
            if len(h) >= 2 and h != n:
                by_norm.setdefault(h, []).append(code)
    pairs = {}

    def _add(a, b, reason):
        if a == b:
            return
        k = _dup_key(a, b)
        if k in dismissed or k in pairs:
            return
        pairs[k] = {"a": infos[a], "b": infos[b], "reason": reason}
    for label, mp in (("呼び名", by_attr["呼び名"]), ("LINE検索名", by_attr["LINE検索名"]),
                      ("本名", by_attr["本名"])):
        for v, codes in mp.items():
            if len(codes) >= 2:
                for i in range(len(codes) - 1):
                    for j in range(i + 1, len(codes)):
                        _add(codes[i], codes[j], f"{label}が同じ")
    # 正規化名の一致(「田中さん」と「田中🍸」等)
    for v, codes in by_norm.items():
        if len(codes) >= 2:
            for i in range(len(codes) - 1):
                for j in range(i + 1, len(codes)):
                    _add(codes[i], codes[j], "名前がほぼ同じ")
    # 包含(「キム」⊂「キムラ」は誤爆が多いので4文字以上が含まれる時だけ)
    norms = sorted(by_norm.items())
    for i in range(len(norms)):
        for j in range(i + 1, len(norms)):
            va, ca = norms[i]
            vb, cb = norms[j]
            short, longer = (va, vb) if len(va) <= len(vb) else (vb, va)
            if len(short) >= 4 and short in longer:
                for x in ca:
                    for y in cb:
                        _add(x, y, "名前の一部が一致")
    # 直近の接触情報を添える(どちらを残すかの判断材料)
    out = list(pairs.values())[:10]
    with db.conn() as c:
        for p in out:
            for side in ("a", "b"):
                code = p[side]["code"]
                try:
                    r1 = c.execute("SELECT MAX(ts) m, COUNT(*) n FROM messages WHERE contact=?",
                                   (code,)).fetchone()
                    p[side]["last_ts"] = r1["m"] or 0
                    p[side]["msgs"] = r1["n"] or 0
                except Exception:
                    p[side]["last_ts"], p[side]["msgs"] = 0, 0
    _DUP_CACHE.update(ts=now, items=out)
    return out
