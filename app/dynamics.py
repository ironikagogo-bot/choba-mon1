"""v172: 関係ダイナミクス分析(「会話の温度・距離・振る舞い」の精密抽出マシン)。

本人の依頼「言語化されにくいパラメータを精密に拾い出しbot返信の完成度を上げる」の実装。
設計はエージェント5体の並列調査(コード監査/分類体系/抽出設計/定量設計/赤チーム)の統合。

2層構造:
  [決定論層] parse_events/compute_metrics/dynamics_block — LLM不使用の数値層。
    txtのタイムスタンプから返信速度の非対称・会話開始者比・くだけ度Δ・沈黙・深夜率・
    モメンタム等を中央値ベースで計算。嘘をつかない(幻覚ゼロ)ので常時注入してよい。
  [LLM層] extract_arc — 温度(5段階)・距離(5段階)・力関係・本人の型・相手の癖・未回収・
    地雷の「関係アーク」をLLM抽出。全項目にsrc(原文引用・部分文字列検証で捏造検知)+conf必須。
    ⚠️家訓(v118/v164): AI推定は本人の○✕確定を経てからしか下書きに効かせない。
    保存はするが、注入はカード画面で本人が「✓下書きに使う」を押した相手のみ。

保存先: 既存のstyle_profileテーブルの相手別JSON(cp["dynamics"] / cp["arc"])。
新テーブルは作らない(v163の教訓=同じ概念を複数系統に分けない)。
鮮度: txt末尾日時をas_ofとして持ち、90日超は注入しない(v148の鮮度規約に従う)。
"""
import datetime
import json
import re
import time
import unicodedata
from statistics import median

import requests

from . import config, db

# ---------- パース(既存regexと互換・日付を捨てない) ----------
DATE_RE = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})")
MSG_RE = re.compile(r"^(\d{1,2}):(\d{2})\t([^\t]+)\t(.*)$")
MEDIA_RE = re.compile(r"^\[(写真|スタンプ|動画|画像|ファイル|ボイスメッセージ|アルバム|位置情報|連絡先|ギフト|GIF)\]")
CALL_RE = re.compile(r"^☎")
UNSENT_RE = re.compile(r"メッセージの送信を取り消しました$")
EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF☀-➿]")

BURST_GAP = 600          # 同一人の連投を1塊にする間隔(10分)
REPLY_CAP = 6 * 3600     # これ以上空いたら「返信」でなく「再開」(6時間)
SESSION_GAP = 6 * 3600   # セッション分割
SILENCE_GAP = 24 * 3600  # 「沈黙」の定義
FRESH_DAYS = 90          # これより古いtxt由来の分析は注入しない
ARC_MIN_CHARS = 3000     # アーク抽出の最小会話量(persona分析と同じ閾値。短い会話に温度を語らせない)


def _kind(t: str) -> str:
    t = t.strip()
    if UNSENT_RE.search(t):
        return "unsent"
    if MEDIA_RE.match(t):
        return "media"
    if CALL_RE.match(t):
        return "call"
    return "text"


def parse_events(text: str, self_name: str):
    """txt → ([{ts, sender, is_self, text, kind}], meta)。行順保持・重複除去。
    既存parse_talk(style_profile)と同じ行判定+日付ヘッダ引き継ぎでtsを付ける。"""
    events, cur_date, seen = [], None, set()
    total = accepted = 0
    for line in text.splitlines():
        if not line.strip() or line.startswith(("[LINE]", "保存日時")):
            continue
        d = DATE_RE.match(line)
        if d:
            cur_date = tuple(map(int, d.groups()))
            continue
        m = MSG_RE.match(line)
        if m and cur_date:
            total += 1
            h, mm, sender, body = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4)
            try:
                ts = datetime.datetime(cur_date[0], cur_date[1], cur_date[2], h, mm).timestamp()
            except ValueError:
                continue
            accepted += 1
            sender = " ".join(unicodedata.normalize("NFKC", sender).split())
            key = (ts, sender, body)
            if key in seen:
                continue
            seen.add(key)
            events.append({"ts": ts, "sender": sender, "is_self": sender == self_name,
                           "text": body, "kind": _kind(body)})
        elif m:
            total += 1   # 日付未確定のメッセージ行(tsが作れない)
        elif events:
            events[-1]["text"] += "\n" + line   # 複数行継続
    meta = {"accept_ratio": round(accepted / total, 2) if total else 0.0,
            "n_senders": len({e["sender"] for e in events}),
            "span_days": round((events[-1]["ts"] - events[0]["ts"]) / 86400, 1) if events else 0,
            "end_ts": events[-1]["ts"] if events else None}
    return events, meta


