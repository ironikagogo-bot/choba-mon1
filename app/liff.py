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
import hmac
import json
import os
import re
import threading
import time

import requests
from fastapi import APIRouter, Request, Response, UploadFile, File
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

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
def liff_page(request: Request):
    path = os.path.join(os.path.dirname(__file__), "static", "liff.html")
    try:
        st = os.stat(path)
        etag = f'"{int(st.st_mtime)}-{st.st_size}"'
        # v229: 「デプロイしたのに画面が変わらない」の根治。WebViewのHTMLキャッシュを
        # 毎回サーバー確認(no-cache)+ETagで制御 — 変更なしなら304(転送ゼロ)、
        # 変更ありなら必ず新しい画面が出る
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={
                "ETag": etag, "Cache-Control": "no-cache"})
        with open(path, encoding="utf-8") as f:
            html = f.read()
    except Exception:
        return Response("liff.html がありません", status_code=500)
    html = html.replace("__LIFF_ID__", LIFF_ID)
    return HTMLResponse(html, headers={"ETag": etag, "Cache-Control": "no-cache"})


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
    # v196: ホーム受信タイルの内訳(本人裁定「緊急何件、店内何件、後で何件に分けるべき」)。
    # 緊急=urgent(店内は無音方針のため除外) / 店内=staff全部 / ふつう=残り
    staff_n = sum(1 for x in q if (x.get("kind") or "customer") == "staff")
    hot_urgent_n = sum(1 for x in q if x.get("urgent")
                       and (x.get("kind") or "customer") != "staff")
    normal_n = len(q) - staff_n - hot_urgent_n
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
        # v190: あとでの相手数(タイルラベル切替・完走画面用。単位は人)
        try:
            deferred_contacts = c.execute(
                "SELECT COUNT(DISTINCT contact) FROM messages WHERE status='deferred'").fetchone()[0]
        except Exception:
            deferred_contacts = 0
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
    # v190: 琥珀バー用(匿名データのみ。名前・ランク語は返さない)
    try:
        _now = time.time()
        _sa_ages = [int((_now - (x.get("ts") or _now)) / 60) for x in q
                    if x.get("rank") in ("S", "A") and not x.get("unlinked")
                    and (x.get("kind") or "customer") == "customer"]
        oldest_sa_min = max(_sa_ages) if _sa_ages else 0
        unlinked_urgent = sum(1 for x in q if x.get("unlinked") and x.get("urgent"))
    except Exception:
        oldest_sa_min, unlinked_urgent = 0, 0
    # v223(🧹段階投入・裁定3): 5日以上前のopenを持つ相手数。受信箱を開かない人にも
    # ホームで「たまり」を見せる入口(mon1実測: 一括片づけの入口が受信箱内のみ→使用0)
    try:
        # v235(監査指摘): 母集団を受信箱の🧹(repIsHotを除いたnorm)と揃える。
        # ズレていると「バナーは出るのに、開くと片づける入口がどこにも無い」になる。
        # 🧹の保護対象=🔥急ぎ・Sランク・📌ピン(=contacts.flag_hot)
        with db.conn() as c:
            sweep_old = c.execute(
                "SELECT COUNT(DISTINCT m.contact) AS n FROM messages m "
                "LEFT JOIN contacts ct ON ct.code = m.contact "
                "WHERE m.status='open' AND m.ts < ? "
                "AND IFNULL(m.category,'') <> 'urgent' "
                "AND IFNULL(ct.rank,'B') <> 'S' AND IFNULL(ct.flag_hot,0) = 0",
                (time.time() - 5 * 86400,)).fetchone()["n"]
    except Exception:
        sweep_old = 0
    # v232: 🎛返信の調整 — あたらしい学び(未確認の🚦・🪞)の件数と配信つまみ既定
    # v235: ホームは頻繁に叩かれるので件数はキャッシュから読む(監査指摘: 全ペルソナの
    # JSON再パースをv218で軽くしたホームに戻していた)。書き換え側でbust。
    try:
        tune_n = _tune_count()
    except Exception:
        tune_n = 0
    try:
        plevel_default = max(0, min(2, int(linebot._meta_get("ann_plevel_default") or 1)))
    except Exception:
        plevel_default = 1
    # v235: バックアップの催促(環境ごと消える事故に効くのは手元への持ち出しだけ)
    try:
        from . import backup as _bk
        _bkst = _bk.status()
        backup_age = _bkst["download_age_days"]
        backup_warn = _bkst["persistence"] == "ephemeral"
    except Exception:
        backup_age, backup_warn = None, False
    return {
        "backup_age": backup_age, "backup_warn": backup_warn,   # v235
        "sweep_old": sweep_old,
        "fixup": fixup_n,
        "tune_n": tune_n, "plevel_default": plevel_default,   # v232: 🎛
        "reader": reader,
        "ok": True,
        "deferred_contacts": deferred_contacts,   # v190: あとでの相手数(人)
        "oldest_sa_min": oldest_sa_min, "unlinked_urgent": unlinked_urgent,
        "queue": len(q), "urgent": urgent_n, "unlinked": unlinked_n,
        # v196: 受信タイルの内訳(緊急=非店内urgent / 店内 / ふつう=残り。あとでは deferred_contacts)
        "hot_urgent_n": hot_urgent_n, "staff_n": staff_n, "normal_n": normal_n,
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

def _needs_fixup(code, ct, attrs):
    """v217: この1人に仕分け(3連タップ)が残っているか。_fixup_itemsと同じ判定の単独版。"""
    try:
        if (ct.get("linked") or 1) == 0:
            return False
        if not ((attrs or {}).get("呼び名") or "").strip():
            return True
        if not (ct.get("kind") or "").strip() or not (ct.get("stand") or "").strip():
            return True
        with db.conn() as c:
            r = c.execute("SELECT 1 FROM linebot_facts WHERE contact=? AND k='🔖種別・立場' "
                          "AND status='pending' LIMIT 1", (code,)).fetchone()
        return bool(r)
    except Exception:
        return False


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
        # v192その3(案1): 「誰の話か」をカード先頭で思い出せるよう直近の受信1行を添える。
        # グループ由来は自分宛てフィルタを通った行を優先(他人の発言で誤想起させない)
        _lt = ""
        try:
            with db.conn() as c:
                _rows = c.execute("SELECT text FROM messages WHERE contact=? "
                                  "ORDER BY ts DESC LIMIT 8", (code,)).fetchall()
            for _r in _rows:
                _t = (_r["text"] or "").strip()
                if _t and linebot.group_visible(_t, code):
                    _lt = linebot._GRP_MARK_RE.sub("", _t).strip().replace("\n", " ")[:60]
                    break
        except Exception:
            pass
        out.append({"code": code, "name": linebot._yobina(code, a),
                    "last_text": _lt,
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
            # v191その2(#8): ⚡おまかせはAIの種別予想の一括採用であって本人の種別確定ではない。
            # 客なら検疫解放(適用)するが、非顧客予想での解放=保留事実の不可逆破棄はしない
            # (検疫のまま維持。破棄は個別の種別確定タップのみ)。
            if kind == "customer":
                linebot.quarantine_release_async(code)   # v187: ⚡おまかせ確定でも検疫解放(客)
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
        tanto = (b.get("担当") or "").strip()[:40]        # v214: 仕分けで担当も一緒に
        koi = 1 if b.get("flag_koi") in (1, "1", True) else 0   # v214: 恋愛系の線引きも一緒に
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    if not db.get_contact(code):
        return JSONResponse({"error": "not found"}, status_code=404)
    if kind == "priv":
        crm.mute(code)
        crm.discard_unlinked(code)
        linebot.quarantine_discard(code)   # v218(S2): 保留事実(検疫)も破棄=永久残留させない
        db.track("liff_fixup_priv")
        return {"ok": True, "discarded": True}
    if not (yb and kind in ("customer", "staff", "peer") and stand in ("up", "even", "down")):
        return JSONResponse({"error": "呼び名・種別・立場は必須です"}, status_code=400)
    # v220: ランクは顧客のみ必須(店内・同業にS(太客)/A/Bを聞くのは変・本人指摘2026-08-13)。
    # 非顧客は既定Bで保存(rank列は全カード共通のため)
    if kind == "customer" and rank not in ("S", "A", "B"):
        return JSONResponse({"error": "ランクは必須です"}, status_code=400)
    if rank not in ("S", "A", "B"):
        rank = "B"
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
        # v191その2(#8): 🔖の値を本人確定の内容に上書き(AI予想値のまま確定になる矛盾を残さない)
        try:
            _st_map = {"up": "senior", "even": "equal", "down": "junior"}
            c.execute("UPDATE linebot_facts SET v=? WHERE contact=? AND k=?",
                      (linebot._rel_value(kind, _st_map.get(stand, "equal")), code,
                       linebot._REL_KEY))
        except Exception as _e:
            print(f"[fixup rel] {_e}", flush=True)
    if belong:
        crm.add_def("所属"); crm.set_attr(code, "所属", belong)
    if tanto and kind == "customer":   # v214: 担当は顧客のみ(staff/peerには意味を持たない)
        try:
            crm.add_def("担当"); crm.set_attr(code, "担当", tanto)
        except Exception as e:
            print(f"[fixup tanto] {e}", flush=True)
    if koi and kind == "customer":     # v214: flag_koiは顧客限定(v187§10: 非客に客UIを誤爆させない)
        with db.conn() as c:
            c.execute("UPDATE contacts SET flag_koi=1 WHERE code=?", (code,))
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
        # v217: 仕分け(呼び名・種別・立場の本人確定)が残っているか。通知→カード直行の動線で
        # ホームと同じ宿題が見えるように(本人指摘2026-08-13: 入口によって聞かれることが違う)
        "needs_fixup": _needs_fixup(code, d, attrs),
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


@router.get("/api/liff/export/{code:path}")
def liff_contact_export(code: str, request: Request, key: str = "", fmt: str = "txt"):
    """v210: 相手1人の生ログ書き出し(owner専用・key付き救済口の規約)。
    用途: 呼び名抽出の実測・デバッグ・移行。中身=取り込みtxt原文+受信(moto経由)+送信記録。
    ブラウザで開ける(ヘッダ不要のkey=INGEST_TOKEN)。破壊なしの読み取りのみ。"""
    if not config.INGEST_TOKEN or key != config.INGEST_TOKEN:
        if not _authed(request):
            return _deny()
    from . import linebot
    if not db.get_contact(code):
        return JSONResponse({"error": "not found"}, status_code=404)
    with db.conn() as c:
        r = c.execute("SELECT text, ts FROM linebot_talks WHERE contact=?", (code,)).fetchone()
        talk = (r["text"] if r else "") or ""
        talk_ts = r["ts"] if r else None
        recv = [dict(x) for x in c.execute(
            "SELECT ts, text, category, status FROM messages WHERE contact=? ORDER BY ts", (code,))]
        sent = [dict(x) for x in c.execute(
            "SELECT ts, text, edited FROM sent_replies WHERE contact=? ORDER BY ts", (code,))]
    db.track("liff_export")
    if fmt == "json":
        return {"contact": code, "talk_txt": talk, "talk_imported_ts": talk_ts,
                "received": recv, "sent": sent}
    import datetime as _dt
    def _t(ts):
        try:
            return _dt.datetime.fromtimestamp(ts, _dt.timezone(_dt.timedelta(hours=9))).strftime("%Y/%m/%d %H:%M")
        except Exception:
            return "?"
    lines = [f"[帳場エクスポート] {code}", ""]
    if talk:
        lines += ["===== 取り込みtxt原文 =====", talk, ""]
    lines.append("===== サーバー蓄積(受信=📩 / 送信=📤) =====")
    merged = [("📩", m["ts"], m["text"], m.get("status", "")) for m in recv] +              [("📤", m["ts"], m["text"], "") for m in sent]
    merged.sort(key=lambda x: x[1] or 0)
    for mark, ts, text, st in merged:
        lines.append(f"{_t(ts)}\t{mark}\t{(text or '').strip()}" + (f"\t[{st}]" if st else ""))
    return Response("\n".join(lines), media_type="text/plain; charset=utf-8")


@router.get("/api/liff/export_flag")
def liff_export_flag(request: Request, key: str = "", flag: str = "koi", fmt: str = "txt"):
    """v213: フラグ指定の一括生ログ書き出し(owner専用key口)。flag=koi(ガチ恋)/ero(下ネタ)/hot(ピン)。
    用途: ガチ恋会話の実例分析など。中身は相手ごとに区切った取り込みtxt+会話蓄積。"""
    if not config.INGEST_TOKEN or key != config.INGEST_TOKEN:
        if not _authed(request):
            return _deny()
    col = {"koi": "flag_koi", "ero": "flag_ero", "hot": "flag_hot"}.get(flag)
    if not col:
        return JSONResponse({"error": "flag は koi/ero/hot"}, status_code=400)
    from . import crm
    crm.ensure()
    with db.conn() as c:
        codes = [r["code"] for r in c.execute(
            f"SELECT code FROM contacts WHERE IFNULL({col},0)=1 ORDER BY code")]
    db.track("liff_export")
    if fmt == "json":
        out = []
        for code in codes:
            with db.conn() as c:
                r = c.execute("SELECT text, ts FROM linebot_talks WHERE contact=?", (code,)).fetchone()
                recv = [dict(x) for x in c.execute(
                    "SELECT ts, text, category, status FROM messages WHERE contact=? ORDER BY ts", (code,))]
                sent = [dict(x) for x in c.execute(
                    "SELECT ts, text FROM sent_replies WHERE contact=? ORDER BY ts", (code,))]
            out.append({"contact": code, "talk_txt": (r["text"] if r else "") or "",
                        "received": recv, "sent": sent})
        return {"flag": flag, "contacts": codes, "items": out}
    import datetime as _dt
    def _t(ts):
        try:
            return _dt.datetime.fromtimestamp(ts, _dt.timezone(_dt.timedelta(hours=9))).strftime("%Y/%m/%d %H:%M")
        except Exception:
            return "?"
    lines = [f"[帳場一括エクスポート] flag={flag} 対象{len(codes)}人: {'、'.join(codes) or 'なし'}", ""]
    for code in codes:
        with db.conn() as c:
            r = c.execute("SELECT text FROM linebot_talks WHERE contact=?", (code,)).fetchone()
            recv = [dict(x) for x in c.execute(
                "SELECT ts, text, status FROM messages WHERE contact=? ORDER BY ts", (code,))]
            sent = [dict(x) for x in c.execute(
                "SELECT ts, text FROM sent_replies WHERE contact=? ORDER BY ts", (code,))]
        lines += [f"■■■■■■■■■■ {code} ■■■■■■■■■■", ""]
        talk = (r["text"] if r else "") or ""
        if talk:
            lines += ["── 取り込みtxt原文 ──", talk, ""]
        merged = [("📩", m["ts"], m["text"]) for m in recv] + [("📤", m["ts"], m["text"]) for m in sent]
        merged.sort(key=lambda x: x[1] or 0)
        if merged:
            lines.append("── サーバー蓄積(受信📩/送信📤) ──")
            lines += [f"{_t(ts)}\t{mark}\t{(tx or '').strip()}" for mark, ts, tx in merged]
        lines.append("")
    return Response("\n".join(lines), media_type="text/plain; charset=utf-8")


# ============ v215: 取り込みtxt→カード反映の点検と自動アップデート(owner専用key口) ============

def _card_audit_rows():
    """取り込み済み(linebot_talks)の各相手について、カード反映状況を棚卸しする。"""
    from . import linebot, crm, situations
    linebot.ensure(); crm.ensure(); situations.ensure()
    rows = []
    with db.conn() as c:
        talks = [dict(r) for r in c.execute(
            "SELECT contact, LENGTH(text) AS chars FROM linebot_talks ORDER BY contact")]
        for t in talks:
            code = t["contact"]
            ct = db.get_contact(code) or {}
            a = crm.get_attrs(code) or {}
            fc = {r["status"]: r["n"] for r in c.execute(
                "SELECT status, COUNT(*) AS n FROM linebot_facts WHERE contact=? GROUP BY status", (code,))}
            try:
                import json as _j
                quar = len(_j.loads(linebot._meta_get(f"quarantine_{code}") or "[]"))
                quar += len(_j.loads(linebot._meta_get(f"quarantine_bak_{code}") or "[]"))   # v218(S5)
            except Exception:
                quar = 0
            has_persona = bool(linebot.get_persona(code))
            has_dyn = bool((db.get_profile(code) or {}).get("dynamics"))
            sitn = c.execute("SELECT COUNT(*) AS n FROM self_examples WHERE contact=?",
                             (code,)).fetchone()
            has_style = bool((db.get_profile(code) or {}).get("samples"))
            rows.append({
                "contact": code, "chars": t["chars"] or 0,
                "kind": ct.get("kind") or "?", "rank": ct.get("rank") or "?",
                "confirmed": linebot.rel_confirmed(code),
                "yobina": a.get("呼び名") or "",
                "attrs_n": len([k for k, v in a.items() if (v or "").strip()]),
                "facts": {"pending": fc.get("pending", 0), "applied": fc.get("applied", 0),
                          "confirmed": fc.get("confirmed", 0)},
                "quarantine_held": quar,
                "persona": has_persona, "dynamics": has_dyn,
                "situations_n": (sitn["n"] if sitn else 0), "style": has_style,
            })
    return rows


@router.get("/api/liff/card_audit")
def liff_card_audit(request: Request, key: str = "", fmt: str = "txt"):
    """v215: txt取り込み→カード反映の点検(読み取りのみ)。ブラウザで開ける。"""
    from . import linebot
    if not config.INGEST_TOKEN or key != config.INGEST_TOKEN:
        if not _authed(request):
            return _deny()
    rows = _card_audit_rows()
    st = (linebot._meta_get("backfill215") or "").split("@")[0]   # @以降は内部の時刻印
    db.track("liff_card_audit")
    if fmt == "json":
        return {"ok": True, "items": rows, "backfill215": st}
    cust = [r for r in rows if r["kind"] == "customer"]
    quar_stuck = [r for r in rows if r["quarantine_held"] > 0 or not r["confirmed"]]
    no_yob = [r for r in cust if r["confirmed"] and not r["yobina"]]
    no_facts = [r for r in cust if r["confirmed"]
                and sum(r["facts"].values()) == 0 and r["quarantine_held"] == 0]
    no_pers = [r for r in cust if r["confirmed"] and not r["persona"] and r["chars"] >= 3000]
    no_dyn = [r for r in cust if r["confirmed"] and not r["dynamics"]]
    L = [f"[カード反映点検] 取り込み済み {len(rows)}人(うち顧客 {len(cust)}人)", ""]
    L.append(f"■ 仕分け待ち(検疫で反映保留・仕分け3連タップ/⚡おまかせで反映): {len(quar_stuck)}人"
             + (f" … {'、'.join(r['contact'] for r in quar_stuck[:20])}" if quar_stuck else ""))
    L.append(f"■ 呼び名なし(確定済み顧客): {len(no_yob)}人"
             + (f" … {'、'.join(r['contact'] for r in no_yob[:20])}" if no_yob else ""))
    L.append(f"■ 抽出事実ゼロ(確定済み顧客・保留もなし): {len(no_facts)}人"
             + (f" … {'、'.join(r['contact'] for r in no_facts[:20])}" if no_facts else ""))
    L.append(f"■ ペルソナ未生成(3000字以上): {len(no_pers)}人"
             + (f" … {'、'.join(r['contact'] for r in no_pers[:20])}" if no_pers else ""))
    L.append(f"■ 関係ダイナミクス未分析: {len(no_dyn)}人"
             + (f" … {'、'.join(r['contact'] for r in no_dyn[:20])}" if no_dyn else ""))
    L += ["", "── 相手別 ──",
          "contact\t字数\t種別\t確定\t呼び名\t属性\t事実(保留/適用/確認)\t検疫\tペルソナ\t力学\t実例\t文体"]
    for r in rows:
        L.append(f"{r['contact']}\t{r['chars']}\t{r['kind']}\t{'✓' if r['confirmed'] else '…'}"
                 f"\t{r['yobina'] or '-'}\t{r['attrs_n']}"
                 f"\t{r['facts']['pending']}/{r['facts']['applied']}/{r['facts']['confirmed']}"
                 f"\t{r['quarantine_held']}\t{'✓' if r['persona'] else '-'}"
                 f"\t{'✓' if r['dynamics'] else '-'}\t{r['situations_n']}"
                 f"\t{'✓' if r['style'] else '-'}")
    if st:
        L += ["", f"自動アップデート状況: {st}"]
    return Response("\n".join(L), media_type="text/plain; charset=utf-8")


def _backfill215(self_name):
    """確定済み顧客のうち、txtは取り込み済みなのに分析が欠けている相手を埋める。
    v187検疫は尊重(未確定カードには触らない=仕分けが先)。抽出事実は通常どおり
    pending/自動適用の関門を通る(v164/v187: 重要項目は本人○✕)。"""
    from . import linebot, crm, situations, dynamics
    rows = _card_audit_rows()
    # v216: 「欠けがある相手」だけを対象にする(欠けゼロの相手に3秒sleepしない・
    # 100人上限で101人目以降が永久に回らない問題の解消=絞ってから上限)
    def _lack(r):
        if (not r["yobina"] or sum(r["facts"].values()) == 0 or not r["dynamics"]
                or r["situations_n"] == 0 or (not r["persona"] and r["chars"] >= 3000)):
            return True
        # v218: 旧形式ペルソナ(「この人へのわたし」なし)も欠け扱い
        # v227: myselfキー自体が無い(=v221前の旧分析)相手だけ対象。空配列([]=分析済みで
        # 材料不足)は正しい結果なので何度も再分析しない
        if r["persona"] and r["chars"] >= 200:
            try:
                from . import linebot as _lb
                return (_lb.get_persona(r["contact"]) or {}).get("myself") is None
            except Exception:
                return False
        return False
    todo = [r for r in rows if r["kind"] == "customer" and r["confirmed"]
            and r["quarantine_held"] == 0 and _lack(r)]
    n_fact = n_yob = n_dyn = n_sit = n_pers = 0
    for i, r in enumerate(todo[:300]):
        code = r["contact"]
        try:
            with db.conn() as c:
                tr = c.execute("SELECT text FROM linebot_talks WHERE contact=?", (code,)).fetchone()
            text = (tr["text"] if tr else "") or ""
            if not text:
                continue
            # ①呼び名: 決定論抽出(v211・AIキー不要)。無ければpendingで○✕へ
            if not r["yobina"]:
                try:
                    _dy = linebot.extract_yobina_calls(text, self_name)
                    if _dy:
                        fs = linebot.curate_facts([{"k": "呼び名", "v": _dy["v"], "src": _dy["src"],
                                                    "conf": _dy["conf"], "alts": _dy.get("alts", [])}])
                        linebot.save_split(code, fs)
                        n_yob += 1
                except Exception as e:
                    print(f"[bf215 yobina] {code}: {e}", flush=True)
            # ②抽出事実ゼロ: 通常の抽出を今の版でやり直す(結果は通常の関門を通る)
            if sum(r["facts"].values()) == 0 and config.ANTHROPIC_API_KEY:
                try:
                    facts, err = linebot.extract_facts(text, code, self_name)
                    facts = linebot.curate_facts(facts or [])
                    facts = [f for f in facts if f.get("k") != "呼び名" or not r["yobina"]]
                    # v224: 自動アップデートは完全に追加専用 — 既に値が入っている属性キーには
                    # 一切書き込まない(手入力値がAI抽出で上書きされる経路を遮断・本人質問2026-08-13)
                    _cur = crm.get_attrs(code) or {}
                    facts = [f for f in facts if not (_cur.get(f.get("k")) or "").strip()]
                    if facts:
                        linebot.save_split(code, facts)
                        n_fact += 1
                except Exception as e:
                    print(f"[bf215 facts] {code}: {e}", flush=True)
            # ③後段分析の欠け埋め(取り込み時と同じ関数・同じ順)
            if not r["dynamics"]:
                try:
                    if dynamics.analyze_and_save(code, text, self_name):
                        n_dyn += 1
                except Exception as e:
                    print(f"[bf215 dyn] {code}: {e}", flush=True)
            if r["situations_n"] == 0:
                try:
                    situations.harvest_and_save(code, text, self_name)
                    n_sit += 1
                except Exception as e:
                    print(f"[bf215 sit] {code}: {e}", flush=True)
            if not r["persona"] and r["chars"] >= 3000:
                try:
                    linebot.maybe_auto_persona(code)
                    n_pers += 1
                except Exception as e:
                    print(f"[bf215 persona] {code}: {e}", flush=True)
            elif r["persona"] and r["chars"] >= 200:
                # v218: 旧形式ペルソナ(v212の「この人へのわたし」が無い)は再分析して付ける。
                # ○✕確定済みの項目はpersona_asyncのマージ(v118)で保持される
                # v227: 下限3000→200字(短い会話の相手が永久に旧形式のまま残っていた)
                try:
                    _p = linebot.get_persona(code) or {}
                    if _p.get("myself") is None:   # v227: 旧分析のみ(空配列=材料不足は再分析しない)
                        linebot.persona_async(code)
                        n_pers += 1
                except Exception as e:
                    print(f"[bf215 myself] {code}: {e}", flush=True)
            linebot._meta_set("backfill215",
                              f"実行中 {i + 1}/{len(todo)}人目({code})@{time.time()}")
            # API負荷を平す(dynamics backfillと同じ間隔)。テストは0秒に落とせる
            time.sleep(float(os.environ.get("CHOUBA_BF215_SLEEP", "3")))
        except Exception as e:
            print(f"[bf215] {code}: {e}", flush=True)
    import datetime as _dt
    _now = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=9))).strftime("%m/%d %H:%M")
    linebot._meta_set("backfill215",
                      f"完了 {_now} 対象{len(todo)}人: 呼び名+{n_yob} 事実+{n_fact} "
                      f"力学+{n_dyn} 実例+{n_sit} ペルソナ+{n_pers}")
    print(f"[bf215] 完了: {len(todo)}人 呼び名{n_yob} 事実{n_fact} 力学{n_dyn} "
          f"実例{n_sit} ペルソナ{n_pers}", flush=True)


