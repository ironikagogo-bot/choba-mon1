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


# ---------- v119: 軽い話題の材料(事実のみ・捏造ゼロ設計) ----------

_WX_CODE = {0: "快晴", 1: "晴れ", 2: "晴れ時々くもり", 3: "くもり", 45: "霧", 48: "霧",
            51: "小雨", 53: "小雨", 55: "小雨", 61: "雨", 63: "雨", 65: "大雨",
            66: "みぞれ", 67: "みぞれ", 71: "雪", 73: "雪", 75: "大雪", 77: "雪",
            80: "にわか雨", 81: "にわか雨", 82: "激しいにわか雨", 85: "雪", 86: "雪",
            95: "雷雨", 96: "雷雨", 99: "雷雨"}


def _lt_cache_get(k, max_age):
    try:
        from . import news as _news
        _news.ensure()
        raw = _news._meta_get(k)
        if raw:
            ts, _, val = raw.partition("|")
            age = max_age if val else 600   # 失敗(空)は10分だけキャッシュ=すぐ再挑戦
            if time.time() - float(ts) < age:
                return val
    except Exception:
        pass
    return None


def _lt_cache_set(k, val):
    try:
        from . import news as _news
        _news._meta_set(k, f"{time.time()}|{val}")
    except Exception:
        pass


def weather_insights(lat=35.670, lon=139.765, label="銀座周辺"):
    """v121: 時間帯別予報(24h)から「会話に使える読み」を導く。
    生の気温・天気でなく、夜の雨/猛暑/冷え/過ごしやすさ等の示唆に変換して返す。
    失敗=None(天気に触れないだけ)。3時間キャッシュ。"""
    key = f"wxi_{round(lat, 2)}_{round(lon, 2)}"
    c = _lt_cache_get(key, 3 * 3600)
    if c is not None:
        try:
            return json.loads(c) if c else None
        except Exception:
            return None
    out = None
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast",
                         params={"latitude": lat, "longitude": lon,
                                 "hourly": "temperature_2m,apparent_temperature,"
                                           "precipitation_probability,weather_code",
                                 "timezone": "Asia/Tokyo", "forecast_hours": 24},
                         timeout=8)
        h = r.json()["hourly"]
        times = h["time"]
        temps = [x for x in h["temperature_2m"] if x is not None]
        apps = [x for x in h["apparent_temperature"] if x is not None]
        eve_idx = [i for i, t in enumerate(times) if 17 <= int(t[11:13]) <= 23]
        eve_rain = max(((h["precipitation_probability"][i] or 0) for i in eve_idx), default=0)
        eve_thunder = any((h["weather_code"][i] or 0) >= 95 for i in eve_idx)
        tmax = max(temps) if temps else None
        amax = max(apps) if apps else None
        ins = []
        if amax is not None and amax >= 35:
            ins.append("猛暑・体感35度超 → 体調・水分の気遣いの型")
        elif tmax is not None and tmax >= 32:
            ins.append("かなり暑い → 暑さの実感の型")
        if tmax is not None and tmax <= 8:
            ins.append("かなり冷える → あったかくしてねの型")
        if eve_thunder:
            ins.append("夕方〜夜に雷雨予報 → 帰り・お出かけの心配の型")
        elif eve_rain >= 50:
            ins.append("夕方〜夜に雨の可能性大 → 傘・足元の型")
        elif eve_rain >= 30:
            ins.append("夜ににわか雨あるかも → 軽く傘の型")
        if not ins and tmax is not None and 18 <= tmax <= 27 and eve_rain < 30:
            ins.append("過ごしやすい陽気 → 心地よさの一言の型")
        out = {"label": label, "insights": ins} if ins else None
    except Exception as e:
        print(f"[weather] {e}", flush=True)
    _lt_cache_set(key, json.dumps(out, ensure_ascii=False) if out else "")
    return out


def _geocode_area(area):
    """v121: 地名→座標(Open-Meteoジオコーディング・30日キャッシュ)。失敗=None。"""
    area = (area or "").strip()[:20]
    if not area:
        return None
    key = "geo_" + area
    c = _lt_cache_get(key, 30 * 86400)
    if c is not None:
        try:
            return json.loads(c) if c else None
        except Exception:
            return None
    out = None
    try:
        r = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                         params={"name": area, "language": "ja", "count": 1}, timeout=8)
        res = (r.json().get("results") or [])
        if res:
            out = {"lat": res[0]["latitude"], "lon": res[0]["longitude"],
                   "name": res[0].get("name") or area}
    except Exception as e:
        print(f"[geocode] {e}", flush=True)
    _lt_cache_set(key, json.dumps(out, ensure_ascii=False) if out else "")
    return out


