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


# ============ 合言葉レス・オンボーディング (v151) ============
# 16字の合言葉コピペが実運用の最大の脱落ポイント(2026-08-07 ゆみさん3回失敗)。
# ワンタイムリンクをタップ→LINEログイン(自動)→ひも付け完了、の0入力方式に置き換える。

@router.get("/line/bindlink")
def line_bindlink(key: str = ""):
    """ひも付けリンクの発行(発行者=運用者のみ・72時間有効・1回使うと無効)。
    既にひも付いていても上書きする=機種変更・ロックアウト復旧を兼ねる。"""
    if not config.INGEST_TOKEN or key != config.INGEST_TOKEN:
        return Response(status_code=403)
    from . import linebot
    linebot.ensure()
    import secrets
    tok = secrets.token_urlsafe(12)
    linebot._meta_set("bind_tok", f"{tok}|{time.time() + 72 * 3600}")
    if not LIFF_ID:
        return JSONResponse({"error": "LIFF未設定(CHOUBA_LIFF_ID)"}, status_code=500)
    url = f"https://liff.line.me/{LIFF_ID}?bind={tok}"
    return HTMLResponse(f"""<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<body style="font-family:sans-serif;max-width:440px;margin:40px auto;padding:0 16px">
<h2>🔑 ひも付けリンク(72時間有効・1回きり)</h2>
<p>このリンクを<b>本人のLINEに送って、タップしてもらうだけ</b>でひも付けが完了します。
合言葉の入力は不要です。</p>
<p style="background:#F5F2EA;border-radius:10px;padding:14px;word-break:break-all;
  font-size:15px;user-select:all">{url}</p>
<p style="color:#888;font-size:13px">・タップした人がこの帳場くんの利用者になります(既存のひも付けは上書き)<br>
・間違った人がタップした場合は、このページを開き直して新しいリンクを発行すれば古いリンクは無効になります</p>
</body>""")


@router.post("/api/liff/bind")
async def liff_bind(request: Request):
    """ひも付けリンクの消化。LIFFのIDトークン(本人)+ワンタイムトークンでownerを設定。"""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return JSONResponse({"error": "no token"}, status_code=401)
    sub = _verify_id_token(auth[7:].strip())
    if not sub:
        return JSONResponse({"error": "bad id token"}, status_code=401)
    try:
        body = await request.json()
        tok = (body.get("bind") or "").strip()
    except Exception:
        tok = ""
    from . import linebot
    linebot.ensure()
    rec = linebot._meta_get("bind_tok") or ""
    if not tok or "|" not in rec:
        return JSONResponse({"error": "リンクが無効です"}, status_code=400)
    saved, exp = rec.split("|", 1)
    try:
        expired = time.time() > float(exp)
    except ValueError:
        expired = True
    if tok != saved or expired:
        return JSONResponse({"error": "リンクの期限が切れています"}, status_code=400)
    linebot._meta_set("owner", sub)
    with db.conn() as c:
        c.execute("DELETE FROM linebot_meta WHERE k='bind_tok'")
    print(f"[bind] リンクひも付け完了: …{sub[-6:]}", flush=True)
    db.track("liff_bind_link")
    return {"ok": True, "bound": True}


