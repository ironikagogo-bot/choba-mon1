"""「来そうな1通」を作る部分(受信案の生成)。**現在どこからも呼ばれていない**。

経緯: v237で🎬リハーサル(送信手前まで通す練習モード)として実装したが、
本人裁定(2026-08-14)「このリハーサルしょぼいから全部一回消して。受信案は取っておいて」で
**UI・ルートごと撤去した**(v240)。このファイルを残しているのは、下の
「その人が送ってきそうな1通を作る」ロジックが単体で価値のある資産だから:

  _incoming_lines()  取り込みtxtから「相手が送ってきた行」だけを取り出す
                     (自分の発言・スタンプ・取り消し・極端な長短を除く)
  _pick_replay()     その中から最近寄りの1通を選ぶ(AI呼び出しゼロ・本物の言い回し)
  _pick_synth()      カードと直近の会話から「今なら送ってきそうな1通」をAIに作らせる

将来の使い道の候補: デモ専用面の台本づくり/下書き品質のオフライン評価
(実顧客の実文面を入力にして、生成した返信を人が見比べる)/オンボーディングの体験。

以下は撤去した機能の名残(start/clear/gc/is_rehearsal)。**is_rehearsal だけは生きている**:
liff.py の /api/liff/reply/act が、撤去前に作られた練習用の受信(status='rehearsal')を
送信記録・文体学習・実績に混ぜないための番人として今も呼んでいる。
練習用データは消していない(本人「取っておいて」)。どの画面にも出ないので実害はない。
"""
import random
import re
import time

from . import config, db

STATUS = "rehearsal"
_MAX_AGE = 6 * 3600          # 取り残しの自動掃除(6時間)

# txtの1行(日付ヘッダ配下): "21:00\t宝条さん\t本文"
_LINE_RE = re.compile(r"^(\d{1,2}):(\d{2})\t([^\t]+)\t(.+)$")


def _self_name():
    try:
        return (db.get_profile("_selfname") or {}).get("name") or ""
    except Exception:
        return ""


def candidates(limit=12):
    """リハーサルに使える相手。実カードがあり、取り込みtxtか受信履歴がある顧客。

    「その人らしさ」が出ないと意味がないので、材料の無い相手は出さない。
    """
    out = []
    try:
        with db.conn() as c:
            rows = c.execute(
                "SELECT ct.code, ct.rank, "
                "  (SELECT COUNT(*) FROM linebot_talks t WHERE t.contact=ct.code) AS has_talk, "
                "  (SELECT COUNT(*) FROM messages m WHERE m.contact=ct.code) AS n_msg, "
                "  (SELECT MAX(m2.ts) FROM messages m2 WHERE m2.contact=ct.code) AS last_ts "
                "FROM contacts ct "
                "WHERE IFNULL(ct.kind,'customer')='customer' AND IFNULL(ct.linked,1)<>0 "
                "ORDER BY CASE IFNULL(ct.rank,'B') WHEN 'S' THEN 0 WHEN 'A' THEN 1 ELSE 2 END, "
                "  last_ts DESC").fetchall()
    except Exception as e:
        print(f"[rehearsal] 候補の取得失敗: {e}", flush=True)
        return []
    from . import linebot
    for r in rows:
        if not (r["has_talk"] or r["n_msg"]):
            continue           # 材料なし=その人らしい1通が作れない
        out.append({"code": r["code"], "rank": r["rank"] or "B",
                    "name": linebot._yobina(r["code"]),
                    "can_replay": bool(r["has_talk"])})
        if len(out) >= limit:
            break
    return out


def _incoming_lines(code, cap=400):
    """取り込みtxtから「相手が送ってきた行」だけを取り出す。"""
    try:
        with db.conn() as c:
            r = c.execute("SELECT text FROM linebot_talks WHERE contact=?", (code,)).fetchone()
    except Exception:
        return []
    if not r or not r["text"]:
        return []
    me = _self_name()
    out = []
    for line in (r["text"] or "").splitlines()[-6000:]:
        m = _LINE_RE.match(line)
        if not m:
            continue
        sender, body = m.group(3).strip(), m.group(4).strip()
        if me and sender == me:
            continue           # 自分の発言は「来そうな1通」ではない
        if not body or body.startswith(("[スタンプ]", "[写真]", "[動画]", "[ファイル]")):
            continue
        if "メッセージの送信を取り消しました" in body or len(body) < 6 or len(body) > 120:
            continue
        out.append(body)
        if len(out) > cap:
            out.pop(0)
    return out


