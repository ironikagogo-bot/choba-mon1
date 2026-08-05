"""帳場 バックエンド API。

起動:  uvicorn app.main:app --reload --port 8000
UI:    http://localhost:8000/
デモ受信: python scripts/demo_feed.py  (別ターミナル)
"""
import os
import time
import hmac
import hashlib

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import campaign, config, db, deskservice, drafts, push, triage
from .style_profile import extract_profile
from .provisioning import linking, MockProvisioner, DeskState

app = FastAPI(title="帳場 pilot API")
db.init()
linking.init()

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ==================== 玄関認証(パスワード1枚) ====================
_AUTH_COOKIE = "choba_auth"
# Cookie不要で通す経路: ログイン・機械投入(トークンで別認証)・静的・PWA・ヘルス
_EXEMPT = ("/login", "/api/login", "/api/logout", "/static/", "/sw.js",
           "/manifest.webmanifest", "/api/android/notify", "/api/quickdraft", "/healthz", "/api/stats")
_login_hits: dict = {}

_LOGIN_HTML = """<!DOCTYPE html><html lang=ja><head><meta charset=UTF-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>帳場</title>
<style>@import url('https://fonts.googleapis.com/css2?family=Shippori+Mincho:wght@700&family=Zen+Kaku+Gothic+New&display=swap');
*{box-sizing:border-box;margin:0}body{min-height:100vh;display:flex;align-items:center;justify-content:center;
background:radial-gradient(700px 500px at 50% 20%,#332c24,#1b1712);font-family:'Zen Kaku Gothic New',sans-serif;color:#efe8db}
.box{width:min(360px,88%);text-align:center}.b{font-family:'Shippori Mincho',serif;font-size:40px;letter-spacing:.34em;color:#fff}
.t{font-size:12px;color:#b3a988;margin:10px 0 26px;letter-spacing:.1em}
input{width:100%;padding:13px 14px;border-radius:10px;border:1px solid #5a5142;background:#2b2620;color:#fff;font-size:16px;text-align:center}
button{width:100%;margin-top:12px;padding:13px;border:none;border-radius:10px;background:#A8842F;color:#fff;font-size:15px;font-weight:700;font-family:inherit}
.err{color:#e08a7c;font-size:12.5px;margin-top:12px;min-height:16px}</style></head>
<body><div class=box><div class=b>帳　場</div><div class=t>ちょうば</div>
<input id=pw type=password placeholder=パスワード autofocus autocomplete=current-password>
<button id=go>入る</button><div class=err id=err></div></div>
<script>
var pw=document.getElementById('pw'),err=document.getElementById('err');
async function go(){err.textContent='';var v=pw.value;
 try{var r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:v})});
 if(r.ok){location.href='/';}else if(r.status===429){err.textContent='試行が多すぎます。少し待って。';}else{err.textContent='パスワードが違います';pw.value='';pw.focus();}}
 catch(e){err.textContent='通信エラー';}}
document.getElementById('go').onclick=go;pw.addEventListener('keydown',function(e){if(e.key==='Enter')go();});
</script></body></html>"""

def _session_token():
    return hmac.new(b"choba-session-v1", config.PASSWORD.encode("utf-8"), hashlib.sha256).hexdigest()

def _rate_ok(ip):
    now = time.time()
    q = [t for t in _login_hits.get(ip, []) if now - t < 60]
    q.append(now)
    _login_hits[ip] = q[-30:]
    return len(q) <= 10



# ---- 機能利用の計測(名前と時刻のみ) ----
def _feature_of(method: str, path: str):
    if method == "GET":
        return {"/": "app_open", "/crm": "crm_open", "/seki": "seki_open",
                "/campaign": "campaign_open", "/api/anniversaries": "anniversaries"}.get(path)
    if method != "POST":
        return None
    static = {"/api/quickdraft": "quickdraft", "/api/admin/wipe": "wipe",
              "/api/transfer": "transfer", "/api/inbox/classify": "classify",
              "/api/profile/import": "talk_import", "/api/sittings": "seki_record",
              "/api/campaign/generate": "campaign_generate"}
    if path in static:
        return static[path]
    if path.startswith("/api/messages/") and path.endswith("/drafts"):
        return "draft_generate"
    if path.startswith("/api/contacts/"):
        if path.endswith("/alias/remove"):
            return "alias_remove"
        if path.endswith("/alias"):
            return "alias_add"
        if path.endswith("/kind"):
            return "kind_change"
        if path.endswith("/delete"):
            return "contact_delete"
        if path.count("/") == 3:
            return "contact_edit"
    return None


def _track_request(request):
    try:
        f = _feature_of(request.method, request.url.path)
        if f:
            db.track(f)
    except Exception:
        pass

FEATURE_LABELS = {
    "app_open": "アプリを開いた", "crm_open": "連絡先画面", "seki_open": "実績画面",
    "campaign_open": "アナウンス画面", "draft_generate": "AI下書き", "quickdraft": "クイック下書き",
    "seki_record": "お席の記録", "campaign_generate": "一斉アナウンス生成", "anniversaries": "記念日",
    "talk_import": "トーク履歴取り込み", "classify": "未登録の仕分け", "alias_add": "紐付け追加",
    "alias_remove": "紐付け解除", "kind_change": "種別変更", "contact_edit": "カード編集",
    "contact_delete": "連絡先削除", "transfer": "移籍", "wipe": "データ消去",
}


@app.middleware("http")
async def _auth_guard(request: Request, call_next):
    if not config.PASSWORD:
        _track_request(request)
        return await call_next(request)
    path = request.url.path
    if any(path == e or path.startswith(e) for e in _EXEMPT):
        _track_request(request)
        return await call_next(request)
    tok = request.cookies.get(_AUTH_COOKIE, "")
    if tok and hmac.compare_digest(tok, _session_token()):
        _track_request(request)
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"error": "unauthorized", "login": "/login"}, status_code=401)
    return RedirectResponse("/login", status_code=302)

# ---- デモ用: AI生成の使用量ガード(認証なし公開URLでのトークン燃やし対策) ----
_DEMO_USAGE = {"date": "", "total": 0, "ips": {}}
_DEMO_LIMIT_TOTAL = int(os.environ.get("CHOUBA_DEMO_AI_LIMIT", "200"))     # 全体/日
_DEMO_LIMIT_IP = int(os.environ.get("CHOUBA_DEMO_AI_LIMIT_IP", "30"))      # IPあたり/日

def _demo_ai_guard(request: Request):
    """DEMOモードのみ: AI生成の回数制限。超過は429。"""
    if not config.DEMO or not config.ANTHROPIC_API_KEY:
        return
    import datetime as _dt
    today = _dt.date.today().isoformat()
    if _DEMO_USAGE["date"] != today:
        _DEMO_USAGE.update({"date": today, "total": 0, "ips": {}})
    ip = request.client.host if request.client else "?"
    if _DEMO_USAGE["total"] >= _DEMO_LIMIT_TOTAL or _DEMO_USAGE["ips"].get(ip, 0) >= _DEMO_LIMIT_IP:
        raise HTTPException(429, "デモの生成上限に達しました。また明日お試しください。")
    _DEMO_USAGE["total"] += 1
    _DEMO_USAGE["ips"][ip] = _DEMO_USAGE["ips"].get(ip, 0) + 1