@router.get("/api/liff/card_backfill")
def liff_card_backfill_confirm(request: Request, key: str = ""):
    """v215: 自動アップデートの確認ページ(規約: 破壊的/重いURLのGET直実行禁止→確認を挟む)。"""
    if not config.INGEST_TOKEN or key != config.INGEST_TOKEN:
        if not _authed(request):
            return _deny()
    rows = _card_audit_rows()
    cust = [r for r in rows if r["kind"] == "customer" and r["confirmed"]
            and r["quarantine_held"] == 0]
    miss = sum(1 for r in cust if not r["yobina"] or sum(r["facts"].values()) == 0
               or not r["persona"] or not r["dynamics"] or r["situations_n"] == 0)
    stuck = sum(1 for r in rows if r["quarantine_held"] > 0 or not r["confirmed"])
    html = f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>カード自動アップデート</title></head>
<body style="font-family:sans-serif;max-width:480px;margin:30px auto;padding:0 16px;line-height:1.8">
<h2>🔄 カードの自動アップデート</h2>
<p>取り込み済み {len(rows)}人のうち、分析が欠けている確定済み顧客 <b>{miss}人</b> を埋めます
(呼び名・抽出事実・関係分析・実例・ペルソナ)。重要項目はこれまで通りカードでの○✕確認を通ります。</p>
<p style="color:#8a5a00">仕分け待ち {stuck}人 はここでは触りません(LIFFの仕分け/⚡おまかせで確定すると自動反映されます)。</p>
<p style="font-size:13px;color:#666">AI分析を使うためAPI利用料がかかります。進み具合は card_audit で見られます。</p>
<form method="post" action="/api/liff/card_backfill">
<input type="hidden" name="key" value="{esc(key)}">
<button type="submit" style="padding:14px 22px;font-size:16px;font-weight:700">実行する</button></form>
<!-- v216: keyはこのページを開くのに使った値をそのまま返すだけ(Bearer閲覧時に運用トークンを開示しない) -->
</body></html>"""
    return Response(html, media_type="text/html; charset=utf-8")


@router.post("/api/liff/card_backfill")
async def liff_card_backfill_run(request: Request):
    try:
        form = await request.form()
        key = form.get("key") or ""
    except Exception:
        key = ""
    if not ((config.INGEST_TOKEN and key == config.INGEST_TOKEN) or _authed(request)):
        return _deny()   # v216: GET側と同じ判定(key or 認証。トークン未設定でもBearerで通る)
    from . import linebot
    st = linebot._meta_get("backfill215") or ""
    # v216: 「実行中」の永久ロック対策 — 開始時刻を持ち、30分を過ぎた実行中表記は
    # 死んだスレッドの残骸とみなして再実行を許す(再デプロイ・例外死からの復旧口)
    if st.startswith("実行中"):
        try:
            _t0 = float((st.split("@") + ["0"])[1])
        except Exception:
            _t0 = 0
        if time.time() - _t0 < 1800:
            return Response("すでに実行中です。card_audit で進み具合を確認してください。",
                            media_type="text/plain; charset=utf-8")
    self_name = (db.get_profile("_selfname") or {}).get("name") or "自分"
    linebot._meta_set("backfill215", f"実行中 0人目@{time.time()}")
    threading.Thread(target=_backfill215, args=(self_name,), daemon=True).start()
    db.track("liff_card_backfill")
    return Response("🔄 自動アップデートを開始しました(裏で実行)。進み具合は card_audit で見られます。",
                    media_type="text/plain; charset=utf-8")


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
    if action not in ("del", "fix", "summary", "tolok", "tolng", "tolfix", "toldel",
                      "myfix", "mydel", "myok", "myng"):   # v230: 🪞の○✕
        return JSONResponse({"error": "bad action"}, status_code=400)
    p = linebot.edit_persona(code, action, index, value)
    if p is None:
        return JSONResponse({"error": "ペルソナがありません"}, status_code=404)
    _tune_bust()   # v235: ○✕でホームの学び件数が変わる
    db.track("liff_persona_edit")
    return {"ok": True, "persona": p}


# ============ 🎛 返信の調整(v232) ============
# ホーム⚙️配下のハブ。①あたらしい学び=相手横断の未確認🚦・🪞を順に○✕
# ②じぶんの方針リンク ③配信の個別化つまみの既定値 ④効き方の1行例。
# 位置づけ(本人裁定2026-08-13): 確認は宿題ではない — 既定ONですでに効いており、
# ここは「気が向いたら目を通して✕で止める」場所。文言・色もその前提(金・赤禁止)。

def _tune_undecided(it):
    """未確認か。v235(監査指摘・重大): analyze_personaは tolerance を "ok": None 付きで
    作るため、v232の `"ok" in it` (キー存在判定)では🚦が1件も🎛に出ていなかった。
    クライアント側は t.ok == null を未確認として扱っており、そちらが正。"""
    return it.get("ok") not in (0, 1)


def _tune_count():
    """あたらしい学びの件数。ホームが毎回叩くのでメタにキャッシュ(v235)。
    明示bustに加えて120秒で失効させる(再分析など、bustを置き忘れた経路でも
    表示が永久にズレない。ズレても数字だけで、押せる中身は/api/liff/tuneが正)。"""
    from . import linebot
    cached = linebot._meta_get("tune_n_cache")
    if cached:
        try:
            n, ts = cached.split("@")
            if time.time() - float(ts) < 120:
                return int(n)
        except Exception:
            pass
    n = len(_tune_items())
    linebot._meta_set("tune_n_cache", f"{n}@{time.time()}")
    return n


def _tune_bust():
    """学びの○✕・再分析・一括確定のあとにキャッシュを捨てる。"""
    from . import linebot
    try:
        linebot._meta_set("tune_n_cache", "")
    except Exception:
        pass


def _tune_items():
    """未確認(ok未設定)の🚦tolerance・🪞myselfを相手横断で全件列挙。新しい分析から順。
    v233: capはここでは掛けない(バッジ=総数。30件で切るとv232の「30個確定→また30個」問題)。"""
    import json as _json
    from . import linebot
    linebot.ensure()
    alive = {x["code"] for x in db.list_contacts()}
    out = []
    with db.conn() as c:
        rows = c.execute(
            "SELECT contact, data FROM linebot_persona ORDER BY ts DESC").fetchall()
    for r in rows:
        code = r["contact"]
        if code not in alive:
            continue
        try:
            p = _json.loads(r["data"])
        except Exception:
            continue
        for kind, arr in (("tol", p.get("tolerance") or []), ("my", p.get("myself") or [])):
            for i, it in enumerate(arr):
                if not _tune_undecided(it):   # ○✕確定済みは出さない(未確認だけ)
                    continue
                if not ((it.get("k") or "").strip() and (it.get("v") or "").strip()):
                    continue
                out.append({"code": code, "kind": kind, "index": i,
                            "k": it.get("k") or "", "v": it.get("v") or "",
                            "src": (it.get("src") or "")[:60], "conf": it.get("conf") or ""})
    return out


def _tune_example():
    """④効き方の1行例の材料(決定論・AI呼び出し無し)。直近やり取りのある顧客から
    呼び名と鮮度のある話題語を1つ。無ければ topic=None(クライアントが汎用文に落とす)。"""
    from . import linebot, crm, campaign
    with db.conn() as c:
        rows = c.execute(
            "SELECT m.contact AS code FROM messages m JOIN contacts ct ON ct.code=m.contact "
            "WHERE IFNULL(ct.kind,'customer')='customer' "
            "GROUP BY m.contact ORDER BY MAX(m.ts) DESC LIMIT 10").fetchall()
    for r in rows:
        code = r["code"]
        try:
            keys = campaign._fresh_topic_keys(code)
        except Exception:
            keys = set()
        topic = None
        if keys:
            a = crm.get_attrs(code) or {}
            corpus = "".join(tx for _, _, tx in campaign._recent_corpus(code))
            for k in sorted(keys):
                toks = [t for t in re.split(r"[\s、。・,/()（）「」]+", (a.get(k) or ""))
                        if len(t) >= 2 and t in corpus]
                if toks:
                    topic = toks[0][:12]
                    break
        if topic:
            return {"yobina": linebot._yobina(code), "topic": topic}
    if rows:   # 話題が拾えなくても呼び名だけは実物で
        return {"yobina": linebot._yobina(rows[0]["code"]), "topic": None}
    return None


@router.get("/api/liff/tune")
def liff_tune(request: Request):
    if not _authed(request):
        return _deny()
    from . import linebot
    try:
        pl = int(linebot._meta_get("ann_plevel_default") or 1)
    except Exception:
        pl = 1
    try:
        ex = _tune_example()
    except Exception:
        ex = None
    try:
        allitems = _tune_items()
    except Exception as e:      # v235: ペルソナ1行の破損で🎛ごと500にしない
        print(f"[tune] 一覧の組み立て失敗: {e}", flush=True)
        allitems = []
    # v233: total=総数(バッジ用)・itemsは30件ずつ。使い切ったらクライアントが再取得して継ぎ足す
    return {"ok": True, "items": allitems[:30], "total": len(allitems),
            "plevel_default": max(0, min(2, pl)), "example": ex}


# ============ 💾 バックアップ(v235) ============
# HTMLに値を差し込む前の始末。owner向けページでもトークンや名前を生で埋めない
# (v233の指摘: 確認ページのkeyがf-string生埋めだった)。


def esc(s):
    import html as _html
    return _html.escape(str(s or ""), quote=True)


def _q(s):
    from urllib.parse import quote as _quote
    return _quote(str(s or ""), safe="")


# 既知の最重大課題「バックアップゼロ」への対応。三層(自動世代/手元ダウンロード/復元)。
# 詳細な設計意図は app/backup.py の冒頭に書いた。ここはその口。

def _bk_authed(request: Request, key: str) -> bool:
    if config.INGEST_TOKEN and key == config.INGEST_TOKEN:
        return True
    return _authed(request)


def _mb(n):
    return f"{n / 1024 / 1024:.1f} MB" if n else "0 MB"


@router.get("/api/liff/backup_info")
def liff_backup_info(request: Request):
    """LIFF画面用。数字と、ダウンロード用の短命チケット(5分・1回)を返す。

    ダウンロードは <a href> の素の遷移なので Authorization ヘッダが載らない。
    LIFF側は運用トークンを持たないため、認証済みのこのAPIでチケットを切って渡す。"""
    from . import backup as bk
    if not _authed(request):
        return _deny()
    st = bk.status()
    with db.conn() as c:
        n_c = c.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        n_m = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        n_t = c.execute("SELECT COUNT(*) FROM linebot_talks").fetchone()[0]
    return {"ok": True, "contacts": n_c, "messages": n_m, "talks": n_t,
            "db_mb": _mb(st["db_bytes"]), "gen_n": st["gen_n"], "keep": bk.GENERATIONS,
            "download_age_days": st["download_age_days"],
            "persistence": st["persistence"], "persistence_note": st["persistence_note"],
            "ticket": _bk_ticket()}


def _bk_ticket():
    """5分・1回きりのダウンロード用チケット。"""
    import secrets
    from . import linebot
    linebot.ensure()
    t = secrets.token_urlsafe(12)
    linebot._meta_set("backup_tkt", f"{t}|{time.time() + 300}")
    return t


def _bk_ticket_ok(t):
    from . import linebot
    if not t:
        return False
    raw = linebot._meta_get("backup_tkt") or ""
    try:
        tok, exp = raw.split("|")
    except Exception:
        return False
    if not hmac.compare_digest(tok, t) or float(exp) < time.time():
        return False
    linebot._meta_set("backup_tkt", "")   # 1回きり(URLが履歴に残っても再利用できない)
    return True


@router.get("/api/liff/backup")
def liff_backup(request: Request, key: str = "", dl: int = 0, gen: str = "", t: str = ""):
    """情報ページ(既定)とダウンロード(dl=1)。読み取りのみなのでGETで完結してよい。"""
    from . import backup as bk
    if not (_bk_authed(request, key) or (dl and _bk_ticket_ok(t))):
        return _deny()
    if dl:
        import re as _re
        d = bk.backup_dir()
        if gen:
            # 世代の取り出し。パス片(/ . )を弾いて backups/ の外へ出られないようにする
            if not _re.fullmatch(r"[A-Za-z0-9_]+\.db", gen):
                return JSONResponse({"error": "bad gen"}, status_code=400)
            path = os.path.join(d, gen)
            if not os.path.exists(path):
                return JSONResponse({"error": "not found"}, status_code=404)
            fname = gen
        else:
            # 「いま」の整合スナップショットをその場で作って渡す(ファイルcpは壊れうる)
            stamp = time.strftime("%Y%m%d_%H%M", time.gmtime(time.time() + 9 * 3600))
            fname = f"chouba_backup_{stamp}.db"
            path = os.path.join(d, "_download.db")
            try:
                bk.snapshot(path)
            except Exception as e:
                return JSONResponse({"error": f"スナップショットを作れませんでした: {e}"},
                                    status_code=500)
            bk.mark_download()
        db.track("liff_backup_dl")
        return FileResponse(path, media_type="application/octet-stream", filename=fname)
    st = bk.status()
    with db.conn() as c:
        n_c = c.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        n_m = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        n_t = c.execute("SELECT COUNT(*) FROM linebot_talks").fetchone()[0]
    warn = ""
    if st["persistence"] == "ephemeral":
        warn = (f'<p style="background:#F9ECEA;border:1px solid #C0402C;border-radius:8px;'
                f'padding:10px 12px"><b>⚠️ 置き場所の警告</b><br>{esc(st["persistence_note"])}</p>')
    elif st["persistence"] == "unknown":
        warn = (f'<p style="background:#FBF3DC;border:1px solid #E3C98A;border-radius:8px;'
                f'padding:10px 12px">{esc(st["persistence_note"])}</p>')
    age = st["download_age_days"]
    agel = ("まだ一度も取っていません" if age is None
            else ("今日取りました" if age == 0 else f"{age}日前"))
    gens = "".join(
        f'<li>{esc(g["name"])} — {_mb(g["bytes"])} '
        f'<a href="/api/liff/backup?key={_q(key)}&dl=1&gen={_q(g["name"])}">取り出す</a></li>'
        for g in st["generations"])
    html = f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>バックアップ</title></head>
<body style="font-family:sans-serif;max-width:520px;margin:24px auto;padding:0 16px;line-height:1.8">
<h2>💾 バックアップ</h2>
{warn}
<p><b>いまの中身</b>: お客様 {n_c}人・受信 {n_m}通・取り込んだトーク {n_t}人分({_mb(st['db_bytes'])})</p>
<p><b>手元に取った最後</b>: {agel}</p>
<p style="font-size:13px;color:#666">下のボタンで、いまこの瞬間の状態を1つのファイルに固めて
ダウンロードします。iPhoneなら「"ファイル"に保存」でiCloudに置いておけば、
サーバーが消えても戻せます。<b>週に1回</b>を目安にどうぞ。</p>
<p><a href="/api/liff/backup?key={_q(key)}&dl=1"
   style="display:inline-block;background:#1B2A4A;color:#fff;text-decoration:none;
   padding:14px 22px;border-radius:10px;font-weight:700">⬇️ いまの状態をダウンロード</a></p>
<h3 style="font-size:15px;margin-top:26px">自動で残している世代({st['gen_n']}件)</h3>
<p style="font-size:13px;color:#666">サーバーの中に1日1回・{bk.GENERATIONS}日分。
これは操作ミスから戻すための控えで、サーバーごと消える事故には無力です。</p>
<ul style="font-size:13px">{gens or "<li>まだありません</li>"}</ul>
<p style="font-size:13px;margin-top:24px"><a href="/api/liff/restore?key={_q(key)}">
🔁 バックアップから復元する</a>(取り消せない操作です)</p>
</body></html>"""
    db.track("liff_backup_page")
    return Response(html, media_type="text/html; charset=utf-8")


