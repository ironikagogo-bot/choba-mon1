"""トリアージエンジン。
判定順: ラリー(同一相手の連続受信) → 即対応(S客 or 用件キーワード) → まとめ。
ルールは意図的に単純・可読に保つ。LLM判定は将来ここに差せるが、
「なぜ鳴ったか」を本人に説明できることを優先する。
"""
import json
import re
import time

import requests

from . import config, db

# 用件キーワード(来店・日程・金銭・急ぎ・不機嫌)
URGENT_PATTERNS = [
    (r"(今日|今夜|今から|これから).{0,12}(行|寄|向か|飲み)", "来店の申し出"),
    (r"(予約|席|何時|空いて)", "来店・席の確認"),
    (r"(同伴|アフター)", "同伴の相談"),
    (r"(明日|今週|来週|曜日).{0,10}(行|会|どう|空い)", "日程の相談"),
    (r"(振込|支払|払う|請求|ツケ|売掛|金)", "金銭の話"),
    (r"(至急|急ぎ|すぐ|早く)", "急ぎの気配"),
    (r"(怒|ふざけ|舐め|もういい|最悪)", "不機嫌の気配"),
]


def classify(contact_code: str, text: str, now: float | None = None) -> tuple[str, str]:
    """(category, reason) を返す。category は urgent / rally / batch。"""
    now = now or time.time()

    # 1) ラリー: 同一相手から RALLY_WINDOW_MIN 分以内の連続受信
    last = db.last_message_ts(contact_code)
    if last is not None and (now - last) <= config.RALLY_WINDOW_MIN * 60:
        return "rally", f"{config.RALLY_WINDOW_MIN}分以内の連続受信"

    # 2) 用件キーワード(強シグナル=即決・AI呼ばない)
    for pat, reason in URGENT_PATTERNS:
        if re.search(pat, text):
            return "urgent", reason

    # 3) S客は内容によらず即対応(即決)
    c = db.get_contact(contact_code)
    rank = (c or {}).get("rank", "B")
    if rank == "S":
        return "urgent", "S客からの受信"

    # 4) グレー(キーワード無し・非S客)だけAIで再判定。失敗時はまとめ箱にフォールバック
    ai = ai_classify(text, rank)
    if ai:
        return ai
    return "batch", "雑談・近況"


def ai_classify(text: str, rank: str) -> tuple[str, str] | None:
    """曖昧な受信をAIで urgent/batch に再判定。失敗・無効時は None(→キーワードにフォールバック)。
    ラリーは時間判定で別に決まるのでここでは urgent/batch の2択のみ。
    """
    if not config.TRIAGE_AI or not config.ANTHROPIC_API_KEY:
        return None
    system = (
        "あなたは銀座クラブのホステスの受信LINEを2分類する仕分け係。\n"
        "urgent=今すぐ返した方がよい(来店/日程/同伴/金銭/急ぎ、相手が返事を待つ問いかけ、"
        "不満・不機嫌など感情が強い)。\n"
        "batch=急がない(雑談/近況報告/軽い挨拶で、こちらの返事を特に待っていない)。\n"
        "皮肉や遠回しな催促・感情の強さも汲む。判断は控えめに——迷ったら urgent にしない(ノイズ削減優先)。\n"
        '出力はJSONのみ: {"category":"urgent|batch","reason":"短い日本語(例:日程の相談/不満の気配/雑談)"}'
    )
    user = f"相手のランク:{rank}\n受信メッセージ:「{text}」"
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": config.ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": config.TRIAGE_MODEL, "max_tokens": 80, "system": system,
                  "messages": [{"role": "user", "content": user}]},
            timeout=12,
        )
        r.raise_for_status()
        out = "".join(b.get("text", "") for b in r.json().get("content", []))
        out = re.sub(r"```(json)?", "", out).strip()
        d = json.loads(out)
        cat = d.get("category")
        if cat not in ("urgent", "batch"):
            return None
        reason = (d.get("reason") or "").strip()[:24]
        return cat, (reason or ("即対応の気配" if cat == "urgent" else "雑談・近況"))
    except Exception:
        return None


def churn_risk(contact: dict, now: float | None = None) -> bool:
    """来店周期の超過(離脱兆候)。"""
    now = now or time.time()
    if not contact.get("cycle_days") or not contact.get("last_visit_ts"):
        return False
    return (now - contact["last_visit_ts"]) > contact["cycle_days"] * 86400 * 1.5