@app.get("/healthz")
def _healthz():
    return {"ok": True}

@app.get("/api/stats")
def usage_stats(token: str = ""):
    """モニター観測用の統計(件数のみ・本文/相手名は一切含まない)。INGEST_TOKENで認証。
    運用者がダッシュボードから3インスタンスを横断で見る想定。CORSを許可(トークン必須のため)。"""
    if config.INGEST_TOKEN:
        if not token or not hmac.compare_digest(token, config.INGEST_TOKEN):
            raise HTTPException(401, "bad token")
    import datetime
    from . import crm as _crm
    _crm.ensure()   # kind等の後付け列を保証(初回起動直後でも落ちない)
    with db.conn() as c:
        for _tbl, _ddl in (("sent_replies", "edited INTEGER DEFAULT 0"),
                           ("sent_replies", "edit_ratio INTEGER DEFAULT 100"),
                           ("messages", "acted_ts REAL")):
            try:
                c.execute(f"ALTER TABLE {_tbl} ADD COLUMN {_ddl}")
            except Exception:
                pass
        n_contacts = c.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        kinds = {r[0] or "customer": r[1] for r in c.execute(
            "SELECT kind, COUNT(*) FROM contacts GROUP BY kind")}
        last_ts = c.execute("SELECT MAX(ts) FROM messages").fetchone()[0]
        n_examples = c.execute("SELECT COUNT(*) FROM sent_replies").fetchone()[0]
        days = []
        today = datetime.date.today()
        for i in range(13, -1, -1):
            d = today - datetime.timedelta(days=i)
            t0 = datetime.datetime(d.year, d.month, d.day).timestamp()
            t1 = t0 + 86400
            received = c.execute("SELECT COUNT(*) FROM messages WHERE ts>=? AND ts<?", (t0, t1)).fetchone()[0]
            drafted = c.execute(
                "SELECT COUNT(DISTINCT message_id) FROM drafts d JOIN messages m ON m.id=d.message_id "
                "WHERE m.ts>=? AND m.ts<?", (t0, t1)).fetchone()[0]
            verbatim = c.execute("SELECT COUNT(*) FROM sent_replies WHERE ts>=? AND ts<? AND edited=0", (t0, t1)).fetchone()[0]
            edited = c.execute("SELECT COUNT(*) FROM sent_replies WHERE ts>=? AND ts<? AND edited=1", (t0, t1)).fetchone()[0]
            skipped = c.execute("SELECT COUNT(*) FROM messages WHERE ts>=? AND ts<? AND status='skipped'", (t0, t1)).fetchone()[0]
            days.append({"date": d.isoformat(), "received": received, "drafted": drafted,
                         "sent_verbatim": verbatim, "sent_edited": edited, "skipped": skipped})

        # ---- 行動分析(本文なし・開発用) ----
        t14 = time.time() - 14 * 86400
        # 時間帯ヒスト(0-23時): 受信/返信
        hourly = {h: {"received": 0, "sent": 0} for h in range(24)}
        for r in c.execute("SELECT ts FROM messages WHERE ts>=?", (t14,)):
            hourly[datetime.datetime.fromtimestamp(r[0]).hour]["received"] += 1
        for r in c.execute("SELECT ts FROM sent_replies WHERE ts>=?", (t14,)):
            hourly[datetime.datetime.fromtimestamp(r[0]).hour]["sent"] += 1
        # 対応の内訳(最終ステータス)
        actions = {r[0]: r[1] for r in c.execute(
            "SELECT status, COUNT(*) FROM messages WHERE ts>=? GROUP BY status", (t14,))}
        # 放置(24時間以上openのまま)
        neglected = c.execute("SELECT COUNT(*) FROM messages WHERE status='open' AND ts<?",
                              (time.time() - 86400,)).fetchone()[0]
        # 応答時間(受信→対応・分): repliedのみ・中央値/90%点
        lat = [max(0.0, (r[1] - r[0]) / 60.0) for r in c.execute(
            "SELECT ts, acted_ts FROM messages WHERE status='replied' AND acted_ts IS NOT NULL AND ts>=?", (t14,))]
        lat.sort()
        def _pct(v, q):
            return round(v[min(len(v)-1, int(len(v)*q))], 1) if v else None
        latency = {"n": len(lat), "median_min": _pct(lat, 0.5), "p90_min": _pct(lat, 0.9)}
        # ランク別(太客S〜細客B)の対応差
        by_rank = {}
        for rk in ("S", "A", "B"):
            rec = c.execute("SELECT COUNT(*) FROM messages m JOIN contacts k ON k.code=m.contact "
                            "WHERE k.rank=? AND m.ts>=?", (rk, t14)).fetchone()[0]
            rep = c.execute("SELECT COUNT(*) FROM messages m JOIN contacts k ON k.code=m.contact "
                            "WHERE k.rank=? AND m.ts>=? AND m.status='replied'", (rk, t14)).fetchone()[0]
            skp = c.execute("SELECT COUNT(*) FROM messages m JOIN contacts k ON k.code=m.contact "
                            "WHERE k.rank=? AND m.ts>=? AND m.status='skipped'", (rk, t14)).fetchone()[0]
            rl = [max(0.0, (r[1] - r[0]) / 60.0) for r in c.execute(
                "SELECT m.ts, m.acted_ts FROM messages m JOIN contacts k ON k.code=m.contact "
                "WHERE k.rank=? AND m.status='replied' AND m.acted_ts IS NOT NULL AND m.ts>=?", (rk, t14))]
            rl.sort()
            er = c.execute("SELECT AVG(edit_ratio) FROM sent_replies s JOIN contacts k ON k.code=s.contact "
                           "WHERE k.rank=? AND s.ts>=?", (rk, t14)).fetchone()[0]
            by_rank[rk] = {"received": rec, "replied": rep, "skipped": skp,
                           "median_min": _pct(rl, 0.5),
                           "avg_edit_ratio": round(er, 1) if er is not None else None}
        # 仕分けカテゴリ別
        by_cat = {}
        for cat in ("urgent", "rally", "batch"):
            rec = c.execute("SELECT COUNT(*) FROM messages WHERE category=? AND ts>=?", (cat, t14)).fetchone()[0]
            rep = c.execute("SELECT COUNT(*) FROM messages WHERE category=? AND ts>=? AND status='replied'", (cat, t14)).fetchone()[0]
            by_cat[cat] = {"received": rec, "replied": rep}
        # 対応モードの利用状況
        modes = {
            "inashi_on": c.execute("SELECT COUNT(*) FROM contacts WHERE flag_ero=1").fetchone()[0],
            "nori_ok": c.execute("SELECT COUNT(*) FROM contacts WHERE flag_ero=2").fetchone()[0],
            "gachikoi": c.execute("SELECT COUNT(*) FROM contacts WHERE flag_koi=1").fetchone()[0],
            "snoozed_now": c.execute("SELECT COUNT(*) FROM snoozed_names WHERE until_ts>?", (time.time(),)).fetchone()[0],
        }
        avg_edit = c.execute("SELECT AVG(edit_ratio) FROM sent_replies WHERE ts>=?", (t14,)).fetchone()[0]
        # 機能の利用状況(14日・未使用機能の検出)
        c.execute("CREATE TABLE IF NOT EXISTS feature_events("
                  "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, ts REAL NOT NULL)")
        used = {r[0]: r[1] for r in c.execute(
            "SELECT name, COUNT(*) FROM feature_events WHERE ts>=? GROUP BY name", (t14,))}
        features = {k: {"label": v, "count": used.get(k, 0)} for k, v in FEATURE_LABELS.items()}
    return JSONResponse({
        "ok": True, "now": time.time(), "last_ingest_ts": last_ts,
        "contacts_total": n_contacts, "contacts_by_kind": kinds,
        "learning_examples": n_examples, "days": days,
        "hourly": hourly, "actions": actions, "neglected_over_24h": neglected,
        "latency": latency, "by_rank": by_rank, "by_category": by_cat,
        "modes": modes, "avg_edit_ratio": round(avg_edit, 1) if avg_edit is not None else None,
        "features": features,
    }, headers={"Access-Control-Allow-Origin": "*"})