def _bursts(events):
    out = []
    for e in events:
        if out and out[-1]["is_self"] == e["is_self"] and e["ts"] - out[-1]["ts_end"] <= BURST_GAP:
            b = out[-1]
            b["ts_end"] = e["ts"]
            b["n"] += 1
        else:
            out.append({"is_self": e["is_self"], "ts_start": e["ts"], "ts_end": e["ts"], "n": 1})
    return out


def _sessions(bursts):
    out = []
    for b in bursts:
        if out and b["ts_start"] - out[-1]["ts_end"] <= SESSION_GAP:
            s = out[-1]
            s["ts_end"] = b["ts_end"]
            s["swaps"] += (1 if s["last_self"] != b["is_self"] else 0)
            s["last_self"] = b["is_self"]
            s["gaps"].append(b["ts_start"] - s["prev_end"])
            s["prev_end"] = b["ts_end"]
        else:
            out.append({"starter_self": b["is_self"], "last_self": b["is_self"],
                        "ts_start": b["ts_start"], "ts_end": b["ts_end"],
                        "swaps": 0, "gaps": [], "prev_end": b["ts_end"]})
    return out


def compute_metrics(events, meta, global_profile=None) -> dict:
    """充足しない指標はキー自体を入れない(誤った確信>無情報 の害を避ける)。"""
    ev = [e for e in events if e["kind"] != "unsent"]
    out = {"n": len(ev), "meta": meta}
    if len(ev) < 30 or meta["span_days"] < 14 or meta["accept_ratio"] < 0.6 or meta["n_senders"] > 2:
        return out
    n_self = sum(1 for e in ev if e["is_self"])
    share = n_self / len(ev)
    out["share_self"] = round(share, 2)
    if share >= 0.9 or share <= 0.1:
        out["one_sided"] = True
        return out
    bs = _bursts([e for e in ev if e["kind"] != "call"])
    ss = _sessions(bs)
    # 返信ペア(交代する隣接バースト・6時間以内のみ)
    lat_self, lat_them = [], []
    for a, b in zip(bs, bs[1:]):
        if a["is_self"] == b["is_self"]:
            continue
        lat = b["ts_start"] - a["ts_end"]
        if not (0 <= lat < REPLY_CAP):
            continue
        (lat_self if b["is_self"] else lat_them).append(lat)
    if len(lat_self) >= 5:
        out["reply_med_self_min"] = round(median(lat_self) / 60, 1)
    if len(lat_them) >= 5:
        out["reply_med_them_min"] = round(median(lat_them) / 60, 1)
    if len(ss) >= 6:
        out["init_self"] = round(sum(1 for s in ss if s["starter_self"]) / len(ss), 2)
        rallies = sum(1 for s in ss if s["swaps"] >= 6 and s["gaps"] and median(s["gaps"]) < 180)
        out["rally_rate"] = round(rallies / len(ss), 2)
    # 文長(text・URL単独行除外)
    tx_self = [e["text"] for e in ev if e["is_self"] and e["kind"] == "text"
               and not e["text"].strip().startswith("http")]
    tx_them = [e["text"] for e in ev if not e["is_self"] and e["kind"] == "text"
               and not e["text"].strip().startswith("http")]
    if len(tx_self) >= 10 and len(tx_them) >= 10:
        out["len_med_self"] = median(len(t) for t in tx_self)
        out["len_med_them"] = median(len(t) for t in tx_them)
    # くだけ度Δ(この相手への絵文字密度 / 全体平均)
    if len(tx_self) >= 15 and global_profile and global_profile.get("emoji_per_msg", 0) > 0.05:
        dens = sum(len(EMOJI_RE.findall(t)) for t in tx_self) / len(tx_self)
        out["casual_delta"] = round(dens / global_profile["emoji_per_msg"], 2)
    # 沈黙(24h+)と破り手
    silences = brk_self = 0
    for a, b in zip(ss, ss[1:]):
        if b["ts_start"] - a["ts_end"] >= SILENCE_GAP:
            silences += 1
            brk_self += 1 if b["starter_self"] else 0
    if silences >= 4 and meta["span_days"] >= 45:
        out["silence_break_self"] = round(brk_self / silences, 2)
    # 深夜率(0-5時)
    deep = sum(1 for e in ev if 0 <= datetime.datetime.fromtimestamp(e["ts"]).hour <= 5)
    out["deep_rate"] = round(deep / len(ev), 2)
    # モメンタム(直近30日 vs 全期間。基準日=txt末尾=「今日」ではない)
    if meta["span_days"] >= 60:
        base = meta["end_ts"]
        recent = [e for e in ev if e["ts"] >= base - 30 * 86400]
        per_day_all = len(ev) / meta["span_days"]
        per_day_recent = len(recent) / 30
        if per_day_all > 0:
            out["momentum"] = round(per_day_recent / per_day_all, 2)
    return out


