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
        CREATE TABLE IF NOT EXISTS linebot_talks(
          contact TEXT PRIMARY KEY,    -- 相手ごとの最新txt原文(掘り直し用)
          text TEXT NOT NULL,
          ts REAL
        );
        CREATE TABLE IF NOT EXISTS linebot_persona(
          contact TEXT PRIMARY KEY,
          data TEXT NOT NULL,          -- {summary, sections:[{k,v,src,conf}]} JSON
          ts REAL
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
        # 後付け列の保証(新品DB・古いDBのどちらでも落ちない)
        for tbl, ddl in (("sent_replies", "edited INTEGER DEFAULT 0"),
                         ("sent_replies", "edit_ratio INTEGER DEFAULT 100"),
                         ("messages", "acted_ts REAL"),
                         ("messages", "swept INTEGER DEFAULT 0")):
            try:
                c.execute(f"ALTER TABLE {tbl} ADD COLUMN {ddl}")
            except Exception:
                pass


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


def urgent_push_enabled():
    """v123: 緊急のみLINE push(既定ON)。'0'でOFF。"""
    return _meta_get("urgent_push") != "0"


def set_urgent_push(on):
    _meta_set("urgent_push", "1" if on else "0")


def _lp_month_key():
    return "lp_" + time.strftime("%Y%m")


def urgent_push_count():
    try:
        return int(_meta_get(_lp_month_key()) or 0)
    except Exception:
        return 0


URGENT_PUSH_CAP = 150   # 無料枠200通/月のうち緊急用に150。残りはカード完成通知等の予備


def push_urgent(contact, reason):
    """v123: 即対応の受信をLINEチャットに1通で知らせる(Web Pushが購読切れでも届く経路)。
    月間上限で自動停止=枠切れによる想定外課金を防ぐ。"""
    if not urgent_push_enabled():
        return False
    n = urgent_push_count()
    if n >= URGENT_PUSH_CAP:
        if n == URGENT_PUSH_CAP:
            _meta_set(_lp_month_key(), str(n + 1))
            push_owner([{"type": "text",
                         "text": f"🔔 今月の緊急通知が上限({URGENT_PUSH_CAP}通)に達したため、"
                                 "月末まで通知を止めます。受信箱は通常どおり動いています。"}])
        return False
    ok = push_owner([{"type": "text",
                      "text": f"🔥 {_yobina(contact)}さんから急ぎの気配({reason})。"
                              "メニューの📨返信から下書きを確認できます。"}])
    if ok:
        _meta_set(_lp_month_key(), str(n + 1))
    return ok


def push_owner(messages):
    """本人へpush(課金対象・無料枠200通/月)。v96: LIFF通知用。使いすぎない。"""
    uid = owner_id()
    if not uid:
        return False
    if len(messages) > 5:
        messages = messages[:5]
    try:
        r = requests.post(f"{API}/v2/bot/message/push", headers=_hdr(),
                          json={"to": uid, "messages": messages}, timeout=10)
        if r.status_code != 200:
            print(f"[linebot push {r.status_code}] {r.text[:300]}", flush=True)
        return r.status_code == 200
    except Exception as e:
        print(f"[linebot push err] {e}", flush=True)
        return False


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
                      "ts": m.get("ts"),   # v120: 受信時刻(いつ来たかの表示に必須)
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
    # 秘書として能動的に浮上: ご無沙汰の太客(S/A)
    try:
        est = estranged()
        est_sa = [e for e in est if e["rank"] in ("S", "A")]
    except Exception:
        est, est_sa = [], []
    gb_line = ""
    quick = None
    if est_sa:
        top = est_sa[0]
        gb_line = f"\n🕰 ご無沙汰の太客 {len(est_sa)}人(最長 {_yobina(top['code'])} {top['gap']}日)"
        quick = [("📣 ご無沙汰に挨拶", "f=ann&a=plan&v=GB"), ("📨 返信", "m=rep"),
                 ("🗂 顧客", "m=crm")]
    return [flexmsg("🏮 帳場くん — いまの状況",
                    f"📨 返信待ち {len(q)}件(急ぎ{urgent_n}・未登録{unlinked_n})\n"
                    f"📰 ネタ {neta}件　🎂 記念日 {anni}件" + gb_line + "\n"
                    f"📡 受信係：{moto}",
                    footer="下のメニューから選んでください👇", quick=quick)]


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
    n_prof = (db.get_profile("_global") or {}).get("n_messages") or 0
    pon = persona_enabled()
    return [flexmsg("📊 今日の状況",
                    f"📨 返信待ち：{len(q)}件\n"
                    f"✍️ 文体の学習：txtから{n_prof}文＋送信から{nsamp}文\n"
                    f"📈 今週：送信{sent_n}件・そのまま率{rate}\n"
                    f"🧠 ペルソナ分析：{'ON' if pon else 'OFF'}",
                    quick=[("📨 返信", "m=rep"), ("✍️ 文体を見る", "m=style"),
                           (f"🧠 ペルソナを{'OFF' if pon else 'ON'}に", "m=ptoggle"),
                           ("🗂 顧客", "m=crm"), ("ホームへ", "m=home")])]


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
    # 📄 原文を保存 → 🔎 抽出はバックグラウンドで(v80: reply1分制限と分離・無言失敗の根絶)
    _lt = parse_last_talk_ts(text)
    for nm in profiled[:3]:
        save_talk(nm, text)
        if _lt:
            _meta_set(f"lasttalk_{nm}", str(_lt))
        dig_async(nm)
    who = "・".join(profiled[:3]) or "相手"
    body = (f"✓ あなた={self_name} として {p.n_messages}文を学習\n"
            f"✓ {who} の口調・話題を記憶\n"
            + (f"✓ 新規カード: {'・'.join(registered[:5])}" if registered
               else f"✓ {who} の既存カードを更新\n")
            + ("\n🔎 AIが情報を整理しています(30秒〜1分)。"
               "できあがったら「✎ カードができました」で1通お知らせします。"
               "タップすると編集画面が開きます☺️"
               if os.environ.get("CHOUBA_LIFF_ID")
               else "\n🔎 AIが情報を掘っています(30秒〜1分)。"
                    "重要項目(呼び名・誕生日・種別)だけ確認をお願いします。"
                    "他は自動でカードに反映され、後から🧹見直せます👇"))
    quick = [("🔎 確認する", "m=fact"),
             (f"🗂 {profiled[0]}のカード"[:20], f"m=card&c={_q(profiled[0], safe='')}") if profiled else ("🗂 顧客", "m=crm"),
             ("ホームへ", "m=home")]
    return reply(token, [flexmsg(f"📄 「{name}」を取り込みました", body, accent=GREEN,
                                 quick=quick)])


# ============ 📇 txt抽出→タップ確認(カード整備) ============

FACT_KEYS = ("呼び名", "本名", "年齢", "誕生日", "仕事・会社", "家族", "資産・事業",
             "好きなお酒", "好きな食べ物", "趣味・関心", "健康", "記念日",
             "進行中の話", "NG話題", "関係性メモ", "担当", "お気に入りキャスト",
             "住まい・エリア", "その他")

# 超重要=1件ずつ○✕確認する項目。それ以外は自動でカードに反映し、後で「見直す」で修正/削除。
CRITICAL_KEYS = ("呼び名", "本名", "誕生日")   # _REL_KEY も後で加える

CHUNK = 42000   # 1回の掘りで読む文字数(長文は分割して全文を読む)


def extract_facts(text, partner, self_name):
    """トーク履歴から顧客カード向けの事実をLLM抽出。長文は分割して全文を読む(v83)。
    戻り値: (facts, err)。err=Noneなら成功(0件もあり得る)。"""
    if not config.ANTHROPIC_API_KEY:
        return [], "APIキー未設定"
    chunks = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)][:4]  # 最大約17万字
    allf, first_err = [], None
    for idx, ch in enumerate(chunks):
        f, e = _extract_chunk(ch, partner, self_name, idx + 1, len(chunks))
        if e and first_err is None:
            first_err = e
        allf.extend(f)
    # 重複統合(同じ項目・同じ値)
    seen, merged = set(), []
    for f in allf:
        key = (f["k"], f["v"])
        if key in seen:
            continue
        seen.add(key)
        merged.append(f)
    if not merged and first_err:
        return [], first_err
    return merged[:40], None


def _extract_chunk(talk, partner, self_name, part, total):
    pos = f"(全{total}分割の{part}番目・時系列順)" if total > 1 else ""
    prompt = (
        f"以下は{self_name}と{partner}(お客様)のLINEトーク履歴{pos}です。"
        f"{partner}の顧客カード・営業台帳に載せる価値のある事実を抽出してください。\n"
        f"項目名の例: {'/'.join(FACT_KEYS)}(これ以外も自由に使ってよい)\n"
        "ルール:\n"
        f"- 【最重要】事実の主語は必ず{partner}。{self_name}(利用者本人)自身の情報"
        f"(本人の仕事・事業・家族・趣味・健康・予定・持ち物)は価値があっても絶対に抽出しない。"
        f"会話が{self_name}の話題中心なら、{partner}の事実だけを少数返すか空配列にする\n"
        f"- 各項目に sub を付ける: その事実の主語。\"相手\"({partner}のこと)のみ許可。"
        f"{self_name}のことなら sub:\"自分\" とする(後で機械的に除外される)\n"
        "- 履歴に根拠のある事実のみ。推測で作らない。金額・日付・固有名詞は具体的に書く\n"
        f"- 呼び名={self_name}が{partner}を実際どう呼んでいるか(表示名がローマ字や記号のとき特に重要)\n"
        "- 「進行中の話」=商談・約束・貸し借り・宿題など未完了の件。現状と次の一手まで書く\n"
        "- 「NG話題」=相手が怒った/嫌がった/避けるべき話題\n"
        "- 「関係性メモ」=距離感・呼ばれ方の変化・信頼の証拠など営業に効く観察\n"
        "- 「担当」=この客の担当キャストが会話から分かる場合のみ(自分が担当なら「自分」)\n"
        "- 「お気に入りキャスト」=この客が気に入っている他のキャスト・よく指名する子\n"
        "- src=根拠となる実際の発言の断片(40字以内)\n"
        "- conf=高(複数回/明言)・中(1回だが明確)・低(弱い根拠)\n"
        "- alts=同じ項目の別解釈があれば最大2つ\n"
        "- 重要なものから最大15項目。無い場合のみ空配列\n"
        '出力はJSON配列のみ(説明文なし): '
        '[{"k":"誕生日","v":"8月19日","src":"来週誕生日なんだ","conf":"高","alts":[],"sub":"相手"}]\n'
        f"---\n{talk}"
    )
    system = ("あなたは接客業向け顧客管理(CRM)アプリの抽出エンジン。利用者(ホステス本人)が"
              "自分自身のトーク履歴から自分の顧客メモを作る正当な業務であり、本人の依頼と同意に基づく。"
              "出力は必ずJSON配列のみ。説明文・前置き・コードブロック記号は書かない。")
    out = ""
    try:
        for attempt in range(2):
            # 注: プリフィル(assistant書き出し固定)はモデルによって400になるため使わない(v82)
            msgs = [{"role": "user", "content": prompt}]
            if attempt == 1:
                msgs = [{"role": "user", "content": prompt},
                        {"role": "assistant", "content": out[:800]},
                        {"role": "user", "content": "出力が読めませんでした。前置き・説明・```を一切付けず、"
                                                    "JSON配列だけを出し直してください。"}]
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": config.ANTHROPIC_API_KEY,
                         "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": config.ANTHROPIC_MODEL, "max_tokens": 3000,
                      "system": system, "messages": msgs},
                timeout=90)
            if r.status_code != 200:
                return [], f"API {r.status_code}: {r.text[:90]}"
            out = "".join(b.get("text", "") for b in r.json().get("content", []))
            t = out.replace("```json", "").replace("```", "").strip()
            try:
                arr = t[t.index("["):t.rindex("]") + 1]
                facts = json.loads(arr)
                break
            except (ValueError, json.JSONDecodeError):
                print(f"[linebot facts parse retry] head={out[:300]!r}", flush=True)
                if attempt == 1:
                    # AIが実際に何と言ったかを理由に載せる(遠隔診断用)
                    return [], f"AIの返答がJSONでない:「{out[:80]}…」"
    except requests.Timeout:
        return [], "時間切れ(トークが長すぎ)"
    except Exception as e:
        print(f"[linebot facts] {type(e).__name__}: {e}", flush=True)
        return [], f"{type(e).__name__}: {str(e)[:60]}"
    ok = []
    for f in facts[:15]:
        k = str(f.get("k", "")).strip()[:14]
        v = str(f.get("v", "")).strip()
        if not k or not v:
            continue
        # v100: 主語ガード。相手以外(自分/self/不明表記)の事実は機械的に落とす
        sub = str(f.get("sub", "相手")).strip()
        if sub not in ("相手", partner):
            continue
        ok.append({"k": k, "v": v[:80], "src": str(f.get("src", ""))[:60],
                   "conf": f.get("conf") if f.get("conf") in ("高", "中", "低") else "中",
                   "alts": [str(a)[:40] for a in (f.get("alts") or [])[:2]]})
    return ok, None