@router.get("/api/liff/restore")
def liff_restore_page(request: Request, key: str = ""):
    """復元の1段目(規約: 破壊的URLのGET直実行禁止・取り消し不可はタップ2段階)。"""
    from . import backup as bk
    if not _bk_authed(request, key):
        return _deny()
    st = bk.status()
    with db.conn() as c:
        n_c = c.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        n_m = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    html = f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>復元</title></head>
<body style="font-family:sans-serif;max-width:520px;margin:24px auto;padding:0 16px;line-height:1.8">
<h2>🔁 バックアップから復元</h2>
<p style="background:#F9ECEA;border:1px solid #C0402C;border-radius:8px;padding:10px 12px">
<b>いまのデータ(お客様 {n_c}人・受信 {n_m}通)は、選んだファイルの中身に置き換わります。</b><br>
置き換える直前の状態はサーバー内に <code>pre_restore_…</code> として残すので、
間違えた時はもう一度この画面から戻せます。</p>
<form method="post" action="/api/liff/restore" enctype="multipart/form-data">
<input type="hidden" name="key" value="{esc(key)}">
<p><input type="file" name="file" accept=".db,application/octet-stream" required></p>
<p><label><input type="checkbox" name="confirm" value="RESTORE" required>
いまのデータが置き換わることを理解しました</label></p>
<button type="submit" style="padding:14px 22px;font-size:16px;font-weight:700;
  background:#C0402C;color:#fff;border:none;border-radius:10px">復元する</button>