@app.get("/login")
def _login_page():
    return HTMLResponse(_LOGIN_HTML)

class _LoginIn(BaseModel):
    password: str = ""

@app.post("/api/login")
def _login(body: _LoginIn, request: Request):
    ip = request.client.host if request.client else "?"
    if not _rate_ok(ip):
        raise HTTPException(429, "試行が多すぎます。1分ほど待ってからやり直してください。")
    if not config.PASSWORD:
        return {"ok": True}
    if not hmac.compare_digest(body.password or "", config.PASSWORD):
        raise HTTPException(401, "パスワードが違います")
    is_https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    resp = JSONResponse({"ok": True})
    resp.set_cookie(_AUTH_COOKIE, _session_token(), httponly=True, samesite="lax",
                    secure=is_https, max_age=60 * 60 * 24 * 90, path="/")
    return resp

@app.post("/api/logout")
def _logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(_AUTH_COOKIE, path="/")
    return resp
# ================================================================

# パイロット: プロビジョナはモック(道4スパイクで RealProvisioner に差し替え)
PROVISIONER = MockProvisioner()
DESKS: dict[str, object] = {}   # user_id -> Desk (パイロットはメモリ保持)

# 初期データ(デモ用の見せる顧客)
def seed_initial_contacts():
    if config.DEMO:
        from . import crm as _crm
        _now = time.time()
        _demo = [
            # code, rank, cyc, tags, bd, days, kind, stand, kids_bday, founding
            ("T.会長", "S", 10, "VIP", "07-20", 2, "customer", "", "お孫さん:07-28", "07-25"),
            ("K.専務", "A", 14, "常連", "", 40, "customer", "", "", ""),
            ("M.先生", "A", 21, "", "07-22", None, "customer", "", "", ""),
            ("Y.社長", "B", 30, "", "", 1, "customer", "", "", "07-30"),
            ("S.部長", "B", 30, "常連", "", 90, "customer", "", "", ""),
            ("H.常務", "S", 12, "VIP", "", 5, "customer", "", "", ""),
            ("九条さん", "S", 14, "VIP・紹介者", "", 7, "customer", "senior", "", ""),
            ("財前さん", "S", 14, "VIP", "07-30", 1, "customer", "", "", ""),
            # 対応モードのデモ用(架空): 宮下=いなしON済み / 早乙女=未設定(下ネタアラームが出る) / 神楽=ガチ恋
            ("宮下さん", "B", 30, "遠方", "", 60, "customer", "", "", ""),
            ("早乙女さん", "B", 30, "", "", 45, "customer", "", "", ""),
            ("神楽さん", "A", 21, "常連", "", 12, "customer", "", "", ""),
            # 同業者(店外) peer ── 立場stand: senior/equal/junior
            ("るり子ママ", "B", 0, "同業・アフター", "", None, "peer", "senior", "", ""),
            ("ゲンさん", "B", 0, "同業", "", None, "peer", "senior", "", ""),
            ("澪さん", "B", 0, "同業", "", None, "peer", "equal", "", ""),
            ("まりも", "B", 0, "同業", "", None, "peer", "junior", "", ""),
            # 店内 staff
            ("担当ママ", "B", 0, "店内", "", None, "staff", "senior", "", ""),
            ("ひなの", "B", 0, "店内・ヘルプ", "", None, "staff", "junior", "", ""),
            ("心春", "B", 0, "店内・ヘルプ", "", None, "staff", "junior", "", ""),
        ]
        for code, rank, cyc, tags, bd, days, kind, stand, kids, founding in _demo:
            db.upsert_contact(code, rank, cyc, "", tags, bd)
            if days is not None:
                db.set_last_visit(code, _now - days * 86400)
            _crm.update_contact(code, {"stand": stand, "kids_bday": kids, "founding": founding})
            if kind and kind != "customer":
                _crm.set_kind(code, kind)
        # 対応モードのデモ設定(架空客)
        _crm.update_contact("宮下さん", {"flag_ero": 1, "note_neg": "下ネタ多い"})
        _crm.update_contact("神楽さん", {"flag_koi": 1})

        # デモ用の文体実例シード: 実例ゼロだと安全側の丁寧語既定で硬くなるため、
        # 架空の「本人の声」を与えてデモの下書き品質を実運用相当にする(2026-07-29)
        _g_samples = ["ほんと！？嬉しい〜、席とっとくね", "おっけー、空けとく！何時ごろ来れそう？",
                      "りょ！また連絡して〜", "昨日はありがとう！たのしかった〜",
                      "えー大変だったね😭 無理しないでね", "そういえば例の話どうなった？",
                      "今週なら火曜か木曜いけるよ〜", "やった！待ってるね😊"]
        db.save_profile("_global", {"n_messages": 300, "avg_len": 19, "emoji_per_msg": 0.9,
                                    "exclaim_per_msg": 0.7, "warai_per_msg": 0.2,
                                    "top_endings": ["〜", "ね！", "よ"], "top_emojis": ["😊", "🤣", "😭"],
                                    "samples": _g_samples})
        for _code, _samps in {
            "財前さん": ["財前さんおつかれさま！今日もありがとう🍶", "日本酒の会行ってきたよ〜、好きそうなの見つけた！",
                        "ほんと！？嬉しい、3名さまで席とっとくね", "9時に待ってるね！気をつけて来てね"],
            "九条さん": ["九条さんこの前はありがとうございました！", "また素敵なご縁があったら教えてください😊",
                        "近いうちにご挨拶に伺いますね"],
            "神楽さん": ["ありがと😊 それより神楽さん最近ちゃんと寝てる？", "お店でゆっくり話そ！待ってるね",
                        "考えすぎ🤣 仕事忙しいんじゃないの？", "おつかれさま！今日も一日がんばったね"],
            "宮下さん": ["なにそれ🤣 ふつうの写真で我慢して！", "その話は聞かなかったことにするね！",
                        "温泉すっごい良かったよ、景色の写真送るね〜", "宮下さんも元気にしてた？"],
        }.items():
            db.save_profile(_code, {"my_message_count": 40, "honorifics_to_them": [_code],
                                    "my_samples_to_them": _samps, "their_recent": []})
    else:
        # 本番: ダミーデータは入れない(本人が自分で登録・トーク履歴取り込み)
        return

