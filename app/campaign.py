"""一斉下書き(キャンペーン)。

グループ(ランク/タグ)を選ぶ → 一人ずつ違う「営業のきっかけ」または「来店お礼」の
下書きを一括生成する。**送信はしない**(本人が承認して1通ずつ送る)。

材料:
- 季節(日付から自動) / 前回来店・来店周期 / タグの話題 / 相手ごとの文体プロファイル
原則:
- タグの話題は"事実がある時だけ"触れる(誕生日/来店記録が無いのに作らない=捏造しない)。
- 実例の声を最優先で真似る。数値は参考。押し売り・長文にしない。
"""
import datetime
import json
import re
import time

import requests

from . import config, db
from .style_profile import profile_prompt_block, contact_profile_block

VISIT_THANKS_DAYS = 3      # 「来店お礼」の対象=この日数以内に来店
BIRTHDAY_WINDOW = 10       # 「誕生日近い」タグ=この日数以内
RECENT_VISIT_DAYS = 7      # 「直近来店」タグ

_SEASON = {
    1: "真冬・新年", 2: "晩冬", 3: "早春", 4: "春", 5: "初夏", 6: "梅雨",
    7: "真夏", 8: "真夏・お盆", 9: "初秋", 10: "秋", 11: "晩秋", 12: "冬・年末",
}


def season_label(ts=None) -> str:
    d = datetime.date.fromtimestamp(ts or time.time())
    return _SEASON[d.month]