</form>
<p style="font-size:13px;color:#666;margin-top:20px">復元後は Render の画面で
一度サービスを再起動(Restart)してください。動いているプロセスが古い中身を
覚えている場合があります。</p>
<p style="font-size:13px">サーバー内の世代から戻す場合は、先に
<a href="/api/liff/backup?key={_q(key)}">バックアップの画面</a>で世代を取り出し、
そのファイルをここで選んでください。</p>
</body></html>"""
    return Response(html, media_type="text/html; charset=utf-8")


@router.post("/api/liff/restore")
async def liff_restore_run(request: Request, file: UploadFile = File(None),
                           key: str = "", confirm: str = ""):
    """復元の2段目。検証→復元前退避→置き換え。"""
    from . import backup as bk
    try:
        form = await request.form()
        key = str(form.get("key") or key or "")
        confirm = str(form.get("confirm") or confirm or "")
        file = form.get("file") or file
    except Exception:
        pass
    if not _bk_authed(request, key):
        return _deny()
    if confirm != "RESTORE" or file is None or not getattr(file, "filename", ""):
        return JSONResponse({"error": "確認またはファイルがありません"}, status_code=400)
    tmp = os.path.join(bk.backup_dir(), "_upload.db")
    try:
        with open(tmp, "wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
    except Exception as e:
        return JSONResponse({"error": f"受け取れませんでした: {e}"}, status_code=400)
    ok, why, info = bk.restore_validate(tmp)
    if not ok:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        html = (f"<!DOCTYPE html><html lang='ja'><head><meta charset='UTF-8'></head>"
                f"<body style='font-family:sans-serif;max-width:520px;margin:24px auto;padding:0 16px'>"
                f"<h2>✕ 復元しませんでした</h2><p>{esc(why)}</p>"
                f"<p><a href='/api/liff/restore?key={_q(key)}'>戻る</a></p></body></html>")
        return Response(html, media_type="text/html; charset=utf-8", status_code=400)
    bak = bk.restore(tmp)
    try:
        os.unlink(tmp)
    except Exception:
        pass
    db.track("liff_restore")
    html = f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>復元 完了</title></head>
<body style="font-family:sans-serif;max-width:520px;margin:24px auto;padding:0 16px;line-height:1.8">
<h2>✓ 復元しました</h2>
<p>お客様 {info.get('contacts', '-')}人・受信 {info.get('messages', '-')}通・
取り込んだトーク {info.get('talks', '-')}人分の状態に戻しました。</p>
{f"<p style='font-size:13px;color:#666'>置き換える前の状態は <code>{esc(os.path.basename(bak))}</code> として残しています。</p>" if bak else ""}
<p style="background:#FBF3DC;border:1px solid #E3C98A;border-radius:8px;padding:10px 12px">
<b>Render の画面でサービスを一度 Restart してください。</b>
動いているプロセスが古い中身を覚えている場合があります。</p>
</body></html>"""
    return Response(html, media_type="text/html; charset=utf-8")