def headlines_today(n=6):
    """今日の一般ニュース見出し(Google News RSS・3時間キャッシュ)。実在の見出しのみ=捏造ゼロ。"""
    c = _lt_cache_get("hl_cache", 3 * 3600)
    if c is not None:
        return [x for x in c.split("‖") if x]
    out = []
    try:
        from . import news as _news
        r = requests.get("https://news.google.com/rss",
                         params={"hl": "ja", "gl": "JP", "ceid": "JP:ja"},
                         headers={"User-Agent": "Mozilla/5.0 (chouba-neta)"}, timeout=12)
        r.raise_for_status()
        items = _news.parse_rss(r.content)
        out = [i["title"][:60] for i in items[:n]]
    except Exception as e:
        print(f"[headlines] {e}", flush=True)
    _lt_cache_set("hl_cache", "‖".join(out))
    return out


def light_topic_block(code=""):
    """v121: 軽い話題の材料。天気は「予報の読み上げ」でなく実感・気遣いの型に変換させる。
    相手のカードに住まい・エリアがあればその地域の読みも添える。"""
    mats = []
    wx = weather_insights()
    if wx and wx.get("insights"):
        mats.append(f"天気の読み({wx['label']}・実測から): " + " / ".join(wx["insights"]))
    # 相手の地域(カードの「住まい・エリア」)があればその地域の読みも
    if code:
        try:
            from . import crm as _crm
            area = ((_crm.get_attrs(code) or {}).get("住まい・エリア") or "").strip()
            if area:
                g = _geocode_area(area)
                if g:
                    wx2 = weather_insights(g["lat"], g["lon"], label=area)
                    if wx2 and wx2.get("insights") and (not wx or wx2["insights"] != wx["insights"]):
                        mats.append(f"相手の住まい({area})の読み: " + " / ".join(wx2["insights"]))
        except Exception as e:
            print(f"[area wx] {e}", flush=True)
    hl = headlines_today()
    if hl:
        mats.append("今日の実在ニュース見出し: " + " / ".join(hl))
    if not mats:
        return ""
    return ("【軽い話題の材料(ここにある事実のみ。想像で補わない)】\n" + "\n".join(mats) +
            "\n【天気の使い方・厳守】天気予報の読み上げ(「今日の東京は晴れ、最高34度」等)は禁止。"
            "「〜の型」に沿って実感と気遣いの一言に変換する。例: 猛暑→「今日もすごい暑さだね、"
            "ちゃんと水分とってる？」/ 夜雨→「夜は雨みたいだから、傘忘れずにね」/ "
            "過ごしやすい→「今日は久しぶりに気持ちいい陽気だね」。"
            "気温・降水確率などの数字は本文に書かない(会話で数字を言う人はいない)。"
            "【見出しの使い方】明るい話題のみ(事件・事故・政治・訃報・不祥事は禁止)。"
            "記事の中身を推測で補わない。合うものが無ければ天気か季節だけでよい。"
            "どちらも入り口か結びに1つだけ、さらっと。")


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


GREETING_SYSTEM = """あなたは銀座の一流ホステス本人の「営業メッセージの下書き係」。本人から相手へ送る、
"送られて嬉しい"営業LINEを作る。目的は、相手が来店したくなる・返信したくなる温かい一通。

【長さ】素っ気ない一言で終わらせない。基本は2〜4文。①軽い呼びかけ/近況の触れ ②本題(必達内容)
③相手を気にかける結び、の流れで、気持ちの乗った長さにする。ただし本人の実例が明らかに短文・
そっけないスタイルなら、その人らしさを優先して短くしてよい(実例が最優先の手本)。

【絵文字・温度】本人の実例に絵文字があれば、その人の使い方・頻度をそのまま真似る。
実例が無い/少ない場合も、営業メッセージとして親しみが出るよう絵文字を1〜2個添えてよい
(😊🍾🌸✨🙇‍♀️ など文脈に合うもの)。機械的な連打・全文が絵文字だらけ、はしない。堅い定型文にもしない。

【本題(必達内容)が主役】本人が「伝えたい内容」を書いていれば、それがこのメッセージの骨。
自然な言葉で膨らませる。情報(日時・場所)は削らない。ただし書かれていない日時・金額は捏造しない。

【個別化は"自然な範囲で"】顧客カードに、今の文脈に自然に効く事実(進行中の話・好み・関係性)が
あれば1つだけ、さりげなく織り込む。無理なら入れない。事実が薄い相手に個人的な話を作り込むと
不自然・嘘くさくなる=絶対にしない。全員に同じ話題を貼り付ける使い回しも禁止。

【声】本人が実際に書いた文の言い回し・崩し・句読点・語尾をなぞる。きれいすぎる作文にしない。
砕けた相手には砕けて、目上・丁寧な相手には品よく。相手ごとに温度を変える。

【最後に自分で読み返す】送信前に一度読み、テンプレ臭・不自然な作り込み・冷たさが無いか点検し、
あれば整えてから出す。

出力はJSONのみ: {"text":"営業メッセージ本文"}。前置き・説明・コードブロック記号は禁止。"""

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