if not db.list_contacts():
    seed_initial_contacts()


# 見せる用インスタンス(DEMO)はデスクを最初から開いておく(「未開設」表示を避ける)
if config.DEMO:
    try:
        deskservice.SERVICE.start("demo", 1.0)
        deskservice.SERVICE.approve_login()
    except Exception:
        pass


class Incoming(BaseModel):
    contact: str
    text: str


class Action(BaseModel):
    action: str  # replied / stamped / deferred / skipped
    text: str = ""  # replied のとき: 実際にコピーした最終文(編集込み)→文体学習に還元


@app.post("/api/incoming")
def incoming(body: Incoming):
    """手動の受信登録(開発・検証用)。デスク経由と同じパイプラインを通る。"""
    mid, cat, reason = deskservice.ingest(body.contact, body.text)
    return {"id": mid, "category": cat, "reason": reason}


# ---------- 仮想デスク(デモ盤: 本番と同じ流れ・LINEを読む部分だけ台本) ----------

class DeskStart(BaseModel):
    user_id: str = "demo"
    speed: float = 1.0   # 台本の速度倍率。1.0=約90秒で1日分。0.5で倍速


@app.post("/api/desk/start")
def desk_start(body: DeskStart):
    """デスク開設: 仮想PC起動(模擬) → LINEログインQR提示。"""
    desk = deskservice.SERVICE.start(body.user_id, body.speed)
    return {
        "desk_state": desk.state.value,
        "line_login_qr": desk.line_login_qr,
        "guide": [
            "本番: このQRをLINEアプリで読み、『PCでログイン』を承認します",
            "デモ: 下のボタンを押すと承認が再現されます",
        ],
        "notice": "実接続はLINE利用規約上グレーな領域を含みます。説明と同意の上での提供になります。",
    }


@app.post("/api/desk/approve")
def desk_approve():
    """LINEログイン承認(デモではタップで再現)→ 監視開始、受信が流れ始める。"""
    try:
        state = deskservice.SERVICE.approve_login()
    except RuntimeError:
        raise HTTPException(409, "デスクが未開設です")
    return {"desk_state": state.value, "active": state == DeskState.ACTIVE}


@app.get("/api/desk/status")
def desk_status():
    """デスク状態と裏側コンソール(デスクの動きログ)。"""
    return deskservice.SERVICE.status()


@app.post("/api/desk/replay")
def desk_replay():
    """受信デモをもう一度最初から流す(受信箱はリセット)。"""
    try:
        deskservice.SERVICE.replay()
    except RuntimeError:
        raise HTTPException(409, "デスクが稼働していません")
    return {"ok": True}


@app.post("/api/desk/stop")
def desk_stop():
    deskservice.SERVICE.stop()
    return {"ok": True}


# ---------- Android通知の受信口(本命構成) ----------
# Androidのサブ端末(同一アカウント)の転送アプリ/専用アプリが、LINEの着信通知を
# ここに POST する。JSON でも form でも受け付ける(転送アプリの実装差を吸収)。
#   例(JSON): {"token":"...","package":"jp.naver.line.android","title":"相手名","text":"本文"}

@app.post("/api/android/notify")
async def android_notify(request: Request):
    # クエリパラメータ・JSON・form のどれでも受ける(転送アプリの実装差を吸収)。
    # クエリパラメータはURLエンコード済みで、長文・改行・記号に強い(推奨)。
    data = {}
    for k, v in request.query_params.items():
        if v and not data.get(k):
            data[k] = v
    try:
        body = await request.json()
    except Exception:
        try:
            body = dict(await request.form())
        except Exception:
            body = {}
    if isinstance(body, dict):
        for k, v in body.items():
            if v and not data.get(k):
                data[k] = str(v)
    if config.INGEST_TOKEN and not hmac.compare_digest(str(data.get("token", "")), config.INGEST_TOKEN):
        raise HTTPException(401, "bad token")
    res = deskservice.SERVICE.android_ingest(
        str(data.get("package", "") or ""),
        str(data.get("title", "") or ""),
        str(data.get("text", "") or ""),
        ticker=str(data.get("ticker", "") or ""),
        big_text=str(data.get("big_text", "") or ""),
        sub_text=str(data.get("sub_text", "") or ""),
        text_lines=str(data.get("text_lines", "") or ""),
        key=str(data.get("key", "") or ""),
        ts=str(data.get("ts", "") or ""),
        sender=str(data.get("sender", "") or ""),
        group=str(data.get("group", "") or ""),
    )
    return res