@router.get("/api/liff/tune_ackall")
def liff_tune_ackall_confirm(request: Request, key: str = ""):
    """v233: 既存分の学びを一括○にする確認ページ(本人裁定2026-08-13「すでにアップされて
    いるtxtでは全てOKにしておいて。モニターに100回以上タップは厳しい。新しいtxt読み込み時
    から選別で十分」)。規約: 破壊的URLのGET直実行禁止→確認を挟む。"""
    if not config.INGEST_TOKEN or key != config.INGEST_TOKEN:
        if not _authed(request):
            return _deny()
    items = _tune_items()
    n_codes = len({x["code"] for x in items})
    html = f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>あたらしい学びの一括確定</title></head>
<body style="font-family:sans-serif;max-width:480px;margin:30px auto;padding:0 16px;line-height:1.8">
<h2>🎛 あたらしい学びの一括確定</h2>
<p>未確認の学び <b>{len(items)}件</b>({n_codes}人)を、すべて「○ この通り」として確定します。</p>
<p style="font-size:13px;color:#666">確定後も、各カードの🚦🪞からいつでも個別に「✕」で止められます。
これ以降に取り込むtxtの学びは、通常どおり🎛返信の調整に並びます。</p>
<form method="post" action="/api/liff/tune_ackall">
<input type="hidden" name="key" value="{esc(key)}">
<button type="submit" style="padding:14px 22px;font-size:16px;font-weight:700">一括で○にする</button></form>
</body></html>"""
    return Response(html, media_type="text/html; charset=utf-8")


@router.post("/api/liff/tune_ackall")
async def liff_tune_ackall_run(request: Request):
    """v233: 未確認の🚦tolerance・🪞myself全件に ok=1 を付ける(完全追加専用ではないが
    値は書き換えない=okフラグの付与のみ)。"""
    import json as _json
    try:
        form = dict(await request.form())
    except Exception:
        form = {}
    key = str(form.get("key") or "")
    if not config.INGEST_TOKEN or key != config.INGEST_TOKEN:
        if not _authed(request):
            return _deny()
    from . import linebot
    linebot.ensure()
    alive = {x["code"] for x in db.list_contacts()}
    n_items = 0
    n_fail = 0
    codes = set()
    with db.conn() as c:
        rows = c.execute("SELECT contact, data FROM linebot_persona").fetchall()
    for r in rows:
        code = r["contact"]
        if code not in alive:
            continue
        try:
            p = _json.loads(r["data"])
        except Exception:
            continue
        changed = False
        for arr in (p.get("tolerance") or [], p.get("myself") or []):
            for it in arr:
                if _tune_undecided(it) and (it.get("k") or "").strip() and (it.get("v") or "").strip():
                    it["ok"] = 1
                    n_items += 1
                    changed = True
        if changed:
            try:
                linebot.save_persona(code, p)
                codes.add(code)
            except Exception as e:   # v235: 1件の失敗で以降を止めない(冪等なので再実行可)
                n_fail += 1
                print(f"[tune_ackall] 保存失敗 {code}: {e}", flush=True)
    _tune_bust()
    db.track("liff_tune_ackall")
    html = f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>一括確定 完了</title></head>
<body style="font-family:sans-serif;max-width:480px;margin:30px auto;padding:0 16px;line-height:1.8">
<h2>✓ 一括確定しました</h2>
<p><b>{n_items}件</b>({len(codes)}人)を「○ この通り」にしました。下書き生成に効きます。</p>
{f"<p style='color:#C0402C'>{n_fail}人分は保存できませんでした。この画面をもう一度開いて実行すると、残りだけを処理します。</p>" if n_fail else ""}
<p style="font-size:13px;color:#666">違うものはカードの🚦🪞から個別に「✕」で止められます。</p>
</body></html>"""
    return Response(html, media_type="text/html; charset=utf-8")