def dynamics_block(m: dict) -> str:
    """指標→150字目標の日本語ブロック。中立域(言う価値のない偏り)は出さない。
    数値の羅列でなく行動指針に翻訳。該当なしは空文字(=注入ゼロ・従来とバイト一致)。"""
    if not m or m.get("one_sided"):
        return ""
    lines = []
    mo = m.get("momentum")
    if mo is not None and mo < 0.5:
        lines.append("・直近1ヶ月はやり取りが減速中→重い話より軽い近況ノックが合う")
    elif mo is not None and mo > 1.5:
        lines.append("・直近1ヶ月は加熱中→話を先に進めてよい流れ")
    ini = m.get("init_self")
    if ini is not None and ini >= 0.65:
        lines.append("・会話は本人から始めることが多い→自分から話題を切り出すのが自然")
    elif ini is not None and ini <= 0.35:
        lines.append("・会話はほぼ相手から来る→こちらから送る時は軽い口実を添えると自然")
    rs, rt = m.get("reply_med_self_min"), m.get("reply_med_them_min")
    if rs is not None and rt is not None:
        if rt <= 10 and rs >= 60:
            lines.append("・相手は即レス型/本人はマイペース型→間が空いても謝らない(いつもの型)")
        elif rs <= 10 and rt >= 60:
            lines.append("・本人は即レス型/相手はゆっくり型→返事を急かさない")
    cd = m.get("casual_delta")
    if cd is not None and cd > 1.5:
        lines.append("・この相手には普段より絵文字多め→砕けてよい")
    elif cd is not None and cd < 0.5:
        lines.append("・この相手には普段より固め→今回も固めを維持")
    if m.get("rally_rate", 0) >= 0.4:
        lines.append("・短文ラリーで盛り上がる仲→一通に詰め込まず短めで打ち返す")
    sb = m.get("silence_break_self")
    if sb is not None and sb >= 0.75:
        lines.append("・間が空いたら戻すのはいつも本人→こちらから軽く再開してよい")
    if m.get("deep_rate", 0) >= 0.15:
        lines.append("・深夜のやり取りが普通にある仲→時間帯の前置きは不要")
    if not lines:
        return ""
    lines = lines[:4]
    block = "【関係の型(履歴の数値から・機械集計の事実)】\n" + "\n".join(lines)
    while len(block) > 170 and len(lines) > 1:
        lines.pop()
        block = "【関係の型(履歴の数値から・機械集計の事実)】\n" + "\n".join(lines)
    return block