@app.get("/api/inbox")
def inbox():
    from .notify_ingest import is_call_notice
    msgs = db.open_messages()
    out = {"urgent": [], "rally": {}, "batch": [], "batch_time": config.BATCH_TIME}
    # 通話系通知の残骸(v25より前に取り込まれた「音声通話を着信中」等)は表示せず閉じる
    kept = []
    for m in msgs:
        if is_call_notice(m["text"]):
            db.set_status(m["id"], "skipped")
            continue
        kept.append(m)
    # 名無しグループ通知(送信者不明)と同一本文の個別通知が両方ある時は、グループ側を閉じる
    def _is_group_name(cn):
        return ("," in (cn or "")) or ("、" in (cn or ""))
    _texts_indiv = {(m["text"] or "").replace("【グループ】", "").strip()
                    for m in kept if not _is_group_name(m["contact"])}
    _kept2 = []
    for m in kept:
        if _is_group_name(m["contact"]):
            _body = (m["text"] or "").replace("【グループ】", "").strip()
            if len(_body) >= 10 and _body in _texts_indiv:
                db.set_status(m["id"], "skipped")
                continue
        _kept2.append(m)
    kept = _kept2
    # 即対応に出ている本文と同一のラリー項目は「通知の再掲」→表示せず閉じる
    # (短文の本物の連投を巻き込まないよう10字以上のみ対象)
    urgent_keys = {(m["contact"], (m["text"] or "").strip())
                   for m in kept if m["category"] == "urgent"}
    # v57 2欄化: ラリー欄を廃止。全欄「1相手1カード」。
    # 置き場所の決定: 即対応(urgentあり) / 会話中(直近の自分の返信への続き) / まとめ箱。
    # 店内(kind=staff)と未紐付け(unlinked)は従来どおりメッセージ単位で urgent/batch 配列に残す。
    TALK_WINDOW = int(os.environ.get("CHOUBA_TALK_WINDOW_MIN", "45")) * 60
    _now = time.time()
    _groupable = []
    for m in kept:
        _c = db.get_contact(m["contact"]) or {}
        m["rank"] = _c.get("rank", "B")
        m["unlinked"] = 1 if (_c.get("linked") == 0) else 0
        m["kind"] = _c.get("kind") or "customer"
        m["flag_ero"] = int(_c.get("flag_ero") or 0)
        if m["category"] == "rally":
            key = (m["contact"], (m["text"] or "").strip())
            if len(key[1]) >= 10 and key in urgent_keys:
                db.set_status(m["id"], "skipped")
                continue
        if m["kind"] == "staff" or m["unlinked"]:
            (out["urgent"] if m["category"] == "urgent" else out["batch"]).append(m)
        else:
            _groupable.append(m)
    # あとでにした分(deferred)もその相手のカードに合流(未紐付け/店内は従来どおりまとめ箱配列へ)
    try:
        with db.conn() as c:
            _defs = [dict(r) for r in c.execute(
                "SELECT * FROM messages WHERE status='deferred' ORDER BY ts ASC LIMIT 30")]
        for m in _defs:
            _c = db.get_contact(m["contact"]) or {}
            m["rank"] = _c.get("rank", "B")
            m["unlinked"] = 1 if (_c.get("linked") == 0) else 0
            m["kind"] = _c.get("kind") or "customer"
            m["flag_ero"] = int(_c.get("flag_ero") or 0)
            m["deferred"] = 1
            if m["kind"] == "staff" or m["unlinked"]:
                m["reason"] = "あとでにした分"
                out["batch"].append(m)
            else:
                _groupable.append(m)
    except Exception:
        pass
    # 相手ごとに束ねる
    _by = {}
    for m in sorted(_groupable, key=lambda x: x.get("ts") or 0):
        _by.setdefault(m["contact"], []).append(m)
    groups = []
    for _contact, _lst in _by.items():
        _open = [x for x in _lst if not x.get("deferred")]
        _base = _open or _lst
        # 会話中判定: 直近の自分の返信がTALK_WINDOW内 かつ その後に受信がある
        _mode = "batch"
        if any(x["category"] == "urgent" for x in _base):
            _mode = "urgent"
        else:
            try:
                with db.conn() as c:
                    _r = c.execute("SELECT ts FROM sent_replies WHERE contact=? ORDER BY ts DESC LIMIT 1",
                                   (_contact,)).fetchone()
                if _r and (_now - (_r["ts"] or 0)) <= TALK_WINDOW and                         any((x.get("ts") or 0) >= (_r["ts"] or 0) for x in _base):
                    _mode = "talk"
            except Exception:
                pass
        # 自分の返信(帳場経由)を時系列で挟む
        try:
            _first = min((x["ts"] or 0) for x in _lst)
            with db.conn() as c:
                _rows = c.execute(
                    "SELECT text, ts FROM sent_replies WHERE contact=? AND ts>=? ORDER BY ts ASC LIMIT 10",
                    (_contact, _first - 60)).fetchall()
            for _t, _ts in _rows:
                _lst.append({"own": 1, "text": _t, "ts": _ts})
            _lst.sort(key=lambda x: x.get("ts") or 0)
        except Exception:
            pass
        _last = [x for x in _lst if not x.get("own")][-1]
        groups.append({"contact": _contact, "mode": _mode,
                       "rank": _last.get("rank", "B"), "kind": _last.get("kind", "customer"),
                       "flag_ero": _last.get("flag_ero", 0),
                       "has_deferred": 1 if any(x.get("deferred") for x in _lst) else 0,
                       "first_ts": min((x.get("ts") or 0) for x in _lst if not x.get("own")),
                       "msgs": _lst})
    groups.sort(key=lambda g: g["first_ts"])
    out["groups"] = groups
    out["batch"].sort(key=lambda x: x.get("ts") or 0)
    return out


@app.post("/api/messages/{mid}/drafts")
def make_drafts(mid: int, request: Request):
    _demo_ai_guard(request)
    if not db.get_message(mid):
        raise HTTPException(404)
    existing = db.get_drafts(mid)
    if existing:
        # デスクが受信時に先行生成済み(即対応)。再生成せずそれを出す
        return {"drafts": [{"text": d["text"], "tone": d["tone"]} for d in existing],
                "mode": "prepared"}
    return {"drafts": drafts.generate(mid),
            "mode": "llm" if config.ANTHROPIC_API_KEY else "template"}


class WipeIn(BaseModel):
    scope: str  # "messages"(受信箱のみ) / "all"(全データ初期化)


@app.post("/api/admin/wipe")
def admin_wipe(body: WipeIn):
    """データ消去。玄関認証の内側のみ(ミドルウェアで保護済み)。
    messages: 受信箱とその下書きだけ空にする(連絡先・学習・実績は残す)
    all: 全テーブル初期化(押し間違い防止はフロントの2タップ確認)
    """
    if body.scope not in ("messages", "all"):
        raise HTTPException(400, "bad scope")
    from . import crm as _crm
    _crm.ensure()
    with db.conn() as c:
        c.execute("DELETE FROM drafts")
        c.execute("DELETE FROM messages")
        if body.scope == "all":
            for t in ("sent_replies", "events", "style_profile",
                      "contact_aliases", "muted_names", "snoozed_names",
                      "pending_links", "contact_attrs", "attr_defs", "contacts"):
                try:
                    c.execute(f"DELETE FROM {t}")
                except Exception:
                    pass
            for t in ("sitting_members", "sittings"):
                try:
                    c.execute(f"DELETE FROM {t}")
                except Exception:
                    pass
    return {"ok": True, "scope": body.scope}


@app.post("/api/messages/{mid}/action")
def act(mid: int, body: Action):
    msg = db.get_message(mid)
    if not msg:
        raise HTTPException(404)
    if body.action not in ("replied", "stamped", "deferred", "skipped"):
        raise HTTPException(400, "bad action")
    db.set_status(mid, body.action)
    # ラリー連投対策: 返信/スタンプは「その相手との未対応スレッド全体」への対応とみなして閉じる。
    # これをしないと連投の残り(古い通)が open のまま残り、コピペ後も同じ会話がカードに再表示され続ける。
    if body.action in ("replied", "stamped"):
        try:
            with db.conn() as c:
                _thread = [dict(r) for r in c.execute(
                    "SELECT id, contact, ts FROM messages WHERE status IN ('open','deferred')")]
            for _m in _thread:
                if (_m["contact"] == msg["contact"] and _m["id"] != mid
                        and (_m["ts"] or 0) <= (msg["ts"] or 0)):
                    db.set_status(_m["id"], body.action)
        except Exception:
            pass
    # 返信したら、実際に送った最終文(編集込み)を文体プロファイルに還元=使うほど賢くなる
    if body.action == "replied" and (body.text or "").strip():
        try:
            from .style_profile import learn_from_sent
            # そのまま送れたか自動判定: 生成済み下書きのどれかと一致=そのまま(0)/不一致=編集(1)/下書き無し(クイック等)=0
            import difflib
            _dr = [d.get("text", "").strip() for d in (db.get_drafts(mid) or [])]
            _sent = body.text.strip()
            _edited = 1 if (_dr and _sent not in _dr) else 0
            _ratio = 100
            if _dr:
                _ratio = int(round(max(difflib.SequenceMatcher(None, _sent, d).ratio() for d in _dr) * 100))
            learn_from_sent(msg["contact"], body.text, edited=_edited, edit_ratio=_ratio)
        except Exception:
            pass
    # 返信したら実績を自動記録(入力ゼロ原則): 来店系→visit、同伴系→dohan
    # ※店内・業務(黒服/ママ)は営業対象外なので実績を記録しない
    _kind = (db.get_contact(msg["contact"]) or {}).get("kind", "customer")
    if body.action == "replied" and _kind != "staff":
        _r = msg["reason"] or ""
        if ("来店" in _r) or ("席" in _r):
            db.add_event(msg["contact"], "visit", f"{msg['contact']} 来店(仮)", "tentative")
        elif ("同伴" in _r) or ("アフター" in _r):
            db.add_event(msg["contact"], "dohan", f"{msg['contact']} 同伴(仮)", "tentative")
    return {"ok": True}


