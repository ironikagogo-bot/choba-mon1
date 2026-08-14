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
    # v217: 呼び名を配信文面の呼びかけに使う(本人指摘2026-08-13: 「Yasuhiro Yamamotoさん」と
    # LINE登録名で呼びかけていた。呼び名がカードにあるのに使っていなかった)
    yob = ""
    try:
        from . import crm
        yob = ((crm.get_attrs(contact["code"]) or {}).get("呼び名") or "").strip()
    except Exception:
        pass
    return {
        "code": contact["code"],
        "yobina": yob,
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
        # v177: 未仕分けの仮カード(linked=0=未知の送信者からingestが自動作成)は配信対象外。
        # 顧客一覧(search_contacts)・ご無沙汰(estranged)と同じ境界。NULLは既存is_linkedの
        # 意味論どおり紐付け済み扱い(==0のみ弾く)。
        if c.get("linked") == 0:
            continue
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

【長さ】素っ気ない一言で終わらせない。基本は3〜5文。①軽い呼びかけ/季節・天気の実感
②本題(必達内容)を自分の言葉で少し語る ③相手を気にかける結び、の流れで、気持ちの乗った
長さにする。1文が短くても、話の膨らみで「ちゃんと書いてくれた」と感じる密度を出す。
ただし本人の実例が明らかに短文スタイルなら、その人らしさを優先して短くしてよい(実例が最優先の手本)。

【絵文字・温度】本人の実例に絵文字があれば、その人の使い方・頻度をそのまま真似る。
実例が無い/少ない場合も、営業メッセージとして親しみが出るよう絵文字を1〜2個添えてよい
(😊🍾🌸✨🙇‍♀️ など文脈に合うもの)。機械的な連打・全文が絵文字だらけ、はしない。堅い定型文にもしない。

【本題(必達内容)が主役】本人が「伝えたい内容」を書いていれば、それがこのメッセージの骨。
自然な言葉で膨らませる。情報(日時・場所)は削らない。ただし書かれていない日時・金額は捏造しない。

【この相手専用に見える一文を、ちょうど1つ】全員に送れる文は営業として弱い。カードの事実
(進行中の話・好み・仕事・前回の話題)から自然に効くものを1つだけ選び、さりげなく織り込む。
カードが薄い相手は、天気・季節をその人の生活(仕事帰り・出張・お酒)に寄せた一言でもよい。
2つ以上の詰め込み・研究してます感は不気味になるので禁止。事実の捏造も禁止。
全員に同じ話題を貼り付ける使い回しも禁止。

【声】本人が実際に書いた文の言い回し・崩し・句読点・語尾をなぞる。きれいすぎる作文にしない。
砕けた相手には砕けて、目上・丁寧な相手には品よく。相手ごとに温度を変える。

【敬称を重ねない】呼び名・表示名に既に「さん」「ちゃん」「くん」「様」等の敬称が含まれる場合、
そのまま使い敬称を足さない(「HI!さん」→「HI!さん」。「HI!さんさん」は禁止)。

【過去の出来事を今の予定にしない】カードやトーク由来の出来事(旅行・会食・イベント等)は、
記録が古いもの・日付が既に過ぎたものを「今度の」「もうすぐ」のように現在の予定として書かない。
触れるなら「そういえば◯◯どうでしたか」の振り返りとしてのみ。振り返りに使うのも直近2ヶ月程度まで。
それより古い話題は持ち出さない(何ヶ月も前の話を持ち出すと監視されている印象になる)。

【最後に自分で読み返す】送信前に一度読み、テンプレ臭・不自然な作り込み・冷たさが無いか点検し、
あれば整えてから出す。

出力はJSONのみ: {"text":"営業メッセージ本文"}。前置き・説明・コードブロック記号は禁止。"""

# v158: 一般モード(CHOUBA_MODE=general)は営業でなく1対1の挨拶・近況として文面を作る
if config.MODE == "general":
    GREETING_SYSTEM = (GREETING_SYSTEM
        .replace("銀座の一流ホステス本人の「営業メッセージの下書き係」", "本人の「まとめて連絡の下書き係」")
        .replace('"送られて嬉しい"営業LINE', '"送られて嬉しい"挨拶・近況LINE')
        .replace("相手が来店したくなる・返信したくなる", "相手が返信したくなる・また会いたくなる")
        .replace("営業メッセージとして親しみが出るよう", "挨拶として親しみが出るよう")
        .replace("全員に送れる文は営業として弱い", "全員に送れる文は挨拶として弱い")
        .replace('{"text":"営業メッセージ本文"}', '{"text":"メッセージ本文"}'))

THANKS_SYSTEM = """あなたは本人の「お礼の下書き係」。来店してくれた相手へ送る"お礼の一言"を作る。

- 実例の声をそのまま真似る。丁寧すぎ・長文にしない。テンプレ臭を出さない。
- 毎回同じ言い回しにしない。指定された「お礼の切り口」に寄せ、相手の距離感・前回来店のタイミング・メモ(好み/話題)を手がかりに、書き出しも結びも変える。
- 「ありがとうございました＋また会いたい」の型をなぞらない。切り口に合わせて主役を変える。
- 事実(次回日程・金額)を捏造しない。相手ごとに温度を変える(砕けた相手は砕けて、目上は丁寧に)。
- 呼び名に既に「さん」等の敬称が含まれる場合は敬称を重ねない(「HI!さんさん」禁止)。
出力はJSONのみ: {"text":"お礼の一言"}。前置き・説明・記号は禁止。"""
if config.MODE == "general":   # v158
    THANKS_SYSTEM = THANKS_SYSTEM.replace("来店してくれた相手", "会ってくれた相手").replace("来店", "再会")


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

# APIキー無しの時のお礼テンプレ(名前だけ差し替えの1文にしない)。{n}は敬称込みで渡す
_THANKS_TEMPLATES = [
    "{n}、{when}はありがとうございました。楽しい時間でした、また近いうちにぜひ。",
    "{n}、来てくれて嬉しかったです！またゆっくりお話ししましょうね。",
    "{n}、{when}はありがとう。おかげで元気出ました、また会いたいです。",
    "{n}、お忙しいところありがとうございました。次も楽しみにしてます。",
    "{n}、ありがとうございました！また顔を見せてくれたら嬉しいです。",
    "{n}、{when}は楽しかったです。落ち着いたらまた寄ってくださいね。",
]

# v141: 敬称の重ね付け防止(HI!さん→HI!さんさん問題)。アプリ横断で使う
_HON_RE = re.compile(r"(さん|様|さま|ちゃん|くん|君|先生|ママ)\s*$")


def hon(name: str) -> str:
    """名前に敬称が無ければ「さん」を付け、既にあればそのまま返す。"""
    name = (name or "").strip()
    return name if _HON_RE.search(name) else name + "さん"


def _template_one(v, mode, template="") -> str:
    """APIキー無しのフォールバック(品質検証用ではない・ダミー)。
    v177: template=本人が書いた必達内容(「今週は金曜と土曜おります」等)。UIは
    「書いた内容は全員の文面に必ず入ります」と約束しているので、AI失敗時の
    フォールバックでも捨てずに機械連結して守る。"""
    # v217: 呼び名があれば必ず呼び名で(無ければ従来どおり登録名から)
    name = hon((v.get("yobina") or "").strip()
               or (v["code"].split(".")[-1] if "." in v["code"] else v["code"]))
    if mode == "thanks":
        ds = v.get("days_since")
        when = "昨日" if (ds is not None and ds <= 1) else "先日"
        tpl = _THANKS_TEMPLATES[_seed(v["code"]) % len(_THANKS_TEMPLATES)]
        return tpl.format(n=name, when=when)
    if "誕生日近い" in v["tags"]:
        base = f"{name} もうすぐお誕生日ですね。近いうちにお祝いさせてください。"
    elif "ご無沙汰" in v["tags"]:
        base = f"{name} ごぶさたしてます。お変わりないですか？そろそろお会いしたいです。"
    else:
        base = f"{name} こんにちは。落ち着いたら、また顔を見せてくださいね。"
    t = (template or "").strip()
    return (base + "\n" + t) if t else base


# v226(B案): 直近の実やり取り(30日以内・末尾3通)。話題は「この流れの続き」だけに絞らせる。
# 唐突さの主因=カード属性(点)から話題を選び会話の流れ(線)を知らないこと、への直接対策
_TOPIC_KEYS = ("進行中の話", "関係性メモ", "家族", "好きなお酒", "好きな食べ物",
               "趣味・関心", "健康", "仕事・会社", "お気に入りキャスト", "記念日")


def _recent_corpus(code, days=30):
    """直近days日の受信・送信本文(新しい順)。[(who, ts, text)]"""
    import re as _re3
    cutoff = time.time() - days * 86400
    with db.conn() as c:
        recv = [("相手", r["ts"], r["text"]) for r in c.execute(
            "SELECT ts, text FROM messages WHERE contact=? AND ts>? ORDER BY ts DESC LIMIT 40",
            (code, cutoff))]
        sent = [("自分", r["ts"], r["text"]) for r in c.execute(
            "SELECT ts, text FROM sent_replies WHERE contact=? AND ts>? ORDER BY ts DESC LIMIT 40",
            (code, cutoff))]
    rows = sorted(recv + sent, key=lambda x: x[1] or 0)
    return [(w, t, _re3.sub(r"【\??[^】]{0,30}】", "", (x or "")).strip()) for w, t, x in rows]


def _recent_exchange_block(code):
    rows = [r for r in _recent_corpus(code) if r[2]][-3:]
    if not rows:
        return ""
    lines = "\n".join(f"{w}「{tx[:80]}」" for w, _, tx in rows)
    return ("【直近の実際のやり取り(30日以内・古→新)】\n" + lines +
            "\n→ 相手個別の話題に触れる場合は、この流れの自然な続きだけにする。"
            "ここに出ていない話題は、カードに書いてあっても本文で蒸し返さない"
            "(急に古い話を出すと監視されている印象になる)。流れが無ければ挨拶と季節だけでよい。")


def _freshness_corpus(code, days=30):
    """v235(監査指摘・重大): 鮮度照合に使う会話。

    messages/sent_repliesは「受信係が動いてから」のものしか無く、**txt取り込みだけの相手には
    1行も存在しない**(取り込みはlinebot_talksに入る)。v226はそこを見ずに
    「直近30日に出ていない話題は落とす」を適用したため、モニターの大半の相手で
    カードの事実が全部落ち、配信が天気とニュースだけになっていた。
    取り込みtxtの末尾(=会話の新しい側)を鮮度の材料に加える。"""
    corpus = "".join(tx for _, _, tx in _recent_corpus(code, days))
    if corpus:
        return corpus
    try:
        with db.conn() as c:
            r = c.execute("SELECT text FROM linebot_talks WHERE contact=?", (code,)).fetchone()
        return ((r["text"] or "")[-6000:]) if r else ""
    except Exception as e:
        print(f"[freshness] {e}", flush=True)
        return ""


def _fresh_topic_keys(code, days=30):
    """v226(A案): カードの話題キーのうち、直近days日の会話に実際に登場したものだけ(決定論)。"""
    from . import crm as _crm
    a = _crm.get_attrs(code) or {}
    corpus = _freshness_corpus(code, days)
    if not corpus:
        return set()
    out = set()
    for k in _TOPIC_KEYS:
        val = (a.get(k) or "").strip()
        if not val:
            continue
        toks = [t for t in re.split(r"[\s、。・,/()（）「」]+", val)
                if len(t) >= 2 and not t.isdigit()]   # v235: 「1990/10/5」の断片が本文の「10月」に当たる誤爆
        if any(t in corpus for t in toks):
            out.add(k)
    return out


def _topic_hits(text, code):
    """生成文に登場するカード話題の数(決定論検品)。値のトークンが本文に現れたキーを数える。"""
    from . import crm as _crm
    a = _crm.get_attrs(code) or {}
    hits = 0
    for k in _TOPIC_KEYS:
        val = (a.get(k) or "").strip()
        if not val:
            continue
        toks = [t for t in re.split(r"[\s、。・,/()（）「」]+", val)
                if len(t) >= 2 and not t.isdigit()]   # v235: 「1990/10/5」の断片が本文の「10月」に当たる誤爆
        if any(t in (text or "") for t in toks):
            hits += 1
    return hits


def _generate_one_ai(v, mode, template, now, purpose="", plevel=1, strict=False):
    profile = db.get_profile("_global") or {}
    per = db.get_profile(v["code"]) or {}
    cp = contact_profile_block(per)
    _yob = (v.get("yobina") or "").strip()
    ctx_lines = [
        (f"相手: {v['code']}(ランク{v['rank']})。呼び名: {_yob} — 呼びかけは必ずこの呼び名を使う"
         f"(登録名「{v['code']}」で呼ばない)" if _yob
         else f"相手: {v['code']}(ランク{v['rank']})"),
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
        # v226: 個別化つまみ(plevel 0=入れない/1=ひとつまみ/2=しっかり)
        if plevel <= 0:
            ctx_lines.append("【個別化の分量(厳守)】相手ごとの話題・事実には一切触れない。"
                             "呼びかけの名前以外は全員に送れる共通の文面でよい。季節・天気の一言はOK。")
        elif plevel >= 2:
            ctx_lines.append("【個別化の分量(厳守)】この相手専用に見える話題は2つまで。"
                             "羅列・研究してます感は禁止。")
        else:
            # v119: 「濃すぎる」対策。個人事実の上限を明文化し、軽い話題を既定の入り口にする
            ctx_lines.append("【個別化の分量(厳守)】この相手専用に見える一文を、ちょうど1つ。"
                             "2つ以上の詰め込み/細かい会話引用/研究してます感は不気味なので禁止。"
                             "カードに使える事実が無い時は、天気・季節をこの相手の生活に寄せた一言で代用する。")
        _lt = light_topic_block(v["code"])
        if _lt:
            ctx_lines.append(_lt)
        # v226(B案): 直近のやり取りの流れを渡し、話題は「続き」だけに絞る
        if plevel >= 1:
            _rx = _recent_exchange_block(v["code"])
            if _rx:
                ctx_lines.append(_rx)
        if strict:   # v226: 検品リトライ(1回目が話題超過だった時)
            _lim = 0 if plevel <= 0 else (2 if plevel >= 2 else 1)
            ctx_lines.append(f"【重要・出し直し】前回の出力は相手個別の話題を入れすぎていた。"
                             f"個別の話題は{_lim}個以内に減らし、残りは挨拶・季節・本題だけにする。")
    # v101: 顧客カードを配信生成にも実接続
    # v226(A案): plevel1では直近30日の会話に実際に出た話題だけカードから渡す(鮮度フィルタ)。
    # plevel0はカード事実そのものを渡さない
    try:
        from . import crm as _crm
        if mode == "thanks" or plevel >= 2:
            _cb = _crm.card_prompt_block(v["code"])
        elif plevel <= 0:
            _cb = _crm.card_prompt_block(v["code"], only_keys=set())   # 安全系(NG話題・担当)だけ残る
        else:
            _cb = _crm.card_prompt_block(v["code"], only_keys=_fresh_topic_keys(v["code"]))
    except Exception:
        _cb = ""
    # v118: 関係性(事実)＋許容レベル(本人確定のみ)を配信にも注入
    _rel = _tol = _sm = ""
    try:
        from . import linebot as _lb
        _rel = _lb.relationship_prompt_block(v["code"])
        _tol = _lb.tolerance_prompt_block(v["code"])
        _tol += ("\n\n" + _lb.myself_prompt_block(v["code"])) if _lb.myself_prompt_block(v["code"]) else ""   # v230
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
    # v240(本人報告「アナウンスでAI生成に失敗することが多い」)。配信は人数分を連続で叩くため、
    # 1回きりの呼び出し+素のjson.loadsでは落ちやすかった。3つ直した:
    #   ① 429(レート制限)・5xx(過負荷)・タイムアウトを短い待ちで2回まで再試行
    #      … 連続生成でいちばん当たりやすいのがここ。従来は1発で諦めて定型文へ落ちていた
    #   ② JSONを頑丈に取り出す(前置き・後語り・途中切れを救出)。drafts.pyと同じ手当て
    #   ③ max_tokens 450→700。カード・関係性・許容・🪞・実例と注入が増えた分、
    #      450では閉じ括弧の手前で切れることがあった
    last = None
    for attempt in range(3):
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": config.ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": config.ANTHROPIC_MODEL,
                    "max_tokens": 700,
                    "system": system,
                    "messages": [{"role": "user", "content": user_prompt}],
                },
                timeout=30,
            )
            if r.status_code in (429, 500, 502, 503, 504, 529):
                last = f"HTTP {r.status_code}"
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise RuntimeError(last)
            r.raise_for_status()
            out = "".join(b.get("text", "") for b in r.json().get("content", []))
            text = str(_json_text(out) or "").strip()
            if not text:
                raise ValueError("本文が空")
            return text
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            if attempt < 2 and isinstance(e, (requests.Timeout, requests.ConnectionError,
                                              RuntimeError)):
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
    raise RuntimeError(last or "生成できませんでした")


def _json_text(out: str) -> str:
    """AI出力から text を頑丈に取り出す(前置き・後語り・途中切れに耐える)。

    drafts.py の _parse_json_out と同じ思想。従来は ``` を消して json.loads を
    直呼びしていたため、AIが一言添えただけ・末尾が切れただけで全部失敗していた。
    """
    out = re.sub(r"```(json)?", "", out or "").strip()
    s, e = out.find("{"), out.rfind("}")
    if s >= 0 and e > s:
        try:
            v = json.loads(out[s:e + 1]).get("text")
            if v:
                return v
        except Exception:
            pass
    # 閉じが欠けている時: "text": "…" だけを取り出す(エスケープ済みの引用符も追う)
    m = re.search(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"', out)
    if m:
        try:
            return json.loads(f'"{m.group(1)}"')
        except Exception:
            return m.group(1).replace("\\n", "\n").replace('\\"', '"')
    # 最後の砦: max_tokens切れで閉じ引用符ごと欠けた場合。ちぎれた断片を送らせないよう
    # ある程度の長さがある時だけ拾う(短い欠片は定型文に落としたほうがまし)。
    # どのみち本人が緑を押す前に目にするので、直せる形で見せるほうが手が止まらない
    m2 = re.search(r'"text"\s*:\s*"((?:[^"\\]|\\.)+)$', out)
    if m2:
        frag = m2.group(1).replace("\\n", "\n").replace('\\"', '"').rstrip()
        if len(frag) >= 15:
            print(f"[campaign] 途中で切れた出力を救出({len(frag)}字)", flush=True)
            return frag
    raise ValueError("AI出力にJSONが見つからない")


def _force_yobina(text, v):
    """v217: 文面中の登録名呼びかけを呼び名へ置換する決定論ガード(プロンプト指示の保険)。
    登録名と呼び名が部分文字列関係(山本/山本さん等)の時は二重敬称の危険があるため触らない。"""
    yob = (v.get("yobina") or "").strip()
    code = (v.get("code") or "").strip()
    if not yob or not text or not code or code == yob:
        return text
    h = hon(yob)
    if code in h or h in code or code in yob or yob in code:
        return text
    out = text
    # v235(監査実測): 敬称の取りこぼしで「山本先生」→「ヒロさん先生」の二重敬称が出ていた。
    # _HON_RE と同じ範囲まで広げる
    for suf in ("さん", "様", "さま", "ちゃん", "くん", "君", "先生", "ママ"):
        out = out.replace(code + suf, h)
    # v235(監査実測): 敬称なしの裸置換は語境界が無く、短い登録名が普通語の一部に当たる
    #   「あいにくの雨」→「アイリさんにくの雨」/「なおさら」→「ナオミさんさら」
    # 取りこぼし(呼び捨て言及)より誤爆のほうが高くつくので、短い名前では裸置換をしない
    if len(code) >= 3:
        out = out.replace(code, h)
    return out


def generate(mode="greeting", ranks=None, tags=None, template="", now=None, codes=None,
             purpose="", plevel=1) -> dict:
    """あて先を選び、一人ずつ違う下書きを一括生成して返す(保存も送信もしない)。
    codes 指定時はその相手だけ(UIで個別に外した人を除く)。
    template=必達内容(本人が書いた文) / purpose=配信の種類(出勤・イベント等)。
    plevel=個別化つまみ(0=入れない/1=ひとつまみ/2=しっかり)。v226"""
    now = now or time.time()
    recips = select_recipients(ranks, tags, mode, now, codes)
    ai = bool(config.ANTHROPIC_API_KEY)
    items = []
    _fails = []      # v240: AI生成に失敗して定型文に落ちた相手
    for v in recips:
        try:
            text = (_generate_one_ai(v, mode, template, now, purpose=purpose, plevel=plevel)
                    if ai else _template_one(v, mode, template))
            row_ai = ai
        except Exception as e:
            # v240: 理由を残す。従来は黙って定型文に落ちるだけで、なぜ失敗したのかが
            # ログにも画面にも残らず、本人の「失敗することが多い」を追えなかった
            print(f"[campaign 生成失敗] {v['code']}: {type(e).__name__}: {e}", flush=True)
            _fails.append(v["code"])
            text = _template_one(v, mode, template)
            row_ai = False
        # v226: 決定論検品 — つまみの上限を超えて話題が入っていたら1回だけ厳しめに出し直す
        if row_ai and mode != "thanks":
            _lim = 0 if plevel <= 0 else (2 if plevel >= 2 else 1)
            try:
                if _topic_hits(text, v["code"]) > _lim:
                    print(f"[campaign 検品] {v['code']}: 話題{_topic_hits(text, v['code'])}個>"
                          f"上限{_lim} → 出し直し", flush=True)
                    text = _generate_one_ai(v, mode, template, now, purpose=purpose,
                                            plevel=plevel, strict=True)
            except Exception as _e:
                print(f"[campaign 検品] {_e}", flush=True)
        text = _force_yobina(text, v)   # v217: AIが登録名で呼んでも決定論で呼び名に戻す
        items.append({
            "code": v["code"], "rank": v["rank"], "tags": v["tags"],
            "last_visit": v["last_visit"], "why": _why(v, mode),
            "text": text, "ai": row_ai,
        })
    if _fails:
        print(f"[campaign] {len(_fails)}/{len(items)}人がAI生成に失敗→定型文: "
              + "、".join(_fails[:8]), flush=True)
    return {"mode": mode, "season": season_label(now), "ai": ai,
            "count": len(items), "items": items,
            "ai_failed": len(_fails)}   # v240: 画面で「何人が定型文になったか」を出せるように