def web_research(contact):
    """🌐 公開情報の人物調査(AnthropicのWeb検索ツールをサーバー側で使用)。
    戻り値: (facts, err)。結果は通常の確認フローに載る(勝手にカードへは書かない)。"""
    if not config.ANTHROPIC_API_KEY:
        return [], "APIキー未設定"
    from . import crm
    attrs = crm.get_attrs(contact)
    hints = "、".join(f"{k}: {v}" for k, v in list(attrs.items())[:10])
    prompt = (
        f"「{contact}」という人物について、以下の手がかりと矛盾しない公開情報を"
        "Web検索で調べてください。\n"
        f"手がかり: {hints or '(なし)'}\n"
        "調べる項目: 会社名・役職・経歴・業界での評判・最近のニュース・公開されている資産や事業。\n"
        "ルール:\n"
        "- 手がかりと一致する人物だと確信できる情報のみ。同姓同名の可能性が高いものは出さない\n"
        "- 確信できる人物が見つからなければ空配列を返す(推測で埋めない)\n"
        "- src=出典(サイト名やURLの要約、40字以内)\n"
        "- conf=高(複数ソース一致)・中(単一ソース)・低(同一人物か不確か)\n"
        "- 最大8項目\n"
        '最終出力はJSON配列のみ: [{"k":"会社","v":"...","src":"...","conf":"中","alts":[]}]'
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": config.ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": config.ANTHROPIC_MODEL, "max_tokens": 3000,
                  "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=180)
        if r.status_code != 200:
            return [], f"API {r.status_code}: {r.text[:90]}"
        out = "".join(b.get("text", "") for b in r.json().get("content", [])
                      if b.get("type") == "text")
        t = out.replace("```json", "").replace("```", "").strip()
        try:
            facts = json.loads(t[t.index("["):t.rindex("]") + 1])
        except (ValueError, json.JSONDecodeError):
            return [], f"検索結果を読めません:「{out[:80]}…」"
    except requests.Timeout:
        return [], "検索が時間切れ"
    except Exception as e:
        return [], f"{type(e).__name__}: {str(e)[:60]}"
    ok = []
    for f in facts[:8]:
        k = str(f.get("k", "")).strip()[:14]
        v = str(f.get("v", "")).strip()
        if not k or not v:
            continue
        ok.append({"k": f"🌐{k}", "v": v[:80], "src": str(f.get("src", ""))[:60],
                   "conf": f.get("conf") if f.get("conf") in ("高", "中", "低") else "低",
                   "alts": [str(a)[:40] for a in (f.get("alts") or [])[:2]]})
    return ok, None


def web_async(contact):
    ensure()
    _meta_set(f"dig_{contact}", f"running:{int(time.time())}")

    def work():
        try:
            facts, err = web_research(contact)
            if err:
                _meta_set(f"dig_{contact}", f"error:{err}")
            else:
                save_facts(contact, facts)
                _meta_set(f"dig_{contact}", f"done:{len(facts)}")
        except Exception as e:
            _meta_set(f"dig_{contact}", f"error:{type(e).__name__}")

    threading.Thread(target=work, daemon=True).start()


def save_talk(contact, text):
    ensure()
    with db.conn() as c:
        c.execute("INSERT INTO linebot_talks(contact,text,ts) VALUES(?,?,?) "
                  "ON CONFLICT(contact) DO UPDATE SET text=excluded.text, ts=excluded.ts",
                  (contact, text, time.time()))


# ---- txtから 種別(顧客/同業/店内/私用) と 立場勾配(先輩/対等/後輩) を推定 ----

_KIND_LABEL = {"customer": "顧客", "peer": "同業(仲間)", "staff": "店内・スタッフ", "private": "私用"}
_STAND_LABEL = {"senior": "先輩・目上", "equal": "対等", "junior": "後輩・目下"}
_REL_KEY = "🔖種別・立場"


def _rel_value(kind, stand):
    if kind == "customer":
        return f"顧客（{_STAND_LABEL.get(stand, '対等')}）"
    if kind == "private":
        return "私用（受け取らない）"
    return f"{_KIND_LABEL.get(kind, kind)}・{_STAND_LABEL.get(stand, '対等')}"


def _parse_rel_value(v):
    """表示値 → (kind, stand)。"""
    kind = "customer"
    if v.startswith("同業"):
        kind = "peer"
    elif v.startswith("店内"):
        kind = "staff"
    elif v.startswith("私用"):
        kind = "private"
    stand = "equal"
    if "先輩" in v or "目上" in v:
        stand = "senior"
    elif "後輩" in v or "目下" in v:
        stand = "junior"
    return kind, stand


def classify_relationship(text, contact, self_name):
    """会話から相手の種別と立場を推定。戻り(fact or None)。confも付ける。"""
    if not config.ANTHROPIC_API_KEY:
        return None
    talk = text[-40000:]
    prompt = (
        f"あなたは銀座のホステス{self_name}の秘書。以下は{self_name}と「{contact}」のLINE。\n"
        f"「{contact}」が{self_name}にとって何者かを判定してください。\n"
        "kind(種別): customer=お客様 / peer=同業の仲間(他店のホステス・夜職仲間・友人) / "
        "staff=自分の店の同僚・黒服・ママ・後輩 / private=私用(家族や受け取らない相手)\n"
        "stand(立場): senior=相手が先輩/目上 / equal=対等 / junior=相手が後輩/目下\n"
        "判断材料: 敬語の向き・呼称(さん付け/呼び捨て/ちゃん)・お金を払う側か・"
        "来店/同伴の話があるか・仕事の相談の向き。\n"
        "確信が持てないときはconf=低に。\n"
        '出力はJSONのみ: {"kind":"customer","stand":"equal","conf":"高","why":"根拠(実発言の断片40字)"}'
    )
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
                          headers={"x-api-key": config.ANTHROPIC_API_KEY,
                                   "anthropic-version": "2023-06-01", "content-type": "application/json"},
                          json={"model": config.ANTHROPIC_MODEL, "max_tokens": 400,
                                "messages": [{"role": "user", "content": prompt}]}, timeout=60)
        if r.status_code != 200:
            return None
        out = "".join(b.get("text", "") for b in r.json().get("content", []))
        t = out.replace("```json", "").replace("```", "").strip()
        obj = json.loads(t[t.index("{"):t.rindex("}") + 1])
    except Exception as e:
        print(f"[linebot classify] {e}", flush=True)
        return None
    kind = obj.get("kind") if obj.get("kind") in _KIND_LABEL else "customer"
    stand = obj.get("stand") if obj.get("stand") in _STAND_LABEL else "equal"
    conf = obj.get("conf") if obj.get("conf") in ("高", "中", "低") else "中"
    v = _rel_value(kind, stand)
    # 代替候補(取り違えやすい組合せ)
    alts = []
    for k2 in ("customer", "peer", "staff"):
        if k2 != kind:
            alts.append(_rel_value(k2, stand))
    return {"k": _REL_KEY, "v": v, "src": str(obj.get("why", ""))[:60], "conf": conf, "alts": alts[:2]}


# ============ ご無沙汰スクリーニング(秘書の目・v87) ============
# 設計: 「来店記録」ではなく「最後にLINEでやりとりした日」を軸にする。
# LINE運用では last_visit_ts はほぼ空。実データ(受信/返信/txt最終会話日)で判定する。

import re as _re

_DATE_HDR = _re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})")


def parse_last_talk_ts(text):
    """トーク履歴txtの最終会話日時(JST基準のepoch)を推定。日付ヘッダ+時刻から。"""
    last_date = None
    last_hm = (0, 0)
    for line in text.splitlines():
        m = _DATE_HDR.match(line)
        if m:
            last_date = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            last_hm = (0, 0)
            continue
        tm = _re.match(r"^(\d{1,2}):(\d{2})\t", line)
        if tm and last_date:
            last_hm = (int(tm.group(1)), int(tm.group(2)))
    if not last_date:
        return None
    import datetime
    try:
        dt = datetime.datetime(last_date[0], last_date[1], last_date[2], last_hm[0], last_hm[1])
        # JSTの壁時計として解釈 → epoch(UTC)へ
        return dt.timestamp() - 0  # サーバはUTC。表示は概算日数なので厳密なTZは不要
    except Exception:
        return None


def _last_interaction(code):
    """その相手と最後に接触した時刻(受信・返信・txt最終会話 の最大)。無ければNone。"""
    ensure()
    cands = []
    with db.conn() as c:
        r = c.execute("SELECT MAX(ts) t FROM messages WHERE contact=?", (code,)).fetchone()
        if r and r["t"]:
            cands.append(r["t"])
        r = c.execute("SELECT MAX(ts) t FROM sent_replies WHERE contact=?", (code,)).fetchone()
        if r and r["t"]:
            cands.append(r["t"])
    lt = _meta_get(f"lasttalk_{code}")
    if lt:
        try:
            cands.append(float(lt))
        except Exception:
            pass
    lv = (db.get_contact(code) or {}).get("last_visit_ts")
    if lv:
        cands.append(lv)
    return max(cands) if cands else None