@app.post("/api/profile/import")
async def import_profile(file: UploadFile, self_name: str = "自分",
                         contact: str | None = None, auto_register: bool = True):
    """LINEトーク履歴(.txt)を取り込む。
    - 本人全体の文体(_global)を抽出・保存
    - contact 指定時: その相手専用プロファイルも作成
    - contact 未指定 & auto_register: 履歴に登場する相手を顧客として自動登録し、
      各相手の専用プロファイルを作成(=顧客登録と学習を一度に行う本命フロー)
    """
    from .style_profile import discover_contacts, extract_contact_profile, Profile
    _MAX = 30 * 1024 * 1024
    _buf = b""
    while True:
        _chunk = await file.read(1024 * 1024)
        if not _chunk:
            break
        _buf += _chunk
        if len(_buf) > _MAX:
            raise HTTPException(413, "ファイルが大きすぎます(最大30MB)")
    text = _buf.decode("utf-8", errors="replace")

    result = {"registered": [], "profiled": []}

    if contact:
        # 特定相手のスレッドから本人文体 + 相手別プロファイル(カードの「この相手の履歴を取り込む」)
        p = extract_profile(text, self_name=self_name)
        if p.n_messages == 0:
            raise HTTPException(422, "本人のメッセージを検出できませんでした。self_name を確認してください。")
        db.save_profile("_global", p.to_dict())
        # txt内の相手表示名を自動検出: カードの識別名と違ってもこのカードに正しく載せる
        partners = discover_contacts(text, self_name=self_name)
        src = contact if contact in partners else (partners[0] if partners else contact)
        cp = extract_contact_profile(text, src, self_name=self_name)
        db.save_profile(contact, cp)   # 保存キーは常にカードの識別名=下書き生成が引くキー
        from . import crm as _crm
        if src:
            _crm.add_alias(src, contact)   # txtの表示名をこのカードの紐付けに自動登録(名前ズレ解消)
            result["alias_added"] = src
        if not db.get_contact(contact):
            db.upsert_contact(contact, "B")
            result["registered"].append(contact)
        result["profiled"].append(contact)
        result["global_msgs"] = p.n_messages
        result["contact_msgs"] = int(cp.get("my_message_count") or 0)
        return result

    # 全体取り込み: 本人文体 + 全相手の自動登録&学習
    p = extract_profile(text, self_name=self_name)
    if p.n_messages == 0:
        raise HTTPException(422, "本人のメッセージを検出できませんでした。self_name を確認してください。")
    db.save_profile("_global", p.to_dict())
    result["global_msgs"] = p.n_messages

    for name in discover_contacts(text, self_name=self_name):
        cp = extract_contact_profile(text, name, self_name=self_name)
        db.save_profile(name, cp)
        result["profiled"].append(name)
        if auto_register and not db.get_contact(name):
            db.upsert_contact(name, "B")
            result["registered"].append(name)
    return result


class ContactIn(BaseModel):
    code: str
    rank: str = "B"
    cycle_days: int | None = None
    note: str = ""
    tags: str = ""
    birthday: str = ""   # 'MM-DD'(任意)


@app.post("/api/contacts")
def add_contact(body: ContactIn):
    db.upsert_contact(body.code, body.rank, body.cycle_days, body.note,
                      body.tags, body.birthday)
    if body.cycle_days:
        db.set_cycle(body.code, body.cycle_days)
    return db.get_contact(body.code)


@app.post("/api/contacts/{code}/visit")
def record_visit(code: str):
    """来店を記録(お礼・ご無沙汰判定の元データ)。"""
    if not db.get_contact(code):
        raise HTTPException(404)
    db.set_last_visit(code)
    return db.get_contact(code)


class RankIn(BaseModel):
    rank: str


@app.put("/api/contacts/{code}/rank")
def update_rank(code: str, body: RankIn):
    if not db.get_contact(code):
        raise HTTPException(404)
    if body.rank not in ("S", "A", "B"):
        raise HTTPException(400, "rank は S/A/B")
    db.set_rank(code, body.rank)
    return db.get_contact(code)


@app.get("/api/profile")
def profile(contact: str = "_global"):
    return db.get_profile(contact) or {}


@app.get("/api/events")
def events():
    return db.list_events()


@app.get("/api/contacts")
def contacts():
    return db.list_contacts()


# ---------- 一斉下書き(キャンペーン) ----------
def _split(s: str):
    return [x for x in (s or "").replace("　", ",").split(",") if x.strip()]


@app.get("/api/campaign/recipients")
def campaign_recipients(ranks: str = "", tags: str = "", mode: str = "greeting"):
    """選択中のあて先プレビュー(生成はしない)。"""
    recips = campaign.select_recipients(_split(ranks), _split(tags), mode)
    return {"count": len(recips), "season": campaign.season_label(), "recipients": recips}


class CampaignIn(BaseModel):
    mode: str = "greeting"        # greeting=営業のきっかけ / thanks=来店お礼
    ranks: list[str] = []
    tags: list[str] = []
    template: str = ""
    codes: list[str] = []         # 指定時はこの相手だけ(UIの個別チェックを反映)


@app.post("/api/campaign/generate")
def campaign_generate(body: CampaignIn, request: Request):
    _demo_ai_guard(request)
    """あて先ぶんの下書きを一人ずつ違う内容で一括生成(送信も保存もしない)。"""
    return campaign.generate(body.mode, body.ranks, body.tags, body.template,
                             codes=body.codes or None)


@app.get("/campaign")
def campaign_page():
    return FileResponse(os.path.join(STATIC_DIR, "campaign.html"))


@app.get("/customers")
def customers_page():
    return FileResponse(os.path.join(STATIC_DIR, "customers.html"))


@app.get("/demo")
def demo_page():
    """サーバー不要でも動く静的UXデモ(友人に見せる用・単体で完結)。"""
    return FileResponse(os.path.join(STATIC_DIR, "demo.html"))


class OnboardStart(BaseModel):
    user_id: str


@app.post("/api/onboard/start")
def onboard_start(body: OnboardStart):
    """①端末リンクQR発行 + ②仮想デスク起動(LINEログインQR取得)。"""
    link = linking.create_link_token(body.user_id)
    desk = PROVISIONER.provision(body.user_id)
    DESKS[body.user_id] = desk
    return {
        "device_link": link,                    # ①: 本人iPhoneを帳場に紐付け
        "line_login_qr": desk.line_login_qr,    # ②: 本人がLINEアプリで読む(PCログイン承認)
        "desk_state": desk.state.value,
        "guide": [
            "手順1: この端末リンクQRを読むと、あなたのスマホに通知が届くようになります",
            "手順2: LINEログインQRをLINEアプリで読み、『PCでログイン』を承認してください",
            "手順3: 完了。以降デスクが受信を見張ります(読み取りのみ・送信はしません)",
        ],
        "notice": "手順2はPCでLINEを開くのと同じ操作です。規約上グレーな領域を含むため、説明と同意の上でご利用ください。",
    }


