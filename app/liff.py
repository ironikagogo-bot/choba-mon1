"""LIFF Phase 1 (v96): SPA配信 + JSON API。

役割:
- GET /liff/          … 単一HTML SPA(static/liff.html)を配信(LIFF_IDを埋め込み)
- /api/liff/*         … 既存ロジック(crm/抽出/スクリーニング/ペルソナ)のJSONラッパ

認証(2段):
1. Authorization: Bearer <LIFF IDトークン> → LINEの検証API(oauth2/v2.1/verify)で検証し、
   sub(LINE userId)が帳場くんのowner(合言葉でひも付けた本人)と一致するか確認。
2. X-Ingest-Token: <INGEST_TOKEN> → 暫定/開発用フォールバック(LIFF ID未設定期)。

環境変数:
- CHOUBA_LIFF_ID         … LIFFアプリID(例 1234567890-abcdefgh)。SPAのliff.init用
- CHOUBA_LIFF_CHANNEL_ID … LINE LoginチャネルのチャネルID(IDトークン検証のclient_id)
"""
import json
import os
import re
import threading
import time

import requests
from fastapi import APIRouter, Request, Response, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse

from . import config, db

router = APIRouter()

LIFF_ID = os.environ.get("CHOUBA_LIFF_ID", "")
LIFF_CHANNEL_ID = os.environ.get("CHOUBA_LIFF_CHANNEL_ID", "")

# ---- IDトークン検証キャッシュ(検証APIの叩きすぎ防止。TTL10分) ----
_tok_cache: dict = {}


def _verify_id_token(id_token: str):
    """LINEの検証APIでIDトークンを検証し、sub(LINE userId)を返す。失敗はNone。"""
    now = time.time()
    hit = _tok_cache.get(id_token)
    if hit and hit[1] > now:
        return hit[0]
    if not LIFF_CHANNEL_ID:
        return None
    try:
        r = requests.post("https://api.line.me/oauth2/v2.1/verify",
                          data={"id_token": id_token, "client_id": LIFF_CHANNEL_ID},
                          timeout=10)
        if r.status_code != 200:
            return None
        body = r.json()
        sub = body.get("sub")
        exp = float(body.get("exp") or (now + 600))
        if sub:
            # トークン自体の期限とTTL10分の短い方までキャッシュ
            _tok_cache[id_token] = (sub, min(exp, now + 600))
            # 掃除
            for k in [k for k, v in list(_tok_cache.items()) if v[1] < now]:
                _tok_cache.pop(k, None)
        return sub
    except Exception:
        return None


def _authed(request: Request) -> bool:
    """LIFF IDトークン(本人一致) or INGEST_TOKEN(暫定)で認証。"""
    ing = request.headers.get("X-Ingest-Token", "")
    if config.INGEST_TOKEN and ing == config.INGEST_TOKEN:
        return True
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        sub = _verify_id_token(auth[7:].strip())
        if sub:
            from . import linebot
            owner = linebot.owner_id()
            if not owner:
                # v101: ひも付け前にLIFFを開けるのは開発時(PASSWORD未設定)のみ。
                # 本番はまずOAトークで合言葉→ひも付けしてから(先着者が乗っ取れる穴を塞ぐ)
                return not config.PASSWORD
            return sub == owner
    return False


def _deny():
    return JSONResponse({"error": "unauthorized"}, status_code=401)


# ============ SPA配信 ============

@router.get("/liff/")
@router.get("/liff")
def liff_page():
    path = os.path.join(os.path.dirname(__file__), "static", "liff.html")
    try:
        with open(path, encoding="utf-8") as f:
            html = f.read()
    except Exception:
        return Response("liff.html がありません", status_code=500)
    html = html.replace("__LIFF_ID__", LIFF_ID)
    return HTMLResponse(html)


@router.get("/api/liff/hello")
def liff_hello():
    """認証不要の起動確認。リセット後の「ひも付け前」を検知して案内を出すため(v115)。"""
    from . import linebot
    try:
        bound = bool(linebot.owner_id())
    except Exception:
        bound = False
    return {"ok": True, "bound": bound, "has_liff": bool(LIFF_ID)}


# ============ ホーム(今日の状況ハブ) ============

@router.get("/api/liff/home")
def liff_home(request: Request):
    if not _authed(request):
        return _deny()
    from . import linebot, crm, news
    linebot.ensure()
    q = linebot.build_queue()
    urgent_n = sum(1 for x in q if x.get("urgent"))
    unlinked_n = sum(1 for x in q if x.get("unlinked"))
    try:
        neta = len(news.list_items())
    except Exception:
        neta = 0
    try:
        anni = len(crm.upcoming_anniversaries(14))
    except Exception:
        anni = 0
    with db.conn() as c:
        last_ts = c.execute("SELECT MAX(ts) FROM messages").fetchone()[0]
        week = time.time() - 7 * 86400
        sent_n = c.execute("SELECT COUNT(*) FROM sent_replies WHERE ts>=?", (week,)).fetchone()[0]
        verb = c.execute("SELECT COUNT(*) FROM sent_replies WHERE ts>=? AND IFNULL(edited,0)=0",
                         (week,)).fetchone()[0]
    try:
        est = linebot.estranged()
    except Exception:
        est = []
    est_sa = [e for e in est if e["rank"] in ("S", "A")]
    for e in est_sa:
        e["name"] = linebot._yobina(e["code"])
    n_contacts = len([x for x in db.list_contacts()
                      if (x.get("kind") or "customer") == "customer" and x.get("linked") != 0])
    try:
        from . import watchdog
        reader = watchdog.status()
    except Exception:
        reader = None
    try:
        fixup_n = len(_fixup_items())
    except Exception:
        fixup_n = 0
    return {
        "fixup": fixup_n,
        "reader": reader,
        "ok": True,
        "queue": len(q), "urgent": urgent_n, "unlinked": unlinked_n,
        "neta": neta, "anni": anni, "contacts": n_contacts,
        "estranged": est_sa[:5],
        "sent_week": sent_n, "verbatim_week": verb,
        "last_ingest_ts": last_ts, "now": time.time(),
    }