@router.post("/api/liff/tune/plevel")
async def liff_tune_plevel(request: Request):
    """v232: 配信の個別化つまみの既定値。楽観更新禁止(規約) — 保存成功後に点灯。"""
    if not _authed(request):
        return _deny()
    from . import linebot
    try:
        pl = int((await request.json()).get("plevel", 1))
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    pl = max(0, min(2, pl))
    linebot._meta_set("ann_plevel_default", str(pl))
    db.track("liff_tune_plevel")
    return {"ok": True, "plevel_default": pl}


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
    # v234(本人報告「ヘルプの時タップできる人が一切出てこない」): 店内分類が0人だと
    # チップが無言で空になる。過去のお席で手入力したヘルプ名も候補として再利用する
    try:
        seen = {s_["code"] for s_ in staff} | {s_["name"] for s_ in staff}
        with db.conn() as c:
            past = [r["contact"] for r in c.execute(
                "SELECT m.contact, MAX(m.sitting_id) t FROM sitting_members m "
                "WHERE m.role='help' GROUP BY m.contact ORDER BY t DESC LIMIT 20")]
        for nm_ in past:
            if len(staff) >= 14:
                break
            if nm_ and nm_ not in seen:
                staff.append({"code": nm_, "name": nm_})
                seen.add(nm_)
    except Exception as e:
        print(f"[orei prefill helpers] {e}", flush=True)
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


def _jobs_gc():
    """v236(S8): 取り込みジョブと、それに紐づく原文メタ(最大40万字)の掃除。

    放置された ambiguous/confirm 待ちが増え続け、原文メタごとDBを膨らませていた。
    完了・失敗は30日、放置(ambiguous/confirm待ち)は90日で切る。90日も待って
    確定されないものは、もう一度txtを送ってもらうほうが早い。
    """
    now = time.time()
    try:
        with db.conn() as c:
            done_cut = now - 30 * 86400
            stale_cut = now - 90 * 86400
            old = [r["id"] for r in c.execute(
                "SELECT id FROM liff_import_jobs WHERE "
                "(status IN ('done','error','dismissed') AND ts < ?) OR ts < ?",
                (done_cut, stale_cut))]
            for jid in old:
                c.execute("DELETE FROM linebot_meta WHERE k=?", (f"liffimp_{jid}",))
            if old:
                c.execute("DELETE FROM liff_import_jobs WHERE id IN (%s)"
                          % ",".join("?" * len(old)), old)
                print(f"[jobs gc] 古い取り込みジョブ {len(old)}件と原文を削除", flush=True)
            # 親ジョブごと消えた原文メタの取り残し(過去の削除経路の漏れ)も拾う
            alive = {str(r["id"]) for r in c.execute("SELECT id FROM liff_import_jobs")}
            orphan = [r["k"] for r in c.execute(
                "SELECT k FROM linebot_meta WHERE k LIKE 'liffimp_%'")
                if r["k"].split("_", 1)[1] not in alive]
            for k in orphan:
                c.execute("DELETE FROM linebot_meta WHERE k=?", (k,))
            if orphan:
                print(f"[jobs gc] 親のない原文メタ {len(orphan)}件を削除", flush=True)
    except Exception as e:
        print(f"[jobs gc] {e}", flush=True)


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
        # v236(指示書採用①): 確定済みの非顧客(店内・同業・私用)は、下の§11検疫で
        # どのみち抽出結果を丸ごと捨てている。にもかかわらず抽出LLM(最大4チャンク)と
        # 種別判定LLMを先に走らせていた=会話全文が外部APIへ行き、課金も発生していた。
        # 判定材料(rel_confirmed・kind)は呼び出し前に揃っているので前倒しする。
        _confirmed = (not was_new) and linebot.rel_confirmed(contact)
        _kind_now = (db.get_contact(contact) or {}).get("kind") or "customer"
        _skip_llm = bool(_confirmed and _kind_now != "customer")
        if _skip_llm:
            print(f"[skip extract] {contact}: 確定済み非顧客({_kind_now}) → 抽出LLMを呼ばない",
                  flush=True)
            facts, err = [], None
        else:
            facts, err = linebot.extract_facts(text, contact, self_name)
        # v211: 呼び名は決定論抽出を第一候補に(敬称込み・根拠つき)。LLMの呼び名は降格(重複排除)。
        # AIキーが無い環境でも呼び名だけは取れる=取り込みが空振りしない
        try:
            _dy = linebot.extract_yobina_calls(text, self_name)
        except Exception as _e:
            print(f"[yobina det] {_e}", flush=True)
            _dy = None
        if _dy:
            from . import crm as _crmy
            if (_crmy.get_attrs(contact) or {}).get("呼び名") == _dy["v"]:
                _dy = None   # 既に同じ呼び名が確定済み=聞き直さない
        if _dy:
            facts = [f for f in (facts or []) if f.get("k") != "呼び名"]
            facts = [{"k": "呼び名", "v": _dy["v"], "src": _dy["src"],
                      "conf": _dy["conf"], "alts": _dy.get("alts", [])}] + facts
            err = None if not facts[1:] and err else err
        if not _skip_llm:      # v236: 種別が確定済みの非顧客に種別判定を掛け直さない
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
        if _skip_llm:
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
        linebot._meta_set(f"liffimp_{jid}", text[-400000:])   # v150: 末尾=最新を保持(v218 S4: 20万→40万字。linebot_talksの統合上限と揃える)
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



