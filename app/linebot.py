"""帳場くん — LINE公式アカウント窓口(本実装・第1弾)。

デモボットで検証済みの骨格(3層ガード・Flex視認ルール・続きから再開)を、
本物の帳場データに配線する。同一サーバー・同一SQLite=移行不要の「新しい玄関」。

第1弾: 📨返信(実受信+実AI下書き+4択送信確認=学習信号) / 仕分け(顧客/店内/同業/私用) /
       txt取り込み(LINE Content API→既存学習) / 📰ネタ / 🎂記念日 / 📊状況
第2弾(予定): 📣アナウンス配達 / 🙏お席記録→お礼配達
設計出典: claude/帳場くん_第2次ぶん回し知見.md(A1-A7/F1-F5) / 帳場_設計メモ.md

原則:
- 全操作reply(無料)。pushは使わない(通知は既存Webプッシュが鳴らす係)
- 白い素の吹き出し=転送してよい下書きだけ。案内・カードは全部Flex
- OAトークに出すのは「今この1件の最小限」。ガチ恋/いなし等の生ラベルは出さない(記号◆)
- 状態(フロー・カーソル)はDB永続=再起動・中断に耐える(F3)
"""
import base64
import hashlib
import hmac
import json
import os
import threading
import time

import requests
from fastapi import APIRouter, Request, Response

from . import config, db, drafts

CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
API = "https://api.line.me"
API_DATA = "https://api-data.line.me"

router = APIRouter()

GOLD = "#A8842F"; RED = "#C0402C"; BLUE = "#3A5170"; GREEN = "#2f8a4a"; INK = "#2B2823"
FWD = "▼ すぐ下の白い吹き出しが下書き。長押し→転送で送れます"


# ============ 状態(DB永続・F3) ============