# ============ 🚨 未整備カードの強制仕分け(v103) ============
# どの経路(一括取り込み/チャットtxt/受信/お席の同席・紹介)で生まれたカードでも、
# 呼び名・本名・種別・立場が欠けていれば ここに並ぶ。放置によるデータのゴミ化を防ぐ。

def _fixup_items():
    from . import crm, linebot
    linebot.ensure()
    out = []
    # 会話から抽出済みの候補(pending/appliedのfacts)を事前入力に使う
    with db.conn() as c:
        fact_rows = [dict(r) for r in c.execute(
            "SELECT contact, k, v FROM linebot_facts WHERE k IN ('呼び名','本名','誕生日','🔖種別・立場') "
            "AND status IN ('pending','applied','confirmed') ORDER BY id")]
    facts = {}
    for f in fact_rows:
        facts.setdefault(f["contact"], {})[f["k"]] = f["v"]
    for ct in db.list_contacts():
        if ct.get("linked") == 0:
            continue   # 未紐付けは受信箱の仕分けが担当
        code = ct["code"]
        a = crm.get_attrs(code) or {}
        missing = []
        if not (a.get("呼び名") or "").strip():
            missing.append("呼び名")
        if not (ct.get("kind") or "").strip():
            missing.append("種別")
        if not (ct.get("stand") or "").strip():
            missing.append("立場")
        if not missing:
            continue
        fx = facts.get(code, {})
        sug_kind, sug_stand = "", ""
        rel = fx.get("🔖種別・立場") or ""
        for k_, lab in (("customer", "顧客"), ("staff", "店内"), ("peer", "同業")):
            if lab in rel:
                sug_kind = k_
        for s_, lab in (("up", "目上"), ("even", "対等"), ("down", "目下")):
            if lab in rel:
                sug_stand = s_
        out.append({"code": code, "name": linebot._yobina(code, a),
                    "rank": ct.get("rank") or "B", "missing": missing,
                    "suggest": {"呼び名": fx.get("呼び名") or a.get("呼び名") or "",
                                "本名": fx.get("本名") or a.get("本名") or "",
                                "誕生日": fx.get("誕生日") or ct.get("birthday") or "",
                                "kind": ct.get("kind") or sug_kind or "customer",
                                "stand": ct.get("stand") or sug_stand or "even"}})
    return out


@router.get("/api/liff/fixup")
def liff_fixup(request: Request):
    if not _authed(request):
        return _deny()
    return {"ok": True, "items": _fixup_items()}


@router.post("/api/liff/fixup/save")
async def liff_fixup_save(request: Request):
    """1人分の確定。呼び名・本名・種別・立場は必須(サーバー側でも強制)。"""
    if not _authed(request):
        return _deny()
    from . import crm
    try:
        b = await request.json()
        code = (b.get("code") or "").strip()
        yb = (b.get("呼び名") or "").strip()
        hn = (b.get("本名") or "").strip()
        kind = (b.get("kind") or "").strip()
        stand = (b.get("stand") or "").strip()
        bd = (b.get("誕生日") or "").strip()
        belong = (b.get("所属") or "").strip()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    if not db.get_contact(code):
        return JSONResponse({"error": "not found"}, status_code=404)
    if kind == "priv":
        crm.mute(code)
        crm.discard_unlinked(code)
        db.track("liff_fixup_priv")
        return {"ok": True, "discarded": True}
    if not (yb and kind in ("customer", "staff", "peer") and stand in ("up", "even", "down")):
        return JSONResponse({"error": "呼び名・種別・立場は必須です"}, status_code=400)
    crm.add_def("呼び名"); crm.set_attr(code, "呼び名", yb)
    if hn:
        crm.add_def("本名"); crm.set_attr(code, "本名", hn)
    try:
        crm.add_alias(code, yb)
    except Exception:
        pass
    with db.conn() as c:
        c.execute("UPDATE contacts SET kind=?, stand=? WHERE code=?", (kind, stand, code))
        if bd:
            c.execute("UPDATE contacts SET birthday=? WHERE code=?", (bd, code))
        # 抽出候補は確認済み扱いに(チャット🔎整備で二度聞きしない)
        c.execute("UPDATE linebot_facts SET status='confirmed' WHERE contact=? "
                  "AND k IN ('呼び名','本名','誕生日','🔖種別・立場') AND status='pending'", (code,))
    if belong:
        crm.add_def("所属"); crm.set_attr(code, "所属", belong)
    db.track("liff_fixup_save")
    return {"ok": True}


# ============ 起動ブースト(v103): ホーム+一覧+受信係を1往復で ============

@router.get("/api/liff/boot")
def liff_boot(request: Request):
    if not _authed(request):
        return _deny()
    home = liff_home(request)
    contacts = liff_contacts(request)
    return {"ok": True, "home": home, "contacts": contacts}