def _personal_interval(code):
    """その相手との普段のやりとり間隔(日・中央値)。履歴が薄ければNone。"""
    ts = []
    with db.conn() as c:
        ts += [r["ts"] for r in c.execute("SELECT ts FROM messages WHERE contact=? ORDER BY ts", (code,))]
        ts += [r["ts"] for r in c.execute("SELECT ts FROM sent_replies WHERE contact=? ORDER BY ts", (code,))]
    days = sorted(set(int(t // 86400) for t in ts))
    if len(days) < 4:
        return None
    gaps = [days[i + 1] - days[i] for i in range(len(days) - 1) if days[i + 1] - days[i] > 0]
    if len(gaps) < 3:
        return None
    gaps.sort()
    return gaps[len(gaps) // 2]  # 中央値


# ============ 🧠 ペルソナ分析(ON/OFF・生トーク＋確認済みファクトから) ============

def persona_enabled():
    return _meta_get("persona_enabled") != "0"   # 既定ON


def set_persona_enabled(on):
    _meta_set("persona_enabled", "1" if on else "0")


PERSONA_SECTIONS = ("価値観の核", "知性・教養", "コミュニケーションの好み",
                    "効く話題", "避ける話題", "距離の縮め方", "贈り物の方向")


def analyze_persona(contact):
    """生トーク全文＋確認済みファクトから、運用指針としてのペルソナを生成。
    戻り値: (persona_dict, err)。"""
    if not config.ANTHROPIC_API_KEY:
        return None, "APIキー未設定"
    from . import crm
    with db.conn() as c:
        r = c.execute("SELECT text FROM linebot_talks WHERE contact=?", (contact,)).fetchone()
    talk = (r["text"] if r else "") or ""
    if len(talk) < 200:
        return None, "会話が少なすぎます(txtを取り込んでください)"
    # 長文は頭と尾を優先(関係の始まりと現在=弧が出る)
    if len(talk) > 90000:
        talk = talk[:45000] + "\n…(中略)…\n" + talk[-45000:]
    attrs = crm.get_attrs(contact)
    facts = "／".join(f"{k}:{v}" for k, v in list(attrs.items())[:15])
    system = ("あなたは銀座の一流ホステスに仕える有能な秘書。担当のお客様との実際のLINEを読み、"
              "接客・営業に本当に役立つ人物理解メモを作る。心地よいだけのお世辞や、根拠のない"
              "安楽椅子心理分析は禁止。断定の強さは根拠の強さに一致させる。")
    prompt = (
        f"お客様「{contact}」の人物ペルソナを、次の観点でまとめてください。\n"
        f"観点: {'/'.join(PERSONA_SECTIONS)}\n"
        + (f"既に確認済みの事実: {facts}\n" if facts else "")
        + "ルール:\n"
        "- 各観点は『結論(運用に使える具体)』を書く。抽象語(知的・優しい等)で終わらせず、"
        "『だから“この話題”が効く／“これ”は避ける』まで落とす\n"
        "- v=結論(120字以内)。src=根拠となる本人の実発言の引用(40字以内)。conf=高/中/低\n"
        "- 根拠が弱い観点はconf=低にするか省く。無理に7つ埋めない\n"
        "- summary=この人を一言で(営業視点・誇張なし・40字以内)\n"
        "さらに『許容レベル』(この相手にどこまで踏み込んでよいか)を別配列で出す。\n"
        f"観点(この5つに限定): {'/'.join(TOLERANCE_KEYS)}\n"
        "- 冗談・軽口=どんな冗談に乗ってくる/流すか。営業色の許容=来店を誘う直球をどこまで受けるか。"
        "距離を詰めた時=馴れ馴れしくした時に乗るか引くか。際どい話題=下ネタ等のライン。"
        "待たせた時=返信が遅れた時の反応。\n"
        "- 各項目 v=結論(80字以内)・src=根拠の実発言引用(40字以内)・conf=高/中/低。"
        "根拠となる実際のやり取りが無い観点は出さない(推測で埋めない)\n"
        '出力はJSONのみ: {"summary":"...","sections":[{"k":"価値観の核","v":"...","src":"...","conf":"高"}],'
        '"tolerance":[{"k":"冗談・軽口","v":"...","src":"...","conf":"高"}]}\n'
        f"---\n{talk}"
    )
    try:
        rr = requests.post("https://api.anthropic.com/v1/messages",
                           headers={"x-api-key": config.ANTHROPIC_API_KEY,
                                    "anthropic-version": "2023-06-01", "content-type": "application/json"},
                           json={"model": config.ANTHROPIC_MODEL, "max_tokens": 2500,
                                 "system": system, "messages": [{"role": "user", "content": prompt}]},
                           timeout=120)
        if rr.status_code != 200:
            return None, f"API {rr.status_code}: {rr.text[:90]}"
        out = "".join(b.get("text", "") for b in rr.json().get("content", []))
        t = out.replace("```json", "").replace("```", "").strip()
        try:
            obj = json.loads(t[t.index("{"):t.rindex("}") + 1])
        except (ValueError, json.JSONDecodeError):
            # v101: 空応答・JSON崩れは1回だけ出し直させる(v95知見の再適用)
            rr2 = requests.post("https://api.anthropic.com/v1/messages",
                                headers={"x-api-key": config.ANTHROPIC_API_KEY,
                                         "anthropic-version": "2023-06-01",
                                         "content-type": "application/json"},
                                json={"model": config.ANTHROPIC_MODEL, "max_tokens": 2500,
                                      "system": system,
                                      "messages": [{"role": "user", "content": prompt},
                                                   {"role": "assistant", "content": out[:600]},
                                                   {"role": "user", "content": "出力が読めませんでした。前置き・```を付けず、JSONオブジェクトだけを出し直してください。"}]},
                                timeout=120)
            if rr2.status_code != 200:
                return None, f"API {rr2.status_code}: {rr2.text[:90]}"
            out = "".join(b.get("text", "") for b in rr2.json().get("content", []))
            t = out.replace("```json", "").replace("```", "").strip()
            obj = json.loads(t[t.index("{"):t.rindex("}") + 1])
    except requests.Timeout:
        return None, "時間切れ"
    except Exception as e:
        return None, f"AIの返答が読めません:「{(out[:60] + '…') if 'out' in dir() and out else type(e).__name__}」"
    secs = []
    for s in (obj.get("sections") or [])[:8]:
        k = str(s.get("k", "")).strip()[:14]
        v = str(s.get("v", "")).strip()
        if k and v:
            secs.append({"k": k, "v": v[:160], "src": str(s.get("src", ""))[:60],
                         "conf": s.get("conf") if s.get("conf") in ("高", "中", "低") else "中"})
    if not secs:
        return None, "分析結果が空でした"
    tols = []
    for t in (obj.get("tolerance") or [])[:5]:
        k = str(t.get("k", "")).strip()[:14]
        v = str(t.get("v", "")).strip()
        if k in TOLERANCE_KEYS and v:
            tols.append({"k": k, "v": v[:120], "src": str(t.get("src", ""))[:60],
                         "conf": t.get("conf") if t.get("conf") in ("高", "中", "低") else "中",
                         "ok": None})   # ok: None=未確定 / 1=本人が採用 / 0=却下(注入しない)
    return {"summary": str(obj.get("summary", ""))[:80], "sections": secs,
            "tolerance": tols}, None


def maybe_auto_persona(contact):
    """v109: txt取り込み時にペルソナも自動生成。無駄打ちを避ける3条件:
    - CHOUBA_AUTO_PERSONA=0 で無効化(既定ON)
    - 既にペルソナがある相手はスキップ(再取り込みで毎回焼き直さない)
    - 会話が3000字未満(雑談・薄い相手)はスキップ=コストの無駄打ち防止
    バックグラウンド実行なので取り込みの体感速度は変わらない。"""
    if os.environ.get("CHOUBA_AUTO_PERSONA", "1") != "1":
        return
    if not config.ANTHROPIC_API_KEY:
        return
    try:
        if get_persona(contact):
            return
        with db.conn() as c:
            r = c.execute("SELECT text FROM linebot_talks WHERE contact=?", (contact,)).fetchone()
        if not r or len((r["text"] or "")) < 3000:
            return
        persona_async(contact)
    except Exception as e:
        print(f"[auto persona] {e}", flush=True)


def persona_async(contact):
    ensure()
    _meta_set(f"pstat_{contact}", f"running:{int(time.time())}")

    def work():
        try:
            p, err = analyze_persona(contact)
            if err:
                _meta_set(f"pstat_{contact}", f"error:{err}")
            else:
                # v118: 再分析しても本人の○✕確定を失わない(同じ観点は確定状態を引き継ぐ)
                try:
                    old = get_persona(contact) or {}
                    decided = {t["k"]: t for t in (old.get("tolerance") or [])
                               if t.get("ok") in (0, 1)}
                    merged = []
                    for t in (p.get("tolerance") or []):
                        if t["k"] in decided:
                            merged.append(decided[t["k"]])   # 確定済みは旧内容ごと維持
                        else:
                            merged.append(t)
                    for k, t in decided.items():
                        if not any(x["k"] == k for x in merged):
                            merged.append(t)
                    p["tolerance"] = merged
                except Exception as e:
                    print(f"[persona merge] {e}", flush=True)
                with db.conn() as c:
                    c.execute("INSERT INTO linebot_persona(contact,data,ts) VALUES(?,?,?) "
                              "ON CONFLICT(contact) DO UPDATE SET data=excluded.data, ts=excluded.ts",
                              (contact, json.dumps(p, ensure_ascii=False), time.time()))
                _meta_set(f"pstat_{contact}", "done")
        except Exception as e:
            _meta_set(f"pstat_{contact}", f"error:{type(e).__name__}")
            print(f"[linebot persona] {e}", flush=True)

    threading.Thread(target=work, daemon=True).start()


def get_persona(contact):
    ensure()
    with db.conn() as c:
        r = c.execute("SELECT data FROM linebot_persona WHERE contact=?", (contact,)).fetchone()
    if not r:
        return None
    try:
        return json.loads(r["data"])
    except Exception:
        return None


def partner_stats(contact):
    """v116: 相手の行動データ化。messages(受信)とsent_replies(送信)から集計。
    ★取得できないもの: 相手の既読(LINE APIにもリーダーにも無い=推定もしない)。
    取れるもの: 相手の平均文字数・絵文字頻度・活発な時間帯・相手/本人の返信の速さ・やりとり量。"""
    ensure()
    from .style_profile import EMOJI_RE
    with db.conn() as c:
        recv = [(r["ts"], r["text"] or "") for r in c.execute(
            "SELECT ts, text FROM messages WHERE contact=? ORDER BY ts", (contact,))]
        sent = [(r["ts"], r["text"] or "") for r in c.execute(
            "SELECT ts, text FROM sent_replies WHERE contact=? ORDER BY ts", (contact,))]
    n = len(recv)
    if n < 3:
        return None   # 少なすぎる=出さない(誤った印象を与えない)
    import datetime
    lens = [len(t) for _, t in recv]
    emos = [len(EMOJI_RE.findall(t)) for _, t in recv]
    avg_len = round(sum(lens) / n)
    emo_per = round(sum(emos) / n, 1)
    # 活発な時間帯(JST時刻の最頻2つ)
    hours = {}
    for ts, _ in recv:
        h = datetime.datetime.fromtimestamp(ts + 9 * 3600).hour   # 概算JST
        hours[h] = hours.get(h, 0) + 1
    top_hours = sorted(hours, key=lambda h: -hours[h])[:2]
    # 相手の返信の速さ: 本人が送った直後の相手受信までの間隔(分)の中央値
    merged = sorted([(t, "me") for t, _ in sent] + [(t, "you") for t, _ in recv])
    gaps = []
    for i in range(len(merged) - 1):
        if merged[i][1] == "me" and merged[i + 1][1] == "you":
            gaps.append((merged[i + 1][0] - merged[i][0]) / 60.0)
    gaps = [g for g in gaps if 0 < g < 60 * 48]   # 2日超は「別の会話」として除外
    reply_med = None
    if gaps:
        gaps.sort()
        reply_med = round(gaps[len(gaps) // 2])
    return {
        "n_recv": n, "avg_len": avg_len, "emoji_per_msg": emo_per,
        "top_hours": top_hours, "reply_median_min": reply_med,
        "note": "相手の既読タイミングはLINE上取得できないため含みません",
    }


# ============ v118: ペルソナ3層化 ============
# 第1層=人物(既存persona)・第2層=関係性(ログの機械集計=確信度高)・
# 第3層=許容レベル(LLM推定→本人が○✕で確定。確定済みのみ下書きに注入=誤推定事故防止)

_POLITE_RE = _re.compile(r"(です|ます|でした|ました|ください|ですか|ますか|ですね|ますね|ございま)")


def _polite_ratio(texts):
    """文章群の敬語率(0..1)。判定はヒューリスティック=語尾・丁寧語の出現有無。"""
    if not texts:
        return None
    hit = sum(1 for t in texts if _POLITE_RE.search(t or ""))
    return hit / len(texts)


def _register_label(r):
    if r is None:
        return "不明(実例なし)"
    if r >= 0.7:
        return "敬語で安定"
    if r >= 0.3:
        return "敬語とタメ口の混合"
    return "タメ口主体"


def relationship_stats(contact):
    """第2層=関係性。messages/sent_replies/sittingsから機械集計(LLM不使用)。
    - 口調: 自分→相手の敬語率 / 相手→自分の敬語率
    - 起点: 6時間以上あいた後に先に送るのはどちらが多いか
    - 来店実績: お席记録の回数・同伴・アフター
    - 直近30日のやりとり量"""
    ensure()
    with db.conn() as c:
        recv = [(r["ts"], r["text"] or "") for r in c.execute(
            "SELECT ts, text FROM messages WHERE contact=? ORDER BY ts", (contact,))]
        sent = [(r["ts"], r["text"] or "") for r in c.execute(
            "SELECT ts, text FROM sent_replies WHERE contact=? ORDER BY ts", (contact,))]
    if len(recv) + len(sent) < 3:
        return None
    my_pol = _polite_ratio([t for _, t in sent])
    yr_pol = _polite_ratio([t for _, t in recv])
    # 会話の起点: 6h空白の後、先に発したのはどちらか
    merged = sorted([(t, "me") for t, _ in sent] + [(t, "you") for t, _ in recv])
    me_starts = you_starts = 0
    for i, (ts, who) in enumerate(merged):
        if i == 0 or ts - merged[i - 1][0] >= 6 * 3600:
            if who == "me":
                me_starts += 1
            else:
                you_starts += 1
    total_starts = me_starts + you_starts
    initiator = None
    if total_starts >= 3:
        r = you_starts / total_starts
        initiator = ("相手からが多い" if r >= 0.65 else
                     "自分からが多い" if r <= 0.35 else "半々")
    # 来店実績(お席记録)
    visits = dohan = after = 0
    try:
        from . import sittings as _si
        _si.ensure()
        with db.conn() as c:
            rows = c.execute(
                "SELECT s.dohan_venue, s.after_venue FROM sittings s "
                "WHERE s.main_contact=? OR EXISTS(SELECT 1 FROM sitting_members m "
                "WHERE m.sitting_id=s.id AND m.contact=?)", (contact, contact)).fetchall()
        visits = len(rows)
        dohan = sum(1 for r in rows if (r["dohan_venue"] or "").strip())
        after = sum(1 for r in rows if (r["after_venue"] or "").strip())
    except Exception as e:
        print(f"[rel visits] {e}", flush=True)
    now = time.time()
    n30 = sum(1 for ts, _ in merged if now - ts <= 30 * 86400)
    return {
        "my_register": _register_label(my_pol), "my_polite": my_pol,
        "your_register": _register_label(yr_pol), "your_polite": yr_pol,
        "initiator": initiator, "me_starts": me_starts, "you_starts": you_starts,
        "visits": visits, "dohan": dohan, "after": after, "n_30d": n30,
    }


def relationship_prompt_block(contact):
    """第2層を下書きプロンプトへ(事実の集計なので常時注入してよい)。"""
    rs = relationship_stats(contact)
    if not rs:
        return ""
    lines = [f"- 自分の口調はこの相手に対して「{rs['my_register']}」。この口調を崩さない",
             f"- 相手の口調は「{rs['your_register']}」"]
    if rs.get("initiator"):
        lines.append(f"- 会話の起点は{rs['initiator']}")
    if rs.get("visits"):
        v = f"- お席の実績{rs['visits']}回"
        if rs.get("dohan"):
            v += f"(うち同伴{rs['dohan']})"
        lines.append(v)
    return "【この相手との関係性(ログ集計=事実)】\n" + "\n".join(lines)


TOLERANCE_KEYS = ("冗談・軽口", "営業色の許容", "距離を詰めた時", "際どい話題", "待たせた時")


def tolerance_prompt_block(contact):
    """第3層のうち本人が○で確定した項目だけを『踏み込みの上限』として注入。
    未確定・却下は注入しない=推定ミスがそのまま文面事故になるのを防ぐ。"""
    p = get_persona(contact)
    if not p:
        return ""
    items = [t for t in (p.get("tolerance") or []) if t.get("ok") == 1]
    if not items:
        return ""
    lines = [f"- {t['k']}: {t['v']}" for t in items]
    return ("【踏み込みの上限(本人確認済み・厳守)】この範囲を超える馴れ馴れしさ・営業色・際どさは出さない:\n"
            + "\n".join(lines))


def save_persona(contact, data):
    """v116: 編集したペルソナを保存(項目削除・修正の反映)。"""
    ensure()
    with db.conn() as c:
        c.execute("INSERT INTO linebot_persona(contact,data,ts) VALUES(?,?,?) "
                  "ON CONFLICT(contact) DO UPDATE SET data=excluded.data, ts=excluded.ts",
                  (contact, json.dumps(data, ensure_ascii=False), time.time()))


def edit_persona(contact, action, index, value=""):
    """v116: ペルソナの1項目を削除/修正。戻り: 更新後のpersona or None。"""
    p = get_persona(contact)
    if not p or "sections" not in p:
        return None
    secs = p.get("sections") or []
    if action == "del":
        if 0 <= index < len(secs):
            secs.pop(index)
    elif action == "fix":
        if 0 <= index < len(secs) and value.strip():
            secs[index]["v"] = value.strip()[:160]
            secs[index]["conf"] = "中"        # 人手修正=確信度中(引用は残す)
    elif action == "summary" and value.strip():
        p["summary"] = value.strip()[:80]
    # v118: 許容レベルの○✕確定・修正・削除
    tols = p.get("tolerance") or []
    if action in ("tolok", "tolng", "tolfix", "toldel") and 0 <= index < len(tols):
        if action == "tolok":
            tols[index]["ok"] = 1
        elif action == "tolng":
            tols[index]["ok"] = 0
        elif action == "toldel":
            tols.pop(index)
        elif action == "tolfix" and value.strip():
            tols[index]["v"] = value.strip()[:120]
            tols[index]["ok"] = 1          # 手で直した=本人確認済みとして採用
            tols[index]["conf"] = "高"
            tols[index]["src"] = "本人が修正"
        p["tolerance"] = tols
    p["sections"] = secs
    save_persona(contact, p)
    return p


def persona_msgs(contact):
    """ペルソナカード表示。無ければ実行案内、実行中なら待ちを返す。"""
    cq = _q(contact, safe="")
    stat = _meta_get(f"pstat_{contact}")
    p = get_persona(contact)
    if stat.startswith("running"):
        return [flexmsg(f"🧠 {_yobina(contact)} を分析中…",
                        "会話を読み込んでいます(30秒〜1分)。少し待って🧠をもう一度。",
                        accent=BLUE, quick=[("🧠 もう一度", f"m=persona&c={cq}"),
                                            ("🗂 カードへ", f"m=card&c={cq}"), ("ホームへ", "m=home")])]
    if stat.startswith("error:") and not p:
        return [flexmsg("🧠 分析できませんでした", f"理由: {stat[6:]}",
                        accent=RED, quick=[("🔁 もう一度", f"f=persona&a=run&c={cq}"),
                                           ("🗂 カードへ", f"m=card&c={cq}")])]
    if not p:
        with db.conn() as c:
            has = c.execute("SELECT 1 FROM linebot_talks WHERE contact=?", (contact,)).fetchone()
        if not has:
            return [flexmsg("🧠 まだ分析できません",
                            "この相手のトーク履歴(.txt)を送ってから🧠を押してください。",
                            accent=BLUE, quick=[("🗂 カードへ", f"m=card&c={cq}"), ("ホームへ", "m=home")])]
        return [flexmsg(f"🧠 {_yobina(contact)} のペルソナ分析",
                        "会話全体から、効く話題・地雷・距離の縮め方などを引き出します。実行しますか？",
                        quick=[("▶ 分析する", f"f=persona&a=run&c={cq}"),
                               ("🗂 カードへ", f"m=card&c={cq}"), ("ホームへ", "m=home")])]
    # 結果カード
    CONF = {"高": "●●●", "中": "●●○", "低": "●○○"}
    rows = []
    if p.get("summary"):
        rows.append({"type": "text", "text": p["summary"], "wrap": True, "size": "sm",
                     "weight": "bold", "color": "#1B2A4A", "margin": "sm"})
        rows.append({"type": "separator", "margin": "md", "color": "#E3DCC9"})
    accent_of = {"効く話題": "#2E7D32", "避ける話題": "#C0402C", "贈り物の方向": "#B08A2E"}
    for s in p["sections"]:
        col = accent_of.get(s["k"], "#8A6D2E")
        rows.append({"type": "box", "layout": "vertical", "margin": "md", "spacing": "xs",
                     "contents": [
                         {"type": "box", "layout": "baseline", "contents": [
                             {"type": "text", "text": s["k"], "color": col, "size": "xs",
                              "weight": "bold", "flex": 7, "wrap": True},
                             {"type": "text", "text": CONF.get(s["conf"], ""), "color": "#B0A98F",
                              "size": "xs", "flex": 3, "align": "end"}]},
                         {"type": "text", "text": s["v"], "color": "#2B2823", "size": "sm", "wrap": True},
                     ] + ([{"type": "text", "text": "「" + s["src"] + "」", "color": "#9A958A",
                            "size": "xxs", "wrap": True}] if s.get("src") else [])})
    header = {"type": "box", "layout": "vertical", "backgroundColor": "#2A2140",
              "paddingAll": "16px", "contents": [
                  {"type": "text", "text": f"🧠 {_yobina(contact)} のペルソナ", "color": "#F0ECE2",
                   "weight": "bold", "size": "md", "wrap": True},
                  {"type": "text", "text": "接客・営業の指針（引用と確信度つき）", "color": "#C4B8E0",
                   "size": "xxs", "margin": "sm"}]}
    bubble = {"type": "bubble", "size": "mega", "header": header,
              "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "contents": rows[:24]}}
    return [{"type": "flex", "altText": f"🧠 {_yobina(contact)} のペルソナ", "contents": bubble,
             "quickReply": _quick([("🔁 分析し直す", f"f=persona&a=run&c={cq}"),
                                   ("🗂 カードへ", f"m=card&c={cq}"), ("ホームへ", "m=home")])}]


RANK_DEFAULT_GAP = {"S": 21, "A": 30, "B": 45}
RANK_WEIGHT = {"S": 3.0, "A": 2.0, "B": 1.0}


def estranged(now=None, ranks=None):
    """ご無沙汰の顧客を、秘書の優先順で返す。
    - 最終接触からの空き > しきい値(その人の普段の間隔×1.8、無ければランク既定) で該当
    - いま会話中(3日以内の接触 or 未返信あり)は除外
    - スコア = 空き/しきい値 × ランク重み で降順
    """
    now = now or time.time()
    rankset = set(ranks) if ranks else None
    with db.conn() as c:
        open_contacts = set(r["contact"] for r in c.execute(
            "SELECT DISTINCT contact FROM messages WHERE status IN ('open','deferred')"))
    out = []
    for ct in db.list_contacts():
        if (ct.get("kind") or "customer") != "customer":
            continue
        if ct.get("linked") == 0:
            continue
        code = ct["code"]
        rank = ct.get("rank") or "B"
        if rankset and rank not in rankset:
            continue
        last = _last_interaction(code)
        if not last:
            continue
        gap = (now - last) / 86400.0
        if gap < 3 or code in open_contacts:   # 会話中は「ご無沙汰」ではない
            continue
        pi = _personal_interval(code)
        thr = pi * 1.8 if pi else RANK_DEFAULT_GAP.get(rank, 45)
        if gap <= thr:
            continue
        score = (gap / thr) * RANK_WEIGHT.get(rank, 1.0)
        out.append({"code": code, "rank": rank, "gap": int(gap), "thr": int(thr),
                    "interval": pi, "score": score})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


def _dig_status(contact=None):
    """掘り(抽出)の進行状況。contact指定なしなら全体サマリを返す。"""
    ensure()
    with db.conn() as c:
        rows = {r["k"][4:]: r["v"] for r in c.execute(
            "SELECT k, v FROM linebot_meta WHERE k LIKE 'dig_%'")}
    if contact is not None:
        return rows.get(contact, "")
    return rows


def _notify_card_ready(contact, ncrit, nauto):
    """v96: 抽出完了の通知1通(「✎ カードができました」)→ タップでLIFF編集画面へ。
    LIFF未設定なら何もしない(従来の🔎整備フローのみ)。push=無料枠200通/月の消費に留意。"""
    liff_id = os.environ.get("CHOUBA_LIFF_ID", "")
    if not liff_id:
        return
    try:
        url = f"https://liff.line.me/{liff_id}#edit/{_q(contact, safe='')}"
        n = ncrit + nauto
        push_owner([{
            "type": "flex", "altText": f"✎ {_yobina(contact)}さんのカードができました",
            "contents": {"type": "bubble",
                         "body": {"type": "box", "layout": "vertical", "paddingAll": "16px",
                                  "contents": [
                                      {"type": "text", "text": "✎ カードができました", "weight": "bold",
                                       "size": "md", "color": "#1B2A4A"},
                                      {"type": "text", "text": f"{_yobina(contact)}さん・{n}項目",
                                       "size": "sm", "color": "#6B6455", "margin": "sm", "wrap": True}]},
                         "footer": {"type": "box", "layout": "vertical", "contents": [
                             {"type": "button", "style": "primary", "color": "#A8842F",
                              "action": {"type": "uri", "label": "開いて確認する", "uri": url}}]}}}])
    except Exception as e:
        print(f"[linebot cardready] {e}", flush=True)


def dig_async(contact):
    """バックグラウンドで抽出(reply期限に縛られない)。結果は🔎整備タブで受け取る。"""
    ensure()
    _meta_set(f"dig_{contact}", f"running:{int(time.time())}")

    def work():
        try:
            with db.conn() as c:
                r = c.execute("SELECT text FROM linebot_talks WHERE contact=?", (contact,)).fetchone()
            if not r:
                _meta_set(f"dig_{contact}", "error:トーク原文がありません(txtを再送してください)")
                return
            self_name = (db.get_profile("_selfname") or {}).get("name") or "自分"
            facts, err = extract_facts(r["text"], contact, self_name)
            # 種別・立場の推定を先頭に足す(整備で○✕確認 → 確定でkind/standに反映)
            try:
                rel = classify_relationship(r["text"], contact, self_name)
                if rel:
                    facts = [rel] + (facts or [])
            except Exception as e:
                print(f"[linebot classify hook] {e}", flush=True)
            if err and not facts:
                _meta_set(f"dig_{contact}", f"error:{err}")
            else:
                facts = curate_facts(facts or [])   # v96: 厳選(上位12・正規キー・低確信度破棄)
                facts = _ensure_name_questions(contact, facts)
                ncrit, nauto = save_split(contact, facts)
                _meta_set(f"dig_{contact}", f"done:{ncrit}:{nauto}")
                _notify_card_ready(contact, ncrit, nauto)   # v96: LIFF編集への通知1通
                maybe_auto_persona(contact)                 # v109: ペルソナも同時に
        except Exception as e:
            _meta_set(f"dig_{contact}", f"error:{type(e).__name__}")
            print(f"[linebot dig] {e}", flush=True)

    threading.Thread(target=work, daemon=True).start()


def _is_critical(k):
    return k in CRITICAL_KEYS or k == _REL_KEY


import difflib as _difflib


def _canon_key(k):
    k = (k or "").replace("🌐", "")
    if k.startswith("家族"):
        return "家族"
    if k.startswith("仕事") or k == "会社":
        return "仕事・会社"
    if k.startswith("住所"):
        return "住所"
    return k


def _norm(s):
    return _re.sub(r"[ 　（）()「」『』・,、。／/]", "", str(s)).lower()


def _similar(a, b):
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if len(na) >= 4 and na in nb:
        return True
    if len(nb) >= 4 and nb in na:
        return True
    return _difflib.SequenceMatcher(None, na, nb).ratio() >= 0.62


# ---- 抽出の厳選(curation・v96) : 接客価値で上位12件・正規キー統合・「その他」廃止・低確信度破棄 ----
# 接客価値の順位(小さいほど価値が高い)。営業に直接効くもの→固定プロフィールの順。
_CURATE_ORDER = ("進行中の話", "NG話題", "担当", "記念日", "誕生日", "呼び名", "本名",
                 "関係性メモ", "お気に入りキャスト", "仕事・会社", "家族", "好きなお酒",
                 "好きな食べ物", "趣味・関心", "資産・事業", "健康", "年齢")
_CONF_W = {"高": 0, "中": 1, "低": 2}
_CURATE_MAX = 18   # v101: 12→18(「物足りない」対応。順位付けは維持)
# 接客価値が高いキーは低確信度でも捨てない(見直し🧹で外せる)
_KEEP_LOW = ("進行中の話", "NG話題", "記念日", "関係性メモ", "担当", "お気に入りキャスト")


def curate_facts(facts):
    """抽出結果の厳選。超重要(CRITICAL/種別)は無条件で残す。
    - conf=低 は原則破棄。ただし接客価値の高いキー(_KEEP_LOW)は残す(v101)
    - キーを正規キーへ統合(「その他」廃止: FACT_KEYSへ寄せ、寄らなければ関係性メモへ)
    - 接客価値×確信度で上位18件に厳選"""
    crit, rest = [], []
    for f in (facts or []):
        k = f.get("k") or ""
        if _is_critical(k):
            crit.append(f)
            continue
        if (f.get("conf") or "中") == "低" and _canon_key(k) not in _KEEP_LOW:
            continue
        ck = _canon_key(k)
        if ck not in FACT_KEYS or ck == "その他":
            # 正規キーに最も近いものへ統合。寄らなければ関係性メモに格納(「その他」は作らない)
            m = _difflib.get_close_matches(ck, [x for x in FACT_KEYS if x != "その他"], n=1, cutoff=0.55)
            if m:
                ck = m[0]
            else:
                f = dict(f)
                if ck and ck != "その他":
                    f["v"] = f"{ck}: {f.get('v', '')}"
                ck = "関係性メモ"
        f = dict(f)
        f["k"] = ck
        rest.append(f)
    order = {k: i for i, k in enumerate(_CURATE_ORDER)}
    rest.sort(key=lambda f: (order.get(f["k"], 99), _CONF_W.get(f.get("conf") or "中", 1)))
    return crit + rest[:_CURATE_MAX]


def _prefilter_facts(contact, facts):
    """既にカードが知っている/過去に決めた事は二度と聞かない。言い換えの重複も潰す。"""
    from . import crm
    attrs = crm.get_attrs(contact)
    ct = db.get_contact(contact) or {}
    with db.conn() as c:
        prior = [(r["k"], r["v"]) for r in c.execute(
            "SELECT k, v FROM linebot_facts WHERE contact=?", (contact,))]
    out, batch = [], []
    for f in facts:
        k, v = f["k"], f["v"]
        ck = _canon_key(k)
        # 決定済みの重要項目は再質問しない
        if k in ("呼び名", "本名") and attrs.get(k):
            continue
        if k == "誕生日" and ct.get("birthday"):
            continue
        if k == _REL_KEY and ct.get("kind") and ct.get("stand"):
            continue
        # 既存の属性/カード値と近ければskip
        ev = attrs.get(k) or attrs.get(ck)
        if ev and _similar(ev, v):
            continue
        if k == "誕生日" and ct.get("birthday") and _similar(ct["birthday"], v):
            continue
        # 過去のfact(全status)と、同じ系統のキーで値が近ければskip
        if any(_canon_key(pk) == ck and _similar(pv, v) for pk, pv in prior):
            continue
        # 同一バッチ内の重複
        if any(bk == ck and _similar(bv, v) for bk, bv in batch):
            continue
        batch.append((ck, v))
        out.append(f)
    return out


def save_facts(contact, facts, status="pending"):
    ensure()
    with db.conn() as c:
        for f in facts:
            r = c.execute("SELECT id FROM linebot_facts WHERE contact=? AND k=? AND v=?",
                          (contact, f["k"], f["v"])).fetchone()
            if r:
                continue
            c.execute("INSERT INTO linebot_facts(contact,k,v,src,conf,alts,status,created_ts) "
                      "VALUES(?,?,?,?,?,?,?,?)",
                      (contact, f["k"], f["v"], f["src"], f["conf"],
                       json.dumps(f["alts"], ensure_ascii=False), status, time.time()))


def save_split(contact, facts):
    """超重要=確認待ち(pending) / それ以外=即カード反映(applied)。戻り(重要件数, 自動件数)。"""
    facts = _prefilter_facts(contact, facts)   # 既知・重複を除去(二度聞き防止)
    crit = [f for f in facts if _is_critical(f["k"])]
    auto = [f for f in facts if not _is_critical(f["k"])]
    save_facts(contact, crit, status="pending")
    # 自動反映(重複はスキップ)
    with db.conn() as c:
        existing = set((r["k"], r["v"]) for r in c.execute(
            "SELECT k, v FROM linebot_facts WHERE contact=?", (contact,)))
    applied = []
    for f in auto:
        if (f["k"], f["v"]) in existing:
            continue
        try:
            apply_fact(contact, f["k"], f["v"])
        except Exception as e:
            print(f"[linebot auto-apply] {e}", flush=True)
        applied.append(f)
    save_facts(contact, applied, status="applied")
    with db.conn() as c:
        ncrit = c.execute("SELECT COUNT(*) FROM linebot_facts WHERE contact=? AND status='pending'",
                          (contact,)).fetchone()[0]
    return ncrit, len(applied)


def reviewable_facts(contact):
    ensure()
    with db.conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM linebot_facts WHERE contact=? AND status='applied' ORDER BY id",
            (contact,))]


def _ensure_name_questions(contact, facts):
    """呼び名(ホステスが呼ぶ名前)は必ず確認する。抽出に無ければLINE表示名から質問を作る。
    重要項目(呼び名→本名→誕生日→種別立場)を先頭に並べ替える。"""
    from . import crm
    ks = {f["k"] for f in facts}
    attrs = crm.get_attrs(contact)
    if "呼び名" not in ks and not attrs.get("呼び名"):
        alts = []
        for cand in (contact, f"{contact}さん"):
            if cand and cand not in alts:
                alts.append(cand)
        facts = [{"k": "呼び名", "v": alts[0], "src": "(LINE表示名から・要確認)",
                  "conf": "低", "alts": alts[1:3]}] + facts
    order = {"呼び名": 0, "本名": 1, "誕生日": 2, _REL_KEY: 3}
    facts = sorted(facts, key=lambda f: order.get(f["k"], 9))
    return facts


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
    if k == _REL_KEY:
        # 種別・立場の確定 → contacts.kind / stand に反映
        kind, stand = _parse_rel_value(v)
        try:
            crm.set_kind(contact, kind)
        except Exception:
            pass
        try:
            with db.conn() as c:
                c.execute("UPDATE contacts SET stand=? WHERE code=?", (stand, contact))
        except Exception:
            pass
        db.track("linebot_fact_apply")
        return
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
        # 直近で自動反映された相手がいれば「見直す」に誘導
        with db.conn() as c:
            row = c.execute("SELECT contact, COUNT(*) n FROM linebot_facts WHERE status='applied' "
                            "GROUP BY contact ORDER BY MAX(id) DESC LIMIT 1").fetchone()
        quick = [("🗂 顧客を見る", "m=crm"), ("ホームへ", "m=home")]
        extra = ""
        if row and row["n"]:
            cq = _q(row["contact"], safe="")
            quick.insert(0, (f"🧹 {_yobina(row['contact'])}の自動反映を見直す"[:20], f"m=review&c={cq}"))
            extra = (f"\n細かい情報は{_yobina(row['contact'])}のカードに自動反映済みです。"
                     "気になる所だけ🧹見直すで直せます。")
        return reply(token, head + [flexmsg("📇 重要項目の確認は完了です",
                                            "呼び名・誕生日・種別は反映しました。" + extra,
                                            accent=GREEN, quick=quick)])
    f = pend[0]
    n = len(pend)
    dots = {"高": "●●●", "中": "●●○", "低": "●○○"}[f["conf"]]
    body = [
        # 分析(主役): 大きく濃く
        {"type": "text", "text": f["v"], "size": "lg", "weight": "bold",
         "color": "#1B2A4A", "wrap": True},
        {"type": "box", "layout": "baseline", "margin": "md", "contents": [
            {"type": "text", "text": "確信度", "size": "xs", "color": "#8D8674", "flex": 3},
            {"type": "text", "text": dots, "size": "sm", "color": "#A8842F", "flex": 7,
             "weight": "bold"}]},
    ]
    if f.get("src"):
        # 出典(脇役): 罫線で明確に区切り、小さくグレーで
        body += [
            {"type": "separator", "margin": "lg", "color": "#E3DCC9"},
            {"type": "text", "text": "帳場くんが見つけた根拠", "size": "xxs",
             "color": "#B0A98F", "margin": "md"},
            {"type": "text", "text": "「" + f["src"] + "」", "size": "xs", "color": "#9A958A",
             "wrap": True, "margin": "xs"}]
    bubble = {"type": "bubble", "size": "mega",
              "header": {"type": "box", "layout": "vertical", "backgroundColor": GOLD,
                         "paddingAll": "12px", "contents": [
                             {"type": "text", "text": f"📇 {_yobina(f['contact'])}",
                              "color": "#FFFFFF", "weight": "bold", "size": "xs"},
                             {"type": "text", "text": f"{f['k']}　（残り{n}件）",
                              "color": "#FFFFFF", "weight": "bold", "size": "md", "margin": "xs",
                              "wrap": True}]},
              "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "contents": body}}
    return reply(token, head + [{
        "type": "flex", "altText": f"📇 {f['k']}：{f['v']}", "contents": bubble,
        "quickReply": _quick([("○ 合ってる", f"f=fact&a=ok&i={f['id']}"),
                              ("✕ 違う", f"f=fact&a=no&i={f['id']}"),
                              ("スキップ", f"f=fact&a=skip&i={f['id']}"),
                              ("やめる(続きは🔎から)", "m=home")])}])


def fact_fix_card(uid, token, f):
    set_state(uid, "factfix", {"fid": f["id"]})
    try:
        alts = json.loads(f["alts"] or "[]")
    except Exception:
        alts = []
    # 呼び名は外れたらLINE表示名が最有力。候補が無くても表示名(＋「さん」)を必ず入れる
    if f["k"] == "呼び名":
        contact = f["contact"]
        for cand in (contact, f"{contact}さん"):
            if cand and cand != f["v"] and cand not in alts:
                alts.append(cand)
    alts = alts[:4]
    # 表示に使った候補リストを保存(タップのjインデックスと一致させる)
    with db.conn() as c:
        c.execute("UPDATE linebot_facts SET alts=? WHERE id=?",
                  (json.dumps(alts, ensure_ascii=False), f["id"]))
    quick = [(a[:20], f"f=fact&a=alt&i={f['id']}&j={j}") for j, a in enumerate(alts)]
    quick.append(("この項目を消す", f"f=fact&a=del&i={f['id']}"))
    hint = ("候補をタップするか、正しい呼び名をタイプして送ってください。"
            if f["k"] == "呼び名" else
            "候補をタップするか、そのまま正しい内容をタイプして送ってください。")
    return reply(token, [flexmsg(f"✕ では「{f['k']}」の正しい内容は？", hint,
                                 accent=RED, quick=quick)])


def fact_action(uid, token, a, p):
    fid = p.get("i", "")
    f = _get_fact(int(fid)) if str(fid).isdigit() else None
    if not f or f["status"] not in ("pending", "applied"):
        return fact_card(token, prefix=[flexmsg("そのボタンは処理済みです☺️", accent=BLUE)])
    if a == "ok":
        apply_fact(f["contact"], f["k"], f["v"])
        _set_fact_status(f["id"], "applied")
        set_state(uid, "", {})
        return fact_card(token, prefix=[stamp(f"✓ {f['k']}＝{f['v']} で反映")])
    if a == "no":
        # 超重要(名前・誕生日・種別立場)は直す。それ以外は消して次へ(細かく訂正しない)
        if _is_critical(f["k"]):
            return fact_fix_card(uid, token, f)
        _set_fact_status(f["id"], "deleted")
        set_state(uid, "", {})
        return fact_card(token, prefix=[stamp(f"✕ {f['k']}は消しました")])
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
    if not f or f["status"] not in ("pending", "applied"):
        return fact_card(token, prefix=[flexmsg("入力先の項目が見つかりませんでした", accent=BLUE)])
    v = text.strip()[:80]
    apply_fact(f["contact"], f["k"], v)
    with db.conn() as c:
        c.execute("UPDATE linebot_facts SET status='fixed', v=? WHERE id=?", (v, f["id"]))
    return fact_card(token, prefix=[stamp(f"✓ {f['k']}を「{v}」に直して反映")])


# ---- 🧹 自動反映の見直し(非重要項目を後からまとめて修正・削除) ----

def _card_remove_attr(contact, k):
    from . import crm
    if k == "誕生日":
        with db.conn() as c:
            c.execute("UPDATE contacts SET birthday='' WHERE code=?", (contact,))
    else:
        with db.conn() as c:
            c.execute("DELETE FROM contact_attrs WHERE contact=? AND akey=?", (contact, k))


def review_card(token, contact, prefix=None):
    head = prefix or []
    items = reviewable_facts(contact)
    cq = _q(contact, safe="")
    if not items:
        return reply(token, head + [flexmsg("🧹 見直し完了",
                                            "自動で入れた情報の確認は済みました。",
                                            accent=GREEN,
                                            quick=[("🗂 カードへ", f"m=card&c={cq}"),
                                                   ("ホームへ", "m=home")])])
    f = items[0]
    n = len(items)
    body = [{"type": "text", "text": f["v"], "size": "md", "weight": "bold",
             "color": "#1B2A4A", "wrap": True}]
    if f.get("src"):
        body += [{"type": "separator", "margin": "md", "color": "#E3DCC9"},
                 {"type": "text", "text": "根拠「" + f["src"] + "」", "size": "xs",
                  "color": "#9A958A", "wrap": True, "margin": "sm"}]
    bubble = {"type": "bubble", "size": "mega",
              "header": {"type": "box", "layout": "vertical", "backgroundColor": "#5A6B4A",
                         "paddingAll": "12px", "contents": [
                             {"type": "text", "text": f"🧹 {_yobina(contact)}｜自動反映の見直し",
                              "color": "#FFFFFF", "weight": "bold", "size": "xs", "wrap": True},
                             {"type": "text", "text": f"{f['k']}　（残り{n}件）", "color": "#FFFFFF",
                              "weight": "bold", "size": "md", "margin": "xs", "wrap": True}]},
              "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "contents": body}}
    return reply(token, head + [{
        "type": "flex", "altText": f"🧹 {f['k']}", "contents": bubble,
        "quickReply": _quick([("○ このままでOK", f"f=rev&a=keep&i={f['id']}"),
                              ("✎ 直す", f"f=rev&a=fix&i={f['id']}"),
                              ("🗑 消す", f"f=rev&a=del&i={f['id']}"),
                              ("終わる", f"m=card&c={cq}")])}])


def rev_action(uid, token, a, p):
    fid = p.get("i", "")
    f = _get_fact(int(fid)) if str(fid).isdigit() else None
    if not f:
        return reply(token, [flexmsg("その項目は処理済みです☺️", accent=BLUE,
                                     quick=[("ホームへ", "m=home")])])
    contact = f["contact"]
    if a == "keep":
        _set_fact_status(f["id"], "reviewed")
        return review_card(token, contact, prefix=[stamp(f"○ {f['k']} はそのまま")])
    if a == "del":
        _card_remove_attr(contact, f["k"])
        _set_fact_status(f["id"], "deleted")
        return review_card(token, contact, prefix=[stamp(f"🗑 {f['k']} をカードから消しました")])
    if a == "fix":
        return fact_fix_card(uid, token, f)   # 直したら再反映(statusはfixedに)
    return review_card(token, contact)


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


def _yobina(code, attrs=None):
    """表示に使う呼び名。抽出済みなら「呼び名(表示名)」、無ければ表示名。"""
    from . import crm
    a = attrs if attrs is not None else crm.get_attrs(code)
    y = a.get("呼び名") or a.get("本名") or ""
    if y and y != code:
        return f"{y}({code})"
    return code


def crm_list_msgs(pg=0):
    from . import crm
    rows = _crm_contacts()
    if not rows:
        return [flexmsg("🗂 まだ顧客カードがありません",
                        "トーク履歴の.txtをこのトークに送ると、自動で作られます。",
                        quick=[("ホームへ", "m=home")])]
    # 直近のやりとり順を加味(ランク→直近)
    with db.conn() as c:
        last = {r["contact"]: r["mx"] for r in c.execute(
            "SELECT contact, MAX(ts) mx FROM messages GROUP BY contact")}
    rows.sort(key=lambda r: ({"S": 0, "A": 1}.get(r["rank"], 2), -(last.get(r["code"]) or 0)))
    total = len(rows)
    by_rank = {"S": 0, "A": 0, "B": 0}
    for r in rows:
        by_rank[r["rank"] if r["rank"] in by_rank else "B"] += 1
    page = rows[pg * PAGE:(pg + 1) * PAGE]
    lines = []
    for r in page:
        attrs = crm.get_attrs(r["code"])
        disp = _yobina(r["code"], attrs)
        extra = []
        if r.get("birthday"):
            extra.append(f"🎂{r['birthday']}")
        lt = last.get(r["code"])
        if lt:
            dd = int((time.time() - lt) / 86400)
            extra.append("今日やりとり" if dd == 0 else f"{dd}日前")
        n_attr = len(attrs)
        if n_attr:
            extra.append(f"情報{n_attr}件")
        lines.append(f"・{r['rank']}｜{disp}" + (f"　{'・'.join(extra)}" if extra else ""))
    quick = [(f"{r['rank']}｜{_yobina(r['code'])}"[:20], f"m=card&c={_q(r['code'], safe='')}")
             for r in page]
    if (pg + 1) * PAGE < total:
        quick.append((f"次の{min(PAGE, total-(pg+1)*PAGE)}人 →", f"m=crm&pg={pg+1}"))
    quick.append(("ホームへ", "m=home"))
    foot = "下のボタンで開きます👇"
    if by_rank["S"] == 0 and by_rank["A"] == 0 and total >= 3:
        foot = "まだ全員B。カードを開いて太客にS/Aを付けると、返信の優先順位に効きます👇"
    return [flexmsg(f"🗂 顧客カード {total}人（S{by_rank['S']}・A{by_rank['A']}・B{by_rank['B']}）",
                    "\n".join(lines), footer=foot, quick=quick)]


def style_msgs():
    """✍️ 何を学習したかの見える化(v80)。"""
    prof = db.get_profile("_global") or {}
    n = prof.get("n_messages") or 0
    if not n:
        return [flexmsg("✍️ まだ文体を学習していません",
                        "トーク履歴の.txtを送ると、あなたの言い回し・絵文字の癖を覚えます。",
                        quick=[("ホームへ", "m=home")])]
    lines = [f"学習した実例: {n}文"]
    if prof.get("avg_len"):
        lines.append(f"平均文長: 約{round(prof['avg_len'])}文字")
    if prof.get("emoji_per_msg") is not None:
        lines.append(f"絵文字: 1通あたり約{round(prof['emoji_per_msg'], 1)}個")
    if prof.get("top_emojis"):
        lines.append(f"よく使う絵文字: {''.join(prof['top_emojis'][:6])}")
    if prof.get("top_endings"):
        lines.append(f"よく使う文末: {'／'.join(prof['top_endings'][:4])}")
    ex = (prof.get("samples") or [])[:3]
    if ex:
        lines.append("\nあなたの実例(下書きはこれを真似ます):")
        for e in ex:
            lines.append(f"「{str(e)[:60]}」")
    with db.conn() as c:
        n_sent = c.execute("SELECT COUNT(*) FROM sent_replies").fetchone()[0]
    lines.append(f"\n送信からの追加学習: {n_sent}件(「案◯を転送した」を押すたびに増えます)")
    return [flexmsg("✍️ 覚えているあなたの文体", "\n".join(lines),
                    quick=[("🗂 顧客", "m=crm"), ("ホームへ", "m=home")])]


RANK_COLOR = {"S": "#C6A04A", "A": "#8FA2C2", "B": "#9A958A"}


def _row(label, value, accent="#B08A2E"):
    """台帳の1行: 左に小さな金ラベル、右に大きめの黒い値。"""
    return {"type": "box", "layout": "baseline", "spacing": "sm", "margin": "md",
            "contents": [
                {"type": "text", "text": label[:12], "color": accent, "size": "xs",
                 "weight": "bold", "flex": 3, "wrap": True},
                {"type": "text", "text": str(value)[:200], "color": "#2B2823", "size": "sm",
                 "flex": 7, "wrap": True}]}


def card_msgs(code):
    from . import crm
    d = crm.contact_detail(code)
    if not d:
        return [flexmsg("カードが見つかりませんでした", accent=BLUE, quick=[("🗂 一覧へ", "m=crm")])]
    attrs = d.get("attrs") or {}
    rank = d.get("rank") or "B"
    kind_lab = {"customer": "顧客", "staff": "店内", "peer": "同業", "private": "私用"}.get(
        d.get("kind") or "customer", "顧客")
    disp = _yobina(code, attrs)

    rows = []
    if d.get("birthday"):
        rows.append(_row("誕生日", "🎂 " + d["birthday"]))
    if attrs.get("年齢"):
        rows.append(_row("年齢", attrs["年齢"]))
    if d.get("last_visit_ts"):
        days = int((time.time() - d["last_visit_ts"]) / 86400)
        rows.append(_row("最終来店", "今日" if days == 0 else f"{days}日前"))
    if d.get("cycle_days"):
        rows.append(_row("来店周期", f"約{d['cycle_days']}日"))
    # 進行中の話・NG話題・関係性メモは目立つ色で先頭寄せ
    priority = ("進行中の話", "NG話題", "関係性メモ")
    shown = set()
    for pk in priority:
        if attrs.get(pk):
            col = "#C0402C" if pk == "NG話題" else ("#2E7D32" if pk == "進行中の話" else "#B08A2E")
            rows.append(_row(pk, attrs[pk], accent=col))
            shown.add(pk)
    for k, v in attrs.items():
        if k in shown or k in ("年齢", "呼び名", "本名"):
            continue
        rows.append(_row(k.replace("🌐", "🌐"), v))
    if d.get("tags"):
        rows.append(_row("タグ", d["tags"]))
    if d.get("note"):
        rows.append(_row("メモ", d["note"][:120]))
    al = [a for a in (d.get("aliases") or []) if a != code and a != disp]
    if al:
        rows.append(_row("別名", "・".join(al[:3])))
    if not rows:
        rows.append({"type": "text", "text": "まだ情報が少ないです。トーク履歴のtxtを送るか、"
                     "🔎で掘り直すと貯まります。", "size": "sm", "color": "#8D8674", "wrap": True,
                     "margin": "md"})

    header = {"type": "box", "layout": "vertical", "backgroundColor": "#141A2A",
              "paddingAll": "16px", "spacing": "sm", "contents": [
                  {"type": "box", "layout": "baseline", "contents": [
                      {"type": "text", "text": disp, "color": "#F0ECE2", "weight": "bold",
                       "size": "lg", "flex": 8, "wrap": True},
                      {"type": "text", "text": rank, "color": RANK_COLOR.get(rank, "#9A958A"),
                       "weight": "bold", "size": "xxl", "flex": 2, "align": "end"}]},
                  {"type": "box", "layout": "vertical", "height": "3px",
                   "backgroundColor": "#C6A04A", "contents": [], "margin": "sm", "width": "56px"},
                  {"type": "text",
                   "text": kind_lab + (f"・{_STAND_LABEL[d['stand']]}"
                                       if d.get("stand") in _STAND_LABEL else ""),
                   "color": "#B0A98F", "size": "xs"}]}
    bubble = {"type": "bubble", "size": "mega", "header": header,
              "body": {"type": "box", "layout": "vertical", "paddingAll": "16px",
                       "paddingTop": "12px", "contents": rows[:20]}}

    cq = _q(code, safe="")
    quick = [(f"S{'●' if rank == 'S' else ''}", f"f=crank&c={cq}&v=S"),
             (f"A{'●' if rank == 'A' else ''}", f"f=crank&c={cq}&v=A"),
             (f"B{'●' if rank == 'B' else ''}", f"f=crank&c={cq}&v=B")]
    with db.conn() as c:
        has_talk = c.execute("SELECT 1 FROM linebot_talks WHERE contact=?", (code,)).fetchone()
    nrev = len(reviewable_facts(code))
    if nrev:
        quick.append((f"🧹 自動反映を見直す({nrev})", f"m=review&c={cq}"))
    if persona_enabled():
        quick.append(("🧠 ペルソナ" + ("を見る" if get_persona(code) else "分析"),
                      f"m=persona&c={cq}"))
    if has_talk:
        quick.append(("🔎 AIで掘り直す", f"f=fact&a=dig&c={cq}"))
    quick.append(("🌐 ネットで調べる", f"f=fact&a=web&c={cq}"))
    quick += [("🗂 一覧へ", "m=crm"), ("ホームへ", "m=home")]
    return [{"type": "flex", "altText": f"🗂 {disp}", "contents": bubble,
             "quickReply": _quick(quick)}]


# ============ 📣 アナウンス配達(計画→一人ずつ下書き配達) ============

def _kind_of(code):
    return (db.get_contact(code) or {}).get("kind") or "customer"


def _ann_counts():
    """セグメントごとの人数を数える(顧客は種別=顧客のみ／同業・店内は別枠)。"""
    from . import campaign
    now = time.time()
    cust = [v for v in campaign.select_recipients(mode="greeting", ranks=["S", "A", "B"], now=now)
            if _kind_of(v["code"]) == "customer"]
    def n_tag(tag):
        return sum(1 for v in cust if tag in v["tags"])
    peers = [c for c in db.list_contacts() if (c.get("kind") == "peer")]
    staff = [c for c in db.list_contacts() if (c.get("kind") == "staff")]
    return {
        "cust": cust,
        "S": sum(1 for v in cust if v["rank"] == "S"),
        "A": sum(1 for v in cust if v["rank"] == "A"),
        "ALL": len(cust),
        "GB": len(estranged(now=now)),   # v87: 最終接触ベースの実判定
        "RV": n_tag("直近来店"),
        "BD": n_tag("誕生日近い"),
        "PEER": len(peers),
        "STAFF": len(staff),
    }


def start_ann(uid, token):
    """計画画面: 誰に配るか。既存の途中があれば続きから。"""
    st = get_state(uid)
    d = st["data"]
    if st["flow"] == "ann" and 0 < d.get("ai", 0) < len(d.get("q") or []):
        return reply(token, [flexmsg(f"📣 前回の配達が {d['ai']+1}/{len(d['q'])}人目 で止まっています",
                                     "続きからにしますか？",
                                     quick=[("続きから", "f=ann&a=resume"),
                                            ("最初から選び直す", "m=ann&re=1"),
                                            ("ホームへ", "m=home")])])
    c = _ann_counts()
    # お客様向け(季節のご挨拶トーン)
    quick = []
    if c["S"]:
        quick.append((f"S客だけ({c['S']})", "f=ann&a=plan&v=S"))
    if c["S"] + c["A"]:
        quick.append((f"S+A客({c['S']+c['A']})", "f=ann&a=plan&v=SA"))
    quick.append((f"全顧客({c['ALL']})", "f=ann&a=plan&v=ALL"))
    if c["GB"]:
        quick.append((f"久しぶりの客({c['GB']})", "f=ann&a=plan&v=GB"))
    if c["RV"]:
        quick.append((f"最近来た客({c['RV']})", "f=ann&a=plan&v=RV"))
    if c["BD"]:
        quick.append((f"誕生日近い({c['BD']})", "f=ann&a=plan&v=BD"))
    # お客様以外(トーンが変わる)
    if c["PEER"]:
        quick.append((f"同業の仲間({c['PEER']})", "f=ann&a=plan&v=PEER"))
    if c["STAFF"]:
        quick.append((f"店内の子・スタッフ({c['STAFF']})", "f=ann&a=plan&v=STAFF"))
    quick.append(("やめる", "m=home"))
    body = (f"お客様{c['ALL']}人（S{c['S']}・A{c['A']}）"
            + (f"／同業{c['PEER']}" if c["PEER"] else "")
            + (f"／店内{c['STAFF']}" if c["STAFF"] else "")
            + "\n\n誰に配りますか？（配達中も1人ずつスキップできます）\n"
            "※同業・店内は文面のトーンが自動で変わります。")
    return reply(token, [cover("📣 アナウンス配達", "まず配る相手を決めます"),
                         flexmsg("配る相手を選ぶ", body, quick=quick)])


# トーン: cust=季節のご挨拶(campaign) / peer=仲間口調 / staff=身内の連絡
_ANN_TONE = {"S": "cust", "SA": "cust", "ALL": "cust", "GB": "cust", "RV": "cust", "BD": "cust",
             "PEER": "peer", "STAFF": "staff"}


def ann_plan(uid, token, v):
    from . import campaign
    now = time.time()
    tone = _ANN_TONE.get(v, "cust")
    if v == "PEER":
        codes = [c["code"] for c in db.list_contacts() if c.get("kind") == "peer"]
    elif v == "STAFF":
        codes = [c["code"] for c in db.list_contacts() if c.get("kind") == "staff"]
    else:
        if v == "S":
            recips = campaign.select_recipients(mode="greeting", ranks=["S"], now=now)
        elif v == "SA":
            recips = campaign.select_recipients(mode="greeting", ranks=["S", "A"], now=now)
        elif v == "GB":
            # v87: 最終接触ベースの秘書判定。優先順(スコア降順)をそのまま配達順に
            codes = [e["code"] for e in estranged(now=now)]
            if not codes:
                return reply(token, [flexmsg("📣 ご無沙汰の客はいません✨",
                                             "みんな最近つながっています。",
                                             accent=GREEN, quick=[("ホームへ", "m=home")])])
            set_state(uid, "ann", {"q": codes, "ai": 0, "sent": 0, "skip": [], "v": v, "tone": "cust"})
            loading(uid, 20)
            return reply(token, [stamp(f"✓ ご無沙汰の{len(codes)}人に配ります（空きが長い順）")]
                         + ann_item_msgs(uid)[:4])
        elif v == "RV":
            recips = campaign.select_recipients(mode="greeting", tags=["直近来店"], now=now)
        elif v == "BD":
            recips = campaign.select_recipients(mode="greeting", tags=["誕生日近い"], now=now)
        else:
            recips = campaign.select_recipients(mode="greeting", ranks=["S", "A", "B"], now=now)
        codes = [r["code"] for r in recips if _kind_of(r["code"]) == "customer"]
    if not codes:
        return reply(token, [flexmsg("📣 対象の相手がいません", "ランクや種別を確認するか、別の条件でお試しを。",
                                     accent=BLUE, quick=[("ホームへ", "m=home")])])
    set_state(uid, "ann", {"q": codes, "ai": 0, "sent": 0, "skip": [], "v": v, "tone": tone})
    loading(uid, 20)
    return reply(token, [stamp(f"✓ {len(codes)}人に配ります")] + ann_item_msgs(uid)[:4])


def _casual_draft(code, tone):
    """同業・店内向けの一言(トーン別)。API無ければテンプレ。"""
    nm = _yobina(code)
    tmpl = {"peer": f"{nm}、ごぶさた！元気にしてる？また近いうちご飯でも行こ〜",
            "staff": f"{nm}、いつもありがとう！また一緒のお店入るとき　よろしくね😊"}[tone]
    if not config.ANTHROPIC_API_KEY:
        return tmpl
    from . import db as _db
    prof = _db.get_profile("_global") or {}
    ex = "／".join((prof.get("samples") or [])[:3])
    role = ("同業(同じ夜職の仲間)" if tone == "peer" else "自分の店の後輩・黒服・スタッフ")
    # v114: 店内の性別で相手像を具体化(店内女=ヘルプ/ママ, 店内男=黒服/ボーイ)
    if tone == "staff":
        try:
            from . import crm as _crm
            sg = (_crm.get_attrs(code) or {}).get("店内区分") or ""
        except Exception:
            sg = ""
        if sg == "女":
            role = "自分の店の女性スタッフ(ヘルプの子・ママ・女性キャスト)"
        elif sg == "男":
            role = "自分の店の男性スタッフ(黒服・ボーイ・店長)"
    prompt = (
        f"あなたは本人の下書き係。{role}である「{nm}」へ送る、久しぶりの軽い一言を1つ作る。\n"
        "営業や堅い挨拶にしない。友達/身内へのLINEの軽さ。短く。押し付けない。\n"
        + (f"本人の実際の口調の例:{ex}\n" if ex else "")
        + '出力はJSONのみ: {"text":"..."}')
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
                          headers={"x-api-key": config.ANTHROPIC_API_KEY,
                                   "anthropic-version": "2023-06-01", "content-type": "application/json"},
                          json={"model": config.ANTHROPIC_MODEL, "max_tokens": 300,
                                "messages": [{"role": "user", "content": prompt}]}, timeout=30)
        if r.status_code != 200:
            return tmpl
        out = "".join(b.get("text", "") for b in r.json().get("content", []))
        out = out.replace("```json", "").replace("```", "").strip()
        return json.loads(out[out.index("{"):out.rindex("}") + 1]).get("text") or tmpl
    except Exception:
        return tmpl


def ann_item_msgs(uid):
    from . import campaign
    st = get_state(uid)
    d = st["data"]
    q = d.get("q") or []
    i = d.get("ai", 0)
    tone = d.get("tone", "cust")
    if i >= len(q):
        done = f"📣 配達完了！{d.get('sent', 0)}人に送りました"
        if d.get("skip"):
            done += f"・{len(d['skip'])}人スキップ"
        set_state(uid, "", {})
        return [flexmsg(done, "送った分は記録しました。返信が来たら📨に入ります。",
                        accent=GREEN, quick=[("ホームへ", "m=home"), ("📨 返信を見る", "m=rep")])]
    code = q[i]
    if tone == "cust":
        try:
            gen = campaign.generate(mode="greeting", codes=[code])
            item = (gen.get("items") or [{}])[0]
            text = item.get("text") or f"{_yobina(code)}、ごぶさたしてます！お元気ですか？"
            why = item.get("why") or ""
        except Exception:
            text, why = f"{_yobina(code)}、ごぶさたしてます！お元気ですか？", ""
        # ご無沙汰配達では「何日空いたか・普段の周期」を秘書メモとして添える
        if d.get("v") == "GB":
            last = _last_interaction(code)
            if last:
                gap = int((time.time() - last) / 86400)
                pi = _personal_interval(code)
                why = (f"最後のやりとりから{gap}日"
                       + (f"（普段は約{pi}日に一度）" if pi else "") + "・そろそろ一声を")
    else:
        text = _casual_draft(code, tone)
        why = "同業(仲間)" if tone == "peer" else "店内・スタッフ"
    card = flexmsg(f"📣 {i+1}/{len(q)}人目｜{_yobina(code)}", why,
                   footer=f"{FWD}【{code} 宛】")
    return [card, txt(text, quick=[
        ("転送した→次へ", "f=ann&a=sent"),
        ("自分で書いた→次へ", "f=ann&a=self"),
        ("スキップ", "f=ann&a=skip"),
        ("やめる(続きは保存)", "f=ann&a=quit"),
    ])]


def ann_action(uid, token, a):
    st = get_state(uid)
    d = st["data"]
    q = d.get("q") or []
    i = d.get("ai", 0)
    if st["flow"] != "ann" or i >= len(q):
        return reply(token, wrong_flow(st))
    if a == "resume":
        loading(uid, 20)
        return reply(token, ann_item_msgs(uid)[:5])
    if a == "quit":
        return reply(token, [flexmsg(f"中断しました（{i+1}人目の手前で保存）",
                                     "📣を押すと「続きから」を選べます。",
                                     quick=[("ホームへ", "m=home")])])
    cur = q[i]
    if a in ("sent", "self"):
        d["sent"] = d.get("sent", 0) + 1
        # 顧客向けの転送のみ学習(同業・店内は口調が別なので学習に混ぜない)
        if a == "sent" and d.get("tone", "cust") == "cust":
            try:
                from . import campaign
                gen = campaign.generate(mode="greeting", codes=[cur])
                sent_text = (gen.get("items") or [{}])[0].get("text", "")
                if sent_text:
                    from .style_profile import learn_from_sent
                    learn_from_sent(cur, sent_text, edited=0, edit_ratio=100)
            except Exception:
                pass
        stp = stamp(f"✓ {_yobina(cur)}に" + ("送信(転送) " if a == "sent" else "送信(自分の文) ") + jst_hm())
    elif a == "skip":
        d.setdefault("skip", []).append(cur)
        stp = stamp(f"↷ {_yobina(cur)}はスキップ")
    else:
        return reply(token, wrong_flow(st))
    d["ai"] = i + 1
    set_state(uid, "ann", d)
    return reply(token, [stp] + ann_item_msgs(uid)[:4])


# ============ 🙏 お席記録 → お礼配達 ============

def _orei_cands(kinds, limit=6, exclude=None):
    """お礼相手の候補(種別で絞り・直近やりとり順)。"""
    exclude = set(exclude or [])
    with db.conn() as c:
        last = {r["contact"]: r["mx"] for r in c.execute(
            "SELECT contact, MAX(ts) mx FROM messages GROUP BY contact")}
    out = []
    for ct in db.list_contacts():
        if ct.get("linked") == 0:
            continue
        k = ct.get("kind") or "customer"
        if k not in kinds or ct["code"] in exclude:
            continue
        out.append(ct["code"])
    out.sort(key=lambda code: ({"S": 0, "A": 1}.get((db.get_contact(code) or {}).get("rank"), 2),
                               -(last.get(code) or 0)))
    return out[:limit]


def start_orei(uid, token):
    """今夜の立場から。①自分が主役(自分のお席) ②自分がヘルプ(他の子の席)。"""
    set_state(uid, "orei", {})
    return reply(token, [cover("🙏 お礼を配る", "今夜のお席を記録して、お礼を一巡します"),
                         flexmsg("今夜のあなたの立場は？",
                                 "主役＝あなたのお客様のお席。ヘルプ＝他の子の席に入った夜。",
                                 quick=[("自分が主役（自分の客）", "f=orei&a=role&v=host"),
                                        ("自分はヘルプ（他の子の席）", "f=orei&a=role&v=help"),
                                        ("やめる", "m=home")])])


# ---- 主役モード ----

def orei_main_pick(uid, token):
    cand = _orei_cands({"customer"}, 6)
    if not cand:
        return reply(token, [flexmsg("顧客カードがまだありません",
                                     "先にトーク取り込みや仕分けで顧客を登録してください。",
                                     accent=BLUE, quick=[("ホームへ", "m=home")])])
    quick = [(_yobina(c)[:20], f"f=orei&a=main&c={_q(c, safe='')}") for c in cand]
    quick.append(("やめる", "m=home"))
    return reply(token, [flexmsg("今夜の主賓（あなたのお客様）は？", "顧客カードから選んでください。",
                                 quick=quick)])


def orei_flow_pick(uid, main):
    return [flexmsg(f"🍶 主賓＝{_yobina(main)}", "今夜の流れは？（同伴・アフターは実績にも残ります）",
                    quick=[("店内のみ", f"f=orei&a=flow&v=in&c={_q(main, safe='')}"),
                           ("同伴あり", f"f=orei&a=flow&v=dohan&c={_q(main, safe='')}"),
                           ("アフターあり", f"f=orei&a=flow&v=after&c={_q(main, safe='')}"),
                           ("店外のみ", f"f=orei&a=flow&v=gaiso&c={_q(main, safe='')}")])]


def orei_record_main(uid, token, main, typ):
    from . import sittings
    date_label = time.strftime("%m/%d", time.gmtime(time.time() + 9 * 3600)).lstrip("0")
    kw, stype = {}, ""
    if typ == "dohan":
        kw["dohan_venue"] = "同伴"
    elif typ == "after":
        kw["after_venue"] = "アフター"
    elif typ == "gaiso":
        stype = "gaiso"
    try:
        sid = sittings.create_sitting(date_label, main,
                                      [{"contact": main, "role": "customer", "stand": "equal"}],
                                      stype=stype, **kw)
    except Exception as e:
        print(f"[linebot orei] {e}", flush=True)
        sid = 0
    d = get_state(uid)["data"]
    d.update({"sid": sid, "main": main, "typ": typ, "stype": stype, "kw": kw, "self": "host"})
    set_state(uid, "orei", d)
    return reply(token, [stamp(f"✓ お席を記録（主賓 {_yobina(main)}・"
                               + {"in": "店内", "dohan": "同伴", "after": "アフター", "gaiso": "店外"}[typ]
                               + "）来店実績も付きました")]
                 + _orei_draft_msgs(uid, main, "customer", "equal"))


def _orei_draft_msgs(uid, contact, role, stand):
    """指定の相手×役割のお礼下書きを出す(AI優先・失敗時テンプレ)。"""
    from . import sittings
    d = get_state(uid)["data"]
    main = d.get("main", "")
    stype = d.get("stype", "")
    kw = d.get("kw", {})
    text = None
    if role == "customer":   # 主賓客はAIで本人口調
        try:
            from . import campaign
            text = (campaign.generate(mode="thanks", codes=[contact]).get("items") or [{}])[0].get("text")
        except Exception:
            text = None
    if not text:
        try:
            text = sittings.orei_text(role, stand, _yobina(contact), _yobina(main), stype, "",
                                      kw.get("dohan_venue", ""), kw.get("after_venue", ""))
        except Exception:
            text = f"{_yobina(contact)}、今夜はありがとうございました！"
    d["pending_sent"] = {"contact": contact, "role": role, "text": text}
    set_state(uid, "orei", d)
    return [flexmsg(f"下書き【{_yobina(contact)} 宛】",
                    {"customer": "主賓へのお礼", "intro": "紹介者へのお礼", "guest": "同席客へのお礼",
                     "afterhost": "アフター先へのお礼", "help": "ヘルプの子・スタッフへ",
                     "host": "呼んでくれた方へ", "guesthost": "お客様（ゲスト立場）へ"}.get(role, "お礼"),
                    footer=f"{FWD}【{contact} 宛】"),
            txt(text, quick=[("転送した→次へ", "f=orei&a=sent"),
                             ("自分で書いた→次へ", "f=orei&a=sent"),
                             ("この人は送らない", "f=orei&a=hub"),
                             ("ホームへ", "m=home")])]


def orei_hub(uid, token, note=None):
    """主役モードの中継: 他にお礼を出す相手を足せる。"""
    d = get_state(uid)["data"]
    added = d.get("added", [])
    pre = [stamp(note)] if note else []
    quick = [("同席のお客様", "f=orei&a=add&r=guest"),
             ("紹介者", "f=orei&a=add&r=intro"),
             ("ヘルプの子・スタッフ", "f=orei&a=add&r=help"),
             ("アフター先の人", "f=orei&a=add&r=afterhost"),
             ("これで完了", "f=orei&a=fin")]
    body = ("主賓へのお礼はできました。同席した人にもお礼を出せます。\n"
            + (f"（追加済み: {len(added)}人）" if added else "") + "\n相手の種類を選んでください。")
    return reply(token, pre + [flexmsg("🙏 他にもお礼を出しますか？", body, accent=GOLD, quick=quick)])


def orei_add_pick(uid, token, role):
    d = get_state(uid)["data"]
    exclude = [d.get("main", "")] + [a["contact"] for a in d.get("added", [])]
    kinds = {"guest": {"customer"}, "intro": {"customer"},
             "help": {"staff", "peer"}, "afterhost": {"peer", "staff"}}.get(role, {"customer"})
    cand = _orei_cands(kinds, 6, exclude=exclude)
    if not cand:
        return orei_hub(uid, token, note="その種類の登録相手がいません")
    d["add_role"] = role
    set_state(uid, "orei", d)
    lab = {"guest": "同席のお客様", "intro": "紹介者", "help": "ヘルプの子・スタッフ",
           "afterhost": "アフター先の人"}[role]
    quick = [(_yobina(c)[:20], f"f=orei&a=addpick&c={_q(c, safe='')}") for c in cand]
    quick.append(("戻る", "f=orei&a=hub"))
    return reply(token, [flexmsg(f"{lab}を選ぶ", "タップした人にお礼下書きを作ります。", quick=quick)])


# ---- ヘルプモード(自分がヘルプに入った夜) ----

def orei_help_host_pick(uid, token):
    cand = _orei_cands({"peer", "staff"}, 6)
    quick = [(_yobina(c)[:20], f"f=orei&a=hhost&c={_q(c, safe='')}") for c in cand]
    quick.append(("戻る", "m=orei"))
    body = ("あなたを呼んでくれた子（先輩・同僚・ママ）は誰でしたか？\n"
            "※お客様はその子のお客様なので、あなたからのお礼は"
            "『呼んでもらった感謝』＋『ゲストとしての挨拶』になります。")
    return reply(token, [flexmsg("🙏 ヘルプのお礼", body, accent=BLUE, quick=quick)])


def _help_host_draft(host):
    """呼んでくれた子への礼(立場別)。"""
    stand = (db.get_contact(host) or {}).get("stand", "equal")
    nm = _yobina(host)
    if stand == "senior":
        return f"{nm}、今日はお席に呼んでくださってありがとうございました！とても勉強になりました。またぜひ入らせてください。"
    if stand == "junior":
        return f"{nm}、今日はありがとう〜！楽しかったし助けになれてたら嬉しい。また声かけてね。"
    return f"{nm}、今日はお席に呼んでくれてありがとう！すごく楽しかった。また一緒に入れたら嬉しいな。"


def _help_guest_draft(guest):
    """他の子のお客様への、ゲスト立場の軽い挨拶(営業クローズはしない)。"""
    nm = _yobina(guest)
    return f"{nm}、今夜はご一緒させていただき嬉しかったです！お話楽しくて時間があっという間でした。ありがとうございました。"


def orei_action(uid, token, a, p):
    st = get_state(uid)
    if st["flow"] != "orei":
        return reply(token, wrong_flow(st))
    d = st["data"]

    if a == "role":
        if p.get("v") == "help":
            d["self"] = "help"
            set_state(uid, "orei", d)
            return orei_help_host_pick(uid, token)
        d["self"] = "host"
        set_state(uid, "orei", d)
        return orei_main_pick(uid, token)

    # --- 主役モード ---
    if a == "main":
        main = _uq(p.get("c", ""))
        if not db.get_contact(main):
            return reply(token, wrong_flow(st))
        return reply(token, orei_flow_pick(uid, main))
    if a == "flow":
        return orei_record_main(uid, token, _uq(p.get("c", "")), p.get("v", "in"))
    if a == "sent":
        ps = d.get("pending_sent")
        if ps:
            # 記録＆学習
            try:
                from . import sittings
                if d.get("sid") and ps["contact"] == d.get("main"):
                    sittings.mark_sent(d["sid"], ps["contact"])
                if ps["role"] == "customer" and ps.get("text"):
                    from .style_profile import learn_from_sent
                    learn_from_sent(ps["contact"], ps["text"], edited=0, edit_ratio=100)
            except Exception:
                pass
            d.setdefault("added", []).append({"contact": ps["contact"], "role": ps["role"]})
            d["pending_sent"] = None
            set_state(uid, "orei", d)
        if d.get("self") == "help":
            return orei_help_after(uid, token, note=f"✓ {_yobina(ps['contact']) if ps else ''}に送信")
        return orei_hub(uid, token, note=f"✓ {_yobina(ps['contact']) if ps else ''}に送信 {jst_hm()}")
    if a == "hub":
        return orei_hub(uid, token)
    if a == "add":
        return orei_add_pick(uid, token, p.get("r", "guest"))
    if a == "addpick":
        c0 = _uq(p.get("c", ""))
        role = d.get("add_role", "guest")
        stand = (db.get_contact(c0) or {}).get("stand", "equal")
        # 同席顧客/紹介者はsittingのメンバーにも追加(実績の一貫性)
        try:
            from . import sittings
            if d.get("sid"):
                with db.conn() as c:
                    c.execute("INSERT INTO sitting_members(sitting_id,contact,role,stand,sent) "
                              "VALUES(?,?,?,?,0)", (d["sid"], c0, role, stand))
        except Exception:
            pass
        return reply(token, _orei_draft_msgs(uid, c0, role, stand))
    if a == "fin":
        set_state(uid, "", {})
        return reply(token, [flexmsg("🙏 お礼の配達、完了です",
                                     f"今夜は{len(d.get('added', []))}人にお礼を用意しました。おつかれさまでした。",
                                     accent=GREEN, quick=[("📣 アナウンス", "m=ann"), ("ホームへ", "m=home")])])

    # --- ヘルプモード ---
    if a == "hhost":
        host = _uq(p.get("c", ""))
        if not db.get_contact(host):
            return reply(token, wrong_flow(st))
        d["host"] = host
        set_state(uid, "orei", d)
        text = _help_host_draft(host)
        d["pending_sent"] = {"contact": host, "role": "host", "text": text}
        set_state(uid, "orei", d)
        return reply(token, [flexmsg(f"下書き【{_yobina(host)} 宛】", "呼んでくれた方へのお礼",
                                     footer=f"{FWD}【{host} 宛】"),
                             txt(text, quick=[("転送した→次へ", "f=orei&a=sent"),
                                              ("自分で書いた→次へ", "f=orei&a=sent"),
                                              ("スキップ", "f=orei&a=helpguest"),
                                              ("ホームへ", "m=home")])])
    if a == "helpguest":
        return orei_help_after(uid, token)
    if a == "guestpick":
        guest = _uq(p.get("c", ""))
        if not db.get_contact(guest):
            return reply(token, wrong_flow(st))
        text = _help_guest_draft(guest)
        d["guest_done"] = 1
        d["pending_sent"] = {"contact": guest, "role": "guesthost", "text": text}
        set_state(uid, "orei", d)
        return reply(token, [flexmsg(f"下書き【{_yobina(guest)} 宛】", "お客様へ（ゲスト立場の挨拶）",
                                     footer=f"{FWD}【{guest} 宛】"),
                             txt(text, quick=[("転送した→完了", "f=orei&a=sent"),
                                              ("自分で書いた→完了", "f=orei&a=sent"),
                                              ("ホームへ", "m=home")])])
    if a == "fin2":
        d["guest_done"] = 1
        set_state(uid, "orei", d)
        return orei_help_after(uid, token, note="お客様へは送りません")

    return reply(token, wrong_flow(st))


def orei_help_after(uid, token, note=None):
    """ヘルプモード: 呼んでくれた方の後、お客様(ゲスト立場)にも挨拶するか。"""
    d = get_state(uid)["data"]
    if d.get("guest_done"):
        set_state(uid, "", {})
        pre = [stamp(note)] if note else []
        return reply(token, pre + [flexmsg("🙏 ヘルプのお礼、完了です", "今夜もおつかれさまでした。",
                                           accent=GREEN, quick=[("ホームへ", "m=home")])])
    cand = _orei_cands({"customer"}, 6, exclude=[d.get("host", "")])
    pre = [stamp(note)] if note else []
    quick = [(_yobina(c)[:20], f"f=orei&a=guestpick&c={_q(c, safe='')}") for c in cand]
    quick.append(("お客様には送らない→完了", "f=orei&a=fin2"))
    return reply(token, pre + [flexmsg("🍷 お客様（その子のお客様）にもご挨拶しますか？",
                                       "送る場合はゲスト立場の軽いお礼にします（『また来てね』等の"
                                       "営業クローズはしません）。相手を選ぶか、送らないで完了。",
                                       accent=GOLD, quick=quick)])


# ============ ルーター ============

def _liff_redirect_card(hash_=""):
    """v104: LIFF一本化後、チャットの旧ボタン/コマンドは1枚のカードでLIFFへ誘導するだけ。
    (チャットとLIFFの行き来をなくす=チャットはUIを展開しない)"""
    liff_id = os.environ.get("CHOUBA_LIFF_ID", "")
    if not liff_id:
        return None
    return [{"type": "flex", "altText": "🏮 帳場を開く",
             "contents": {"type": "bubble", "body": {
                 "type": "box", "layout": "vertical", "paddingAll": "16px", "contents": [
                     {"type": "text", "text": "🏮 操作は帳場の画面に集約しました",
                      "weight": "bold", "size": "sm", "color": "#1B2A4A", "wrap": True},
                     {"type": "text", "text": "下のメニューか、このボタンからどうぞ",
                      "size": "xs", "color": "#6B6455", "margin": "sm"}]},
                 "footer": {"type": "box", "layout": "vertical", "contents": [
                     {"type": "button", "style": "primary", "color": "#A8842F",
                      "action": {"type": "uri", "label": "帳場を開く",
                                 "uri": f"https://liff.line.me/{liff_id}{hash_}"}}]}}}]


# LIFF一本化後もチャットに残す操作(txt整備の○✕確認・見直し・修正入力)
_CHAT_KEEP_M = ("fact", "review", "home", "unbind2", "card", "style", "persona", "ptoggle")


def route_postback(uid, data, token):
    p = dict(kv.split("=", 1) for kv in (data or "").split("&") if "=" in kv)
    m = p.get("m")
    # v104: LIFF設定済みなら、旧チャットUIのタイル/ボタンはLIFF誘導カード1枚に置き換え
    if os.environ.get("CHOUBA_LIFF_ID", ""):
        _redir = {"rep": "#inbox", "crm": "#list", "news": "#news", "dash": "#home",
                  "ann": "#ann", "orei": "#orei", "anni": "#home"}
        f_ = p.get("f")
        if (m in _redir) or (f_ in ("rep", "ann", "orei") and get_state(uid)["flow"] == ""):
            card = _liff_redirect_card(_redir.get(m, "#home"))
            if card:
                return reply(token, card)
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
    if m == "style":
        return reply(token, style_msgs())
    if m == "review":
        return review_card(token, _uq(p.get("c", "")))
    if m == "persona":
        if not persona_enabled():
            return reply(token, [flexmsg("🧠 ペルソナ分析はOFFです",
                                         "📊状況からONにできます。",
                                         accent=BLUE, quick=[("📊 状況へ", "m=dash"),
                                                             ("ホームへ", "m=home")])])
        return reply(token, persona_msgs(_uq(p.get("c", ""))))
    if m == "ptoggle":
        set_persona_enabled(not persona_enabled())
        return reply(token, dash_msgs())
    if m == "fact":
        n = len(pending_facts())
        if n:
            return fact_card(token, prefix=[cover("🔎 カード整備",
                                                  f"抽出した{n}件を確認します。全部タップ、1件5秒")])
        digs = _dig_status()
        running, errors, zero = [], [], []
        for c_, s_ in digs.items():
            if s_.startswith("running"):
                try:
                    t0 = int(s_.split(":")[1])
                except Exception:
                    t0 = 0
                if time.time() - t0 > 300:
                    # 再起動等でスレッドが死んだ掘り→時限で失敗扱いにする(永遠の「掘っています」防止)
                    _meta_set(f"dig_{c_}", "error:中断されました(再デプロイ等)")
                    errors.append((c_, "中断されました(再デプロイ等)"))
                else:
                    running.append(c_)
            elif s_.startswith("error:"):
                errors.append((c_, s_[6:]))
            elif s_.startswith("done:"):
                # done:ncrit:nauto → 両方0なら「見つからなかった」
                parts = s_.split(":")
                nums = [int(x) for x in parts[1:] if x.isdigit()]
                if nums and sum(nums) == 0:
                    zero.append(c_)
        if running:
            return reply(token, [flexmsg("🔎 いま掘っています…",
                                         f"対象: {'・'.join(running[:3])}\n30秒ほどしてから"
                                         "もう一度このボタンを押してください。",
                                         accent=BLUE, quick=[("🔎 もう一度", "m=fact"),
                                                             ("ホームへ", "m=home")])])
        if errors:
            c0, why = errors[0]
            return reply(token, [flexmsg(f"🔎 {c0} の抽出に失敗しました",
                                         f"理由: {why}\nもう一度掘り直せます👇",
                                         accent=RED,
                                         quick=[("🔁 掘り直す", f"f=fact&a=dig&c={_q(c0, safe='')}"),
                                                ("ホームへ", "m=home")])])
        # 何も無い: 0件成功の説明+掘り直せる相手(原文保存済み)の案内
        with db.conn() as c:
            talks = [r["contact"] for r in c.execute(
                "SELECT contact FROM linebot_talks ORDER BY ts DESC LIMIT 3")]
        quick = [(f"🔁 {t}を掘り直す"[:20], f"f=fact&a=dig&c={_q(t, safe='')}") for t in talks]
        quick += [("🗂 顧客を見る", "m=crm"), ("ホームへ", "m=home")]
        body = ("トーク履歴の.txtを送ると、AIがカードに載せる情報を拾ってここに並べます。"
                + ("掘り直しもできます👇" if talks else ""))
        title = "🔎 確認待ちの抽出はありません"
        if zero:
            title = f"🔎 {zero[0]} からは載せられる情報が見つかりませんでした"
            body = ("トークが雑談中心だとこうなります(異常ではありません)。"
                    "掘り直すか、別のトーク履歴も送ってみてください👇")
            for z in zero:
                _meta_set(f"dig_{z}", "seen")
        return reply(token, [flexmsg(title, body, quick=quick)])
    if m == "news":
        return reply(token, news_msgs())
    if m == "anni":
        return reply(token, anni_msgs())
    if m == "dash":
        return reply(token, dash_msgs())
    if m == "ann":
        if p.get("re"):
            set_state(uid, "", {})
        return start_ann(uid, token)
    if m == "orei":
        return start_orei(uid, token)
    f = p.get("f")
    if f == "rep":
        return rep_action(uid, token, p.get("a", ""), p)
    if f == "ann":
        if p.get("a") == "plan":
            return ann_plan(uid, token, p.get("v", "ALL"))
        return ann_action(uid, token, p.get("a", ""))
    if f == "orei":
        return orei_action(uid, token, p.get("a", ""), p)
    if f == "rev":
        return rev_action(uid, token, p.get("a", ""), p)
    if f == "fact":
        if p.get("a") == "dig":
            c0 = _uq(p.get("c", ""))
            with db.conn() as c:
                has = c.execute("SELECT 1 FROM linebot_talks WHERE contact=?", (c0,)).fetchone()
            if not c0 or not has:
                return reply(token, [flexmsg("原文が見つかりませんでした",
                                             "その相手のトーク履歴.txtをもう一度送ってください。",
                                             accent=RED, quick=[("ホームへ", "m=home")])])
            dig_async(c0)
            return reply(token, [flexmsg(f"🔎 {c0} を掘り直しています…",
                                         "長いトークは分割して全文を読みます(1〜2分)。"
                                         "終わったら🔎整備を押してください。",
                                         accent=BLUE, quick=[("🔎 整備を開く", "m=fact"),
                                                             ("ホームへ", "m=home")])])
        if p.get("a") == "web":
            c0 = _uq(p.get("c", ""))
            if not c0 or not db.get_contact(c0):
                return reply(token, [flexmsg("カードが見つかりませんでした", accent=RED,
                                             quick=[("ホームへ", "m=home")])])
            web_async(c0)
            db.track("linebot_web_research")
            return reply(token, [flexmsg(f"🌐 {c0} を公開情報から調べています…",
                                         "カードの手がかり(本名・会社など)と一致する情報だけを拾います。"
                                         "1〜3分後に🔎整備を押してください。\n"
                                         "見つかった情報も○✕で確認してからカードに載ります。",
                                         accent=BLUE, quick=[("🔎 整備を開く", "m=fact"),
                                                             ("ホームへ", "m=home")])])
        return fact_action(uid, token, p.get("a", ""), p)
    if f == "persona":
        if p.get("a") == "run":
            c0 = _uq(p.get("c", ""))
            if not c0 or not db.get_contact(c0):
                return reply(token, [flexmsg("カードが見つかりませんでした", accent=RED,
                                             quick=[("ホームへ", "m=home")])])
            persona_async(c0)
            db.track("linebot_persona")
            return reply(token, [flexmsg(f"🧠 {_yobina(c0)} を分析しています…",
                                         "会話全体を読み込みます(30秒〜1分)。"
                                         "終わったら🧠を押すと結果が出ます。",
                                         accent=BLUE, quick=[("🧠 結果を見る", f"m=persona&c={_q(c0, safe='')}"),
                                                             ("ホームへ", "m=home")])])
        return reply(token, home_msgs())
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
            elif t in ("文体", "学習"):
                reply(token, style_msgs())
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


@router.get("/line/reset")
@router.post("/line/reset")
def line_reset(key: str = "", confirm: str = "", full: str = ""):
    """【テスト用】データ消去。/line/reset?key=<INGEST_TOKEN>&confirm=RESET
    v117: 既定では「顧客・学習・お席・抽出・受信ログ」を消し、**リーダー接続とひも付けは保持**
    (リセットのたびに再接続・再ひも付けが要る問題を解消)。
    端末引き渡し等で完全初期化したい時だけ &full=1 を付ける(接続もひも付けも消える)。"""
    if not config.INGEST_TOKEN or key != config.INGEST_TOKEN:
        return Response(status_code=403)
    if confirm != "RESET":
        return {"ok": False,
                "warning": "データが消えます。取り消せません。",
                "how": "URLの末尾に &confirm=RESET を付けて、もう一度開いてください。"}
    is_full = str(full) in ("1", "true", "yes")
    # リーダー接続(reader_tokens/reader_codes)は既定で保護=リセット後も繋がったまま
    protect_tables = set() if is_full else {"reader_tokens", "reader_codes"}
    wiped, kept = [], []
    with db.conn() as c:
        tables = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for t in tables:
            if t in protect_tables:
                kept.append(t)
                continue
            try:
                if t == "linebot_meta" and not is_full:
                    # ひも付け(owner)だけ残し、他のメタ(受信係状態・掘り等)は消す
                    # owner=ひも付け / reader_hb・reader_batt=リーダー死活(消すと監視が「未接続」誤報)
                    c.execute("DELETE FROM linebot_meta WHERE k NOT IN ('owner','reader_hb','reader_batt')")
                else:
                    c.execute(f"DELETE FROM {t}")
                wiped.append(t)
            except Exception as e:
                print(f"[reset {t}] {e}", flush=True)
    try:
        db.init()
    except Exception:
        pass
    ensure()
    nxt = ("まっさらになりました。リーダー接続とひも付けは保持したので、"
           "そのまま使えます(再接続・合言葉の再送は不要)。" if not is_full
           else "完全初期化しました。トークに玄関パスワードを送ってひも付けし直し、"
                "リーダーもQRで再接続してください。")
    return {"ok": True, "wiped": wiped, "kept": kept, "full": is_full, "next": nxt}


@router.get("/line/setup")
@router.post("/line/setup")
def line_setup(key: str = ""):
    """リッチメニュー作成。デプロイ後にブラウザで /line/setup?key=<INGEST_TOKEN> を開く。"""
    if not config.INGEST_TOKEN or key != config.INGEST_TOKEN:
        return Response(status_code=403)
    liff_id = os.environ.get("CHOUBA_LIFF_ID", "")
    if liff_id:
        # v104: LIFF一本化=4大タイル(全部LIFF直行)。チャットにUIを展開するタイルは廃止
        img_path = os.path.join(os.path.dirname(__file__), "static", "lineimg", "richmenu_liff.png")
        if not os.path.exists(img_path):
            return {"error": "richmenu_liff.png がありません"}
        _base = f"https://liff.line.me/{liff_id}"
        cw = 2496 // 4
        areas = [{"bounds": {"x": i * cw, "y": 0, "width": cw, "height": 842},
                  "action": {"type": "uri", "uri": f"{_base}{h}"}}
                 for i, h in enumerate(["#home", "#inbox", "#list", "#ann"])]
    else:
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
