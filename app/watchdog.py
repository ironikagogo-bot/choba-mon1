"""受信係(帳場リーダー)の死活監視 (v97)。

2026-08-06のMichiさん障害(リーダー無音死・通知は正常表示のまま)の再発防止。
「静かな夜」と「リーダーの死」を区別し、止まっていたら本人とAkiに能動的に知らせる。

3層構え:
1. ハートビート: リーダーv0.5が POST /api/android/heartbeat を10分毎に打つ(旧版は未対応
   でもOK=受信メッセージのtsで代用。ただし静かな夜と区別できない旨は表示で正直に言う)
2. 監視スレッド: 10分毎に無音時間を判定。しきい値超えでLINE push(1エピソード1回)
   +Webプッシュ。復帰したら「復帰しました」を1回。
3. 外部監視の口: GET /healthz/reader は停止中503を返す(UptimeRobot等を挿せる)。
   ※Render自体のヘルスチェックには /healthz (常時200) を使うこと。503だと再起動ループになる。

環境変数:
- CHOUBA_READER_ALERT_HOURS  無音何時間で警報か(既定6)
- CHOUBA_READER_ALERT       "0"で警報オフ(既定オン)
"""
import os
import threading
import time

from . import db

ALERT_HOURS = float(os.environ.get("CHOUBA_READER_ALERT_HOURS", "6"))
ALERT_ON = os.environ.get("CHOUBA_READER_ALERT", "1") == "1"
_CHECK_SEC = 600


def _meta_get(k, default=""):
    try:
        from .linebot import _meta_get as g
        return g(k) or default
    except Exception:
        return default


def _meta_set(k, v):
    try:
        from .linebot import _meta_set as s
        s(k, v)
    except Exception:
        pass


def beat(battery=None):
    """リーダーからのハートビートを記録(v0.5+)。"""
    _meta_set("reader_hb", str(time.time()))
    if battery is not None:
        _meta_set("reader_batt", str(battery))


def status():
    """受信係の状態。last=最終の生存証拠(ハートビート優先、無ければ最終受信)。"""
    now = time.time()
    hb = None
    try:
        hb = float(_meta_get("reader_hb") or 0) or None
    except Exception:
        pass
    with db.conn() as c:
        r = c.execute("SELECT MAX(ts) FROM messages").fetchone()
        last_msg = r[0] if r else None
    last = max([t for t in (hb, last_msg) if t], default=None)
    gap_h = (now - last) / 3600 if last else None
    stale = bool(last and gap_h >= ALERT_HOURS)
    batt = _meta_get("reader_batt") or None
    return {"ok": not stale, "mode": "heartbeat" if hb else "messages_only",
            "last_ts": last, "gap_hours": round(gap_h, 1) if gap_h is not None else None,
            "threshold_hours": ALERT_HOURS, "battery": batt,
            "note": ("ハートビート受信中(死活を確実に判定)" if hb else
                     "旧リーダー=受信メッセージでの推定(静かな夜と停止は区別できない)")}


def _alert(st):
    gap = st["gap_hours"]
    body = (f"{gap}時間、受信がありません。\n"
            "①リーダー端末: アプリを開く→通知アクセスOFF→ON→再起動\n"
            "②それでもダメなら誰かにLINEを1通送って様子を見る")
    sent = False
    try:
        from . import linebot
        sent = linebot.push_owner([{
            "type": "flex", "altText": "⚠️ 受信係が止まっているかもしれません",
            "contents": {"type": "bubble", "body": {
                "type": "box", "layout": "vertical", "paddingAll": "16px", "contents": [
                    {"type": "text", "text": "⚠️ 受信係が止まっているかも", "weight": "bold",
                     "size": "md", "color": "#C0402C"},
                    {"type": "text", "text": body, "size": "sm", "color": "#2B2823",
                     "margin": "md", "wrap": True}]}}}]) or sent
    except Exception as e:
        print(f"[watchdog line] {e}", flush=True)
    try:
        from . import push
        n = push.notify("⚠️ 受信係が止まっているかも", f"{gap}時間受信なし。端末を確認してください。",
                        url="/", tag="reader-down")
        sent = sent or bool(n)
    except Exception as e:
        print(f"[watchdog webpush] {e}", flush=True)
    return sent   # どちらも届かなければFalse=次の周期で再試行(警報が黙って消えない)


def _recovered():
    try:
        from . import push
        push.notify("✅ 受信係が復帰しました", "受信が再開しています。", url="/", tag="reader-down")
    except Exception:
        pass


def _loop():
    while True:
        try:
            if ALERT_ON:
                st = status()
                alerted_for = _meta_get("reader_alerted_for")   # 警報済みエピソードのlast_ts
                if st["last_ts"] and not st["ok"]:
                    key = str(int(st["last_ts"]))
                    if alerted_for != key:
                        if _alert(st):
                            _meta_set("reader_alerted_for", key)
                elif st["ok"] and alerted_for:
                    _meta_set("reader_alerted_for", "")
                    _recovered()
        except Exception as e:
            print(f"[watchdog] {type(e).__name__}: {e}", flush=True)
        time.sleep(_CHECK_SEC)


_started = False


def start():
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True).start()