# ============ 顧客一覧 ============

@router.get("/api/liff/contacts")
def liff_contacts(request: Request, q: str = ""):
    if not _authed(request):
        return _deny()
    from . import crm, linebot
    linebot.ensure()
    rows = crm.search_contacts(q=q)
    order = {"S": 0, "A": 1, "B": 2}
    out = []
    for r in rows:
        attrs = r.get("attrs") or {}
        out.append({
            "code": r["code"],
            "name": linebot._yobina(r["code"], attrs),
            "rank": r.get("rank") or "B",
            "birthday": r.get("birthday") or "",
            "company": attrs.get("仕事・会社") or r.get("company") or "",
            "ongoing": attrs.get("進行中の話") or "",
            "ng": attrs.get("NG話題") or "",
        })
    out.sort(key=lambda x: (order.get(x["rank"], 3), x["code"]))
    return {"ok": True, "contacts": out}


# ============ 顧客カード詳細 ============

_PROFILE_KEYS = ("本名", "年齢", "誕生日", "仕事・会社", "家族", "資産・事業",
                 "好きなお酒", "好きな食べ物", "趣味・関心", "健康", "記念日")


@router.get("/api/liff/contact/{code:path}")
def liff_contact(code: str, request: Request):
    if not _authed(request):
        return _deny()
    from . import crm, linebot, news
    linebot.ensure()
    d = crm.contact_detail(code)
    if not d:
        return JSONResponse({"error": "not found"}, status_code=404)
    attrs = d.get("attrs") or {}
    # 履歴: 受信・返信・お席(直近)
    with db.conn() as c:
        msgs = [dict(r) for r in c.execute(
            "SELECT ts, text, status FROM messages WHERE contact=? ORDER BY ts DESC LIMIT 10", (code,))]
        sents = [dict(r) for r in c.execute(
            "SELECT ts, text FROM sent_replies WHERE contact=? ORDER BY ts DESC LIMIT 10", (code,))]
        try:
            seki = [dict(r) for r in c.execute(
                "SELECT ts, kind FROM sittings WHERE main_contact=? ORDER BY ts DESC LIMIT 5", (code,))]
        except Exception:
            seki = []
    try:
        persona = linebot.get_persona(code)
    except Exception:
        persona = None
    try:
        items = [x for x in news.list_items() if x.get("contact") == code][:3]
    except Exception:
        items = []
    pstat = linebot._meta_get(f"pstat_{code}") or ""
    pending_n = len([f for f in linebot.pending_facts() if f["contact"] == code])
    review_n = len(linebot.reviewable_facts(code))
    gap = None
    try:
        last = linebot._last_interaction(code)
        if last:
            gap = int((time.time() - last) / 86400)
    except Exception:
        pass
    return {
        "ok": True,
        "code": code,
        "name": linebot._yobina(code, attrs),
        "rank": d.get("rank") or "B",
        "kind": d.get("kind") or "customer",
        "stand": d.get("stand") or "",
        "birthday": d.get("birthday") or "",
        "note": d.get("note") or "",
        "flag_ero": d.get("flag_ero") or 0,
        "flag_koi": d.get("flag_koi") or 0,
        "aliases": d.get("aliases") or [],
        "attrs": attrs,
        "profile_keys": [k for k in _PROFILE_KEYS if attrs.get(k)],
        "now_keys": {"ongoing": attrs.get("進行中の話") or "",
                     "ng": attrs.get("NG話題") or "",
                     "relmemo": attrs.get("関係性メモ") or ""},
        "persona": persona, "persona_stat": pstat, "has_talk": _has_talk(code),
        "pstats": (lambda: linebot.partner_stats(code))(),
        "rel": (lambda: linebot.relationship_stats(code))(),   # v118: 第2層(関係性)
        "news": items,
        "history": {"received": msgs, "sent": sents, "seki": seki},
        "pending_facts": pending_n, "review_facts": review_n,
        "gap_days": gap,
    }


@router.post("/api/liff/contact/{code:path}")
async def liff_contact_update(code: str, request: Request):
    if not _authed(request):
        return _deny()
    from . import crm, linebot
    linebot.ensure()
    if not db.get_contact(code):
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    fields = dict(body.get("fields") or {})
    attrs = body.get("attrs") or {}
    # v114: 種別(kind)は update_contact の許可外だったため保存されていなかった実バグ。
    # set_kind で明示反映する(顧客→店内→同業の切替が効くようになる)。
    kind = (fields.pop("kind", "") or "").strip()
    if kind in ("customer", "staff", "peer", "private"):
        try:
            crm.set_kind(code, kind)
        except Exception:
            with db.conn() as c:
                c.execute("UPDATE contacts SET kind=? WHERE code=?", (kind, code))
    # v114: 店内の性別(店内区分=女/男)。下書きの呼び方・トーンに効く
    sg = (fields.pop("staff_gender", "") or "").strip()
    if kind == "staff" and sg in ("女", "男"):
        crm.add_def("店内区分"); crm.set_attr(code, "店内区分", sg)
    crm.update_contact(code, fields)
    for k, v in attrs.items():
        if not isinstance(k, str) or not k.strip():
            continue
        v = (v or "").strip() if isinstance(v, str) else v
        try:
            if v == "":
                # 空=削除
                with db.conn() as c:
                    c.execute("DELETE FROM contact_attrs WHERE contact=? AND akey=?", (code, k))
            else:
                crm.add_def(k)
                crm.set_attr(code, k, v)
        except Exception as e:
            print(f"[liff attr {k}] {e}", flush=True)
    db.track("liff_card_edit")
    return {"ok": True}