@router.get("/api/liff/hello")
def liff_hello():
    """認証不要の起動確認。リセット後の「ひも付け前」を検知して案内を出すため(v115)。"""
    from . import linebot
    try:
        bound = bool(linebot.owner_id())
    except Exception:
        bound = False
    return {"ok": True, "bound": bound, "has_liff": bool(LIFF_ID),
            "mode": config.MODE}   # v157: 表示プロファイル(generalの時だけLIFFにトグル表示)


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
        _nitems = news.list_items()
        # v183: タイルの数字=話題数(同じ興味/同じ会社の複数記事は1と数える)。3日で自動失効
        neta = len({(x.get("kw") or "") or ((x.get("contact") or "") + "|" + (x.get("company") or ""))
                    for x in _nitems})
        _jst0 = (int((time.time() + 9 * 3600) // 86400)) * 86400 - 9 * 3600   # 当日JST 0:00
        # 🔥=当日新着の格上げネタ(群3人以上・行事・紙面) / blink=点滅対象(行事・紙面のみ=v184から発火)
        neta_hot = sum(1 for x in _nitems
                       if (x.get("tier") or "") in ("group", "event", "paper")
                       and (x.get("created_ts") or 0) >= _jst0)
        neta_blink = sum(1 for x in _nitems
                         if (x.get("tier") or "") in ("event", "paper")
                         and (x.get("created_ts") or 0) >= _jst0)
        neta_latest = max((x.get("created_ts") or 0 for x in _nitems), default=0)
    except Exception:
        neta, neta_hot, neta_blink, neta_latest = 0, 0, 0, 0
    try:
        anni = len(crm.upcoming_anniversaries(14))
    except Exception:
        anni = 0
    with db.conn() as c:
        last_ts = c.execute("SELECT MAX(ts) FROM messages").fetchone()[0]
        # v177: ↷あとで(deferred)の件数を別枠で返す(queue数は変えない)
        try:
            deferred_n = c.execute(
                "SELECT COUNT(*) FROM messages WHERE status='deferred'").fetchone()[0]
        except Exception:
            deferred_n = 0
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
    try:
        pending_n = len(linebot.visible_pending())   # v150: 4項目+🌐のみ数える
    except Exception:
        pending_n = 0
    try:
        dups_n = len(crm.find_duplicates())   # v184: 同じ人かも(5分キャッシュ)
    except Exception:
        dups_n = 0
    return {
        "fixup": fixup_n,
        "reader": reader,
        "ok": True,
        "queue": len(q), "urgent": urgent_n, "unlinked": unlinked_n,
        # v175: 人数と通数を分けて返す(本人指摘「何通溜まっているか把握されていない」)
        "queue_msgs": sum(int(x.get("count") or 1) for x in q),
        "deferred": deferred_n,   # v177: ↷あとで分(まとめ箱)の通数

        "dups": dups_n,   # v184: 重複カードの疑い(組数)
        "neta": neta, "neta_hot": neta_hot, "neta_blink": neta_blink,
        "neta_latest_ts": neta_latest,
        "anni": anni, "contacts": n_contacts,
        "estranged": est_sa[:5],
        "sent_week": sent_n, "verbatim_week": verb,
        "pending_facts": pending_n,
        "urgent_push": {"on": linebot.urgent_push_enabled(),
                        "used": linebot.urgent_push_count(),
                        "cap": linebot.URGENT_PUSH_CAP},
        "last_ingest_ts": last_ts, "now": time.time(),
    }


@router.post("/api/liff/notify/toggle")
async def liff_notify_toggle(request: Request):
    """v123: 緊急LINE通知のON/OFF。"""
    if not _authed(request):
        return _deny()
    from . import linebot
    try:
        body = await request.json()
        on = bool(body.get("on"))
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    linebot.set_urgent_push(on)
    db.track("liff_notify_toggle")
    return {"ok": True, "on": on}


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
    # v182: AIの種別・立場の予想を暫定適用して動いている相手(=🔖がpendingのまま)は、
    # kindが埋まっていても本人の3連タップ(種別・立場・ランク)が済むまでキューに残す
    with db.conn() as c:
        _pend_rel = set(r["contact"] for r in c.execute(
            "SELECT DISTINCT contact FROM linebot_facts WHERE k='🔖種別・立場' AND status='pending'"))
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
        if code in _pend_rel and "種別" not in missing:
            missing.append("種別・立場(AI予想)")
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
        _hn = fx.get("本名") or a.get("本名") or ""
        # v164: 本人要望「LINE名が本名ぽければ本名に最初から入力できないか」。
        # 会話から本名が抽出できていない時だけ、LINE表示名(=code)自体が本名らしい形かを
        # 見て候補を出す(あくまで候補=仕分け画面で人が確認・タップ確定する。自動確定はしない)。
        _hn_guess = "" if _hn else (code if crm.looks_like_real_name(code) else "")
        out.append({"code": code, "name": linebot._yobina(code, a),
                    "rank": ct.get("rank") or "B", "missing": missing,
                    "suggest": {"呼び名": fx.get("呼び名") or a.get("呼び名") or "",
                                "本名": _hn, "本名候補": _hn_guess,
                                "誕生日": fx.get("誕生日") or ct.get("birthday") or "",
                                "kind": ct.get("kind") or sug_kind or "customer",
                                "stand": ct.get("stand") or sug_stand or "even"}})
    return out


@router.get("/api/liff/fixup")
def liff_fixup(request: Request):
    if not _authed(request):
        return _deny()
    return {"ok": True, "items": _fixup_items()}


@router.post("/api/liff/fixup/bulk")
async def liff_fixup_bulk_ep(request: Request):
    """v151: 「ぜんぶおまかせで確定」(審査TOP10-7)。毎晩の赤警告×人数分の宿題を1タップに。
    既定: 呼び名=表示名の人名部分 / AI推定の種別・立場があれば採用、無ければ顧客・対等。
    あとからカードでいつでも直せる(=完璧より前進)。"""
    if not _authed(request):
        return _deny()
    from . import crm, linebot
    n = 0
    for it in _fixup_items():
        code = it.get("code") or ""
        try:
            sg = it.get("suggest") or {}
            a = crm.get_attrs(code) or {}
            yb = (a.get("呼び名") or "").strip() or (sg.get("呼び名") or "").strip()
            if not yb:
                g, p = crm.group_split(code)
                base = (p or code).split()[0] if (p or code).split() else code
                yb = base.strip("()（）:：・~〜*☆★♪!！?？💕🍸🌸✨ ") or code
            crm.add_def("呼び名"); crm.set_attr(code, "呼び名", yb)
            try:
                crm.add_alias(yb, code)
            except Exception:
                pass
            # v164: ⚡おまかせ確定でも本名候補があれば入れる(呼び名と同じ「あとで直せる」前提)
            hn = (a.get("本名") or "").strip() or (sg.get("本名") or "").strip()
            if not hn and crm.looks_like_real_name(code):
                hn = code
            if hn:
                crm.add_def("本名"); crm.set_attr(code, "本名", hn)
            kind = (sg.get("kind") or "customer").strip() or "customer"
            stand = (sg.get("stand") or "even").strip() or "even"
            with db.conn() as c:
                c.execute("UPDATE contacts SET kind=?, stand=? WHERE code=?", (kind, stand, code))
                c.execute("UPDATE linebot_facts SET status='confirmed' WHERE contact=? "
                          "AND k IN ('呼び名','本名','誕生日','🔖種別・立場') AND status='pending'", (code,))
            linebot.quarantine_release_async(code)   # v187: ⚡おまかせ確定でも検疫解放
            n += 1
        except Exception as e:
            print(f"[fixup bulk] {code}: {e}", flush=True)
    db.track("liff_fixup_bulk")
    return {"ok": True, "done": n}


@router.post("/api/liff/fixup/save")
async def liff_fixup_save(request: Request):
    """1人分の確定。呼び名・本名・種別・立場は必須(サーバー側でも強制)。"""
    if not _authed(request):
        return _deny()
    from . import crm, linebot
    try:
        b = await request.json()
        code = (b.get("code") or "").strip()
        yb = (b.get("呼び名") or "").strip()
        hn = (b.get("本名") or "").strip()
        kind = (b.get("kind") or "").strip()
        stand = (b.get("stand") or "").strip()
        rank = (b.get("rank") or "").strip()   # v182: 3連の一角(必須)
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
    if not (yb and kind in ("customer", "staff", "peer") and stand in ("up", "even", "down")
            and rank in ("S", "A", "B")):
        return JSONResponse({"error": "呼び名・種別・立場・ランクは必須です"}, status_code=400)
    crm.add_def("呼び名"); crm.set_attr(code, "呼び名", yb)
    if hn:
        crm.add_def("本名"); crm.set_attr(code, "本名", hn)
    try:
        # v150: 引数が逆だった実バグ(code側の受信紐付けを実在しない呼び名宛に上書きしていた)。
        # 正: 「呼び名という表示名が来たらこのカード」= add_alias(line_name=yb, contact=code)
        crm.add_alias(yb, code)
    except Exception:
        pass
    with db.conn() as c:
        c.execute("UPDATE contacts SET kind=?, stand=?, rank=? WHERE code=?", (kind, stand, rank, code))
        if bd:
            c.execute("UPDATE contacts SET birthday=? WHERE code=?", (bd, code))
        # 抽出候補は確認済み扱いに(チャット🔎整備で二度聞きしない)
        c.execute("UPDATE linebot_facts SET status='confirmed' WHERE contact=? "
                  "AND k IN ('呼び名','本名','誕生日','🔖種別・立場') AND status='pending'", (code,))
    if belong:
        crm.add_def("所属"); crm.set_attr(code, "所属", belong)
    linebot.quarantine_release_async(code)   # v187: 種別確定→検疫解放(客=適用/非客=破棄)
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
def liff_contacts(request: Request, q: str = "", kind: str = ""):
    """v132: kind= "":顧客のみ(既定) / staff / peer / private / all"""
    if not _authed(request):
        return _deny()
    from . import crm, linebot
    linebot.ensure()
    # v141: グループ由来コード(「グループ名: 人名」)の掃除。取り込み元の記載＋人名の別名登録。
    # 該当カードが無ければ実質no-op(通常0件)
    try:
        for ct in db.list_contacts():
            if ":" in ct["code"] or "：" in ct["code"]:
                crm.annotate_group_origin(ct["code"])
    except Exception:
        pass
    kinds = None if not kind else ("all" if kind == "all" else [kind])
    rows = crm.search_contacts(q=q, kinds=kinds)
    # v174: 並び順「やり取りが新しい順」用の最終接触ts(受信+送信の新しい方)を1クエリで取得
    _lt = {}
    try:
        with db.conn() as c:
            _lt = {r[0]: r[1] for r in c.execute(
                "SELECT contact, MAX(ts) FROM (SELECT contact, ts FROM messages "
                "UNION ALL SELECT contact, ts FROM sent_replies) GROUP BY contact")}
    except Exception:
        pass
    order = {"S": 0, "A": 1, "B": 2}
    out = []
    for r in rows:
        attrs = r.get("attrs") or {}
        g, p = crm.group_split(r["code"])
        nm = linebot._yobina(r["code"], attrs)
        if g:
            nm = nm.replace(r["code"], p)   # 表示は人名に寄せる(コードは不変)
        out.append({
            "code": r["code"],
            "name": nm,
            # v170: ゆみさん要望「見出しは登録名にしてほしい」→v171で全体設定に拡張。
            # 見出しの表示名は「登録名/呼び名/本名」の3パターンから本人が選ぶ(全カード一貫)。
            # iname=登録名(グループ由来はグループ印を落とした人名部分)。
            "iname": (p if g else r["code"]),
            "yobina": attrs.get("呼び名") or "",
            "honmyo": attrs.get("本名") or "",
            "last_ts": _lt.get(r["code"]) or 0,
            "gname": g or "",
            "rank": r.get("rank") or "B",
            "kind": r.get("kind") or "customer",
            "sg": attrs.get("店内区分") or "",
            "birthday": r.get("birthday") or "",
            "company": attrs.get("仕事・会社") or r.get("company") or "",
            "ongoing": attrs.get("進行中の話") or "",
            "ng": attrs.get("NG話題") or "",
        })
    out.sort(key=lambda x: (order.get(x["rank"], 3), x["code"]))
    # v141: 🔒私用タブには「受信ごと削除」にした相手(muted_names)も並べる。
    # この人たちはカードを持たない=一覧のどこにも出ず、間違えて私用にした時に戻す手段が無かった
    if kind == "private":
        have = {o["code"] for o in out}
        for m in crm.list_muted():
            nm = (m.get("line_name") or "").strip()
            if not nm or nm in have:
                continue
            out.append({"code": nm, "name": nm, "gname": "", "rank": "–",
                        "kind": "muted", "sg": "", "birthday": "",
                        "company": "", "ongoing": "", "ng": ""})
    return {"ok": True, "contacts": out}


@router.post("/api/liff/unmute")
async def liff_unmute(request: Request):
    """v141: 私用(受信ごと削除)にした相手を戻す。次の受信から通常どおり届く。"""
    if not _authed(request):
        return _deny()
    from . import crm
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "no name"}, status_code=400)
    crm.unmute(name)
    db.track("liff_unmute")
    return {"ok": True}


# ============ 顧客カード詳細 ============

_PROFILE_KEYS = ("本名", "年齢", "誕生日", "仕事・会社", "家族", "資産・事業",
                 "好きなお酒", "好きな食べ物", "趣味・関心", "健康", "記念日")