# ---------- LLM層: 関係アーク抽出 ----------
CHUNK = 42000
TEMPS = ("冷えている", "低め", "ふつう", "あたたかい", "熱い")
DISTS = ("他人行儀", "丁寧", "打ち解け", "親密", "ベッタリ")
TRENDS = ("上昇", "横ばい", "下降", "乱高下")
POWERS = ("相手主導", "やや相手", "対等", "やや本人", "本人主導")
SELF_MODES_MIZU = ("聞き役", "甘やかし", "ツッコミ・いじり", "営業(来店誘導)", "線引き・かわし",
                   "素の友達", "励まし", "教わり役・持ち上げ", "おねだり", "事務的対応")
SELF_MODES_GEN = ("聞き役", "甘やかし", "ツッコミ・いじり", "用件推進・調整", "線引き・かわし",
                  "素の友達", "励まし", "教わり役・持ち上げ", "依頼・お願い", "事務的対応")
PATTERNS_MIZU = ("かまって連投", "即レス催促", "誘いの圧(日程押し)", "愚痴・弱音", "自慢・武勇伝",
                 "褒められ待ち", "嫉妬・独占", "際どい試し", "貢ぎ・世話焼きアピール",
                 "特別扱い要求", "説教・マウント", "素直な雑談")
PATTERNS_GEN = ("かまって連投", "即レス催促", "誘いの圧(日程押し)", "愚痴・弱音", "自慢・武勇伝",
                "褒められ待ち", "嫉妬・独占", "際どい試し", "世話焼きアピール",
                "特別扱い要求", "説教・マウント", "素直な雑談")