def _days_since(ts, now):
    if not ts:
        return None
    return int((now - ts) // 86400)


def _gap_str(days):
    if days is None:
        return "来店記録なし"
    if days <= 0:
        return "本日"
    if days < 7:
        return f"{days}日前"
    if days < 30:
        return f"{days // 7}週間前"
    if days < 365:
        return f"{days // 30}ヶ月前"
    return f"{days // 365}年前"


def _birthday_near(bd, now, window=BIRTHDAY_WINDOW):
    """bd = 'MM-DD'。今日から window 日以内に誕生日が来るか。"""
    if not bd:
        return False
    m = re.match(r"^\s*(\d{1,2})[-/](\d{1,2})\s*$", bd)
    if not m:
        return False
    mm, dd = int(m.group(1)), int(m.group(2))
    today = datetime.date.fromtimestamp(now)
    for y in (today.year, today.year + 1):
        try:
            nxt = datetime.date(y, mm, dd)
        except ValueError:
            return False
        diff = (nxt - today).days
        if 0 <= diff <= window:
            return True
        if diff >= 0:
            break
    return False


def smart_tags(contact, now) -> list:
    """手動タグ + データから自動導出したタグ。"""
    tags = []
    for t in (contact.get("tags") or "").split(","):
        t = t.strip()
        if t and t not in tags:
            tags.append(t)
    days = _days_since(contact.get("last_visit_ts"), now)
    cyc = contact.get("cycle_days")
    if days is not None and days <= RECENT_VISIT_DAYS and "直近来店" not in tags:
        tags.append("直近来店")
    if days is not None and cyc and days > cyc and "ご無沙汰" not in tags:
        tags.append("ご無沙汰")
    if _birthday_near(contact.get("birthday"), now) and "誕生日近い" not in tags:
        tags.append("誕生日近い")
    return tags


def contact_view(contact, now=None) -> dict:
    now = now or time.time()
    days = _days_since(contact.get("last_visit_ts"), now)
    return {
        "code": contact["code"],
        "rank": contact.get("rank", "B"),
        "tags": smart_tags(contact, now),
        "last_visit": _gap_str(days),
        "days_since": days,
        "note": contact.get("note", "") or "",
    }


def select_recipients(ranks=None, tags=None, mode="greeting", now=None, codes=None) -> list:
    """あて先を選ぶ。
    - greeting: ランクかタグを最低1つ選ぶ。両方指定は AND、片方だけならその条件。
    - thanks  : まず「直近{VISIT_THANKS_DAYS}日以内の来店客」。ランク/タグは任意の追加絞り込み。
    - codes   : 指定があれば、その相手コードだけに絞る(UIの個別チェックを反映)。
    """
    now = now or time.time()
    ranks = set(ranks or [])
    tags = set(tags or [])
    only = set(codes) if codes else None
    out = []
    for c in db.list_contacts():
        v = contact_view(c, now)
        if only is not None and v["code"] not in only:
            continue
        rank_ok = (not ranks) or v["rank"] in ranks
        tag_ok = (not tags) or any(t in v["tags"] for t in tags)
        if mode == "thanks":
            if v["days_since"] is None or v["days_since"] > VISIT_THANKS_DAYS:
                continue
            if ranks and not rank_ok:
                continue
            if tags and not tag_ok:
                continue
            out.append(v)
        else:  # greeting
            if not ranks and not tags:
                continue
            if ranks and tags:
                if not (rank_ok and tag_ok):
                    continue
            elif ranks and not rank_ok:
                continue
            elif tags and not tag_ok:
                continue
            out.append(v)
    return out


def _why(v, mode) -> str:
    if mode == "thanks":
        return "来店お礼"
    for t in ("誕生日近い", "ご無沙汰", "直近来店", "VIP", "常連"):
        if t in v["tags"]:
            return {"誕生日近い": "誕生日が近い", "ご無沙汰": f"{v['last_visit']}・ご無沙汰",
                    "直近来店": "直近来店", "VIP": "VIP", "常連": "常連"}[t]
    return f"前回{v['last_visit']}"


GREETING_SYSTEM = """あなたは本人の「営業の下書き係」。返信ではなく、本人から相手へ送る"きっかけの一言"を作る。

いちばん大事:「本人が実際に書いた文」の実例をそのまま真似る。数値は参考。数字合わせをすると"わざとらしく"なる。
- 実例より丁寧・完全に書かない。端折る・崩す・句読点少なめ。きれいな作文にしない。
- 押し売り・長文にしない。相手が一言で返しやすい軽さ。
- タグの話題(来店お礼/誕生日/ご無沙汰の近況/季節)は、"事実がある時だけ"触れる。無い情報を作らない(捏造禁止)。日時・金額は断定しない。
- 相手ごとに温度を変える(相手専用の実例が最優先の手本)。砕けた相手には砕けて、丁寧な相手には丁寧に。
- 決まり文句を実例に無いのに足さない。相手の状況(前回来店/タグ)を1つ拾うと自然。

出力はJSONのみ: {"text":"送る一言"}。前置き・説明・コードブロック記号は禁止。"""

THANKS_SYSTEM = """あなたは本人の「お礼の下書き係」。来店してくれた相手へ送る"お礼の一言"を作る。

- 実例の声をそのまま真似る。丁寧すぎ・長文にしない。テンプレ臭を出さない。
- 毎回同じ言い回しにしない。指定された「お礼の切り口」に寄せ、相手の距離感・前回来店のタイミング・メモ(好み/話題)を手がかりに、書き出しも結びも変える。
- 「ありがとうございました＋また会いたい」の型をなぞらない。切り口に合わせて主役を変える。
- 事実(次回日程・金額)を捏造しない。相手ごとに温度を変える(砕けた相手は砕けて、目上は丁寧に)。
出力はJSONのみ: {"text":"お礼の一言"}。前置き・説明・記号は禁止。"""


def _seed(code: str) -> int:
    """相手コードから安定した数値(バリエーションの種)。文字列ハッシュの乱数化に依存しない。"""
    return sum((i + 1) * ord(ch) for i, ch in enumerate(code))


# お礼の「切り口」。相手ごとに変えて、同じ言い回しの連発を防ぐ。
_THANKS_ANGLES = [
    "来てくれたこと自体へのお礼を主役に",
    "一緒に過ごした時間が楽しかったと素直に伝える",
    "また会いたい・次を楽しみにする気持ちを軽く",
    "相手の体調や忙しさを気遣うひとことを添えて",
    "この前の会話の余韻(また続きを話したい)に触れて",
]

# APIキー無しの時のお礼テンプレ(名前だけ差し替えの1文にしない)
_THANKS_TEMPLATES = [
    "{n}さん、{when}はありがとうございました。楽しい時間でした、また近いうちにぜひ。",
    "{n}さん、来てくれて嬉しかったです！またゆっくりお話ししましょうね。",
    "{n}さん、{when}はありがとう。おかげで元気出ました、また会いたいです。",
    "{n}さん、お忙しいところありがとうございました。次も楽しみにしてます。",
    "{n}さん、ありがとうございました！また顔を見せてくれたら嬉しいです。",
    "{n}さん、{when}は楽しかったです。落ち着いたらまた寄ってくださいね。",
]


def _template_one(v, mode) -> str:
    """APIキー無しのフォールバック(品質検証用ではない・ダミー)。"""
    name = v["code"].split(".")[-1] if "." in v["code"] else v["code"]
    if mode == "thanks":
        ds = v.get("days_since")
        when = "昨日" if (ds is not None and ds <= 1) else "先日"
        tpl = _THANKS_TEMPLATES[_seed(v["code"]) % len(_THANKS_TEMPLATES)]
        return tpl.format(n=name, when=when)
    if "誕生日近い" in v["tags"]:
        return f"{name}さん もうすぐお誕生日ですね。近いうちにお祝いさせてください。"
    if "ご無沙汰" in v["tags"]:
        return f"{name}さん ごぶさたしてます。お変わりないですか？そろそろお会いしたいです。"
    return f"{name}さん こんにちは。落ち着いたら、また顔を見せてくださいね。"


def _generate_one_ai(v, mode, template, now):
    profile = db.get_profile("_global") or {}
    per = db.get_profile(v["code"]) or {}
    cp = contact_profile_block(per)
    ctx_lines = [
        f"相手: {v['code']}(ランク{v['rank']})",
        f"季節: {season_label(now)}",
        f"前回来店: {v['last_visit']}",
    ]
    if v["tags"]:
        ctx_lines.append(f"この相手のタグ(話題の手がかり・事実がある時だけ触れる): {'、'.join(v['tags'])}")
    if v["note"]:
        ctx_lines.append(f"メモ(好み・話題): {v['note']}")
    if mode == "thanks":
        angle = _THANKS_ANGLES[_seed(v["code"]) % len(_THANKS_ANGLES)]
        ctx_lines.append(f"今回のお礼の切り口(この方向で・他の相手と被らせない): {angle}")
    if mode != "thanks" and template:
        ctx_lines.append(f"送りたい趣旨のたたき台(そのまま使わず、本人の声で自然に): 「{template}」")
    user_prompt = (
        f"{profile_prompt_block(profile)}\n\n"
        + (f"{cp}\n\n" if cp else "")
        + "\n".join(ctx_lines)
        + "\n\nこの相手に送る一言を1つ、JSONで。"
    )
    system = THANKS_SYSTEM if mode == "thanks" else GREETING_SYSTEM
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": config.ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": config.ANTHROPIC_MODEL,
            "max_tokens": 300,
            "system": system,
            "messages": [{"role": "user", "content": user_prompt}],
        },
        timeout=30,
    )
    r.raise_for_status()
    out = "".join(b.get("text", "") for b in r.json().get("content", []))
    out = re.sub(r"```(json)?", "", out).strip()
    text = json.loads(out).get("text", "").strip()
    if not text:
        raise ValueError("empty text")
    return text


def generate(mode="greeting", ranks=None, tags=None, template="", now=None, codes=None) -> dict:
    """あて先を選び、一人ずつ違う下書きを一括生成して返す(保存も送信もしない)。
    codes 指定時はその相手だけ(UIで個別に外した人を除く)。"""
    now = now or time.time()
    recips = select_recipients(ranks, tags, mode, now, codes)
    ai = bool(config.ANTHROPIC_API_KEY)
    items = []
    for v in recips:
        try:
            text = _generate_one_ai(v, mode, template, now) if ai else _template_one(v, mode)
            row_ai = ai
        except Exception:
            text = _template_one(v, mode)
            row_ai = False
        items.append({
            "code": v["code"], "rank": v["rank"], "tags": v["tags"],
            "last_visit": v["last_visit"], "why": _why(v, mode),
            "text": text, "ai": row_ai,
        })
    return {"mode": mode, "season": season_label(now), "ai": ai,
            "count": len(items), "items": items}