def _generate_one_ai(v, mode, template, now, purpose=""):
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
    if mode != "thanks" and purpose:
        # v115: 目的別の長さ・温度ヒント
        _tone = {
            "出勤のお知らせ": "出勤案内。いつ店にいるかを軽やかに伝え、会いたい気持ちを一言添える。2〜3文＋絵文字1〜2個。",
            "イベントの案内": "イベント告知。楽しそうな雰囲気を出し、来たくなるように。3〜4文＋絵文字。",
            "季節のご挨拶": "季節の挨拶。時候に触れて、相手を気遣い、さりげなく店へ誘う。品よく2〜4文。",
            "ご無沙汰の相手への軽い挨拶": "久しぶりの掘り起こし。重くせず、また会いたいと自然に。押し付けない温かさ。2〜3文。",
        }.get(purpose, "")
        ctx_lines.append(f"配信の種類: {purpose}" + (f"（{_tone}）" if _tone else ""))
    if mode != "thanks" and template:
        # v103: 本人が書いた内容は必達事項。v115: これを主役に自然に膨らませる
        ctx_lines.append("★最優先・必達内容(この配信で伝えたい本題。この文面の骨にして、"
                        "本人の口調で自然に膨らませる。情報・事実は削らない): 「" + template + "」")
    if mode != "thanks":
        # v119: 「濃すぎる」対策。個人事実の上限を明文化し、軽い話題を既定の入り口にする
        ctx_lines.append("【濃度の上限(厳守)】顧客カード・ペルソナ・過去会話からの個人的な事実は"
                         "1通につき最大1つ。基本は0でよい。2つ以上入れる/会話を細かく引用する/"
                         "相手をよく研究している感を出す、のは重くて不気味な印象になるので禁止。"
                         "既定の入り口は天気・季節などの軽い話題＋本題＋短い気遣い。それで十分良い営業文になる。")
        _lt = light_topic_block(v["code"])
        if _lt:
            ctx_lines.append(_lt)
    # v101: 顧客カードを配信生成にも実接続
    try:
        from . import crm as _crm
        _cb = _crm.card_prompt_block(v["code"])
    except Exception:
        _cb = ""
    # v118: 関係性(事実)＋許容レベル(本人確定のみ)を配信にも注入
    _rel = _tol = _sm = ""
    try:
        from . import linebot as _lb
        _rel = _lb.relationship_prompt_block(v["code"])
        _tol = _lb.tolerance_prompt_block(v["code"])
    except Exception:
        pass
    try:
        from .style_profile import samples_to_them as _stt
        _sm = _stt(v["code"])   # v123: この相手への実例(接し方プロファイル)
    except Exception:
        pass
    user_prompt = (
        f"{profile_prompt_block(profile)}\n\n"
        + (f"{cp}\n\n" if cp else "")
        + (f"{_cb}\n\n" if _cb else "")
        + (f"{_rel}\n\n" if _rel else "")
        + (f"{_tol}\n\n" if _tol else "")
        + (f"{_sm}\n\n" if _sm else "")
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
            "max_tokens": 450,
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


def generate(mode="greeting", ranks=None, tags=None, template="", now=None, codes=None,
             purpose="") -> dict:
    """あて先を選び、一人ずつ違う下書きを一括生成して返す(保存も送信もしない)。
    codes 指定時はその相手だけ(UIで個別に外した人を除く)。
    template=必達内容(本人が書いた文) / purpose=配信の種類(出勤・イベント等)。"""
    now = now or time.time()
    recips = select_recipients(ranks, tags, mode, now, codes)
    ai = bool(config.ANTHROPIC_API_KEY)
    items = []
    for v in recips:
        try:
            text = (_generate_one_ai(v, mode, template, now, purpose=purpose)
                    if ai else _template_one(v, mode))
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