def _arc_prompt(talk, partner, self_name, win_label=None):
    gen = config.MODE == "general"
    modes = SELF_MODES_GEN if gen else SELF_MODES_MIZU
    pats = PATTERNS_GEN if gen else PATTERNS_MIZU
    who = partner if gen else f"{partner}(お客様)"
    if win_label:
        pos = f"の抜粋(全体の{win_label}にあたる部分・時系列順)"
        note = (f"ただしこの抜粋は履歴全体の{win_label}だけなので、snapshotsは win=\"{win_label}\" の1件だけを出す。"
                + ("currentとopen_loopsは必ずこの抜粋を根拠に判定する。" if win_label == "直近"
                   else "current・open_loopsはこの抜粋からは出さない(直近の担当ではないため)。"))
    else:
        pos = ""
        note = "序盤・中盤・直近は、この履歴全体を時系列で三等分したそれぞれの時期とする。"
    return (
        f"以下は{self_name}(本人)と{who}のLINEトーク履歴{pos}です。"
        "この会話の「温度・距離・力関係・本人の振る舞いの型」を読み取り、指定のJSONオブジェクト1個だけで出力してください。返信文は作りません。\n"
        "【ラベルは必ず次の定義から選ぶ。自由記述は禁止(集計に使うため)】\n"
        "温度(temp): 冷えている=事務的で続かない / 低め=礼儀は保つが盛り上がらない / ふつう=雑談が普通に続く / "
        "あたたかい=冗談・絵文字・自発的な話題出しが双方にある / 熱い=好意表現・連投・「会いたい」系が頻出\n"
        f"推移(trend): {'/'.join(TRENDS)}\n"
        "距離(dist): 他人行儀=敬語のみ定型的 / 丁寧=敬語基調だが個人的な話題あり / 打ち解け=敬語とタメ口混在・冗談が通じる / "
        "親密=タメ口基調・軽口・軽い甘え / ベッタリ=恋人的・依存的な近さ\n"
        f"主導権(power): {'/'.join(POWERS)}\n"
        f"本人の型(self_modes・複数可): {'/'.join(modes)}\n"
        f"相手の癖(partner_patterns・複数可): {'/'.join(pats)}\n"
        "ルール:\n"
        f"- snapshots=「序盤・中盤・直近」の各時点のtempとdistを1つずつ。目的は関係の変化の弧を捉えること。{note}\n"
        f"- 【主語ガード・最重要】self_modesは{self_name}自身の振る舞いだけ。partner_patternsは{partner}の振る舞いだけ。絶対に混ぜない\n"
        "- 全項目にsrc=根拠となる実際の発言の断片(40字以内・原文のまま・要約しない)とconf=高(複数回/明白)・中(1回だが明確)・低(弱い根拠)を必ず付ける。実発言を引用できない項目は出さない\n"
        "- 【推測禁止】読み取れない項目はキーごと省略。null・空文字・「不明」で埋めない\n"
        "- open_loops=未回収の話題・感情的な宿題(約束したきりの件・答えを待っている質問・土産等)。最後のメッセージ日から2ヶ月以内のものだけ。v≤40字\n"
        "- landmines=相手が不機嫌・不快になった場面。trigger≤30字/self_action=本人の対応≤40字/result=回復・未回復・不明/src_hit/src_recover(回復時のみ)\n"
        "- 「写真を送信しました」等のシステム行・スタンプのみは根拠にしない\n"
        "- 温度と距離を混同しない(tempは盛り上がり、distは口調と甘えの近さ。丁寧×あたたかい、親密×冷えている、どちらもあり得る)\n"
        '- 出力はJSONオブジェクトのみ。形式(値は例): {"snapshots":[{"win":"序盤","temp":"低め","dist":"丁寧","src":"お待ちしております","conf":"高"}],'
        '"current":{"temp":"あたたかい","trend":"上昇","src":"じゃあ23時ごろ寄るわ","conf":"高"},'
        '"power":{"v":"対等","src":"たこ焼き買ってきてよ","conf":"中"},'
        '"self_modes":[{"v":"ツッコミ・いじり","src":"スケール小さい会長","conf":"高"}],'
        '"partner_patterns":[{"v":"かまって連投","src":"既読はよ","conf":"高"}],'
        '"open_loops":[{"v":"同伴の日程を返す約束のまま","src":"シフト見て連絡するね","conf":"高"}],'
        '"landmines":[{"trigger":"嫉妬","self_action":"冗談で流す","result":"回復","src_hit":"もっと長くついてよ","src_recover":"わかってるよ。冗談","conf":"高"}]}\n'
        f"---\n{talk}"
    )


def _arc_system():
    if config.MODE == "general":
        return ("あなたは人間関係メモアプリの会話分析エンジン。利用者(本人)が自分自身のトーク履歴から"
                "相手との関係の温度感メモを作る正当な用途であり、本人の依頼と同意に基づく。"
                "評価や助言はせず、会話に書かれていることだけを読み取る。"
                "出力は必ずJSONオブジェクト1個のみ。説明文・前置き・コードブロック記号は書かない。")
    return ("あなたは接客業向け顧客管理(CRM)アプリの会話分析エンジン。利用者(ホステス本人)が"
            "自分自身のトーク履歴から相手との関係の温度感メモを作る正当な業務であり、本人の依頼と同意に基づく。"
            "評価や助言はせず、会話に書かれていることだけを読み取る。"
            "出力は必ずJSONオブジェクト1個のみ。説明文・前置き・コードブロック記号は書かない。")


def _call_arc(talk, partner, self_name, win_label=None):
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": config.ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": config.ANTHROPIC_MODEL, "max_tokens": 1500,
                  "system": _arc_system(),
                  "messages": [{"role": "user",
                                "content": _arc_prompt(talk, partner, self_name, win_label)}]},
            timeout=90)
        r.raise_for_status()
        out = "".join(b.get("text", "") for b in r.json().get("content", []))
        out = re.sub(r"```(json)?", "", out).strip()
        return json.loads(out[out.index("{"):out.rindex("}") + 1])
    except Exception as e:
        print(f"[dynamics arc call] {e}", flush=True)
        return None