def contact_payload(code: str):
    """カード詳細の組み立て。v184: web閲覧ビュー(main.py /api/web/contact)と共用。
    認証は呼び出し側の責務。見つからなければNone。"""
    from . import crm, linebot, news
    linebot.ensure()
    d = crm.contact_detail(code)
    if not d:
        return None
    attrs = d.get("attrs") or {}
    # 履歴: 受信・返信・お席(直近)
    with db.conn() as c:
        msgs = [dict(r) for r in c.execute(
            "SELECT ts, text, status FROM messages WHERE contact=? ORDER BY ts DESC LIMIT 10", (code,))]
        sents = [dict(r) for r in c.execute(
            "SELECT ts, text FROM sent_replies WHERE contact=? ORDER BY ts DESC LIMIT 10", (code,))]
        try:
            # v150: ts/kind列は存在せず🕘お席履歴が一度も出ていなかった実バグ(正: created_ts/stype)
            seki = [{"ts": r["created_ts"],
                     "kind": ("店外" if (r["stype"] or "") == "gaiso" else "来店")}
                    for r in c.execute(
                        "SELECT created_ts, stype FROM sittings WHERE main_contact=? "
                        "ORDER BY created_ts DESC LIMIT 5", (code,))]
        except Exception:
            seki = []
    try:
        persona = linebot.get_persona(code)
    except Exception:
        persona = None
    try:
        # v183: 興味ネタ(who該当)もこの相手のカードに出す=「顧客情報の見落とし防止」
        items = [x for x in news.list_items()
                 if x.get("contact") == code or code in news.who_codes(x)][:3]
    except Exception:
        items = []
    pstat = linebot._meta_get(f"pstat_{code}") or ""
    pending_n = len([f for f in linebot.visible_pending() if f["contact"] == code])
    review_n = len(linebot.reviewable_facts(code))
    gap = None
    try:
        last = linebot._last_interaction(code)
        if last:
            gap = int((time.time() - last) / 86400)
    except Exception:
        pass
    # v141: グループ由来コードは表示を人名に寄せ、取り込み元を記載
    _g, _p = crm.group_split(code)
    if _g:
        crm.annotate_group_origin(code)
        attrs = crm.get_attrs(code) or attrs   # 記載直後の「取り込み元」を反映
    _nm = linebot._yobina(code, attrs)
    if _g:
        _nm = _nm.replace(code, _p)
    return {
        "ok": True,
        "code": code,
        "name": _nm,
        "gname": _g or "",
        "pname": _p if _g else "",   # v141: グループ由来コードの人名部分(呼び名の既定に使う)
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
        # v148: 中身の月日が2ヶ月超前の「進行中の話」は🟢いま効くことに出さない
        # (txt取り込み日=記録日のため、中身の日付で判定。値自体は✎編集に残る)
        "now_keys": {"ongoing": ("" if crm.stale_by_content(attrs.get("進行中の話") or "")
                                 else (attrs.get("進行中の話") or "")),
                     "ng": attrs.get("NG話題") or "",
                     "relmemo": attrs.get("関係性メモ") or ""},
        "persona": persona, "persona_stat": pstat, "has_talk": _has_talk(code),
        # v172: 温度アーク(AI分析)。ok=None:未判断 / 1:下書きに使う / 0:使わない(本人の✓✕・家訓)
        "arc": (lambda: (db.get_profile(code) or {}).get("arc"))(),
        "dyn_block": (lambda: ((db.get_profile(code) or {}).get("dynamics") or {}).get("block") or "")(),
        "pstats": (lambda: linebot.partner_stats(code))(),
        "rel": (lambda: linebot.relationship_stats(code))(),   # v118: 第2層(関係性)
        "enrich": _enrich_data(code),                          # v125: ネット補強
        "news": items,
        "history": {"received": msgs, "sent": sents, "seki": seki},
        "pending_facts": pending_n, "review_facts": review_n,
        "gap_days": gap,
    }


@router.get("/api/liff/contact/{code:path}")
def liff_contact(code: str, request: Request):
    if not _authed(request):
        return _deny()
    p = contact_payload(code)
    if not p:
        return JSONResponse({"error": "not found"}, status_code=404)
    return p


@router.post("/api/liff/contact_delete")
async def liff_contact_delete(request: Request):
    """v145: カードの完全消去(取り消し不可)。UI側でOK入力確認済みの前提。"""
    if not _authed(request):
        return _deny()
    from . import crm
    try:
        body = await request.json()
        code = (body.get("code") or "").strip()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    r = crm.delete_contact_full(code)
    if not r.get("ok"):
        return JSONResponse({"error": r.get("error") or "not found"}, status_code=404)
    db.track("liff_contact_delete")
    return r


_POLICY_KEYS = {
    "first_dist": ("keigo", "soft", "casual"),
    "invite": ("ride", "vague", "store"),
    "push": ("active", "watch", "rare"),
    "length": ("short", "match", "rich"),
    "koi": ("ng", "some", "auto"),
}


@router.get("/api/liff/selfpolicy")
def liff_selfpolicy_get(request: Request):
    """v167: じぶんの方針(本人の返信の基本姿勢)。実例が無い新規の相手への下書きに効く。"""
    if not _authed(request):
        return _deny()
    from . import linebot
    linebot.ensure()
    import json as _json
    try:
        pol = _json.loads(linebot._meta_get("self_policy") or "{}")
    except Exception:
        pol = {}
    return {"ok": True, "policy": pol if isinstance(pol, dict) else {}}


@router.post("/api/liff/selfpolicy")
async def liff_selfpolicy_set(request: Request):
    if not _authed(request):
        return _deny()
    from . import linebot
    linebot.ensure()
    import json as _json
    try:
        body = await request.json()
        raw = body.get("policy") or {}
        if not isinstance(raw, dict):
            raise ValueError()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    # 許可キー・許可値のみ保存(それ以外は黙って捨てる=プロンプトに任意文字列を注入させない)
    clean = {k: v for k, v in raw.items()
             if k in _POLICY_KEYS and isinstance(v, str) and v in _POLICY_KEYS[k]}
    linebot._meta_set("self_policy", _json.dumps(clean, ensure_ascii=False))
    db.track("liff_selfpolicy")
    return {"ok": True, "policy": clean}


@router.post("/api/liff/dynamics/ok")
async def liff_dynamics_ok(request: Request):
    """v172: 温度アーク(AI分析)を下書きに使うかの本人判断(✓=1/✕=0)。
    家訓(v118/v164): AI推定は本人確定を経てからしか下書きに効かせない。toleranceと同じ思想。"""
    if not _authed(request):
        return _deny()
    try:
        body = await request.json()
        code = (body.get("code") or "").strip()
        ok = 1 if body.get("ok") else 0
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    cp = db.get_profile(code) or {}
    if not cp.get("arc"):
        return JSONResponse({"error": "no arc"}, status_code=404)
    cp["arc"]["ok"] = ok
    db.save_profile(code, cp)
    db.track("liff_dynamics_ok")
    return {"ok": True, "arc_ok": ok}


@router.post("/api/liff/contact/{code:path}/rename")
async def liff_contact_rename(code: str, request: Request):
    """v166: 本人要望(ゆみさん↔Aki間のLINEでのやり取りより)。呼び名はメッセージ文だけに使い、
    カード一覧・見出しの索引名(=code)は、ご自身の端末のLINEで表示されている名前(=本人が既に
    連絡先交換時にフルネーム等へ編集している名前)と揃えたい、という要望。
    既存のcrm.rename_contact()をLIFFから呼べるようにするだけ(呼び名とは別の概念として、
    識別名そのものを直接直せるようにする)。旧名は別名(alias)として残るため、その名前からの
    今後の受信も引き続きこのカードに届く=カード分裂は起きない。
    ⚠️ ルート登録順の注意: 直後の /api/liff/contact/{code:path} (POSTのカード編集)は{code:path}が
    スラッシュも呑み込むため、このrenameルートを先に登録しないと "X/rename" ごとcodeとして
    奪われ、常に404になる(実際にテストで踏んだ実バグ→登録順を入れ替えて解消)。"""
    if not _authed(request):
        return _deny()
    from . import crm
    if not db.get_contact(code):
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        body = await request.json()
        new_code = (body.get("new_code") or "").strip()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    if not new_code:
        return JSONResponse({"error": "empty"}, status_code=400)
    if len(new_code) > 60:
        return JSONResponse({"error": "too long"}, status_code=400)
    res = crm.rename_contact(code, new_code)
    if not res.get("ok"):
        return JSONResponse({"error": res.get("error", "rename failed")}, status_code=400)
    db.track("liff_card_rename")
    return res


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
        if not isinstance(body, dict):
            raise ValueError("not dict")
        fields = body.get("fields") or {}
        attrs = body.get("attrs") or {}
        if not isinstance(fields, dict) or not isinstance(attrs, dict):
            raise ValueError("bad types")
        fields = dict(fields)
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)   # v150: 型不正でも500にしない
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