def _has_talk(code):
    with db.conn() as c:
        return bool(c.execute("SELECT 1 FROM linebot_talks WHERE contact=?", (code,)).fetchone())


# ============ 🧠 ペルソナ(LIFF内実行・v101) ============

@router.post("/api/liff/persona/run")
async def liff_persona_run(request: Request):
    if not _authed(request):
        return _deny()
    from . import linebot
    linebot.ensure()
    try:
        code = ((await request.json()).get("code") or "").strip()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    if not code or not db.get_contact(code):
        return JSONResponse({"error": "not found"}, status_code=404)
    if not _has_talk(code):
        return JSONResponse({"ok": False, "error": "この相手のトーク履歴(.txt)がまだありません。先に取り込んでください。"})
    linebot.persona_async(code)
    db.track("liff_persona_run")
    return {"ok": True}


@router.post("/api/liff/persona/edit")
async def liff_persona_edit(request: Request):
    """v116: ペルソナの1項目を削除/修正(たまに間違う分を手で直せる)。"""
    if not _authed(request):
        return _deny()
    from . import linebot
    try:
        b = await request.json()
        code = (b.get("code") or "").strip()
        action = b.get("action") or ""
        index = int(b.get("index", -1))
        value = b.get("value") or ""
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    if action not in ("del", "fix", "summary", "tolok", "tolng", "tolfix", "toldel"):
        return JSONResponse({"error": "bad action"}, status_code=400)
    p = linebot.edit_persona(code, action, index, value)
    if p is None:
        return JSONResponse({"error": "ペルソナがありません"}, status_code=404)
    db.track("liff_persona_edit")
    return {"ok": True, "persona": p}


# ============ 📡 受信係の接続管理(v102) ============

@router.get("/api/liff/reader/status")
def liff_reader_status(request: Request):
    if not _authed(request):
        return _deny()
    from . import readerauth, watchdog
    return {"ok": True, "watch": watchdog.status(),
            "readers": readerauth.list_readers()}


@router.post("/api/liff/reader/code")
def liff_reader_code(request: Request):
    """接続用ワンタイムコード発行(QRの中身)。10分・1回きり。"""
    if not _authed(request):
        return _deny()
    from . import readerauth
    code = readerauth.make_code()
    db.track("liff_reader_code")
    return {"ok": True, "code": code, "ttl": readerauth.CODE_TTL}


@router.post("/api/liff/reader/revoke")
async def liff_reader_revoke(request: Request):
    if not _authed(request):
        return _deny()
    from . import readerauth
    try:
        rid = int((await request.json()).get("id"))
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    readerauth.revoke(rid)
    return {"ok": True}


# ============ 🙏 お席のタップ素材(v107: タイプ撲滅) ============

@router.get("/api/liff/orei/prefill")
def liff_orei_prefill(request: Request):
    """お席記録を全面タップにするための候補一式。
    - mains: 主賓候補(最近お席に上がった順→S→A)
    - staff: ヘルプ候補(店内の子 全員)
    - guests: 同席・紹介候補(最近やりとりした顧客 上位12)
    - venues: 同伴/アフターでよく行く店(過去のお席から)"""
    if not _authed(request):
        return _deny()
    from . import sittings, linebot
    linebot.ensure()
    sittings.ensure()
    contacts = db.list_contacts()
    def nm(code):
        try:
            return linebot._yobina(code)
        except Exception:
            return code
    staff = [{"code": c["code"], "name": nm(c["code"])} for c in contacts
             if c.get("kind") == "staff" and c.get("linked") != 0][:14]
    custs = [c for c in contacts if (c.get("kind") or "customer") == "customer"
             and c.get("linked") != 0]
    # 最近お席の主賓・よく行く店
    recent_mains, dohan_v, after_v = [], [], []
    try:
        for s_ in (sittings.list_sittings() or [])[:20]:
            m = s_.get("main_contact") or ""
            if m and m not in recent_mains:
                recent_mains.append(m)
            for key, arr in (("dohan_venue", dohan_v), ("after_venue", after_v)):
                v_ = (s_.get(key) or "").strip()
                if v_ and v_ not in arr:
                    arr.append(v_)
    except Exception:
        pass
    # 最近やりとりした顧客(同席・紹介候補)
    with db.conn() as c:
        rec = [r["contact"] for r in c.execute(
            "SELECT contact, MAX(ts) t FROM messages GROUP BY contact ORDER BY t DESC LIMIT 30")]
    cust_codes = {c_["code"] for c_ in custs}
    guests = [x for x in rec if x in cust_codes][:12]
    for c_ in sorted(custs, key=lambda x: {"S": 0, "A": 1}.get(x.get("rank"), 2)):
        if c_["code"] not in guests and len(guests) < 12:
            guests.append(c_["code"])
    order = {m: i for i, m in enumerate(recent_mains)}
    mains = sorted(custs, key=lambda x: (order.get(x["code"], 99),
                                         {"S": 0, "A": 1}.get(x.get("rank"), 2)))
    return {"ok": True,
            "mains": [{"code": x["code"], "name": nm(x["code"]), "rank": x.get("rank") or "B"}
                      for x in mains[:14]],
            "staff": staff,
            "guests": [{"code": g, "name": nm(g)} for g in guests],
            "venues": {"dohan": dohan_v[:6], "after": after_v[:6]}}