def _valid_item(x, allowed_v=None, talk=""):
    """ラベル許可リスト検証+src原文照合(捏造検知)+conf矯正。不正はNone(=破棄)。"""
    if not isinstance(x, dict):
        return None
    src = (x.get("src") or x.get("src_hit") or "").strip()
    if not src:
        return None
    if talk and src[:20] not in talk:   # srcが会話原文の部分文字列でない=捏造の疑い→破棄
        return None
    if allowed_v is not None and (x.get("v") or "").strip() not in allowed_v:
        return None
    if x.get("conf") not in ("高", "中", "低"):
        x["conf"] = "低"   # 不正conf→低に矯正(注入対象外に落ちる=安全側)
    return x


def _validate_arc(arc, talk):
    """スキーマ検証。列挙外ラベル・src欠落/捏造の項目は破棄。"""
    if not isinstance(arc, dict):
        return {}
    gen = config.MODE == "general"
    modes = SELF_MODES_GEN if gen else SELF_MODES_MIZU
    pats = PATTERNS_GEN if gen else PATTERNS_MIZU
    out = {}
    snaps = []
    for s in (arc.get("snapshots") or [])[:3]:
        if (isinstance(s, dict) and s.get("win") in ("序盤", "中盤", "直近")
                and s.get("temp") in TEMPS and s.get("dist") in DISTS and _valid_item(s, None, talk)):
            snaps.append({k: s[k] for k in ("win", "temp", "dist", "src", "conf")})
    if snaps:
        out["snapshots"] = snaps
    cur = arc.get("current")
    if (isinstance(cur, dict) and cur.get("temp") in TEMPS and cur.get("trend") in TRENDS
            and _valid_item(cur, None, talk)):
        out["current"] = {k: cur[k] for k in ("temp", "trend", "src", "conf")}
    pw = _valid_item(arc.get("power"), POWERS, talk)
    if pw:
        out["power"] = {k: pw[k] for k in ("v", "src", "conf")}
    for key, allowed in (("self_modes", modes), ("partner_patterns", pats)):
        items = [_valid_item(x, allowed, talk) for x in (arc.get(key) or [])[:4]]
        items = [{k: x[k] for k in ("v", "src", "conf")} for x in items if x]
        if items:
            out[key] = items
    loops = []
    for x in (arc.get("open_loops") or [])[:3]:
        x = _valid_item(x, None, talk)
        if x and (x.get("v") or "").strip():
            loops.append({"v": x["v"][:40], "src": x["src"][:40], "conf": x["conf"]})
    if loops:
        out["open_loops"] = loops
    mines = []
    for x in (arc.get("landmines") or [])[:3]:
        if (isinstance(x, dict) and x.get("result") in ("回復", "未回復", "不明")
                and _valid_item(x, None, talk)):
            mines.append({"trigger": (x.get("trigger") or "")[:30],
                          "self_action": (x.get("self_action") or "")[:40],
                          "result": x["result"], "src_hit": (x.get("src_hit") or "")[:40],
                          "src_recover": (x.get("src_recover") or "")[:40],
                          "conf": x.get("conf", "低")})
    if mines:
        out["landmines"] = mines
    return out