def ensure():
    with db.conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS linebot_meta(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS linebot_state(
          user_id TEXT PRIMARY KEY,
          flow TEXT DEFAULT '',
          data TEXT DEFAULT '{}',      -- フロー別カーソル等(JSON)
          updated_ts REAL
        );
        CREATE TABLE IF NOT EXISTS linebot_facts(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          contact TEXT NOT NULL,
          k TEXT NOT NULL,             -- 項目名(誕生日/好きなお酒 等)
          v TEXT NOT NULL,             -- 抽出値
          src TEXT DEFAULT '',         -- 出典(実引用の断片)
          conf TEXT DEFAULT '中',      -- 高/中/低
          alts TEXT DEFAULT '[]',      -- 代替候補(JSON配列)
          status TEXT DEFAULT 'pending',  -- pending/applied/fixed/deleted/skipped
          created_ts REAL
        );
        """)


def _meta_get(k):
    with db.conn() as c:
        r = c.execute("SELECT v FROM linebot_meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else ""


def _meta_set(k, v):
    with db.conn() as c:
        c.execute("INSERT INTO linebot_meta(k,v) VALUES(?,?) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


def get_state(uid):
    ensure()
    with db.conn() as c:
        r = c.execute("SELECT flow, data FROM linebot_state WHERE user_id=?", (uid,)).fetchone()
    if not r:
        return {"flow": "", "data": {}}
    try:
        d = json.loads(r["data"] or "{}")
    except Exception:
        d = {}
    return {"flow": r["flow"] or "", "data": d}


def set_state(uid, flow, data):
    with db.conn() as c:
        c.execute("INSERT INTO linebot_state(user_id,flow,data,updated_ts) VALUES(?,?,?,?) "
                  "ON CONFLICT(user_id) DO UPDATE SET flow=excluded.flow, data=excluded.data, "
                  "updated_ts=excluded.updated_ts",
                  (uid, flow, json.dumps(data, ensure_ascii=False), time.time()))


def owner_id():
    return _meta_get("owner")


# ============ LINE API ============

def _hdr(json_ct=True):
    h = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    if json_ct:
        h["Content-Type"] = "application/json"
    return h


def reply(token, messages):
    if len(messages) > 5:
        messages = messages[:5]
    try:
        r = requests.post(f"{API}/v2/bot/message/reply", headers=_hdr(),
                          json={"replyToken": token, "messages": messages}, timeout=10)
        if r.status_code != 200:
            print(f"[linebot reply {r.status_code}] {r.text[:300]}", flush=True)
        return r.status_code == 200
    except Exception as e:
        print(f"[linebot reply err] {e}", flush=True)
        return False


def loading(uid, seconds=20):
    try:
        requests.post(f"{API}/v2/bot/chat/loading/start", headers=_hdr(),
                      json={"chatId": uid, "loadingSeconds": min(60, max(5, seconds // 5 * 5))},
                      timeout=5)
    except Exception:
        pass


def get_content(message_id) -> bytes:
    """添付ファイル(トーク履歴txt等)の取得。保存保証が無いので即時取得(知見A系)。"""
    r = requests.get(f"{API_DATA}/v2/bot/message/{message_id}/content",
                     headers={"Authorization": f"Bearer {ACCESS_TOKEN}"}, timeout=30)
    r.raise_for_status()
    return r.content


def txt(text, quick=None):
    m = {"type": "text", "text": str(text)[:4900]}
    if quick:
        m["quickReply"] = _quick(quick)
    return m


def _quick(pairs):
    return {"items": [
        {"type": "action", "action": {"type": "postback", "label": l[:20], "data": d[:300],
                                      "displayText": l[:20]}}
        for (l, d) in pairs[:13]]}


def flexmsg(title, body="", accent=GOLD, footer="", quick=None):
    contents = {"type": "bubble", "size": "mega",
                "header": {"type": "box", "layout": "vertical", "backgroundColor": accent,
                           "paddingAll": "12px",
                           "contents": [{"type": "text", "text": str(title)[:200], "color": "#FFFFFF",
                                         "weight": "bold", "size": "sm", "wrap": True}]}}
    bc = []
    if body:
        bc.append({"type": "text", "text": str(body)[:1800], "wrap": True, "size": "sm",
                   "color": INK, "lineSpacing": "6px"})
    if footer:
        bc.append({"type": "text", "text": str(footer)[:300], "wrap": True, "size": "xs",
                   "color": accent, "weight": "bold", "margin": "12px" if body else "none"})
    if bc:
        contents["body"] = {"type": "box", "layout": "vertical", "paddingAll": "13px", "contents": bc}
    m = {"type": "flex", "altText": str(title)[:400], "contents": contents}
    if quick:
        m["quickReply"] = _quick(quick)
    return m


def cover(title, subtitle="", accent=GOLD, quick=None):
    body = [{"type": "text", "text": str(title)[:100], "color": "#FFFFFF",
             "weight": "bold", "size": "lg", "wrap": True}]
    if subtitle:
        body.append({"type": "text", "text": str(subtitle)[:300], "color": "#FFFFFFCC",
                     "size": "xs", "wrap": True, "margin": "8px"})
    m = {"type": "flex", "altText": str(title)[:400], "contents": {
        "type": "bubble", "size": "mega",
        "body": {"type": "box", "layout": "vertical", "backgroundColor": accent,
                 "paddingAll": "18px", "contents": body}}}
    if quick:
        m["quickReply"] = _quick(quick)
    return m


def stamp(text, quick=None):
    return flexmsg(text, accent=GREEN, quick=quick)


def jst_hm():
    return time.strftime("%H:%M", time.gmtime(time.time() + 9 * 3600))


# ============ 実データ部品 ============

def _open_msgs():
    from .notify_ingest import is_call_notice
    return [m for m in db.open_messages() if not is_call_notice(m["text"])]


def build_queue():
    """📨返信キュー: 未対応(open)を相手単位で1件化。急ぎ(urgent)→古い順。
    店内(staff)は含めない(第1弾はPWA側で。第2弾でクイック返信を移植)。"""
    from . import crm
    crm.ensure()
    items, seen = [], set()
    msgs = _open_msgs()
    msgs.sort(key=lambda m: ((0 if m["category"] == "urgent" else 1), m["ts"] or 0))
    for m in msgs:
        c = db.get_contact(m["contact"]) or {}
        kind = c.get("kind") or "customer"
        if kind == "staff":
            continue
        if m["contact"] in seen:
            continue
        seen.add(m["contact"])
        items.append({"mid": m["id"], "contact": m["contact"],
                      "unlinked": 1 if c.get("linked") == 0 or not c else 0,
                      "rank": c.get("rank") or "B",
                      "reason": m.get("reason") or "",
                      "urgent": 1 if m["category"] == "urgent" else 0,
                      "text": (m.get("text") or "")[:1000]})
    return items


def _finish_message(mid, action, sent_text=None):
    """PWAの /api/messages/{mid}/action と同じ意味論(v75まで込み)。
    action: replied(下書きをそのまま転送=学習) / self(自分で書いた=doneと同じ時刻あり返信) /
            deferred / skipped
    """
    msg = db.get_message(mid)
    if not msg:
        return
    status = {"replied": "replied", "self": "replied", "deferred": "deferred",
              "skipped": "skipped"}[action]
    db.set_status(mid, status)
    if status == "replied":
        try:
            with db.conn() as c:
                _thread = [dict(r) for r in c.execute(
                    "SELECT id, contact, ts FROM messages WHERE status IN ('open','deferred')")]
            for _m in _thread:
                if (_m["contact"] == msg["contact"] and _m["id"] != mid
                        and (_m["ts"] or 0) <= (msg["ts"] or 0)):
                    db.set_status(_m["id"], "replied", auto=True)
        except Exception:
            pass
        if action == "replied" and (sent_text or "").strip():
            try:
                from .style_profile import learn_from_sent
                # 4択「案◯を転送した」=そのまま送信が確定した学習信号(1-1の後継)
                learn_from_sent(msg["contact"], sent_text, edited=0, edit_ratio=100)
            except Exception:
                pass
        _kind = (db.get_contact(msg["contact"]) or {}).get("kind", "customer")
        if _kind != "staff":
            _r = msg.get("reason") or ""
            if ("来店" in _r) or ("席" in _r):
                db.add_event(msg["contact"], "visit", f"{msg['contact']} 来店(仮)", "tentative")
            elif ("同伴" in _r) or ("アフター" in _r):
                db.add_event(msg["contact"], "dohan", f"{msg['contact']} 同伴(仮)", "tentative")
        db.track("linebot_reply")


def home_msgs():
    from . import crm, news
    q = build_queue()
    urgent_n = sum(1 for x in q if x["urgent"])
    unlinked_n = sum(1 for x in q if x["unlinked"])
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
    if last_ts:
        mins = int((time.time() - last_ts) / 60)
        moto = ("✅ 正常(最終受信 " + (f"{mins}分前" if mins < 120 else f"{mins//60}時間前") + ")"
                if mins < 12 * 60 else f"⚠️ {mins//60}時間 受信なし(受信係の電池・通知設定を確認)")
    else:
        moto = "―(まだ受信がありません)"
    return [flexmsg("🏮 帳場くん — いまの状況",
                    f"📨 返信待ち {len(q)}件(急ぎ{urgent_n}・未登録{unlinked_n})\n"
                    f"📰 ネタ {neta}件　🎂 記念日 {anni}件\n"
                    f"📡 受信係：{moto}",
                    footer="下のメニューから選んでください👇")]


# ============ 📨 返信フロー ============

def rep_item_msgs(uid, st):
    d = st["data"]
    q = d.get("q") or []
    i = d.get("ri", 0)
    if i >= len(q):
        set_state(uid, "", {})
        return [flexmsg("📨 返信待ちはここまで！おつかれさま👏", accent=GREEN,
                        quick=[("ホームへ", "m=home")])]
    it = q[i]
    if it["unlinked"]:
        return [flexmsg(f"📨 {i+1}/{len(q)}｜🆕 未登録の相手",
                        f"{it['contact']}\n「{it['text']}」\n\nはじめての相手です。まず仕分けてください👇",
                        accent=BLUE, quick=[
                            ("顧客にする", "f=rep&a=cls&v=work"),
                            ("店内", "f=rep&a=cls&v=staff"),
                            ("同業(仲間)", "f=rep&a=cls&v=peer"),
                            ("私用(受け取らない)", "f=rep&a=cls&v=priv"),
                            ("あとで", "f=rep&a=later"),
                        ])]
    # 顧客: 実AI下書き(生ラベルは出さない=◆印のみ・知見C)
    gen = drafts.generate(it["mid"])
    ds = [g.get("text", "") for g in gen][:2]
    d["cur_drafts"] = ds
    set_state(uid, "rep", d)
    mark = "" if not it["urgent"] else "・急ぎ"
    # 受信文の表示: 600字まで。それ以上は「…(続きあり)」+📄全文ボタン(v79)
    full = db.get_message(it["mid"]) or {}
    full_text = full.get("text") or it["text"]
    shown = full_text[:600]
    truncated = len(full_text) > 600
    card = flexmsg(f"📨 {i+1}/{len(q)}｜{it['contact']}（{it['rank']}{mark}）",
                   f"「{shown}" + ("…\n(続きあり→📄全文)" if truncated else "」")
                   + f"\n({it['reason']})",
                   accent=RED if it["urgent"] else GOLD,
                   footer=f"{FWD}【{it['contact']} 宛】")
    msgs = [card] + [txt(x) for x in ds if x]
    quick = [
        ("案1を転送した→次へ", "f=rep&a=d1"),
        ("案2を転送した→次へ", "f=rep&a=d2"),
        ("自分で書いた→次へ", "f=rep&a=self"),
        ("あとで", "f=rep&a=later"),
        ("スキップ", "f=rep&a=skip"),
    ]
    if truncated:
        quick.insert(2, ("📄 全文を見る", "f=rep&a=full"))
    msgs[-1]["quickReply"] = _quick(quick)
    return msgs


def start_rep(uid, token):
    st = get_state(uid)
    if 0 < st["data"].get("ri", 0) < len(st["data"].get("q") or []):
        set_state(uid, "rep", st["data"])   # 再開ボタンが効くようフローを戻す
        return reply(token, [flexmsg(f"📨 前回が {st['data']['ri']+1}件目 で止まっています",
                                     "続きからにしますか？",
                                     quick=[("続きから", "f=rep&a=resume"),
                                            ("最初から", "f=rep&a=restart"),
                                            ("ホームへ", "m=home")])])
    q = build_queue()
    if not q:
        return reply(token, [flexmsg("📨 いま返信待ちはありません✨", accent=GREEN,
                                     quick=[("ホームへ", "m=home")])])
    set_state(uid, "rep", {"q": q, "ri": 0})
    loading(uid, 20)
    st = get_state(uid)
    return reply(token, [cover("📨 いまの返信", f"{len(q)}件を急ぎ順に出します", accent=RED)]
                 + rep_item_msgs(uid, st)[:4])


def rep_action(uid, token, a, p):
    st = get_state(uid)
    d = st["data"]
    q = d.get("q") or []
    i = d.get("ri", 0)
    if st["flow"] != "rep" or i >= len(q):
        return reply(token, wrong_flow(st))
    it = q[i]
    if a == "resume":
        loading(uid, 20)
        return reply(token, rep_item_msgs(uid, st)[:5])
    if a == "full":
        # 📄全文表示(v79): 白い吹き出しにしない(下書きと混ざるため)。カーソルは進めない
        full = (db.get_message(it["mid"]) or {}).get("text") or it["text"]
        msgs = []
        for j in range(0, min(len(full), 5200), 1750):
            msgs.append(flexmsg(f"📄 {it['contact']}からの全文" + (f"({j//1750+1})" if len(full) > 1750 else ""),
                                full[j:j+1750], accent=BLUE))
        msgs = msgs[:4]
        msgs[-1]["quickReply"] = _quick([
            ("案1を転送した→次へ", "f=rep&a=d1"),
            ("案2を転送した→次へ", "f=rep&a=d2"),
            ("自分で書いた→次へ", "f=rep&a=self"),
            ("あとで", "f=rep&a=later"),
            ("スキップ", "f=rep&a=skip"),
        ])
        return reply(token, msgs)
    if a == "restart":
        d["ri"] = 0
        set_state(uid, "rep", d)
        loading(uid, 20)
        return reply(token, rep_item_msgs(uid, get_state(uid))[:5])
    if a == "cls":
        if not it["unlinked"]:
            return reply(token, wrong_flow(st))
        from . import crm
        v = p.get("v", "")
        name = it["contact"]
        if v == "work":
            crm.link_contact(name)
            crm.add_alias(name, name)
            d["await_rank"] = 1
            set_state(uid, "rep", d)
            return reply(token, [flexmsg(f"✓ {name}の顧客カードを作成",
                                         "ランクだけ決めておきますか？(名前以外は空欄でも全部動きます)",
                                         accent=GREEN, quick=[
                                             ("S(太客)", "f=rep&a=rank&v=S"),
                                             ("A", "f=rep&a=rank&v=A"),
                                             ("B(ふつう)", "f=rep&a=rank&v=B"),
                                             ("あとで決める", "f=rep&a=rank&v=skip")])])
        if v == "staff":
            crm.mark_staff(name); crm.add_alias(name, name)
        elif v == "peer":
            db.upsert_contact(name, "B")
            with db.conn() as c:
                c.execute("UPDATE contacts SET kind='peer', linked=1 WHERE code=?", (name,))
            crm.add_alias(name, name)
        elif v == "priv":
            crm.mute(name); crm.discard_unlinked(name)
        lab = {"staff": "店内", "peer": "同業(仲間)", "priv": "私用(受け取らない)"}.get(v, v)
        d["ri"] = i + 1
        set_state(uid, "rep", d)
        loading(uid, 15)
        return reply(token, [stamp(f"✓ {name}を「{lab}」に仕分けました")]
                     + rep_item_msgs(uid, get_state(uid))[:4])
    if a == "rank":
        if not d.pop("await_rank", None):
            return reply(token, wrong_flow(st))
        v = p.get("v", "skip")
        name = it["contact"]
        if v in ("S", "A", "B"):
            with db.conn() as c:
                c.execute("UPDATE contacts SET rank=? WHERE code=?", (v, name))
        it["unlinked"] = 0
        q[i] = it
        d["q"] = q
        set_state(uid, "rep", d)
        loading(uid, 20)
        lab = "ランクは未設定(カードから変更可)" if v == "skip" else f"ランク{v}で登録"
        return reply(token, [stamp(f"✓ {lab}")] + rep_item_msgs(uid, get_state(uid))[:4])
    marks = {"d1": 0, "d2": 1}
    if a in marks:
        ds = d.get("cur_drafts") or []
        sent = ds[marks[a]] if marks[a] < len(ds) else None
        _finish_message(it["mid"], "replied", sent_text=sent)
        stp = stamp(f"✓ {it['contact']}に転送で返信(案{marks[a]+1}) {jst_hm()}｜文体を学習しました")
    elif a == "self":
        _finish_message(it["mid"], "self")
        stp = stamp(f"✓ {it['contact']}に自分の文で返信 {jst_hm()}")
    elif a == "later":
        if not it["unlinked"]:
            _finish_message(it["mid"], "deferred")
        stp = stamp(f"↷ {it['contact']}はあとで(まとめ箱)")
    elif a == "skip":
        _finish_message(it["mid"], "skipped")
        stp = stamp(f"↷ {it['contact']}はスキップ")
    else:
        return reply(token, wrong_flow(st))
    d["ri"] = i + 1
    d.pop("cur_drafts", None)
    set_state(uid, "rep", d)
    loading(uid, 20)
    return reply(token, [stp] + rep_item_msgs(uid, get_state(uid))[:4])


def wrong_flow(st):
    hint = "下のメニューからやり直せます👇"
    if st["flow"] == "rep":
        hint = f"いまは返信対応の途中({st['data'].get('ri',0)+1}件目)です。📨からやり直せます。"
    return [flexmsg("そのボタンは前の画面のものです☺️", hint, accent=BLUE,
                    quick=[("📨 返信", "m=rep"), ("ホームへ", "m=home")])]


# ============ 📰 ネタ / 🎂 記念日 / 📊 状況 ============

def news_msgs():
    from . import news
    try:
        items = news.list_items()[:3]
    except Exception:
        items = []
    if not items:
        return [flexmsg("📰 今日のネタ", "まだありません。顧客カードに会社名を入れると、毎朝ここに集まります。",
                        quick=[("ホームへ", "m=home")])]
    body = "\n\n".join(f"■ {x['contact']}（{x['company']}）\n{x['title']}"
                       + (f"\n→ {x['opener']}" if x.get("opener") else "") for x in items)
    return [flexmsg(f"📰 今日のネタ({len(items)}件)", body, quick=[("ホームへ", "m=home")])]


def anni_msgs():
    from . import crm
    try:
        items = crm.upcoming_anniversaries(14)[:5]
    except Exception:
        items = []
    if not items:
        return [flexmsg("🎂 記念日", "2週間以内の記念日はありません。",
                        quick=[("ホームへ", "m=home")])]
    body = "\n".join(f"・{x.get('contact','')}：{x.get('label') or x.get('kind','')}（{x.get('when') or x.get('date','')}）"
                     for x in items)
    return [flexmsg("🎂 近い記念日", body,
                    footer="お祝い文はPWAの実績→記念日から(第2弾でここに来ます)",
                    quick=[("ホームへ", "m=home")])]


def dash_msgs():
    q = build_queue()
    with db.conn() as c:
        week = time.time() - 7 * 86400
        sent_n = c.execute("SELECT COUNT(*) FROM sent_replies WHERE ts>=?", (week,)).fetchone()[0]
        verb = c.execute("SELECT COUNT(*) FROM sent_replies WHERE ts>=? AND IFNULL(edited,0)=0",
                         (week,)).fetchone()[0]
        nsamp = c.execute("SELECT COUNT(*) FROM sent_replies").fetchone()[0]
    rate = f"{round(verb/sent_n*100)}%" if sent_n else "―"
    return [flexmsg("📊 今日の状況",
                    f"📨 返信待ち：{len(q)}件\n✍️ 文体サンプル：{nsamp}文\n"
                    f"📈 今週：送信{sent_n}件・そのまま率{rate}",
                    quick=[("📨 返信", "m=rep"), ("ホームへ", "m=home")])]


# ============ txt取り込み ============

def handle_file(uid, token, message):
    name = message.get("fileName") or "ファイル"
    if not name.lower().endswith(".txt"):
        return reply(token, [flexmsg("📄 受け取りました", f"「{name}」\nトーク履歴(.txt)なら学習に使えます。",
                                     quick=[("ホームへ", "m=home")])])
    loading(uid, 40)
    try:
        raw = get_content(message.get("id"))
        text = raw.decode("utf-8", errors="replace")
    except Exception as e:
        return reply(token, [flexmsg("📄 取り込みに失敗しました", f"もう一度送ってみてください({type(e).__name__})",
                                     accent=RED, quick=[("ホームへ", "m=home")])])
    # 既存の取り込みロジック(v63表示名自動判定・自動登録)をそのまま使う
    from .style_profile import extract_profile, discover_contacts, extract_contact_profile
    from .main import _infer_self_name
    from . import crm
    self_name = _infer_self_name(text, (db.get_profile("_selfname") or {}).get("name") or "自分")
    p = extract_profile(text, self_name=self_name)
    if p.n_messages == 0:
        return reply(token, [flexmsg("📄 読み込めませんでした",
                                     "あなたの発言を見つけられませんでした。PWAの設定→取り込みからお名前を指定して試してください。",
                                     accent=RED, quick=[("ホームへ", "m=home")])])
    db.save_profile("_global", p.to_dict())
    db.save_profile("_selfname", {"name": self_name})
    registered, profiled = [], []
    for nm in discover_contacts(text, self_name=self_name):
        cp = extract_contact_profile(text, nm, self_name=self_name)
        db.save_profile(nm, cp)
        profiled.append(nm)
        if not db.get_contact(nm):
            db.upsert_contact(nm, "B")
            crm.link_contact(nm)
            crm.add_alias(nm, nm)
            registered.append(nm)
    db.track("linebot_txt_import")
    # 📇 事実抽出(v78): 相手ごとにLLMで誕生日・好み等を掘り、確認待ちに積む
    n_facts = 0
    for nm in profiled[:3]:
        try:
            facts = extract_facts(text, nm, self_name)
            save_facts(nm, facts)
            n_facts += len(facts)
        except Exception as e:
            print(f"[linebot facts save] {e}", flush=True)
    n_pend = len(pending_facts())
    body = (f"✓ あなた={self_name} として {p.n_messages}文を学習\n"
            f"✓ 相手別の口調: {len(profiled)}人分\n"
            + (f"✓ 新規カード: {'・'.join(registered[:5])}" if registered else "✓ 既存カードに紐付けました")
            + (f"\n🔎 カードに載せられそうな情報を{n_facts}件見つけました" if n_facts else ""))
    quick = [("ホームへ", "m=home")]
    if n_pend:
        quick.insert(0, (f"🔎 抽出を確認({n_pend}件)", "m=fact"))
    return reply(token, [flexmsg(f"📄 「{name}」を取り込みました", body, accent=GREEN,
                                 quick=quick)])


# ============ 📇 txt抽出→タップ確認(カード整備) ============

FACT_KEYS = ("呼び名", "誕生日", "好きなお酒", "好きな食べ物", "仕事", "記念日",
             "同伴・アフター", "注意点", "その他")


def extract_facts(text, partner, self_name):
    """トーク履歴から顧客カード向けの事実をLLM抽出。失敗時は[]（取り込み自体は成功扱い）。"""
    if not config.ANTHROPIC_API_KEY:
        return []
    talk = text[-48000:]
    prompt = (
        f"以下は{self_name}(ホステス)と{partner}(相手)のLINEトーク履歴。"
        f"{partner}の顧客カードに載せる事実だけを抽出せよ。\n"
        f"項目名は次から選ぶ: {'/'.join(FACT_KEYS)}\n"
        "ルール:\n"
        "- 履歴に根拠のある事実のみ。推測で作らない。無ければ出さない\n"
        f"- 呼び名={self_name}が{partner}を実際どう呼んでいるか\n"
        "- src=根拠となる実際の発言の断片(40字以内)\n"
        "- conf=高(複数回/明言)・中(1回だが明確)・低(弱い根拠)\n"
        "- alts=同じ項目の別解釈があれば最大2つ\n"
        "- 最大8項目\n"
        '出力はJSON配列のみ: [{"k":"誕生日","v":"8月19日","src":"来週誕生日なんだ","conf":"高","alts":[]}]\n'
        f"---\n{talk}"
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": config.ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": config.ANTHROPIC_MODEL, "max_tokens": 1200,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=45)
        r.raise_for_status()
        out = "".join(b.get("text", "") for b in r.json().get("content", []))
        out = out[out.index("["):out.rindex("]") + 1]
        facts = json.loads(out)
    except Exception as e:
        print(f"[linebot facts] {type(e).__name__}: {e}", flush=True)
        return []
    ok = []
    for f in facts[:8]:
        k = str(f.get("k", "")).strip()
        v = str(f.get("v", "")).strip()
        if not k or not v or k not in FACT_KEYS:
            continue
        ok.append({"k": k, "v": v[:80], "src": str(f.get("src", ""))[:60],
                   "conf": f.get("conf") if f.get("conf") in ("高", "中", "低") else "中",
                   "alts": [str(a)[:40] for a in (f.get("alts") or [])[:2]]})
    return ok


def save_facts(contact, facts):
    ensure()
    with db.conn() as c:
        for f in facts:
            # 同じ(contact,k,v)のpendingが既にあれば重複保存しない
            r = c.execute("SELECT id FROM linebot_facts WHERE contact=? AND k=? AND v=? "
                          "AND status='pending'", (contact, f["k"], f["v"])).fetchone()
            if r:
                continue
            c.execute("INSERT INTO linebot_facts(contact,k,v,src,conf,alts,status,created_ts) "
                      "VALUES(?,?,?,?,?,?, 'pending', ?)",
                      (contact, f["k"], f["v"], f["src"], f["conf"],
                       json.dumps(f["alts"], ensure_ascii=False), time.time()))


def pending_facts():
    ensure()
    with db.conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM linebot_facts WHERE status='pending' ORDER BY id")]


def _set_fact_status(fid, status):
    with db.conn() as c:
        c.execute("UPDATE linebot_facts SET status=? WHERE id=?", (status, fid))


def _get_fact(fid):
    with db.conn() as c:
        r = c.execute("SELECT * FROM linebot_facts WHERE id=?", (fid,)).fetchone()
        return dict(r) if r else None


def apply_fact(contact, k, v):
    """確定した事実を実カードへ書き込む。"""
    from . import crm
    if k == "誕生日":
        with db.conn() as c:
            c.execute("UPDATE contacts SET birthday=? WHERE code=?", (v, contact))
    elif k == "呼び名":
        try:
            crm.add_alias(contact, v)
        except Exception:
            pass
        crm.add_def("呼び名")
        crm.set_attr(contact, "呼び名", v)
    else:
        crm.add_def(k)
        crm.set_attr(contact, k, v)
    db.track("linebot_fact_apply")


def fact_card(token, prefix=None):
    """次のpending項目を1枚カードで出す。無ければ完了。"""
    pend = pending_facts()
    head = prefix or []
    if not pend:
        with db.conn() as c:
            done = c.execute("SELECT COUNT(*) FROM linebot_facts WHERE status IN "
                             "('applied','fixed')").fetchone()[0]
        return reply(token, head + [flexmsg("📇 カード整備、完了です",
                                            f"確認済みの項目は顧客カードに反映済み(累計{done}件)。"
                                            "カードは🗂顧客タブから見られます。",
                                            accent=GREEN,
                                            quick=[("🗂 顧客を見る", "m=crm"), ("ホームへ", "m=home")])])
    f = pend[0]
    n = len(pend)
    dots = {"高": "●●●", "中": "●●○", "低": "●○○"}[f["conf"]]
    return reply(token, head + [flexmsg(
        f"📇 {f['contact']}｜{f['k']}（残り{n}件）",
        f"【{f['v']}】\n\n出典:「{f['src']}」\n確信度: {dots}",
        quick=[("○ 合ってる", f"f=fact&a=ok&i={f['id']}"),
               ("✕ 違う", f"f=fact&a=no&i={f['id']}"),
               ("スキップ", f"f=fact&a=skip&i={f['id']}"),
               ("やめる(続きは🔎から)", "m=home")])])


def fact_fix_card(uid, token, f):
    set_state(uid, "factfix", {"fid": f["id"]})
    try:
        alts = json.loads(f["alts"] or "[]")
    except Exception:
        alts = []
    quick = [(a[:20], f"f=fact&a=alt&i={f['id']}&j={j}") for j, a in enumerate(alts)]
    quick.append(("この項目を消す", f"f=fact&a=del&i={f['id']}"))
    return reply(token, [flexmsg(f"✕ では「{f['k']}」の正しい内容は？",
                                 "候補をタップするか、そのまま正しい内容をタイプして送ってください。",
                                 accent=RED, quick=quick)])


def fact_action(uid, token, a, p):
    fid = p.get("i", "")
    f = _get_fact(int(fid)) if str(fid).isdigit() else None
    if not f or f["status"] != "pending":
        return fact_card(token, prefix=[flexmsg("そのボタンは処理済みです☺️", accent=BLUE)])
    if a == "ok":
        apply_fact(f["contact"], f["k"], f["v"])
        _set_fact_status(f["id"], "applied")
        set_state(uid, "", {})
        return fact_card(token, prefix=[stamp(f"✓ {f['k']}＝{f['v']} で反映")])
    if a == "no":
        return fact_fix_card(uid, token, f)
    if a == "alt":
        try:
            v = json.loads(f["alts"] or "[]")[int(p.get("j", "0"))]
        except Exception:
            return fact_fix_card(uid, token, f)
        apply_fact(f["contact"], f["k"], v)
        with db.conn() as c:
            c.execute("UPDATE linebot_facts SET status='fixed', v=? WHERE id=?", (v, f["id"]))
        set_state(uid, "", {})
        return fact_card(token, prefix=[stamp(f"✓ {f['k']}を「{v}」に直して反映")])
    if a == "del":
        _set_fact_status(f["id"], "deleted")
        set_state(uid, "", {})
        return fact_card(token, prefix=[stamp(f"✕ {f['k']}は消しました(カードに載せません)")])
    if a == "skip":
        _set_fact_status(f["id"], "skipped")
        set_state(uid, "", {})
        return fact_card(token, prefix=[stamp(f"↷ {f['k']}はあとで(PWAでも直せます)")])
    return fact_card(token)


def fact_typed(uid, token, text):
    """✕違う→自由入力で確定。"""
    st = get_state(uid)
    fid = st["data"].get("fid")
    f = _get_fact(fid) if fid else None
    set_state(uid, "", {})
    if not f or f["status"] != "pending":
        return fact_card(token, prefix=[flexmsg("入力先の項目が見つかりませんでした", accent=BLUE)])
    v = text.strip()[:80]
    apply_fact(f["contact"], f["k"], v)
    with db.conn() as c:
        c.execute("UPDATE linebot_facts SET status='fixed', v=? WHERE id=?", (v, f["id"]))
    return fact_card(token, prefix=[stamp(f"✓ {f['k']}を「{v}」に直して反映")])


# ============ 🗂 顧客タブ(LINE内カード閲覧) ============

from urllib.parse import quote as _q, unquote as _uq

PAGE = 11


def _crm_contacts():
    from . import crm
    crm.ensure()
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT code, rank, kind, last_visit_ts, birthday FROM contacts "
            "WHERE IFNULL(linked,1)!=0 AND IFNULL(kind,'customer') IN ('customer','peer') "
            "ORDER BY CASE rank WHEN 'S' THEN 0 WHEN 'A' THEN 1 ELSE 2 END, code")]
    return rows


def crm_list_msgs(pg=0):
    rows = _crm_contacts()
    if not rows:
        return [flexmsg("🗂 まだ顧客カードがありません",
                        "トーク履歴の.txtをこのトークに送ると、自動で作られます。",
                        quick=[("ホームへ", "m=home")])]
    total = len(rows)
    by_rank = {"S": 0, "A": 0, "B": 0}
    for r in rows:
        by_rank[r["rank"] if r["rank"] in by_rank else "B"] += 1
    page = rows[pg * PAGE:(pg + 1) * PAGE]
    quick = [(f"{r['rank']}｜{r['code']}"[:20], f"m=card&c={_q(r['code'], safe='')}") for r in page]
    if (pg + 1) * PAGE < total:
        quick.append((f"次の{min(PAGE, total-(pg+1)*PAGE)}人 →", f"m=crm&pg={pg+1}"))
    quick.append(("ホームへ", "m=home"))
    names = "\n".join(f"・{r['rank']}｜{r['code']}" for r in page)
    return [flexmsg(f"🗂 顧客カード {total}人（S{by_rank['S']}・A{by_rank['A']}・B{by_rank['B']}）",
                    names, footer="下のボタンで開きます👇", quick=quick)]


def card_msgs(code):
    from . import crm
    d = crm.contact_detail(code)
    if not d:
        return [flexmsg("カードが見つかりませんでした", accent=BLUE, quick=[("🗂 一覧へ", "m=crm")])]
    lines = []
    kind_lab = {"customer": "顧客", "staff": "店内", "peer": "同業", "private": "私用"}.get(
        d.get("kind") or "customer", "顧客")
    lines.append(f"ランク: {d.get('rank') or 'B'}　種別: {kind_lab}")
    if d.get("birthday"):
        lines.append(f"🎂 誕生日: {d['birthday']}")
    if d.get("last_visit_ts"):
        days = int((time.time() - d["last_visit_ts"]) / 86400)
        lines.append(f"🍶 最終来店: {days}日前")
    if d.get("cycle_days"):
        lines.append(f"周期: {d['cycle_days']}日")
    if d.get("tags"):
        lines.append(f"タグ: {d['tags']}")
    for k, v in list((d.get("attrs") or {}).items())[:8]:
        lines.append(f"{k}: {v}")
    if d.get("note"):
        lines.append(f"メモ: {d['note'][:100]}")
    al = [a for a in (d.get("aliases") or []) if a != code]
    if al:
        lines.append(f"別名: {'・'.join(al[:3])}")
    if len(lines) <= 1:
        lines.append("(まだ情報が少ないです。txt取り込みや🔎整備で貯まります)")
    cq = _q(code, safe="")
    return [flexmsg(f"🗂 {code}", "\n".join(lines),
                    quick=[("ランクS", f"f=crank&c={cq}&v=S"),
                           ("ランクA", f"f=crank&c={cq}&v=A"),
                           ("ランクB", f"f=crank&c={cq}&v=B"),
                           ("🗂 一覧へ", "m=crm"), ("ホームへ", "m=home")])]


# ============ ルーター ============

def route_postback(uid, data, token):
    p = dict(kv.split("=", 1) for kv in (data or "").split("&") if "=" in kv)
    m = p.get("m")
    # ✕違う→入力待ち(factfix)のまま別画面へ移動したら、入力待ちを解除する
    # (次に打った無関係な文字を修正値として誤って食わないため)
    if get_state(uid)["flow"] == "factfix" and p.get("f") != "fact":
        set_state(uid, "", {})
    if m == "home":
        st = get_state(uid)
        set_state(uid, "", st["data"])   # カーソルは保持(📨で「続きから」を出せる)
        return reply(token, home_msgs())
    if m == "unbind2":
        _meta_set("owner", "")
        set_state(uid, "", {})
        return reply(token, [flexmsg("🔓 解除しました",
                                     "次に合言葉(玄関パスワード)を送った人が、"
                                     "この帳場くんの利用者になります。", accent=GREEN)])
    if m == "rep":
        return start_rep(uid, token)
    if m == "crm":
        try:
            pg = int(p.get("pg", "0"))
        except Exception:
            pg = 0
        db.track("linebot_crm")
        return reply(token, crm_list_msgs(pg))
    if m == "card":
        return reply(token, card_msgs(_uq(p.get("c", ""))))
    if m == "fact":
        n = len(pending_facts())
        if not n:
            return reply(token, [flexmsg("🔎 確認待ちの抽出はありません",
                                         "トーク履歴の.txtを送ると、カードに載せられそうな情報を拾って"
                                         "ここに並べます。",
                                         quick=[("🗂 顧客を見る", "m=crm"), ("ホームへ", "m=home")])])
        return fact_card(token, prefix=[cover("🔎 カード整備",
                                              f"抽出した{n}件を確認します。全部タップ、1件5秒")])
    if m == "news":
        return reply(token, news_msgs())
    if m == "anni":
        return reply(token, anni_msgs())
    if m == "dash":
        return reply(token, dash_msgs())
    if m in ("ann", "orei"):
        return reply(token, [flexmsg("🚧 ここは第2弾で開通します",
                                     "アナウンス配達とお席→お礼は数日内にこの場所に来ます。それまではPWA(帳場アプリ)からどうぞ。",
                                     accent=BLUE, quick=[("📨 返信", "m=rep"), ("ホームへ", "m=home")])])
    f = p.get("f")
    if f == "rep":
        return rep_action(uid, token, p.get("a", ""), p)
    if f == "fact":
        return fact_action(uid, token, p.get("a", ""), p)
    if f == "crank":
        code = _uq(p.get("c", ""))
        v = p.get("v", "B")
        if v in ("S", "A", "B") and db.get_contact(code):
            with db.conn() as c:
                c.execute("UPDATE contacts SET rank=? WHERE code=?", (v, code))
            return reply(token, [stamp(f"✓ {code}をランク{v}にしました")] + card_msgs(code))
        return reply(token, card_msgs(code))
    return reply(token, home_msgs())


_SEEN: dict = {}


def _dedup(eid):
    now = time.time()
    for k, t in list(_SEEN.items()):
        if now - t > 600:
            del _SEEN[k]
    if eid and eid in _SEEN:
        return False
    if eid:
        _SEEN[eid] = now
    return True


def handle_event(ev):
    etype = ev.get("type")
    token = ev.get("replyToken", "")
    uid = (ev.get("source") or {}).get("userId", "")
    if not _dedup(ev.get("webhookEventId", "")):
        return
    try:
        _handle(ev, etype, token, uid)
    except Exception as e:
        print(f"[linebot error] {type(e).__name__}: {e}", flush=True)
        try:
            if token:
                reply(token, [flexmsg("ごめんなさい、エラーが起きました🙇‍♀️",
                                      "ホームからやり直してください。",
                                      accent=RED, quick=[("ホームへ", "m=home")])])
        except Exception:
            pass


def _handle(ev, etype, token, uid):
    ensure()
    owner = owner_id()
    # ---- 所有者バインド(1インスタンス=1人。合言葉=玄関パスワード) ----
    if owner and uid != owner:
        if token:
            reply(token, [txt("このアカウントは利用者専用です。")])
        return
    if not owner:
        if etype == "message" and (ev.get("message") or {}).get("type") == "text":
            t = ((ev.get("message") or {}).get("text") or "").strip()
            if config.PASSWORD and hmac.compare_digest(t.encode("utf-8"), config.PASSWORD.encode("utf-8")):
                _meta_set("owner", uid)
                reply(token, [flexmsg("🔑 ひも付けが完了しました", "この帳場くんはあなた専用になりました。",
                                      accent=GREEN)] + home_msgs())
                return
            if not config.PASSWORD:
                _meta_set("owner", uid)   # 開発時のみ(パスワード未設定)
                reply(token, [flexmsg("🔑 (開発モード)ひも付け完了", accent=GREEN)] + home_msgs())
                return
        if token:
            reply(token, [flexmsg("🔑 はじめに合言葉をどうぞ",
                                  "帳場アプリと同じ「玄関パスワード」をこのトークに送ってください。")])
        return

    if etype == "follow":
        reply(token, [cover("🏮 帳場くん", "あなたの夜の、静かな秘書。")] + home_msgs())
        return
    if etype == "postback":
        route_postback(uid, (ev.get("postback") or {}).get("data", ""), token)
        return
    if etype == "message":
        msg = ev.get("message") or {}
        mtype = msg.get("type")
        if mtype == "file":
            handle_file(uid, token, msg)
            return
        if mtype == "text":
            t = (msg.get("text") or "").strip()
            if t in ("ホーム", "メニュー", "home"):
                set_state(uid, "", {})
                reply(token, home_msgs())
            elif get_state(uid)["flow"] == "factfix":
                fact_typed(uid, token, t)
            elif t in ("顧客", "カード"):
                reply(token, crm_list_msgs(0))
            elif t in ("整備", "抽出"):
                route_postback(uid, "m=fact", token)
            elif t == "ひも付け解除":
                reply(token, [flexmsg("🔓 ひも付けを解除しますか?",
                                      "解除すると、次に合言葉(玄関パスワード)を送った人が"
                                      "新しい利用者になります。\n(携帯の変更・引き渡しのとき用)",
                                      accent=RED,
                                      quick=[("はい、解除する", "m=unbind2"), ("やめる", "m=home")])])
            else:
                reply(token, [flexmsg("タイプは不要です☺️",
                                      "下のメニューかボタンで操作してください👇\n(トーク履歴の.txtを送ると口調を学習します)",
                                      quick=[("ホームへ", "m=home")])])
            return
        reply(token, [flexmsg("☺️", "下のメニューからどうぞ👇", quick=[("ホームへ", "m=home")])])
        return


def _verify(body, signature):
    if not CHANNEL_SECRET:
        return False
    mac = hmac.new(CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(mac).decode(), signature or "")


@router.post("/line/webhook")
async def line_webhook(request: Request):
    body = await request.body()
    if not _verify(body, request.headers.get("X-Line-Signature", "")):
        return Response(status_code=403)
    try:
        events = json.loads(body).get("events", [])
    except Exception:
        events = []
    for ev in events:
        threading.Thread(target=handle_event, args=(ev,), daemon=True).start()
    return {"ok": True}


# ============ リッチメニュー(画像は同梱の静的PNG=実行時PIL不要) ============

MENU_ITEMS = [
    ("返信", "m=rep"), ("顧客", "m=crm"), ("ネタ", "m=news"), ("状況", "m=dash"),
    ("アナウンス", "m=ann"), ("お礼", "m=orei"), ("記念日", "m=anni"), ("整備", "m=fact"),
]
MENU_COLS = 4


@router.get("/line/setup")
@router.post("/line/setup")
def line_setup(key: str = ""):
    """リッチメニュー作成。デプロイ後にブラウザで /line/setup?key=<INGEST_TOKEN> を開く。"""
    if not config.INGEST_TOKEN or key != config.INGEST_TOKEN:
        return Response(status_code=403)
    img_path = os.path.join(os.path.dirname(__file__), "static", "lineimg", "richmenu.png")
    if not os.path.exists(img_path):
        return {"error": "richmenu.png がありません"}
    cw, ch = 2496 // MENU_COLS, 842 // 2
    areas = []
    for idx, (_, data) in enumerate(MENU_ITEMS):
        x, y = (idx % MENU_COLS) * cw, (idx // MENU_COLS) * ch
        areas.append({"bounds": {"x": x, "y": y, "width": cw, "height": ch},
                      "action": {"type": "postback", "data": data}})
    r = requests.post(f"{API}/v2/bot/richmenu", headers=_hdr(), json={
        "size": {"width": 2496, "height": 842}, "selected": True,
        "name": "chouba-linebot", "chatBarText": "メニュー", "areas": areas}, timeout=10)
    if r.status_code != 200:
        return {"step": "create", "status": r.status_code, "body": r.text[:300]}
    rid = r.json()["richMenuId"]
    with open(img_path, "rb") as fh:
        r2 = requests.post(f"{API_DATA}/v2/bot/richmenu/{rid}/content",
                           headers={"Authorization": f"Bearer {ACCESS_TOKEN}",
                                    "Content-Type": "image/png"},
                           data=fh.read(), timeout=20)
    if r2.status_code != 200:
        return {"step": "image", "status": r2.status_code, "body": r2.text[:300]}
    r3 = requests.post(f"{API}/v2/bot/user/all/richmenu/{rid}", headers=_hdr(), timeout=10)
    return {"ok": r3.status_code == 200, "richMenuId": rid}