@app.post("/api/onboard/poll")
def onboard_poll(body: OnboardStart):
    """②のLINEログインが承認されたか確認。"""
    desk = DESKS.get(body.user_id)
    if not desk:
        raise HTTPException(404, "desk not found")
    state = PROVISIONER.poll_login(desk)
    return {"desk_state": state.value,
            "active": state == DeskState.ACTIVE}


@app.get("/api/onboard/health")
def onboard_health(user_id: str):
    """デスク死活。SESSION_LOST ならフェイルセーフ(LINE通知を戻す案内)を返す。"""
    desk = DESKS.get(user_id)
    if not desk:
        raise HTTPException(404)
    state = PROVISIONER.health(desk)
    failsafe = state in (DeskState.SESSION_LOST, DeskState.REAUTH_REQUIRED)
    return {"desk_state": state.value, "failsafe": failsafe,
            "message": "デスクが停止しました。LINEアプリの通知を一時的にオンに戻してください。" if failsafe else "正常"}


@app.get("/link")
def link_page(token: str):
    """①QRを読んだ本人のiPhoneが着地するページ(端末紐付け)。"""
    try:
        res = linking.claim_link_token(token)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "device_id": res["device_id"], "message": "この端末を帳場に紐付けました。"}


# ---------- Web Push (③ デスク → 本人スマホ通知) ----------

@app.get("/api/push/public_key")
def push_public_key():
    """PWA が購読時に使う VAPID (Voluntary Application Server Identification) 公開鍵。"""
    return {"available": push.AVAILABLE, "key": push.public_key()}


@app.post("/api/push/subscribe")
def push_subscribe(sub: dict):
    if not isinstance(sub.get("endpoint"), str) or not sub["endpoint"]:
        raise HTTPException(400, "endpoint がありません")
    db.save_subscription(sub)
    return {"ok": True, "count": len(db.list_subscriptions())}


class Unsubscribe(BaseModel):
    endpoint: str


@app.post("/api/push/unsubscribe")
def push_unsubscribe(body: Unsubscribe):
    db.delete_subscription(body.endpoint)
    return {"ok": True}


@app.post("/api/push/test")
def push_test():
    """登録済み端末へテスト通知(到達確認用)。"""
    sent = push.notify("帳場", "通知テスト：この経路で即対応をお知らせします", url="/")
    return {"sent": sent, "subscriptions": len(db.list_subscriptions())}


# ---------- PWA 配信 ----------
# sw.js は「/」スコープで動かすためルート直下で配信する(/static 配下だとスコープが狭まる)

@app.get("/sw.js")
def service_worker():
    return FileResponse(os.path.join(STATIC_DIR, "sw.js"), media_type="application/javascript")


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(os.path.join(STATIC_DIR, "manifest.json"),
                        media_type="application/manifest+json")


@app.get("/")
def index():
    """本人が触る PWA (Progressive Web App)。"""
    return FileResponse(os.path.join(STATIC_DIR, "app.html"))


@app.get("/dev")
def dev_ui():
    """旧・簡易テストUI(開発用に残置)。"""
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/simu")
def simulation():
    """営業用シミュレーション(『ある休日の1日』ウォークスルー。自己完結・API不要)。"""
    return FileResponse(os.path.join(STATIC_DIR, "simulation.html"))


# ---------- クイック下書き(テキスト→下書き / iOSショートカット用) ----------
class QuickDraftIn(BaseModel):
    text: str
    contact: str | None = None
    reason: str = "ラリー"
    register: str | None = None      # keigo_only / keigo / mix / casual
    nickname: str | None = None
    token: str | None = None


@app.post("/api/quickdraft")
def quickdraft(body: QuickDraftIn, request: Request):
    _demo_ai_guard(request)
    """相手の本文テキストを受け取り、本人の文体で返信下書き2案を返す。
    iOSショートカット(コピー→この入口へPOST→下書きをクリップボードへ)から叩く想定。
    認証は android/notify と同じ INGEST_TOKEN(未設定なら認証なし)。"""
    from .quickdraft import draft_from_text
    if config.INGEST_TOKEN and not hmac.compare_digest(str(body.token or ""), config.INGEST_TOKEN):
        raise HTTPException(401, "bad token")
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "empty text")
    drafts = draft_from_text(text, body.contact, body.reason, body.register, body.nickname)
    return {
        "drafts": drafts,
        "primary": drafts[0]["text"] if drafts else "",
        "count": len(drafts),
    }


@app.get("/quick")
def quick_page():
    """クイック下書き(相手本文→下書き。ショートカット不要の簡易ページ)。"""
    return FileResponse(os.path.join(STATIC_DIR, "quick.html"))


# ========== CRM: 未紐付けトレイ / エイリアス / カスタム属性 ==========
class TrayResolve(BaseModel):
    line_name: str
    action: str                 # link / new / private
    contact: str | None = None  # link時=既存code / new時=新規code(空なら表示名を使う)
    rank: str = "B"

class AliasIn(BaseModel):
    line_name: str

class AttrDefIn(BaseModel):
    key: str
    type: str = "text"          # choice / text / number / date
    options: str = ""

class AttrValIn(BaseModel):
    key: str
    value: str = ""

class UnmuteIn(BaseModel):
    line_name: str


@app.get("/api/tray")
def tray_list():
    from . import crm
    return {"pending": crm.list_pending(), "muted": crm.list_muted()}


@app.post("/api/tray/resolve")
def tray_resolve(body: TrayResolve):
    from . import crm
    if body.action not in ("link", "new", "private"):
        raise HTTPException(400, "bad action")
    res = crm.resolve_pending(body.line_name, body.action, body.contact, body.rank)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "resolve failed"))
    return res


@app.post("/api/tray/unmute")
def tray_unmute(body: UnmuteIn):
    from . import crm
    crm.unmute(body.line_name)
    return {"ok": True}


@app.get("/api/contacts/{code}/detail")
def contact_detail(code: str):
    from . import crm
    d = crm.contact_detail(code)
    if not d:
        raise HTTPException(404)
    return d


@app.post("/api/contacts/{code}/alias")
def contact_add_alias(code: str, body: AliasIn):
    from . import crm
    if not db.get_contact(code):
        raise HTTPException(404, "contact not found")
    crm.add_alias(body.line_name, code)
    return {"ok": True, "aliases": crm.aliases_for(code)}


class RenameIn(BaseModel):
    new_code: str


@app.post("/api/contacts/{code}/rename")
def contact_rename(code: str, body: RenameIn):
    from . import crm
    res = crm.rename_contact(code, body.new_code)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "rename failed"))
    return res