def _enrich_data(code):
    """v125: カード詳細に載せるネット補強の状態。"""
    try:
        from . import enrich as _en
        _en.ensure()
        return {"scope": _en.scope(code), "stat": _en.status(code),
                "items": _en.suggestions(code)}
    except Exception as e:
        print(f"[enrich data] {e}", flush=True)
        return None


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
    from . import readerauth, watchdog, linebot
    linebot.ensure()
    diag = None
    try:
        raw = linebot._meta_get("reader_diag")
        diag = json.loads(raw) if raw else None
    except Exception:
        pass
    return {"ok": True, "watch": watchdog.status(),
            "readers": readerauth.list_readers(),
            "remote": {"q": linebot._meta_get("reader_q") or "",
                       "ver": linebot._meta_get("reader_ver") or "",
                       "pending_cmd": linebot._meta_get("reader_cmd") or "",
                       "diag": diag}}


@router.post("/api/liff/reader/cmd")
async def liff_reader_cmd(request: Request):
    """v134: リーダーへの遠隔コマンド予約(次のハートビート=最大15分以内に実行される)。"""
    if not _authed(request):
        return _deny()
    from . import linebot
    try:
        body = await request.json()
        cmd = body.get("cmd") or ""
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    if cmd not in ("flush", "clearq", "diag"):
        return JSONResponse({"error": "bad cmd"}, status_code=400)
    linebot.ensure()
    linebot._meta_set("reader_cmd", cmd)
    db.track("liff_reader_cmd")
    return {"ok": True, "cmd": cmd}


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
                    "members": len(s.get("members") or []),
                    "unsent": (s.get("member_count") or 0) - (s.get("sent_count") or 0)})
    return {"ok": True, "sittings": out}


# ============ 🔀 カード統合 (v133) / 重複検出 (v184) ============

@router.get("/api/liff/dups")
def liff_dups(request: Request):
    """v184: 同じ人かもしれないカードのペア一覧(呼び名/LINE検索名/本名/名前類似)。"""
    if not _authed(request):
        return _deny()
    from . import crm, linebot
    items = crm.find_duplicates()
    for p in items:
        for side in ("a", "b"):
            try:
                p[side]["name"] = linebot._yobina(p[side]["code"])
            except Exception:
                p[side]["name"] = p[side]["code"]
    return {"ok": True, "items": items}


@router.post("/api/liff/dups/dismiss")
async def liff_dups_dismiss(request: Request):
    """v184: 「別人です」= このペアを今後の検出から外す(永久)。"""
    if not _authed(request):
        return _deny()
    from . import crm
    try:
        body = await request.json()
        a = (body.get("a") or "").strip()
        b = (body.get("b") or "").strip()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    if not a or not b:
        return JSONResponse({"error": "no pair"}, status_code=400)
    crm.dup_dismiss(a, b)
    db.track("liff_dups_dismiss")
    return {"ok": True}



@router.post("/api/liff/merge")
async def liff_merge(request: Request):
    """重複カードの統合。absorb(消える側)→keep(残る側)。取り消し不可。"""
    if not _authed(request):
        return _deny()
    from . import crm
    try:
        body = await request.json()
        keep = (body.get("keep") or "").strip()
        absorb = (body.get("absorb") or "").strip()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    r = crm.merge_contact(keep, absorb)
    if not r.get("ok"):
        return JSONResponse({"error": r.get("error") or "統合できませんでした"}, status_code=400)
    try:
        crm._DUP_CACHE["ts"] = 0.0   # v184: 統合したら重複検出キャッシュを捨てる
    except Exception:
        pass
    db.track("liff_merge")
    return {"ok": True}


# ============ 🌐 顧客ネット補強 (v125) ============

@router.post("/api/liff/enrich/run")
async def liff_enrich_run(request: Request):
    if not _authed(request):
        return _deny()
    from . import enrich
    try:
        body = await request.json()
        code = (body.get("code") or "").strip()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    if not db.get_contact(code):
        return JSONResponse({"error": "not found"}, status_code=404)
    enrich.run_async(code)
    db.track("liff_enrich_run")
    return {"ok": True}


@router.post("/api/liff/enrich/act")
async def liff_enrich_act(request: Request):
    if not _authed(request):
        return _deny()
    from . import enrich
    try:
        body = await request.json()
        sid = int(body.get("id"))
        ok = bool(body.get("ok"))
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    r = enrich.act(sid, ok)
    if r is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    db.track("liff_enrich_act")
    return {"ok": True}


@router.post("/api/liff/enrich/scope")
async def liff_enrich_scope(request: Request):
    if not _authed(request):
        return _deny()
    from . import enrich
    try:
        body = await request.json()
        code = (body.get("code") or "").strip()
        person_ok = bool(body.get("person_ok"))
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    enrich.set_scope(code, person_ok)
    db.track("liff_enrich_scope")
    return {"ok": True}


# ============ 📄 カード印刷ビュー (v123: 共有→PDF用) ============

@router.post("/api/liff/print/prep")
async def liff_print_prep(request: Request):
    """10分有効の印刷トークンを発行(外部ブラウザには認証が無いため)。
    mode=full(自分用・全部) / share(見せる用・NG話題や対応モード等の内心データを除外)。"""
    if not _authed(request):
        return _deny()
    import secrets as _sec
    from . import linebot
    try:
        body = await request.json()
        code = (body.get("code") or "").strip()
        mode = body.get("mode") if body.get("mode") in ("full", "share") else "share"
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    if not db.get_contact(code):
        return JSONResponse({"error": "not found"}, status_code=404)
    tok = _sec.token_urlsafe(9)
    linebot._meta_set(f"prt_{tok}", json.dumps(
        {"code": code, "mode": mode, "exp": time.time() + 600}, ensure_ascii=False))
    db.track("liff_print")
    return {"ok": True, "url": f"/print/{tok}"}