def _tmark(ts) -> str:
    """v190: 受信時刻の前置ラベル。同夜=〔HH:MM〕/1日前=〔きのう〕/それ以前=〔N日前〕。
    v191その2(#16): 「同じ夜」の境界を仕様(B-4/B-5)と同じ朝5時JSTに統一。JST0時基準だと
    深夜0:30に今夜の受信へ〔きのう〕が付き、カーソル失効・裁定履歴の夜境界とズレていた。"""
    try:
        ts = float(ts or 0)
        if not ts:
            return ""
        now = time.time()
        d_now = int((now + 9 * 3600 - 5 * 3600) // 86400)
        d_msg = int((ts + 9 * 3600 - 5 * 3600) // 86400)
        if d_now == d_msg:
            return time.strftime("〔%H:%M〕", time.gmtime(ts + 9 * 3600))
        n = d_now - d_msg
        return "〔きのう〕" if n == 1 else f"〔{n}日前〕"
    except Exception:
        return ""


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
        _grp_total = 0
        if opens or defs:
            allm0 = sorted(opens + defs, key=lambda m: m.get("ts") or 0)
            # v192: グループ発言は「自分宛てだけ」を会話記録に出す(本人裁定 2026-08-11)。
            # 隠したぶんは grp_total で件数だけ知らせる(黙って消したと誤解させない)
            allm = [m for m in allm0
                    if linebot.group_visible(m.get("text"), it["contact"])]
            if not allm:
                allm = allm0[-1:]   # 全滅時は最新1通だけ残す(空カード防止の保険)
            if len(allm) < len(allm0):
                _grp_total = len(allm0)
            defs = [m for m in defs if any(m["id"] == x["id"] for x in allm)]
            mids = [m["id"] for m in allm]
            full = "\n".join(f"{_tmark(m.get('ts'))}{(m.get('text') or '').replace('【?', '【', 1)}"
                             for m in allm)   # v199: 疑い印【?…】の?は表示で落とす
        else:
            mids = it.get("mids") or [it["mid"]]
            full = (db.get_message(it["mid"]) or {}).get("text") or it.get("text") or ""
        it["_deferred_n"] = len(defs)
        it["_grp_total"] = _grp_total
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
                    "kind": it.get("kind") or "customer",   # v189: 種別バッジ用
                    "rank": it.get("rank") or "B", "urgent": bool(it.get("urgent")),
                    "unlinked": bool(it.get("unlinked")), "reason": it.get("reason") or "",
                    "ts": (max(m.get("ts") or 0 for m in (opens + defs)) if (opens or defs) else it.get("ts")),
                    "text": full[:600],
                    "mids": mids, "count": len(mids),
                    "sname": _a.get("LINE検索名") or "",
                    "sword": _a.get("LINE検索確定語") or "",   # v176: 学習済み検索語(コピー優先)
                    "cands": cands,
                    "deferred": int(it.get("_deferred_n") or 0),  # v177: ↷あとで分の件数
                    "pin": int(it.get("pin") or 0),   # v192: 🔥ピン留め
                    "grp_total": int(it.get("_grp_total") or 0),   # v192: グループ非表示前の総通数(>0=間引きあり)
                    # v186(P0): 送信前ガード用(内部語は画面に出さない。koi=発火条件、ok=本人が○済みのID)
                    "koi": int(it.get("koi") or 0),
                    "koi_ok": (koi_guard.ok_ids(it["contact"]) if it.get("koi") else []),
                    # v189: グループ発言(【グループ名】印)。UIで「全部があなた宛てとは限らない」注記
                    "grp": 1 if any((m.get("text") or "").lstrip().startswith("【")
                                    for m in (opens + defs)) else 0,
                    "truncated": len(full) > 600})
    # v177: deferredのみの相手(openゼロ)は末尾に「まとめ箱」カードとして追加
    try:
        from . import crm as _crm2
        for ct, msgs in _def_by_contact.items():
            if not ct:
                continue
            c = db.get_contact(ct) or {}
            # v192: グループ発言は「自分宛てだけ」(全滅したらカード自体を出さない)
            _n0 = len(msgs)
            msgs = [m for m in msgs if linebot.group_visible(m.get("text"), ct)]
            if not msgs:
                continue
            _gt = _n0 if len(msgs) < _n0 else 0
            # v189: staff除外をやめる(本流キューと同じく店内の↷あとで分も見えるように)
            mids = [m["id"] for m in msgs]
            full = "\n".join(f"{_tmark(m.get('ts'))}{(m.get('text') or '').replace('【?', '【', 1)}"
                             for m in msgs)   # v199: 疑い印の?は表示で落とす
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
                        "kind": c.get("kind") or "customer",   # v190(#10): 属性補修
                        "pin": int(c.get("flag_hot") or 0),   # v192: 🔥ピン留め
                        "grp_total": _gt,   # v192: グループ間引きあり(>0)
                        "grp": 1 if any((m.get("text") or "").lstrip().startswith("【")
                                        for m in msgs) else 0,
                        # v191その2(#11): 本流キュー(build_queue)と同じくcustomer限定(v187§10)。
                        # koi客を店内へ統合→↷あとで、で非客カードに客UI(koiガード)が誤爆していた
                        "koi": (int(c.get("flag_koi") or 0)
                                if (c.get("kind") or "customer") == "customer" else 0),
                        "koi_ok": (koi_guard.ok_ids(ct)
                                   if (c.get("flag_koi")
                                       and (c.get("kind") or "customer") == "customer") else []),
                        "truncated": len(full) > 600})
    except Exception as e:
        print(f"[inbox deferred cards] {e}", flush=True)
    # v186(P0): 送信前ガードのパターン(koiの相手が1人でもいる時だけ同梱)
    try:
        kp = koi_guard.patterns() if any(x.get("koi") for x in out) else []
    except Exception:
        kp = []
    # v190(#19): 今夜の裁定履歴(同夜=直近の朝5時JST以降・上限30・act_id付き)。undo済みは除く
    recent = []
    acted_n = 0   # v191その2(#20): 完走画面「今夜N人」用(LIMITなし・同名別人も正しく別計上)
    try:
        now = time.time()
        jst = now + 9 * 3600
        day0 = (jst // 86400) * 86400
        night_start = (day0 + 5 * 3600) - 9 * 3600          # きょうのJST5:00(epoch)
        if jst % 86400 < 5 * 3600:
            night_start -= 86400                             # まだ朝5時前なら昨日の5:00から
        with db.conn() as c:
            for r in c.execute("SELECT act_id, contact, action, sent_reply_id, acted_ts "
                               "FROM acted_log WHERE undone=0 AND acted_ts>=? "
                               "ORDER BY act_id DESC LIMIT 30", (night_start,)):
                recent.append({"act_id": r["act_id"],
                               "name": linebot._yobina(r["contact"]),
                               "action": r["action"],
                               "sent": bool(r["sent_reply_id"]),
                               "tm": time.strftime("%H:%M", time.gmtime(r["acted_ts"] + 9 * 3600))})
            # v191その2(#20): 履歴のLIMIT 30(仕様B-5)とは別に、完走画面の人数は正確な
            # COUNT(DISTINCT contact)で返す(30人打ち止め・呼び名Set合算の両方を解消)
            acted_n = c.execute("SELECT COUNT(DISTINCT contact) FROM acted_log "
                                "WHERE undone=0 AND acted_ts>=?", (night_start,)).fetchone()[0] or 0
    except Exception as e:
        print(f"[recent acted] {e}", flush=True)
    return {"ok": True, "items": out, "koi_patterns": kp, "recent_acted": recent,
            "acted_n": acted_n}


@router.post("/api/liff/hotpin")
async def liff_hotpin(request: Request):
    """v192: 🔥ピン留めトグル。ピン留めした相手は内容を問わず「いま返す」区画に出る。"""
    if not _authed(request):
        return _deny()
    from . import crm
    try:
        body = await request.json()
        code = (body.get("code") or "").strip()
        on = 1 if body.get("on") else 0
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    if not code:
        return JSONResponse({"error": "no code"}, status_code=400)
    try:
        crm.update_contact(code, {"flag_hot": on})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    db.track("liff_hotpin")
    return {"ok": True, "pin": on}


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
    # v236(浅賀ガード): 💘ONだが恋情語がほぼ本人発の相手に、中立の一言を返す。
    # 相手を評さず、設定の見直し先だけ示す(覗き見されても内部語が出ない文にする)
    _koi_note = ""
    try:
        _ct = db.get_contact(m.get("contact") or "") or {}
        if int(_ct.get("flag_koi") or 0) and (_ct.get("kind") or "customer") == "customer":
            from . import dynamics as _dyn3
            _dom, _cnt = _dyn3.koi_self_dominant(m.get("contact") or "")
            if _dom:
                _koi_note = "この相手は💘の設定が入っていますが、過去のやり取りでは相手からその手の言葉はあまり出ていません。控えめの下書きにしています(カードで設定を見直せます)"
    except Exception as e:
        print(f"[koi note] {e}", flush=True)
    return {"ok": True, "drafts": [{"text": g.get("text", "")} for g in gen if g.get("text")][:3],
            "card_keys": crm.card_used_keys(m.get("contact") or ""),
            "koi_note": _koi_note,
            "gen_note": drafts.last_err(mid) or (
                # v191その2(一般A2): 一般モードに「お店の担当さん」を出さない
                ("いま自動の下書きがお休み中(設定待ち)。下の文は定型です — サポート担当に『帳場くんのAI設定』と伝えてください"
                 if config.MODE == "general" else
                 "いま自動の下書きがお休み中(設定待ち)。下の文は定型です — お店の担当さんに『帳場くんのAI設定』と伝えてください")
                if not config.ANTHROPIC_API_KEY else "")}   # v150: 技術用語を出さない(詳細はログ)


@router.post("/api/liff/track")
async def liff_track(request: Request):
    """v204: 機能イベントの記録(名前と時刻のみ・本文なし)。許可リスト外は無視。
    用途: 返信のコピー操作を客観計測し、送信記録(sent_replies)との差分で
    「✓押し忘れ」をダッシュボードに見えるようにする。"""
    if not _authed(request):
        return _deny()
    try:
        body = await request.json()
        ev = str(body.get("ev") or "")
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    if ev not in ("copy_send",):
        return JSONResponse({"error": "unknown ev"}, status_code=400)
    db.track("liff_" + ev)
    return {"ok": True}


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
    # v240: リハーサル機能はUIごと撤去したが、この番人だけは残す。
    # 撤去前に作られた練習用の受信(status='rehearsal')が残っている環境で、
    # 万一それが送信記録・文体学習・実績に混ざるのを防ぐため。データ自体は消していない
    try:
        from . import rehearsal as _rh
        if _rh.is_rehearsal(mid):
            return JSONResponse({"error": "これは練習用の受信です(記録しません)"},
                                status_code=400)
    except Exception:
        pass   # rehearsal.py を配布から外していても動く
    # v190: actedログ(トリアージ最終仕様#7)。act前スナップショット→差分を記録し、
    # undo(act_id指定)で status/学習/仮イベント の副作用を一括で巻き戻せるようにする
    _msg0 = db.get_message(mid)
    _ct = (_msg0 or {}).get("contact") or ""
    try:
        with db.conn() as c:
            _before = {r2["id"]: r2["status"] for r2 in c.execute(
                "SELECT id, status FROM messages WHERE contact=?", (_ct,))}
            _sr0 = c.execute("SELECT IFNULL(MAX(id),0) FROM sent_replies").fetchone()[0]
            _ev0 = c.execute("SELECT IFNULL(MAX(id),0) FROM events").fetchone()[0]
    except Exception:
        _before, _sr0, _ev0 = {}, 0, 0
    from .main import act as _act, Action as _Action
    try:
        r = _act(mid, _Action(action=action, text=text, mids=mids))
    except Exception as e:
        code = getattr(e, "status_code", 500)
        return JSONResponse({"error": str(getattr(e, "detail", e))}, status_code=code)
    # v191その2(#12): 裁定記録(acted_log+sent_replies.message_id紐付け)は linebot.record_act に
    # 共通化(チャット経由 _finish_message と同一実装。同一概念を二系統にしない規約)。
    act_id = None
    try:
        from . import linebot
        linebot.ensure()
        act_id = linebot.record_act(mid, _ct, action, _before, _sr0, _ev0)
    except Exception as e:
        print(f"[acted log] {e}", flush=True)
    db.track("liff_reply_act")
    return {**r, "act_id": act_id}


@router.post("/api/liff/reply/sweep")
async def liff_reply_sweep(request: Request):
    """v209: たまった受信の一括片づけ(本人裁定2026-08-12・モック承認済み)。
    クライアントが画面で数えて見せた mids を「返さない」に一括変更する。
    - auto=True(swept=1)で閉じる=そのまま率・スキップ数など成績集計に混ぜない(v72の掃除概念)
    - 相手単位で actedログ を切り、act_ids を返す→黒帯の↩︎で一括アンドゥ可能
    - サーバー側でも保護規則を再適用(S客・ピン・非店内の急ぎは絶対に巻き込まない)"""
    if not _authed(request):
        return _deny()
    from . import linebot
    try:
        body = await request.json()
        mids = [int(x) for x in (body.get("mids") or [])]
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    if not mids or len(mids) > 2000:
        return JSONResponse({"error": "bad mids"}, status_code=400)
    groups: dict[str, list] = {}
    with db.conn() as c:
        for mid in mids:
            m = c.execute("SELECT id, contact, category, status FROM messages WHERE id=?",
                          (mid,)).fetchone()
            if not m or m["status"] not in ("open", "deferred"):
                continue
            ct = db.get_contact(m["contact"]) or {}
            kind = (ct.get("kind") or "customer")
            # 保護(クライアントの🔥いま返す条件と同じ向き): S客・📌ピン・店内以外の急ぎ
            if (ct.get("rank") == "S" and kind == "customer"):
                continue
            if int(ct.get("flag_hot") or 0) == 1:
                continue
            if m["category"] == "urgent" and kind != "staff":
                continue
            groups.setdefault(m["contact"], []).append(m["id"])
        _sr0 = c.execute("SELECT IFNULL(MAX(id),0) FROM sent_replies").fetchone()[0]
        _ev0 = c.execute("SELECT IFNULL(MAX(id),0) FROM events").fetchone()[0]
    act_ids, n_msgs = [], 0
    for contact, ids in groups.items():
        with db.conn() as c:
            before = {r["id"]: r["status"] for r in c.execute(
                "SELECT id, status FROM messages WHERE contact=?", (contact,))}
        for mid in ids:
            db.set_status(mid, "skipped", auto=True)   # auto=swept=1(成績除外)
        n_msgs += len(ids)
        aid = linebot.record_act(ids[0], contact, "skipped", before, sr0=_sr0, ev0=_ev0)
        if aid:
            act_ids.append(aid)
    db.track("liff_sweep")
    return {"ok": True, "contacts": len(groups), "messages": n_msgs, "act_ids": act_ids}


@router.post("/api/liff/reply/undo")
async def liff_reply_undo(request: Request):
    """v190: act_id指定のアンドゥ。status復帰+文体学習の取り消し+仮イベント削除を一括。"""
    if not _authed(request):
        return _deny()
    try:
        act_id = int((await request.json()).get("act_id"))
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    from . import linebot
    linebot.ensure()
    with db.conn() as c:
        row = c.execute("SELECT * FROM acted_log WHERE act_id=? AND undone=0", (act_id,)).fetchone()
    if not row:
        return JSONResponse({"error": "戻せる記録が見つかりません"}, status_code=404)
    row = dict(row)
    try:
        with db.conn() as c:
            # v191その2(#6): 逆順undoの整合ガード。同一相手で「この裁定より後の未undo裁定」に
            # 含まれるmidは巻き戻さない(↷→返信→↷のundoで返信済みがopen復活し、sent_replyが
            # 残ったまま二重送信を誘発する事故の防止)。後の裁定をundoすれば通常どおり戻る。
            _later_mids = set()
            try:
                for r2 in c.execute("SELECT changed FROM acted_log WHERE contact=? AND act_id>? "
                                    "AND undone=0", (row["contact"], act_id)):
                    for m2, _p in json.loads(r2["changed"] or "[]"):
                        _later_mids.add(int(m2))
            except Exception:
                _later_mids = set()
            for m, prev in json.loads(row["changed"] or "[]"):
                if int(m) in _later_mids:
                    continue
                c.execute("UPDATE messages SET status=? WHERE id=?", (prev, int(m)))
            if row.get("sent_reply_id"):
                c.execute("DELETE FROM sent_replies WHERE id=?", (row["sent_reply_id"],))
            if row.get("event_id"):
                c.execute("DELETE FROM events WHERE id=? AND status='tentative'", (row["event_id"],))
            c.execute("UPDATE acted_log SET undone=1 WHERE act_id=?", (act_id,))
        # 文体プロファイルに入った実例も取り消す(相手別+全体。同文を1つだけ除去)
        st = (row.get("sent_text") or "").strip()
        if st:
            for key in (row["contact"], "_global"):
                try:
                    p = db.get_profile(key) or {}
                    fld = "my_samples_to_them" if key != "_global" else "samples"
                    lst = list(p.get(fld) or [])
                    if st in lst:
                        lst.remove(st)
                        p[fld] = lst
                        db.save_profile(key, p)
                except Exception:
                    pass
    except Exception as e:
        return JSONResponse({"error": f"戻せませんでした({type(e).__name__})"}, status_code=500)
    db.track("liff_reply_undo")
    return {"ok": True}


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
        tanto = (body.get("tanto") or "").strip()[:40]   # v214: 登録時に担当も一緒に(既定=自分)
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
        if tanto:   # v214: 担当(自分/別の子)。別の子なら下書きが控えめトーンになる(既存crm.py注入)
            try:
                crm.add_def("担当"); crm.set_attr(name, "担当", tanto)
            except Exception as e:
                print(f"[classify tanto] {e}", flush=True)
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
        from . import linebot as _lb
        _lb.quarantine_discard(name)   # v218(S2): 保留事実(検疫)も破棄
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
    try:
        plevel = int(body.get("plevel") if body.get("plevel") is not None else 1)
    except Exception:
        plevel = 1
    plevel = max(0, min(2, plevel))   # v226: 個別化つまみ
    if not db.get_contact(code):
        return JSONResponse({"error": "not found"}, status_code=404)
    if tone in ("peer", "staff"):
        text = linebot._casual_draft(code, tone)
        row_ai = bool(config.ANTHROPIC_API_KEY)
    else:
        # 注: greetingはranks/tags必須の設計。宛先は確定済みなので全ランクを通す
        r = campaign.generate(mode="greeting", ranks=["S", "A", "B"], codes=[code],
                              template=template, purpose=purpose, plevel=plevel)
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