# ============ 🕘 お席一覧(v101) ============

@router.get("/api/liff/orei/list")
def liff_orei_list(request: Request):
    if not _authed(request):
        return _deny()
    from . import sittings, linebot
    sittings.ensure()
    out = []
    for s in (sittings.list_sittings() or [])[:8]:
        out.append({"sid": s.get("id"), "date": s.get("date_label") or "",
                    "main": s.get("main_contact") or "",
                    "name": linebot._yobina(s.get("main_contact") or ""),
                    "dohan": s.get("dohan_venue") or "", "after": s.get("after_venue") or "",
                    "gaiso": (s.get("stype") or "") == "gaiso",
                    "members": len(s.get("members") or [])})
    return {"ok": True, "sittings": out}


# ============ 📥 一括取り込み ============

def _jobs_ensure():
    with db.conn() as c:
        c.execute("CREATE TABLE IF NOT EXISTS liff_import_jobs("
                  "id INTEGER PRIMARY KEY AUTOINCREMENT, fname TEXT, contact TEXT, "
                  "status TEXT, detail TEXT, ts REAL)")


_NAME_RE = re.compile(r"\[LINE\]\s*(.+?)\s*とのトーク")


def _match_contact(fname: str):
    """ファイル名から相手名を取り、既存contacts(コード/エイリアス/呼び名)と照合。
    戻り: (contact or None, ambiguous:list, extracted_name or None)"""
    from . import crm
    m = _NAME_RE.search(fname or "")
    name = (m.group(1).strip() if m else "") or None
    if not name:
        # 「○○とのトーク履歴.txt」等のゆるい形も拾う
        m2 = re.search(r"(.+?)\s*とのトーク", fname or "")
        name = (m2.group(1).strip() if m2 else "") or None
        if name and name.startswith("[LINE]"):
            name = name[6:].strip() or None
    if not name:
        return None, [], None
    cands = set()
    for ct in db.list_contacts():
        code = ct["code"]
        keys = {code}
        try:
            keys.update(crm.aliases_for(code) or [])
        except Exception:
            pass
        try:
            yb = (crm.get_attrs(code) or {}).get("呼び名")
            if yb:
                keys.add(yb)
        except Exception:
            pass
        if name in keys:
            cands.add(code)
    cands = sorted(cands)
    if len(cands) == 1:
        return cands[0], [], name
    return None, cands, name


def _run_import_job(jid: int, contact: str, text: str):
    from . import linebot
    def upd(status, detail=""):
        with db.conn() as c:
            c.execute("UPDATE liff_import_jobs SET status=?, detail=?, ts=? WHERE id=?",
                      (status, detail, time.time(), jid))
    try:
        upd("running")
        # 文体(全体)は取り込みの度に更新
        from .style_profile import extract_profile, extract_contact_profile
        from .main import _infer_self_name
        from . import crm
        # v100: contactを渡して消去法を強化(相手名を「自分」と誤認しない)
        self_name = _infer_self_name(text, (db.get_profile("_selfname") or {}).get("name") or "自分",
                                     contact=contact)
        p = extract_profile(text, self_name=self_name)
        if p.n_messages:
            db.save_profile("_global", p.to_dict())
            db.save_profile("_selfname", {"name": self_name})
        try:
            cp = extract_contact_profile(text, contact, self_name=self_name)
            db.save_profile(contact, cp)
        except Exception:
            pass
        if not db.get_contact(contact):
            db.upsert_contact(contact, "B")
            crm.link_contact(contact)
            crm.add_alias(contact, contact)
        linebot.save_talk(contact, text)
        lt = linebot.parse_last_talk_ts(text)
        if lt:
            linebot._meta_set(f"lasttalk_{contact}", str(lt))
        facts, err = linebot.extract_facts(text, contact, self_name)
        try:
            rel = linebot.classify_relationship(text, contact, self_name)
            if rel:
                facts = [rel] + (facts or [])
        except Exception:
            pass
        if err and not facts:
            upd("error", err)
            return
        facts = linebot.curate_facts(facts or [])
        facts = linebot._ensure_name_questions(contact, facts)
        ncrit, nauto = linebot.save_split(contact, facts)
        upd("done", f"{ncrit + nauto}")
        linebot.maybe_auto_persona(contact)   # v109: 一括取り込みでもペルソナ同時生成
        db.track("liff_bulk_import")
    except Exception as e:
        upd("error", f"{type(e).__name__}")
        print(f"[liff import] {e}", flush=True)