@app.post("/api/contacts/{code}/alias/remove")
def contact_remove_alias(code: str, body: AliasIn):
    from . import crm
    if not db.get_contact(code):
        raise HTTPException(404, "contact not found")
    return crm.remove_alias(body.line_name, code)


@app.get("/api/attrs")
def attrs_defs():
    from . import crm
    return {"defs": crm.list_defs()}


@app.post("/api/attrs")
def attrs_add(body: AttrDefIn):
    from . import crm
    res = crm.add_def(body.key, body.type, body.options)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "bad def"))
    return res


@app.post("/api/contacts/{code}/attrs")
def contact_set_attr(code: str, body: AttrValIn):
    from . import crm
    if not db.get_contact(code):
        raise HTTPException(404, "contact not found")
    crm.set_attr(code, body.key, body.value)
    return {"ok": True, "attrs": crm.get_attrs(code)}


@app.get("/api/contacts/search")
def contacts_search(q: str = "", attr_key: str = "", attr_val: str = ""):
    from . import crm
    return {"contacts": crm.search_contacts(q, attr_key, attr_val)}


class ContactUpdate(BaseModel):
    rank: str | None = None
    nickname: str | None = None
    register: str | None = None
    note: str | None = None
    tags: str | None = None
    cycle_days: int | None = None
    real_name: str | None = None
    phone: str | None = None
    note_pos: str | None = None
    note_neg: str | None = None
    stand: str | None = None
    birthday: str | None = None
    kids_bday: str | None = None
    founding: str | None = None
    flag_ero: int | None = None   # エロ客スイッチ(いなしモード常時ON)
    flag_koi: int | None = None   # ガチ恋スイッチ(線引きモード常時ON)

@app.post("/api/contacts/{code}")
def contact_update(code: str, body: ContactUpdate):
    from . import crm
    fields = {k: v for k, v in body.dict().items() if v is not None}
    res = crm.update_contact(code, fields)
    if not res.get("ok"):
        raise HTTPException(404, res.get("error", "update failed"))
    return {"ok": True, "contact": crm.contact_detail(code)}

@app.get("/crm")
def crm_page():
    """顧客管理(3層＋未紐付けトレイ＋属性)の実ページ。"""
    return FileResponse(os.path.join(STATIC_DIR, "crm.html"))


@app.get("/seki")
def seki_page():
    """お席の記録→御礼→実績の実ページ。"""
    return FileResponse(os.path.join(STATIC_DIR, "seki.html"))


class InboxClassify(BaseModel):
    contact: str
    action: str   # work(顧客に登録) / private(私用除外)

@app.post("/api/contacts/{code}/delete")
def contact_delete(code: str):
    from . import crm
    if not db.get_contact(code):
        raise HTTPException(404)
    return crm.delete_contact(code)


@app.post("/api/inbox/classify")
def inbox_classify(body: InboxClassify):
    """受信箱の未登録カードの仕分け。work=登録(linked化・別名付け)/private=除外(mute+受信削除)。"""
    from . import crm
    name = (body.contact or "").strip()
    if not name:
        raise HTTPException(400, "contact required")
    if body.action == "work":
        crm.link_contact(name)
        crm.add_alias(name, name)
        return {"ok": True, "contact": name}
    if body.action == "staff":
        crm.mark_staff(name)
        crm.add_alias(name, name)
        return {"ok": True, "contact": name, "kind": "staff"}
    if body.action == "private":
        crm.mute(name)
        crm.discard_unlinked(name)
        return {"ok": True}
    if body.action == "snooze":
        crm.snooze(name, 24)
        crm.discard_unlinked(name)
        return {"ok": True, "snoozed_hours": 24}
    raise HTTPException(400, "bad action")


@app.post("/api/demo/reset")
def demo_reset():
    """デモを完全リセットして最初から再生(受信・下書き・実績＋紐付け/私用/未登録/属性を全消去→再シード→台本を頭から)。デモ専用。"""
    if not config.DEMO:
        raise HTTPException(403, "デモ専用の操作です")
    from . import crm
    db.clear_demo_messages()
    crm.reset_demo()
    seed_initial_contacts()
    try:
        deskservice.SERVICE.replay()   # sim: 台本を頭から再生(watcher再起動)
    except RuntimeError:
        try:
            deskservice.SERVICE.start("demo", 1.0)
            deskservice.SERVICE.approve_login()
        except Exception:
            pass
    return {"ok": True}


# ========== お席（同卓）＝御礼リスト・実績の一次オブジェクト ==========
class MemberIn(BaseModel):
    contact: str
    role: str = "peer"     # customer/intro/peer/after/help/report
    stand: str = "equal"   # senior/equal/junior

class SittingIn(BaseModel):
    date_label: str = ""
    main: str = ""
    stype: str = ""   # ''=お席(店内)/dohan=同伴/after=アフター/gaiso=店外
    venue: str = ""   # 行ったお店(任意)
    members: list[MemberIn] = []

class SentIn(BaseModel):
    contact: str

@app.post("/api/sittings")
def sitting_create(body: SittingIn):
    from . import sittings
    members = [{"contact": m.contact, "role": m.role, "stand": m.stand} for m in body.members]
    sid = sittings.create_sitting(body.date_label, body.main, members,
                                  stype=body.stype, venue=body.venue)
    return {"ok": True, "id": sid}

@app.get("/api/sittings")
def sitting_list():
    from . import sittings
    return {"sittings": sittings.list_sittings()}

@app.get("/api/sittings/{sid}")
def sitting_get(sid: int):
    from . import sittings
    s = sittings.get_sitting(sid)
    if not s:
        raise HTTPException(404)
    return s

@app.post("/api/sittings/{sid}/orei")
def sitting_orei(sid: int):
    from . import sittings
    if not sittings.get_sitting(sid):
        raise HTTPException(404)
    return {"orei": sittings.generate_orei(sid)}

@app.post("/api/sittings/{sid}/sent")
def sitting_sent(sid: int, body: SentIn):
    from . import sittings
    sittings.mark_sent(sid, body.contact)
    return {"ok": True}


# ---------- お席メンバー候補 / 種別変更 / 移籍 / 記念日 ----------
@app.get("/api/roster")
def api_roster(kinds: str = "staff,peer,excolleague"):
    from . import crm
    return {"roster": crm.list_roster(kinds)}


class KindIn(BaseModel):
    kind: str


@app.post("/api/contacts/{code}/kind")
def api_set_kind(code: str, body: KindIn):
    from . import crm
    if body.kind not in ("customer", "staff", "peer", "excolleague"):
        raise HTTPException(400, "kind は customer/staff/peer/excolleague")
    return crm.set_kind(code, body.kind)


class TransferIn(BaseModel):
    from_kind: str = "staff"
    to_kind: str = "excolleague"


@app.post("/api/transfer")
def api_transfer(body: TransferIn):
    """移籍: 店内(staff)を元同僚(excolleague)へ一括付け替え(非破壊)。"""
    from . import crm
    return crm.reclassify(body.from_kind, body.to_kind)


@app.get("/api/anniversaries")
def api_anniversaries(within: int = 14):
    from . import crm
    return {"items": crm.upcoming_anniversaries(within)}