def extract_arc(text, partner, self_name):
    """関係アークの抽出(チャンク戦略: ≤42000字=1回全文 / 超過=序盤・中盤・直近の3窓)。"""
    if not config.ANTHROPIC_API_KEY:
        return None
    chunks = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)]
    if len(chunks) == 1:
        raw = _call_arc(chunks[0], partner, self_name)
        arc = _validate_arc(raw, text) if raw else {}
    else:
        wins = [("序盤", chunks[0]), ("中盤", chunks[len(chunks) // 2]), ("直近", chunks[-1])]
        merged = {"snapshots": [], "self_modes": [], "partner_patterns": [], "landmines": []}
        for label, ch in wins:
            raw = _call_arc(ch, partner, self_name, label)
            if not raw:
                continue
            v = _validate_arc(raw, ch)
            merged["snapshots"].extend([s for s in v.get("snapshots", []) if s["win"] == label])
            if label == "直近":   # current/open_loopsは直近窓の専有(他窓の分は破棄)
                if v.get("current"):
                    merged["current"] = v["current"]
                if v.get("open_loops"):
                    merged["open_loops"] = v["open_loops"]
                if v.get("power"):
                    merged["power"] = v["power"]
            merged["self_modes"].extend(v.get("self_modes", []))
            merged["partner_patterns"].extend(v.get("partner_patterns", []))
            merged["landmines"].extend(v.get("landmines", []))
        # 統合: 同一ラベルは最高confに・上位4件
        for key in ("self_modes", "partner_patterns"):
            best = {}
            rank = {"高": 2, "中": 1, "低": 0}
            for x in merged[key]:
                cur = best.get(x["v"])
                if cur is None or rank[x["conf"]] > rank[cur["conf"]]:
                    best[x["v"]] = x
            merged[key] = sorted(best.values(), key=lambda x: -rank[x["conf"]])[:4]
        merged["landmines"] = merged["landmines"][:3]
        arc = {k: v for k, v in merged.items() if v}
    if not arc:
        return None
    # 距離変化の機械導出
    sn = {s["win"]: s for s in arc.get("snapshots", [])}
    if "序盤" in sn and "直近" in sn:
        arc["dist_change"] = {"from": sn["序盤"]["dist"], "to": sn["直近"]["dist"]}
    return arc


_TEMP_HINT = {
    "冷えている": "短く軽く。距離を詰める言葉・お願い事は入れない",
    "低め": "丁寧に淡々と。急に馴れ馴れしくしない",
    "ふつう": "いつも通りの調子で",
    "あたたかい": "冗談・軽口に乗ってよい。話題を一つ足す余裕あり",
    "熱い": "温度に釣られて燃料を足さない。受けは軽く一言",
}


def arc_block(arc: dict) -> str:
    """アーク→注入ブロック(300字目標)。conf=低は行ごと出さない。currentが無ければ全体を出さない。
    ⚠️呼び出し側で「本人が✓下書きに使うを確定済み(ok==1)」の相手だけに使うこと(家訓)。"""
    if not arc or not arc.get("current"):
        return ""
    cur = arc["current"]
    if cur.get("conf") == "低":
        return ""
    lines = []
    hint = _TEMP_HINT.get(cur["temp"], "")
    extra = "。最近冷え気味。冷えた原因らしき話題を蒸し返さない" if cur["trend"] in ("下降", "乱高下") else ""
    lines.append(f"- いまの温度: {cur['temp']}・{cur['trend']} → {hint}{extra}")
    modes = [x["v"] for x in arc.get("self_modes", []) if x["conf"] != "低"][:3]
    if modes:
        lines.append(f"- 本人の型: {'+'.join(modes)}。この型を崩さない")
    dc = arc.get("dist_change")
    if dc and dc["from"] != dc["to"]:
        lines.append(f"- 距離: {dc['from']}→{dc['to']}に変化済み。今の距離({dc['to']})を基準に書き、昔の距離に戻らない")
    pats = [x["v"] for x in arc.get("partner_patterns", []) if x["conf"] != "低"][:3]
    if pats:
        lines.append(f"- 相手の癖: {'・'.join(pats)}")
    loops = [x for x in arc.get("open_loops", []) if x["conf"] != "低"]
    if loops:
        lines.append(f"- 未回収: {loops[0]['v']}(文脈が合えば一言拾う。無理に入れない)")
    body = "\n".join(lines)
    while len(body) > 300 and len(lines) > 1:
        lines.pop()
        body = "\n".join(lines)
    return ("【この相手との温度・間合い(履歴分析・参考)】上の実例・距離感の指定(敬語厳守など)と食い違う場合はそちらを優先:\n"
            + body)


# ---------- 保存・読み出し(style_profileの相手別JSONに同居=新テーブルなし) ----------

def analyze_and_save(contact: str, text: str, self_name: str, with_arc: bool = True) -> dict:
    """txt取り込み時の本体。決定論指標は常に計算・保存。アークは3000字以上の会話のみ。
    失敗しても例外を上げない(取り込み本流を止めない)。"""
    try:
        events, meta = parse_events(text, self_name)
        gp = db.get_profile("_global") or {}
        metrics = compute_metrics(events, meta, gp)
        cp = db.get_profile(contact) or {}
        prev_arc = (cp.get("arc") or {})
        cp["dynamics"] = {"metrics": {k: v for k, v in metrics.items() if k != "meta"},
                          "block": dynamics_block(metrics),
                          "as_of": meta.get("end_ts"), "meta": metrics.get("meta")}
        if with_arc and len(text) >= ARC_MIN_CHARS:
            arc = extract_arc(text, contact, self_name)
            if arc:
                arc["as_of"] = meta.get("end_ts") or time.time()
                arc["ok"] = prev_arc.get("ok")   # 本人の✓/✕判断は再取り込みでも引き継ぐ(toleranceと同じ)
                cp["arc"] = arc
        db.save_profile(contact, cp)
        n_arc = len((cp.get("arc") or {}).get("snapshots", []))
        print(f"[dynamics] {contact}: 指標{len(cp['dynamics']['metrics'])}件・アーク時点{n_arc}件", flush=True)
        return cp
    except Exception as e:
        print(f"[dynamics analyze] {e}", flush=True)
        return {}


def _fresh(as_of) -> bool:
    return bool(as_of) and (time.time() - as_of) < FRESH_DAYS * 86400


def blocks_for_draft(contact: str) -> str:
    """生成時注入(drafts.pyから呼ぶ)。
    決定論ブロック=常時(鮮度内のみ)。アークブロック=本人がok==1で確定した相手のみ(家訓)。
    データ無し・鮮度切れ・未確定は空文字=注入ゼロで従来とバイト一致。"""
    try:
        cp = db.get_profile(contact) or {}
        out = []
        dyn = cp.get("dynamics") or {}
        if dyn.get("block") and _fresh(dyn.get("as_of")):
            out.append(dyn["block"])
        arc = cp.get("arc") or {}
        if arc.get("ok") == 1 and _fresh(arc.get("as_of")):
            b = arc_block(arc)
            if b:
                out.append(b)
        return "\n\n".join(out)
    except Exception:
        return ""


def backfill_async():
    """過去取り込み済みの全トーク原文から一括分析(初回起動時1回・裏方)。situationsと同型。"""
    if not config.ANTHROPIC_API_KEY:
        return
    import threading

    def work():
        try:
            from . import linebot
            linebot.ensure()
            if linebot._meta_get("dynamics_backfill") == "done":
                return
            with db.conn() as c:
                rows = [dict(r) for r in c.execute("SELECT contact, text FROM linebot_talks")]
            self_name = (db.get_profile("_selfname") or {}).get("name") or "自分"
            done = 0
            for r in rows:
                try:
                    cp = db.get_profile(r["contact"]) or {}
                    if cp.get("dynamics"):
                        continue
                    if analyze_and_save(r["contact"], r["text"], self_name):
                        done += 1
                    time.sleep(3)
                except Exception as e:
                    print(f"[dynamics backfill row] {r.get('contact')}: {e}", flush=True)
            linebot._meta_set("dynamics_backfill", "done")
            print(f"[dynamics backfill] 完了: {len(rows)}人分を走査・{done}人分を新規分析", flush=True)
        except Exception as e:
            print(f"[dynamics backfill] {e}", flush=True)

    threading.Thread(target=work, daemon=True).start()