@router.get("/print/{tok}")
@router.get("/api/liff/printview/{tok}")   # v126: /print が環境要因で開けない時の別口(API名前空間=実績あり)
def liff_print_view(tok: str):
    from fastapi.responses import HTMLResponse
    from . import linebot, crm
    raw = linebot._meta_get(f"prt_{tok}")
    try:
        meta = json.loads(raw) if raw else None
    except Exception:
        meta = None
    if not meta or meta.get("exp", 0) < time.time():
        return HTMLResponse("<h3>リンクの有効期限が切れています(10分)。LIFFからもう一度出してください。</h3>",
                            status_code=410)
    code, mode = meta["code"], meta["mode"]
    d = db.get_contact(code) or {}
    a = crm.get_attrs(code) or {}
    p = linebot.get_persona(code) or {}
    rel = linebot.relationship_stats(code)

    def esc(s):
        return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    rows = []

    # v128: 情報量を全部盛りに(本人指摘:少なすぎる)。full=カードの全情報 / share=内心系のみ除外
    used = set()

    def add(k, v):
        if v:
            rows.append(f"<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>")

    def addk(k, label=None):
        used.add(k)
        add(label or k, a.get(k))
    _SENSITIVE = {"NG話題", "関係性メモ", "健康", "資産・事業", "進行中の話", "検索範囲"}
    _KL = {"customer": "💐お客様", "staff": "店内", "peer": "同業", "private": "私用"}
    _SL = {"up": "目上", "senior": "目上", "even": "対等", "equal": "対等", "down": "目下", "junior": "目下"}
    addk("呼び名")
    if mode == "full":
        used.add("本名")
        add("本名", a.get("本名") or d.get("real_name"))
    add("ランク", d.get("rank"))
    add("種別・立場", f"{_KL.get(d.get('kind') or 'customer', d.get('kind'))}"
        + (f"({a.get('店内区分')})" if a.get("店内区分") else "")
        + (f"・{_SL.get(d.get('stand'), '')}" if d.get("stand") else ""))
    used.add("店内区分")
    used.add("誕生日")
    add("誕生日", d.get("birthday") or a.get("誕生日"))
    for k in ("年齢", "仕事・会社", "家族", "好きなお酒", "好きな食べ物", "趣味・関心",
              "記念日", "住まい・エリア", "担当", "お気に入りキャスト"):
        addk(k)
    if mode == "full":
        for k in ("進行中の話", "NG話題", "関係性メモ", "健康", "資産・事業"):
            addk(k)
        if int(d.get("flag_koi") or 0):
            add("対応モード", "💘 ガチ恋・線引き")
        if int(d.get("flag_ero") or 0) == 1:
            add("対応モード", "下ネタいなし")
        for nk, nl in (("note", "メモ"), ("note_pos", "喜ぶ・強み"), ("note_neg", "地雷・注意")):
            add(nl, d.get(nk))
    # 残りの属性を全部(🌐ネット由来含む)。shareでは内心系を除外
    for k in sorted(a.keys()):
        if k in used or (mode != "full" and k in _SENSITIVE):
            continue
        add(k, a[k])
    if rel:
        add("口調", f"自分→{rel['my_register']} ／ 相手→{rel['your_register']}")
        if rel.get("initiator"):
            add("会話の起点", rel["initiator"])
        if rel.get("visits"):
            add("お席実績", f"{rel['visits']}回" + (f"・同伴{rel['dohan']}" if rel.get("dohan") else "")
                + (f"・アフター{rel['after']}" if rel.get("after") else ""))
    try:
        ps = linebot.partner_stats(code)
    except Exception:
        ps = None
    if ps:
        add("相手のクセ", f"平均{ps['avg_len']}字・絵文字{ps['emoji_per_msg']}個/通"
            + (f"・返信中央値{ps['reply_median_min']}分" if ps.get("reply_median_min") is not None else "")
            + (f"・活発な時間 {'/'.join(str(h) + '時' for h in ps.get('top_hours', []))}" if ps.get("top_hours") else ""))
    pers = ""
    if mode == "full" and (p.get("sections") or p.get("summary")):
        pers = "<h2>🧠 ペルソナ</h2>"
        if p.get("summary"):
            pers += f"<p style='font-weight:700;margin:4px 0'>{esc(p['summary'])}</p>"
        if p.get("sections"):
            pers += ("<table>" + "".join(
                f"<tr><th>{esc(s['k'])}</th><td>{esc(s['v'])}"
                + (f"<div class='sub'>「{esc(s['src'])}」</div>" if s.get("src") else "") + "</td></tr>"
                for s in p["sections"]) + "</table>")
        tols = p.get("tolerance") or []
        if tols:
            _M = {1: "✓確認済み", 0: "✕不採用", None: "未確認"}
            pers += ("<h2>🚦 どこまでOKか</h2><table>" +
                     "".join(f"<tr><th>{esc(t['k'])}<div class='sub'>{_M.get(t.get('ok'), '未確認')}</div></th>"
                             f"<td>{esc(t['v'])}</td></tr>" for t in tols) + "</table>")
    # 直近の履歴(自分用のみ)
    if mode == "full":
        try:
            with db.conn() as c:
                recent = [("📩", r["ts"], r["text"]) for r in c.execute(
                    "SELECT ts, text FROM messages WHERE contact=? ORDER BY ts DESC LIMIT 4", (code,))]
                recent += [("📤", r["ts"], r["text"]) for r in c.execute(
                    "SELECT ts, text FROM sent_replies WHERE contact=? ORDER BY ts DESC LIMIT 4", (code,))]
            recent.sort(key=lambda x: -(x[1] or 0))
            if recent:
                pers += ("<h2>🕘 直近のやりとり</h2><table>" + "".join(
                    f"<tr><th>{time.strftime('%m/%d', time.localtime(ts))} {mk}</th>"
                    f"<td>{esc((tx or '')[:70])}</td></tr>" for mk, ts, tx in recent[:8]) + "</table>")
        except Exception:
            pass
    note = ("" if mode == "full" else
            "<p class='sub'>※共有用：内心メモ・注意事項は載せていません</p>")
    html = f"""<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(code)}</title><style>
body{{font-family:'Hiragino Mincho ProN','Noto Serif JP',serif;color:#2B2823;max-width:720px;margin:0 auto;padding:28px 22px}}
h1{{font-size:26px;border-bottom:3px double #A8842F;padding-bottom:8px;color:#1B2A4A}}
h1 small{{font-size:13px;color:#6B6455;font-weight:400;margin-left:10px}}
h2{{font-size:15px;color:#A8842F;margin:18px 0 6px}}
table{{width:100%;border-collapse:collapse}}
th{{text-align:left;width:9em;font-size:13px;color:#6B6455;font-weight:600;padding:7px 8px;border-bottom:1px solid #E6E1D4;vertical-align:top}}
td{{font-size:15px;padding:7px 8px;border-bottom:1px solid #E6E1D4}}
.sub{{color:#6B6455;font-size:12px}}
.hint{{background:#F7F5EF;border:1px solid #E6E1D4;border-radius:10px;padding:10px 12px;font-size:13px;color:#6B6455;margin:14px 0}}
@media print{{.hint{{display:none}}}}
</style></head><body>
<div class="hint">📄 PDFにする: iPhone=共有ボタン→「プリント」→ピンチアウト→共有→ファイルに保存 ／ 画面のスクショでも可</div>
<h1>{esc(linebot._yobina(code, a))}<small>{esc(code)}｜帳場くん {time.strftime("%Y/%m/%d")}</small></h1>
<table>{"".join(rows)}</table>
{pers}{note}
</body></html>"""
    return HTMLResponse(html)


# ============ 🔎 整備・🧹見直しのLIFF化 (v123) ============

@router.get("/api/liff/facts")
def liff_facts(request: Request, scope: str = "pending", code: str = ""):
    """scope=pending: 全員の確認待ち / scope=review&code=X: その相手の自動反映済み(見直し)。"""
    if not _authed(request):
        return _deny()
    from . import linebot
    linebot.ensure()
    if scope == "review" and code:
        rows = linebot.reviewable_facts(code)
    elif code:
        # v141: この相手の確認待ちだけ(カード発の「必ずやらせる」動線。全員分の整理を強制しない)
        rows = [f for f in linebot.visible_pending() if f["contact"] == code]
    else:
        rows = linebot.visible_pending()
    out = []
    for f in rows[:120]:
        try:
            alts = json.loads(f.get("alts") or "[]")
        except Exception:
            alts = []
        from . import crm as _crm
        _g, _pn = _crm.group_split(f["contact"])
        _nm = linebot._yobina(f["contact"])
        if _g:
            _nm = _nm.replace(f["contact"], _pn)   # v141: グループ名はここでも表示しない
        out.append({"id": f["id"], "contact": f["contact"],
                    "name": _nm,
                    "k": f["k"], "v": f["v"], "src": f.get("src") or "",
                    "conf": f.get("conf") or "中", "alts": alts[:3]})
    return {"ok": True, "items": out, "scope": scope}


@router.post("/api/liff/facts/act")
async def liff_facts_act(request: Request):
    """1項目の確定: ok(そのまま反映)/fix(直して反映)/del(消す)/skip(あとで)。"""
    if not _authed(request):
        return _deny()
    from . import linebot, crm
    try:
        body = await request.json()
        fid = int(body.get("id"))
        action = body.get("action") or ""
        value = (body.get("value") or "").strip()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    if action not in ("ok", "fix", "del", "skip"):
        return JSONResponse({"error": "bad action"}, status_code=400)
    f = linebot._get_fact(fid)
    if not f:
        return JSONResponse({"error": "not found"}, status_code=404)
    if action == "ok":
        linebot.apply_fact(f["contact"], f["k"], f["v"])
        linebot._set_fact_status(fid, "applied")
    elif action == "fix":
        if not value:
            return JSONResponse({"error": "empty value"}, status_code=400)
        linebot.apply_fact(f["contact"], f["k"], value)
        with db.conn() as c:
            c.execute("UPDATE linebot_facts SET status='fixed', v=? WHERE id=?", (value, fid))
    elif action == "del":
        linebot._set_fact_status(fid, "deleted")
        # 自動反映済み(見直し)の削除はカードからも下ろす(値が一致する時のみ=手修正を壊さない)
        if f.get("status") == "applied" and f["k"] not in ("呼び名", "誕生日"):
            try:
                cur = (crm.get_attrs(f["contact"]) or {}).get(f["k"])
                if cur == f["v"]:
                    with db.conn() as c:
                        c.execute("DELETE FROM contact_attrs WHERE contact=? AND akey=?",
                                  (f["contact"], f["k"]))
            except Exception as e:
                print(f"[fact del attr] {e}", flush=True)
    elif action == "skip":
        linebot._set_fact_status(fid, "skipped")
    db.track("liff_fact_act")
    return {"ok": True}