@router.post("/api/liff/import")
async def liff_import(request: Request, files: list[UploadFile] = File(...)):
    if not _authed(request):
        return _deny()
    from . import linebot
    linebot.ensure()
    _jobs_ensure()
    out = []
    for uf in files[:20]:   # 同時上限20ファイル
        fname = uf.filename or "無題.txt"
        try:
            raw = await uf.read()
            text = raw.decode("utf-8-sig", errors="replace").replace("\r\n", "\n")
        except Exception:
            with db.conn() as c:
                cur = c.execute("INSERT INTO liff_import_jobs(fname,contact,status,detail,ts) "
                                "VALUES(?,?,?,?,?)", (fname, "", "error", "読み込み失敗", time.time()))
                out.append({"id": cur.lastrowid, "fname": fname, "status": "error"})
            continue
        if len(text) < 50:
            status, contact, detail = "error", "", "内容が短すぎます(トーク履歴.txt?)"
            with db.conn() as c:
                cur = c.execute("INSERT INTO liff_import_jobs(fname,contact,status,detail,ts) "
                                "VALUES(?,?,?,?,?)", (fname, contact, status, detail, time.time()))
            out.append({"id": cur.lastrowid, "fname": fname, "status": status})
            continue
        contact, cands, name = _match_contact(fname)
        if contact is None and name and not cands:
            contact = name   # 新規カードとして作成(ジョブ内でupsert)
        with db.conn() as c:
            cur = c.execute("INSERT INTO liff_import_jobs(fname,contact,status,detail,ts) "
                            "VALUES(?,?,?,?,?)",
                            (fname, contact or "",
                             "queued" if contact else "ambiguous",
                             json.dumps({"cands": cands, "name": name}, ensure_ascii=False)
                             if not contact else "", time.time()))
            jid = cur.lastrowid
        if contact:
            # 原文をジョブに退避(assign時の再実行にも使う)
            linebot._meta_set(f"liffimp_{jid}", text[:200000])
            threading.Thread(target=_run_import_job, args=(jid, contact, text), daemon=True).start()
        else:
            linebot._meta_set(f"liffimp_{jid}", text[:200000])
        out.append({"id": jid, "fname": fname, "status": "queued" if contact else "ambiguous",
                    "contact": contact, "cands": cands})
    return {"ok": True, "jobs": out}


@router.get("/api/liff/import/status")
def liff_import_status(request: Request):
    if not _authed(request):
        return _deny()
    _jobs_ensure()
    from . import linebot
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM liff_import_jobs WHERE ts>=? ORDER BY id DESC LIMIT 40",
            (time.time() - 86400,))]
    for r in rows:
        if r["status"] == "ambiguous" and r.get("detail"):
            try:
                r["detail"] = json.loads(r["detail"])
            except Exception:
                pass
        if r.get("contact"):
            r["name"] = linebot._yobina(r["contact"])
    return {"ok": True, "jobs": rows}


# ============ 📨 返信 (Phase 3 → v99で前倒し) ============

@router.get("/api/liff/inbox")
def liff_inbox(request: Request):
    if not _authed(request):
        return _deny()
    from . import linebot
    linebot.ensure()
    q = linebot.build_queue()
    out = []
    for it in q:
        full = (db.get_message(it["mid"]) or {}).get("text") or it.get("text") or ""
        out.append({"mid": it["mid"], "contact": it["contact"],
                    "name": linebot._yobina(it["contact"]),
                    "rank": it.get("rank") or "B", "urgent": bool(it.get("urgent")),
                    "unlinked": bool(it.get("unlinked")), "reason": it.get("reason") or "",
                    "ts": it.get("ts"), "text": full[:600],
                    "truncated": len(full) > 600})
    return {"ok": True, "items": out}


@router.get("/api/liff/message/{mid}/full")
def liff_msg_full(mid: int, request: Request):
    if not _authed(request):
        return _deny()
    m = db.get_message(mid)
    if not m:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"ok": True, "text": m.get("text") or ""}


@router.post("/api/liff/reply/drafts")
async def liff_reply_drafts(request: Request):
    """下書き生成。drafts.generate はDBキャッシュ付き=プリフェッチ・再訪が高速。"""
    if not _authed(request):
        return _deny()
    from . import drafts
    try:
        body = await request.json()
        mid = int(body.get("mid"))
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    if not db.get_message(mid):
        return JSONResponse({"error": "not found"}, status_code=404)
    gen = drafts.generate(mid) or []
    from . import crm
    m = db.get_message(mid) or {}
    db.track("liff_draft")
    return {"ok": True, "drafts": [{"text": g.get("text", "")} for g in gen if g.get("text")][:3],
            "card_keys": crm.card_used_keys(m.get("contact") or "")}


@router.post("/api/liff/reply/act")
async def liff_reply_act(request: Request):
    """対応の記録。既存 /api/messages/{mid}/act と同じ意味論(スレッド一括クローズ・
    文体学習・実績自動記録)を main.act に委譲。"""
    if not _authed(request):
        return _deny()
    try:
        body = await request.json()
        mid = int(body.get("mid"))
        action = body.get("action") or ""
        text = body.get("text") or ""
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    from .main import act as _act, Action as _Action
    try:
        r = _act(mid, _Action(action=action, text=text))
    except Exception as e:
        code = getattr(e, "status_code", 500)
        return JSONResponse({"error": str(getattr(e, "detail", e))}, status_code=code)
    db.track("liff_reply_act")
    return r