def _pick_replay(code, seed=None):
    lines = _incoming_lines(code)
    if not lines:
        return ""
    rnd = random.Random(seed if seed is not None else int(time.time()))
    # 末尾(最近)寄りから選ぶ。古すぎる話題は「来そうな1通」に見えない
    tail = lines[-60:] if len(lines) > 60 else lines
    return rnd.choice(tail)


def _pick_synth(code):
    """カードと直近の会話から「今その人が送ってきそうな1通」をAIに作らせる。"""
    if not config.ANTHROPIC_API_KEY:
        return ""
    from . import campaign, crm, linebot
    try:
        card = crm.card_prompt_block(code) or ""
        recent = campaign._freshness_corpus(code)[-1500:]
        who = linebot._yobina(code)
        prompt = (
            f"あなたは、あるお客様「{who}」になりきって、この人が今このタイミングで"
            "送ってきそうなLINEのメッセージを1通だけ書きます。練習用の想定です。\n"
            "条件:\n"
            "- 1〜2文。実際のLINEらしい話し言葉。かしこまらない\n"
            "- この人の口調・話題・関心に沿う。以下の情報にある範囲を出ない\n"
            "- 説明・前置き・引用符は書かない。メッセージ本文だけを出力する\n"
            f"\n【この人の情報】\n{card}\n"
            f"\n【最近のやり取りの断片】\n{recent}\n")
        import requests
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": config.ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": config.ANTHROPIC_MODEL, "max_tokens": 200,
                  "system": "あなたは会話の練習用データを作る補助です。出力は本文1通のみ。",
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=25)
        if r.status_code != 200:
            print(f"[rehearsal synth] HTTP {r.status_code}", flush=True)
            return ""
        t = "".join(b.get("text", "") for b in (r.json().get("content") or []))
        t = (t or "").strip().strip("「」\"'")
        return t.splitlines()[0][:120] if t else ""
    except Exception as e:
        print(f"[rehearsal synth] {e}", flush=True)
        return ""


def start(code, mode="replay"):
    """リハーサル受信を1件仕込む。戻り: {mid, text, mode, name} or {error}。"""
    gc()
    if not db.get_contact(code):
        return {"error": "その相手が見つかりません"}
    text = _pick_replay(code) if mode == "replay" else _pick_synth(code)
    if not text and mode == "synth":
        text = _pick_replay(code)      # 合成できない時は再演に落とす(黙って失敗しない)
        mode = "replay"
    if not text:
        return {"error": "この相手はまだ練習の材料がありません(トーク履歴を取り込むと使えます)"}
    from . import triage
    try:
        cat, why = triage.classify(code, text)
    except Exception as e:
        print(f"[rehearsal triage] {e}", flush=True)
        cat, why = "batch", ""
    mid = db.add_message(code, text, category=cat, reason=why)
    with db.conn() as c:
        # 本物の受信箱・成績・ご無沙汰・一括片づけのどのクエリにも拾われない状態にする
        c.execute("UPDATE messages SET status=? WHERE id=?", (STATUS, mid))
    db.track("rehearsal_start")
    from . import linebot as _lb
    return {"ok": True, "mid": mid, "text": text, "mode": mode,
            "name": _lb._yobina(code), "code": code,
            "urgent": cat == "urgent", "reason": why}


def is_rehearsal(mid):
    try:
        with db.conn() as c:
            r = c.execute("SELECT status FROM messages WHERE id=?", (mid,)).fetchone()
        return bool(r and r["status"] == STATUS)
    except Exception:
        return False


def clear():
    """リハーサル受信を全部消す(終了・離脱時)。下書きの控えも一緒に。"""
    n = 0
    try:
        with db.conn() as c:
            ids = [r["id"] for r in c.execute(
                "SELECT id FROM messages WHERE status=?", (STATUS,))]
            for mid in ids:
                c.execute("DELETE FROM messages WHERE id=?", (mid,))
                n += 1
    except Exception as e:
        print(f"[rehearsal] 掃除失敗: {e}", flush=True)
    if n:
        print(f"[rehearsal] {n}件を片づけました", flush=True)
    return n


def gc():
    """取り残し(6時間以上前のリハーサル受信)の掃除。開始のたびに走らせる。"""
    try:
        with db.conn() as c:
            c.execute("DELETE FROM messages WHERE status=? AND ts < ?",
                      (STATUS, time.time() - _MAX_AGE))
    except Exception as e:
        print(f"[rehearsal gc] {e}", flush=True)