@router.post("/api/liff/orei/resume")
async def liff_orei_resume(request: Request):
    """v123(F3): 途中で離脱したお礼を「最近のお席」から再開。送信済みは✓のまま。"""
    if not _authed(request):
        return _deny()
    from . import sittings
    try:
        body = await request.json()
        sid = int(body.get("sid"))
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    s = sittings.get_sitting(sid)
    if not s:
        return JSONResponse({"error": "お席が見つかりません"}, status_code=404)
    sent = {m["contact"] for m in (s.get("members") or []) if m.get("sent")}
    drafts_ = sittings.generate_orei(sid)
    from . import crm as _crm
    for g in drafts_:
        if g.get("contact") in sent:
            g["done"] = True
        _a = _crm.get_attrs(g.get("contact") or "") or {}
        g["sname"] = _a.get("LINE検索名") or ""  # v150
        g["tanto"] = _a.get("担当") or ""        # v176: 検索語候補の降格判定用
        g["sword"] = _a.get("LINE検索確定語") or ""  # v176: 本人タップで学習した検索語
    db.track("liff_orei_resume")
    return {"ok": True, "sid": sid, "drafts": drafts_}


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
        # 「○○とのトーク履歴.txt」「LINE ○○とのトーク.txt」等のゆるい形も拾う
        m2 = re.search(r"(.+?)\s*とのトーク", fname or "")
        if m2:
            _nm = m2.group(1).strip()
            _nm = re.sub(r"^\[?LINE\]?\s*", "", _nm).strip()   # v129: 裸の「LINE 」接頭辞も除去
            name = _nm or None
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
        was_new = not db.get_contact(contact)   # v182: 新規カードのみ裏予想を適用する判定
        if was_new:
            db.upsert_contact(contact, "B")
            crm.link_contact(contact)
            crm.add_alias(contact, contact)
        linebot.save_talk(contact, text)
        # v180: LINE検索名の自動確定。txtのファイル名/本文ヘッダの相手名は「自分のLINE上の
        # 表示名(編集後)」そのもの=検索名として機械的事実(AI推定ではない→○✕関門不要。
        # v166の紐付け時登録名更新と同じ思想)。空の時だけ埋める(手入力値は上書きしない)。
        try:
            if not (crm.get_attrs(contact) or {}).get("LINE検索名"):
                _nm = None
                with db.conn() as c:
                    _r = c.execute("SELECT fname FROM liff_import_jobs WHERE id=?", (jid,)).fetchone()
                _m = _NAME_RE.search(((_r["fname"] if _r else "") or ""))
                if _m:
                    _nm = _m.group(1).strip()
                if not _nm:
                    _m2 = re.search(r"\[LINE\]\s*(.+?)\s*とのトーク", text[:300])
                    if _m2:
                        _nm = _m2.group(1).strip()
                if _nm:
                    crm.add_def("LINE検索名")
                    crm.set_attr(contact, "LINE検索名", _nm)
                    print(f"[import sname] {contact} <- {_nm}", flush=True)
        except Exception as e:
            print(f"[import sname] {e}", flush=True)
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
        # v187(§11): 検疫=種別が本人確定前のカードには🔖種別・立場以外の事実を適用しない。
        # 確定(仕分け3連タップ/⚡おまかせ/✅整備)で客→保留分適用+分析、非客→破棄。
        # 店内スレッドの第三者機微が客カード化する事故を、判定機なし(誤検知ゼロ)で防ぐ
        _confirmed = (not was_new) and linebot.rel_confirmed(contact)
        _kind_now = (db.get_contact(contact) or {}).get("kind") or "customer"
        if _confirmed and _kind_now != "customer":
            # 確定済みの非顧客(店内・同業・私用): 顧客抽出は恒久スキップ(§11 AC。再取り込みも)
            ncrit, nauto = 0, 0
            _quar = True   # 後段分析(実例庫・力学・ペルソナ)もスキップ
            print(f"[quarantine] {contact}: 確定済み非顧客({_kind_now}) → 顧客抽出スキップ", flush=True)
        elif not _confirmed:
            _quar = True
            _keep = [f for f in facts if f.get("k") == linebot._REL_KEY]
            _hold = [f for f in facts if f.get("k") != linebot._REL_KEY]
            ncrit, nauto = linebot.save_split(contact, _keep)
            linebot.quarantine_add(contact, _hold)
        else:
            _quar = False
            ncrit, nauto = linebot.save_split(contact, facts)
        # v182: 本人裁定「手入力までは裏予想で動く」。AIの種別・立場推定を暫定適用する。
        # 🔖種別・立場のpending(確認待ち)は残る=仕分け画面の3連タップを必ず求め続け、
        # 本人の入力があれば上書きされる(家訓5の管理された例外・既存のkindがある相手は触らない)
        try:
            if rel and was_new:   # 既存カード(紐づけ取り込み)はどんな状態でも触らない
                _v = str(rel.get("v") or "")
                _k = ("peer" if _v.startswith("同業") else "staff" if _v.startswith("店内")
                      else "" if _v.startswith("私用") else "customer")
                _s = ("up" if ("目上" in _v or "先輩" in _v)
                      else "down" if ("目下" in _v or "後輩" in _v) else "even")
                if _k:
                    with db.conn() as c:
                        c.execute("UPDATE contacts SET kind=?, stand=? WHERE code=?", (_k, _s, contact))
                    print(f"[import rel-provisional] {contact} <- {_k}/{_s}", flush=True)
        except Exception as e:
            print(f"[import rel-provisional] {e}", flush=True)
        # v167: 本人実例庫(状況×相手の発言×本人の返し)の収穫。失敗しても取り込みは止めない
        # v187: 検疫中は後段分析もスキップ(種別確定時にquarantine_releaseがまとめて実行)
        if not _quar:
            try:
                from . import situations
                situations.harvest_and_save(contact, text, self_name)
            except Exception as e:
                print(f"[situations liff] {e}", flush=True)
            # v172: 関係ダイナミクス分析(決定論指標+温度アーク)。失敗しても取り込みは止めない
            try:
                from . import dynamics
                dynamics.analyze_and_save(contact, text, self_name)
            except Exception as e:
                print(f"[dynamics liff] {e}", flush=True)
        upd("done", f"{ncrit + nauto}")
        # v150: 原文メタの掃除(無限蓄積の防止。救済再実行はqueued/runningのみ対象なので不要になる)
        try:
            with db.conn() as c:
                c.execute("DELETE FROM linebot_meta WHERE k=?", (f"liffimp_{jid}",))
        except Exception:
            pass
        # v140: どのルート(チャット/📥/ショートカット)でも「顧客カード作成完了」を1通通知
        try:
            linebot._notify_card_ready(contact, ncrit, nauto)
        except Exception as e:
            print(f"[import notify] {e}", flush=True)
        if not _quar:
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
        # v177: ファイル名から取れない時(メール添付・AirDropで「talk.txt」等に化けた場合)、
        # 本文先頭の定型ヘッダ「[LINE] ○○とのトーク履歴」から相手名を拾うフォールバック。
        # _NAME_RE+ゆるい形がそのまま効くので_match_contactを本文先頭2行で再実行する。
        if contact is None and name is None:
            try:
                head = "\n".join(text.split("\n")[:2])
                if "とのトーク" in head:
                    contact, cands, name = _match_contact(head)
            except Exception as e:
                print(f"[import head-name] {e}", flush=True)
        if contact is None and name and not cands:
            contact = name   # 新規カードとして作成(ジョブ内でupsert)
        # v131: 既存カードへのマッチは「このtxtはこの顧客？」確認を挟む(誤マージ防止)
        is_existing = bool(contact and db.get_contact(contact))
        status0 = ("confirm" if is_existing else "queued") if contact else "ambiguous"
        with db.conn() as c:
            cur = c.execute("INSERT INTO liff_import_jobs(fname,contact,status,detail,ts) "
                            "VALUES(?,?,?,?,?)",
                            (fname, contact or "", status0,
                             json.dumps({"cands": cands, "name": name}, ensure_ascii=False)
                             if not contact else "", time.time()))
            jid = cur.lastrowid
        linebot._meta_set(f"liffimp_{jid}", text[-200000:])   # v150: 末尾=最新を保持
        if contact and not is_existing:
            threading.Thread(target=_run_import_job, args=(jid, contact, text), daemon=True).start()
        out.append({"id": jid, "fname": fname, "status": status0,
                    "contact": contact, "cands": cands})
    return {"ok": True, "jobs": out}