@router.post("/api/liff/classify")
async def liff_classify(request: Request):
    """未登録相手の仕分け(顧客/店内/同業/私用)。チャット版rep_action(cls)と同じ処理。"""
    if not _authed(request):
        return _deny()
    from . import crm
    try:
        body = await request.json()
        name = (body.get("contact") or "").strip()
        v = body.get("kind") or ""
        rank = body.get("rank") or ""
        gender = (body.get("gender") or "").strip()   # v120: 店内の女/男(同僚ホステス/黒服)
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    if not name or v not in ("work", "staff", "peer", "priv"):
        return JSONResponse({"error": "bad params"}, status_code=400)
    if v == "work":
        crm.link_contact(name)
        crm.add_alias(name, name)
        if rank in ("S", "A", "B"):
            with db.conn() as c:
                c.execute("UPDATE contacts SET rank=? WHERE code=?", (rank, name))
    elif v == "staff":
        crm.mark_staff(name)
        crm.add_alias(name, name)
        if gender in ("女", "男"):   # v120: 同僚ホステス/黒服さんの区分→下書きトーンに効く
            try:
                crm.add_def("店内区分")
                crm.set_attr(name, "店内区分", gender)
            except Exception as e:
                print(f"[classify gender] {e}", flush=True)
    elif v == "peer":
        db.upsert_contact(name, "B")
        with db.conn() as c:
            c.execute("UPDATE contacts SET kind='peer', linked=1 WHERE code=?", (name,))
        crm.add_alias(name, name)
    elif v == "priv":
        crm.mute(name)
        crm.discard_unlinked(name)
    db.track("liff_classify")
    return {"ok": True}


# ============ 📣 アナウンス (Phase 2) ============

@router.get("/api/liff/ann/segments")
def liff_ann_segments(request: Request):
    if not _authed(request):
        return _deny()
    from . import linebot
    linebot.ensure()
    c = linebot._ann_counts()
    return {"ok": True, "counts": {k: (len(v) if isinstance(v, list) else v)
                                  for k, v in c.items() if k != "cust"}}


def _seg_codes(seg, now):
    """1セグメント分の宛先コード列(順序に意味あり: GBはスコア順)。"""
    from . import linebot, campaign
    if seg == "PEER":
        return [c["code"] for c in db.list_contacts() if c.get("kind") == "peer"]
    if seg == "STAFF":
        return [c["code"] for c in db.list_contacts() if c.get("kind") == "staff"]
    if seg == "GB":
        return [e["code"] for e in linebot.estranged(now=now)]
    ranks = {"S": ["S"], "SA": ["S", "A"]}.get(seg, ["S", "A", "B"])
    tags = {"RV": ["直近来店"], "BD": ["誕生日近い"]}.get(seg)
    recips = campaign.select_recipients(mode="greeting", ranks=None if tags else ranks,
                                        tags=tags, now=now)
    return [r["code"] for r in recips
            if (db.get_contact(r["code"]) or {}).get("kind", "customer") == "customer"]


@router.post("/api/liff/ann/plan")
async def liff_ann_plan(request: Request):
    """セグメント(複数可)→配達キュー。v108: 複数選択の和集合を自動名寄せ
    (同じ人が複数セグメントに該当しても1回だけ)。トーンは1人ずつkindで判定。"""
    if not _authed(request):
        return _deny()
    from . import linebot
    linebot.ensure()
    try:
        body = await request.json()
        segs = body.get("segs") or ([body["seg"]] if body.get("seg") else ["ALL"])
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    now = time.time()
    seen, items = set(), []
    dup = 0
    for seg in segs[:8]:
        for c_ in _seg_codes(seg, now):
            if c_ in seen:
                dup += 1
                continue
            seen.add(c_)
            kind = (db.get_contact(c_) or {}).get("kind") or "customer"
            tone = {"peer": "peer", "staff": "staff"}.get(kind, "cust")
            items.append({"code": c_, "name": linebot._yobina(c_),
                          "rank": (db.get_contact(c_) or {}).get("rank") or "B",
                          "tone": tone})
    return {"ok": True, "items": items, "deduped": dup}


@router.post("/api/liff/ann/draft")
async def liff_ann_draft(request: Request):
    """1人分の個別化下書き。purpose/detail(目的と内容)をtemplateとして渡す。"""
    if not _authed(request):
        return _deny()
    from . import linebot, campaign
    try:
        body = await request.json()
        code = (body.get("code") or "").strip()
        tone = body.get("tone") or "cust"
        template = (body.get("template") or "").strip()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    purpose = (body.get("purpose") or "").strip()
    if not db.get_contact(code):
        return JSONResponse({"error": "not found"}, status_code=404)
    if tone in ("peer", "staff"):
        text = linebot._casual_draft(code, tone)
    else:
        # 注: greetingはranks/tags必須の設計。宛先は確定済みなので全ランクを通す
        r = campaign.generate(mode="greeting", ranks=["S", "A", "B"], codes=[code],
                              template=template, purpose=purpose)
        items = r.get("items") or []
        text = items[0]["text"] if items else ""
    from . import crm
    db.track("liff_ann_draft")
    return {"ok": True, "text": text, "card_keys": crm.card_used_keys(code)}


@router.post("/api/liff/ann/sent")
async def liff_ann_sent(request: Request):
    if not _authed(request):
        return _deny()
    try:
        body = await request.json()
        code = (body.get("code") or "").strip()
        text = (body.get("text") or "").strip()
        edited = 1 if body.get("edited") else 0
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    if text:
        try:
            from .style_profile import learn_from_sent
            learn_from_sent(code, text, edited=edited, edit_ratio=100 if not edited else 80)
        except Exception as e:
            print(f"[liff ann learn] {e}", flush=True)
    db.track("liff_ann_sent")
    return {"ok": True}


# ============ 🙏 お礼・お席記録 (Phase 3 → v99で前倒し) ============