@router.get("/api/liff/import/status")
def liff_import_status(request: Request):
    if not _authed(request):
        return _deny()
    _jobs_ensure()
    from . import linebot
    # v150: サーバー再起動でrunning/queuedのまま孤児化したジョブの救済。
    # 原文(liffimp_メタ)が残っていれば再実行、無ければ分かる言葉でエラー化
    # (「作成中…」が永遠に回り続ける実害の根治。二重起動はアトミックUPDATEで防止)
    try:
        with db.conn() as c:
            stale = [dict(r) for r in c.execute(
                "SELECT id, contact FROM liff_import_jobs "
                "WHERE status IN ('queued','running') AND ts < ?", (time.time() - 600,))]
        for j in stale:
            text = linebot._meta_get(f"liffimp_{j['id']}") or ""
            with db.conn() as c:
                cur = c.execute(
                    "UPDATE liff_import_jobs SET status=?, detail=?, ts=? "
                    "WHERE id=? AND status IN ('queued','running')",
                    (("queued", "再開", time.time(), j["id"]) if (text and j.get("contact"))
                     else ("error", "取り込みが途中で止まりました。お手数ですがもう一度送ってください",
                           time.time(), j["id"])))
                won = cur.rowcount == 1
            if won and text and j.get("contact"):
                print(f"[import rescue] job{j['id']} を再開", flush=True)
                threading.Thread(target=_run_import_job, args=(j["id"], j["contact"], text),
                                 daemon=True).start()
    except Exception as e:
        print(f"[import rescue] {e}", flush=True)
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
    from . import linebot, koi_guard
    linebot.ensure()
    q = linebot.build_queue()
    # v177: deferred(↷あとで)はPWAのまとめ箱でしか見えず、LIFF動線では黒穴だった
    # (build_queueはopenのみ)。openと同じカードに合流し、deferredのみの相手は
    # 末尾にdeferredフラグ付きカードで返す(PWAの既存意味論の移植)。
    _def_by_contact = {}
    try:
        with db.conn() as c:
            for r in c.execute("SELECT * FROM messages WHERE status='deferred' ORDER BY ts ASC"):
                m = dict(r)
                _def_by_contact.setdefault(m.get("contact") or "", []).append(m)
    except Exception as e:
        print(f"[inbox deferred] {e}", flush=True)
    out = []
    for it in q:
        # v175: 同一相手の未対応「全部」を時系列で連結して見せる(いままではアンカー1通だけ
        # 表示していて、AI下書きと完了処理との三者がズレていた)。表示した分のmidsを添えて
        # クライアントが対応時にまとめて閉じられるようにする。
        opens = []
        try:
            opens = db.open_for_contact(it["contact"])
        except Exception:
            pass
        defs = _def_by_contact.pop(it["contact"], [])
        if opens or defs:
            allm = sorted(opens + defs, key=lambda m: m.get("ts") or 0)
            mids = [m["id"] for m in allm]
            full = "\n".join((m.get("text") or "") for m in allm)
        else:
            mids = it.get("mids") or [it["mid"]]
            full = (db.get_message(it["mid"]) or {}).get("text") or it.get("text") or ""
        it["_deferred_n"] = len(defs)
        from . import crm as _crm
        # v145: 未登録相手は既存カードの候補を添える(表記ゆれ・グループ着信の紐付け動線)
        cands = []
        if it.get("unlinked"):
            try:
                cands = [{"code": x["code"], "name": linebot._yobina(x["code"]),
                          "why": x["why"], "strong": bool(x["strong"])}
                         for x in _crm.find_candidates(it["contact"])]
            except Exception as e:
                print(f"[inbox cands] {e}", flush=True)
        _a = _crm.get_attrs(it["contact"]) or {}
        out.append({"mid": it["mid"], "contact": it["contact"],
                    "name": linebot._yobina(it["contact"]),
                    "rank": it.get("rank") or "B", "urgent": bool(it.get("urgent")),
                    "unlinked": bool(it.get("unlinked")), "reason": it.get("reason") or "",
                    "ts": (max(m.get("ts") or 0 for m in (opens + defs)) if (opens or defs) else it.get("ts")),
                    "text": full[:600],
                    "mids": mids, "count": len(mids),
                    "sname": _a.get("LINE検索名") or "",
                    "sword": _a.get("LINE検索確定語") or "",   # v176: 学習済み検索語(コピー優先)
                    "cands": cands,
                    "deferred": int(it.get("_deferred_n") or 0),  # v177: ↷あとで分の件数
                    # v186(P0): 送信前ガード用(内部語は画面に出さない。koi=発火条件、ok=本人が○済みのID)
                    "koi": int(it.get("koi") or 0),
                    "koi_ok": (koi_guard.ok_ids(it["contact"]) if it.get("koi") else []),
                    "truncated": len(full) > 600})
    # v177: deferredのみの相手(openゼロ)は末尾に「まとめ箱」カードとして追加
    try:
        from . import crm as _crm2
        for ct, msgs in _def_by_contact.items():
            if not ct:
                continue
            c = db.get_contact(ct) or {}
            if (c.get("kind") or "customer") == "staff":
                continue
            mids = [m["id"] for m in msgs]
            full = "\n".join((m.get("text") or "") for m in msgs)
            _a = _crm2.get_attrs(ct) or {}
            out.append({"mid": mids[0], "contact": ct,
                        "name": linebot._yobina(ct),
                        "rank": c.get("rank") or "B", "urgent": False,
                        "unlinked": bool(c.get("linked") == 0 or not c),
                        "reason": (msgs[-1].get("reason") or ""),
                        "ts": msgs[-1].get("ts"),
                        "text": full[:600],
                        "mids": mids, "count": len(mids),
                        "sname": _a.get("LINE検索名") or "",
                        "sword": _a.get("LINE検索確定語") or "",
                        "cands": [],
                        "deferred": len(mids),
                        "koi": int(c.get("flag_koi") or 0),   # v186
                        "koi_ok": (koi_guard.ok_ids(ct) if c.get("flag_koi") else []),
                        "truncated": len(full) > 600})
    except Exception as e:
        print(f"[inbox deferred cards] {e}", flush=True)
    # v186(P0): 送信前ガードのパターン(koiの相手が1人でもいる時だけ同梱)
    try:
        kp = koi_guard.patterns() if any(x.get("koi") for x in out) else []
    except Exception:
        kp = []
    return {"ok": True, "items": out, "koi_patterns": kp}


@router.post("/api/liff/koiguard/ok")
async def liff_koiguard_ok(request: Request):
    """v186(P0): 「これは自分の本音」の○。そのパターンはこの相手について以後黙る。"""
    if not _authed(request):
        return _deny()
    from . import koi_guard
    try:
        body = await request.json()
        code = (body.get("code") or "").strip()
        pid = (body.get("pid") or "").strip()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    if not code or not pid:
        return JSONResponse({"error": "no code/pid"}, status_code=400)
    koi_guard.add_ok(code, pid)
    db.track("liff_koiguard_ok")
    return {"ok": True}


@router.get("/api/liff/message/{mid}/full")
def liff_msg_full(mid: int, request: Request):
    if not _authed(request):
        return _deny()
    m = db.get_message(mid)
    if not m:
        return JSONResponse({"error": "not found"}, status_code=404)
    # v175: カード表示が「その相手の未対応ぜんぶ連結」になったので、全文も同じ集合で返す
    try:
        opens = db.open_for_contact(m.get("contact") or "")
        if opens and any(x["id"] == mid for x in opens):
            return {"ok": True, "text": "\n".join((x.get("text") or "") for x in opens)}
    except Exception:
        pass
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
        force = bool(body.get("force"))   # v123(D1): 🔁作り直し
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    if not db.get_message(mid):
        return JSONResponse({"error": "not found"}, status_code=404)
    gen = (drafts.regenerate(mid) if force else drafts.generate(mid)) or []
    from . import crm
    m = db.get_message(mid) or {}
    db.track("liff_draft")
    return {"ok": True, "drafts": [{"text": g.get("text", "")} for g in gen if g.get("text")][:3],
            "card_keys": crm.card_used_keys(m.get("contact") or ""),
            "gen_note": drafts.last_err(mid) or (
                "いま自動の下書きがお休み中(設定待ち)。下の文は定型です — お店の担当さんに『帳場くんのAI設定』と伝えてください"
                if not config.ANTHROPIC_API_KEY else "")}   # v150: 技術用語を出さない(詳細はログ)


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
        # v177: クライアントが送るmids(画面に出ていた同一相手の全メッセージid)を捨てていた。
        # skipped/deferredはclose_contact_openが走らないため、アンカー1通しか閉じず
        # 残りが再出現するv175症状の残存経路だった(regress-1)。
        mids = body.get("mids")
        mids = [int(x) for x in mids] if isinstance(mids, list) else None
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    from .main import act as _act, Action as _Action
    try:
        r = _act(mid, _Action(action=action, text=text, mids=mids))
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
    if not name or v not in ("work", "staff", "peer", "priv", "link"):
        return JSONResponse({"error": "bad params"}, status_code=400)
    if v == "link":
        # v145: 既存カードに紐付け(別名化・孤児カードは吸収・トレイ掃除まで一括)
        target = (body.get("target") or "").strip()
        if not target or not db.get_contact(target):
            return JSONResponse({"error": "bad target"}, status_code=400)
        crm.resolve_pending(name, "link", contact=target)
        db.track("liff_classify_link")
        return {"ok": True, "contact": target}
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
    from . import linebot, crm
    linebot.ensure()
    crm.ensure()
    try:
        body = await request.json()
        segs = body.get("segs") or ([body["seg"]] if body.get("seg") else ["ALL"])
        if not isinstance(segs, list):   # v150: 非配列で500にしない
            return JSONResponse({"error": "segs must be a list"}, status_code=400)
        segs = [s for s in segs if isinstance(s, str)]
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
            _a = crm.get_attrs(c_) or {}
            items.append({"code": c_, "name": linebot._yobina(c_),
                          "rank": (db.get_contact(c_) or {}).get("rank") or "B",
                          "tone": tone,
                          "sname": _a.get("LINE検索名") or "",
                          "tanto": _a.get("担当") or "",           # v176: 検索語候補の降格判定用
                          "sword": _a.get("LINE検索確定語") or ""})  # v176: 学習済み検索語
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
        row_ai = bool(config.ANTHROPIC_API_KEY)
    else:
        # 注: greetingはranks/tags必須の設計。宛先は確定済みなので全ランクを通す
        r = campaign.generate(mode="greeting", ranks=["S", "A", "B"], codes=[code],
                              template=template, purpose=purpose)
        items = r.get("items") or []
        text = items[0]["text"] if items else ""
        # v177: AI生成できたか(row_ai)をフロントに通す=フォールバック時に琥珀注意を出す下地
        row_ai = bool(items[0].get("ai")) if items else False
    from . import crm
    db.track("liff_ann_draft")
    return {"ok": True, "text": text, "ai": row_ai, "card_keys": crm.card_used_keys(code)}