@router.post("/api/liff/orei/record")
async def liff_orei_record(request: Request):
    """お席を記録(来店/同伴の実績イベント自動)→ メンバー全員の御礼下書きを返す。"""
    if not _authed(request):
        return _deny()
    from . import sittings, linebot
    try:
        body = await request.json()
        main = (body.get("main") or "").strip()
        stype = body.get("stype") or ""          # ""=店内 / "gaiso"=店外のみ
        dohan = (body.get("dohan_venue") or "").strip()
        after = (body.get("after_venue") or "").strip()
        venue = (body.get("venue") or "").strip()
        helpers = body.get("helpers") or []       # [{contact, role, stand}] role=help/guest/intro/peer
        day = body.get("day") or "today"          # today / yesterday
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    if not main or not db.get_contact(main):
        return JSONResponse({"error": "主賓が見つかりません"}, status_code=400)
    stand = (db.get_contact(main) or {}).get("stand") or "equal"
    stand = {"up": "senior", "even": "equal", "down": "junior"}.get(stand, stand)
    members = [{"contact": main, "role": "customer", "stand": stand}]
    for h in helpers[:10]:
        hc = (h.get("contact") or "").strip()
        role = h.get("role") or "help"
        if not hc:
            continue
        if not db.get_contact(hc):
            # 未登録でもその場でカード作成(後で仕分けウィザードが拾う)。ヘルプは店内扱い
            db.upsert_contact(hc, "B")
            from . import crm as _crm
            _crm.link_contact(hc)
            _crm.add_alias(hc, hc)
            if role == "help":
                with db.conn() as c:
                    c.execute("UPDATE contacts SET kind='staff' WHERE code=?", (hc,))
        if db.get_contact(hc):
            g_stand = (db.get_contact(hc) or {}).get("stand") or "equal"
            g_stand = {"up": "senior", "even": "equal", "down": "junior"}.get(g_stand, g_stand)
            members.append({"contact": hc, "role": role,
                            "stand": h.get("stand") or g_stand})
    import datetime
    jst = datetime.timezone(datetime.timedelta(hours=9))
    d0 = datetime.datetime.now(jst)
    if day == "yesterday":
        d0 -= datetime.timedelta(days=1)
    label = d0.strftime("%m/%d")
    sid = sittings.create_sitting(label, main, members, stype=stype, venue=venue,
                                  dohan_venue=dohan, after_venue=after)
    drafts_ = sittings.generate_orei(sid)
    db.track("liff_orei_record")
    return {"ok": True, "sid": sid, "drafts": drafts_}


@router.post("/api/liff/orei/sent")
async def liff_orei_sent(request: Request):
    if not _authed(request):
        return _deny()
    from . import sittings
    try:
        body = await request.json()
        sid = int(body.get("sid"))
        code = (body.get("contact") or "").strip()
        text = (body.get("text") or "").strip()
        edited = 1 if body.get("edited") else 0
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    sittings.mark_sent(sid, code)
    if text:
        try:
            from .style_profile import learn_from_sent
            learn_from_sent(code, text, edited=edited, edit_ratio=100 if not edited else 80)
        except Exception:
            pass
    db.track("liff_orei_sent")
    return {"ok": True}


# ============ 🎂 記念日 (v104: チャットタイル廃止→LIFFへ吸収) ============

@router.get("/api/liff/anni")
def liff_anni(request: Request):
    if not _authed(request):
        return _deny()
    from . import crm, linebot
    linebot.ensure()
    try:
        items = crm.upcoming_anniversaries(30) or []
    except Exception:
        items = []
    out = []
    for x in items:
        code = x.get("contact") or ""
        out.append({"contact": code, "name": linebot._yobina(code),
                    "label": x.get("label") or x.get("kind") or "記念日",
                    "when": x.get("when") or x.get("date") or ""})
    return {"ok": True, "items": out}


# ============ 📰 ネタ (Phase 3 → v99で前倒し) ============

@router.get("/api/liff/news")
def liff_news(request: Request):
    if not _authed(request):
        return _deny()
    from . import news, linebot
    items = news.list_items(20)
    for x in items:
        try:
            x["name"] = linebot._yobina(x.get("contact") or "")
        except Exception:
            x["name"] = x.get("contact") or ""
    return {"ok": True, "items": items}


@router.post("/api/liff/news/refresh")
def liff_news_refresh(request: Request):
    if not _authed(request):
        return _deny()
    from . import news
    r = news.refresh(force=True)
    db.track("liff_news_refresh")
    return {"ok": True, "result": r}


@router.post("/api/liff/news/dismiss")
async def liff_news_dismiss(request: Request):
    if not _authed(request):
        return _deny()
    from . import news
    try:
        nid = int((await request.json()).get("nid"))
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    news.dismiss(nid)
    return {"ok": True}


@router.post("/api/liff/import/assign")
async def liff_import_assign(request: Request):
    if not _authed(request):
        return _deny()
    from . import linebot
    _jobs_ensure()
    try:
        body = await request.json()
        jid = int(body.get("id"))
        contact = (body.get("contact") or "").strip()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    if not contact:
        return JSONResponse({"error": "contact required"}, status_code=400)
    text = linebot._meta_get(f"liffimp_{jid}")
    if not text:
        return JSONResponse({"error": "原文が見つかりません(再アップロードしてください)"}, status_code=410)
    with db.conn() as c:
        c.execute("UPDATE liff_import_jobs SET contact=?, status='queued', detail='' WHERE id=?",
                  (contact, jid))
    threading.Thread(target=_run_import_job, args=(jid, contact, text), daemon=True).start()
    return {"ok": True}