@router.post("/api/liff/ann/sent")
async def liff_ann_sent(request: Request):
    if not _authed(request):
        return _deny()
    try:
        body = await request.json()
        code = (body.get("code") or "").strip()
        text = (body.get("text") or "").strip()
        orig = (body.get("orig") or "").strip()
        edited = 1 if body.get("edited") else 0
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    if text:
        try:
            from .style_profile import learn_from_sent
            # v172: edit_ratioを実類似度に(赤チーム指摘=固定80/100では効果測定が原理的に不可能)
            import difflib as _dl
            ratio = (int(round(_dl.SequenceMatcher(None, orig, text).ratio() * 100))
                     if orig else (100 if not edited else 80))
            learn_from_sent(code, text, edited=edited, edit_ratio=ratio)
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
        if not isinstance(helpers, list):         # v150: 型不正で500にしない
            helpers = []
        helpers = [h for h in helpers if isinstance(h, dict)]
        day = body.get("day") or "today"          # today / yesterday / YYYY-MM-DD(v161)
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
    elif re.match(r"^\d{4}-\d{2}-\d{2}$", day or ""):
        # v161: 「日付を選ぶ」で過去日を選んだ来店記録。時刻は分からないので現在時刻のまま日付だけ差し替え、
        # 未来日は今日に丸める(まだ起きていない来店として記録させない)
        try:
            chosen = datetime.datetime.strptime(day, "%Y-%m-%d").date()
            today_jst = datetime.datetime.now(jst).date()
            if chosen > today_jst:
                chosen = today_jst
            d0 = datetime.datetime.combine(chosen, d0.timetz())
        except Exception:
            pass
    label = d0.strftime("%m/%d")
    sid = sittings.create_sitting(label, main, members, stype=stype, venue=venue,
                                  dohan_venue=dohan, after_venue=after, visit_ts=d0.timestamp())
    drafts_ = sittings.generate_orei(sid)
    from . import crm as _crm
    for g in drafts_:   # v150: LINE検索名を添える(g.snameが常にundefinedだった)
        _a = _crm.get_attrs(g.get("contact") or "") or {}
        g["sname"] = _a.get("LINE検索名") or ""
        g["tanto"] = _a.get("担当") or ""        # v176: 検索語候補の降格判定用
        g["sword"] = _a.get("LINE検索確定語") or ""  # v176: 本人タップで学習した検索語
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
            import difflib as _dl
            orig = (body.get("orig") or "").strip()
            ratio = (int(round(_dl.SequenceMatcher(None, orig, text).ratio() * 100))
                     if orig else (100 if not edited else 80))
            learn_from_sent(code, text, edited=edited, edit_ratio=ratio)
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
        # v177: crm側のキーは'code'(旧'contact'は常に空でカード遷移・宛先が壊れていた)。
        # crmは呼び名解決済みのnameを返すのでそれを優先。days/draftも通す(祝い文動線の下地)。
        code = x.get("code") or x.get("contact") or ""
        out.append({"contact": code, "name": x.get("name") or linebot._yobina(code),
                    "label": x.get("label") or x.get("kind") or "記念日",
                    "when": x.get("date") or x.get("when") or "",
                    "days": x.get("days"),
                    "draft": x.get("draft") or ""})
    return {"ok": True, "items": out}


# ============ 📰 ネタ (Phase 3 → v99で前倒し) ============

@router.get("/api/liff/news")
def liff_news(request: Request):
    if not _authed(request):
        return _deny()
    from . import news, linebot, crm as _crm
    items = news.list_items(20)
    latest = 0.0
    for x in items:
        latest = max(latest, x.get("created_ts") or 0)
        try:
            x["name"] = linebot._yobina(x.get("contact") or "")
        except Exception:
            x["name"] = x.get("contact") or ""
        # v183: 単発送信にv176検索語ユニットを接続(会社ネタ=contactあり)
        if x.get("contact"):
            try:
                _a = _crm.get_attrs(x["contact"]) or {}
                x["sname"] = _a.get("LINE検索名") or ""
                x["sword"] = _a.get("LINE検索確定語") or ""
            except Exception:
                x["sname"], x["sword"] = "", ""
    return {"ok": True, "items": items, "latest_ts": latest}


@router.post("/api/liff/news/refresh")
def liff_news_refresh(request: Request):
    """v183: 同期実行を廃止(全パス実行は数分かかりプロキシの約100秒を超える)。
    裏スレッドで起動して即返す。失敗しても本流には影響しない。"""
    if not _authed(request):
        return _deny()
    from . import news
    import threading as _th

    def _run():
        try:
            news.refresh(force=True)
        except Exception as e:
            print(f"[news refresh bg] {e}", flush=True)
    _th.Thread(target=_run, daemon=True).start()
    db.track("liff_news_refresh")
    return {"ok": True, "started": True}


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


@router.post("/api/liff/news/used")
async def liff_news_used(request: Request):
    """v183: 「📤これで話しかける」成立の記録。同じ興味は3日休む(同文使い回し抑制)。"""
    if not _authed(request):
        return _deny()
    from . import news
    try:
        nid = int((await request.json()).get("nid"))
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    news.mark_used(nid)
    db.track("liff_news_used")
    return {"ok": True}


@router.post("/api/liff/import/retry")
async def liff_import_retry_ep(request: Request):
    """v151: エラー行の再実行。原文が残っていれば再アップロード不要でやり直す。"""
    if not _authed(request):
        return _deny()
    from . import linebot
    _jobs_ensure()
    try:
        body = await request.json()
        jid = int(body.get("id"))
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    with db.conn() as c:
        r = c.execute("SELECT * FROM liff_import_jobs WHERE id=?", (jid,)).fetchone()
    if not r:
        return JSONResponse({"error": "not found"}, status_code=404)
    text = linebot._meta_get(f"liffimp_{jid}") or ""
    contact = (dict(r).get("contact") or "").strip()
    if not text or not contact:
        return {"ok": False, "need_reupload": True,
                "msg": "元のデータが残っていませんでした。もう一度トーク履歴を送ってください"}
    with db.conn() as c:
        c.execute("UPDATE liff_import_jobs SET status='queued', detail='再実行', ts=? WHERE id=?",
                  (time.time(), jid))
    threading.Thread(target=_run_import_job, args=(jid, contact, text), daemon=True).start()
    db.track("liff_import_retry")
    return {"ok": True}


@router.post("/api/liff/import/dismiss")
async def liff_import_dismiss(request: Request):
    """v151: エラー行の掃除(赤い行が永久に残り「壊れてる」第一印象を作る問題)。"""
    if not _authed(request):
        return _deny()
    _jobs_ensure()
    try:
        body = await request.json()
        jid = int(body.get("id"))
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    with db.conn() as c:
        c.execute("DELETE FROM liff_import_jobs WHERE id=? AND status='error'", (jid,))
        c.execute("DELETE FROM linebot_meta WHERE k=?", (f"liffimp_{jid}",))
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
