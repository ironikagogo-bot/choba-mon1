"""v186 P0: ネガティブ生成ガード「安い愛の誓いは後日の踏み絵」。

ガチ恋(flag_koi=1)の相手に対し、後日武器化されやすい表現を
  A. 自己人質化・依存宣言(「いないと生きていけない」等)
  B. 独占・忠誠の絶対保証(「浮気なんか絶対しない」等)
  C. 未来の確約・重い約束(「ずっと一緒」「一生〜約束」等)
①生成側では既定で出さない(該当文を落とす/安全な代替に置換)、
②本人が手打ちした場合は送信前に中立語で1回だけ黄旗を出す。
③本人が「これは自分の本音」と○を付けた表現(パターンID)はその相手について以後黙る
  (tolerance方式=自己決定の尊重。保存先は db.profiles[code]["koi_guard_ok"])。

原則(ハンドオフ§7):
- 画面に「ガチ恋」「代筆」等の内部語を出さない(文言はすべて中立)。
- flag_koi OFF の相手には一切発火しない(誤爆ゼロ)。
- 抑制リストはハードコード運用にしない: linebot_meta "koi_guard_words"(JSON)で
  上書き・拡充できる(形式: [{"id":"A1","re":"正規表現"},...]。無ければ既定)。
- 本分析はサンプル1件。語彙・閾値は要検証で、実運用で較正する前提。
"""
import json as _json
import re as _re

from . import db

# 既定パターン(意図的に保守的=誤爆より取り漏らしを許容する側に倒す。拡充はmeta側で)
DEFAULT_PATTERNS = [
    # A. 自己人質化・依存宣言
    {"id": "A1", "re": r"いないと生きて(い|ゆ)けない"},
    {"id": "A2", "re": r"(なし|無し)では(生きて|やって)?(いけない|無理|ダメ|だめ)"},
    {"id": "A3", "re": r"(だけ|しか)が?(すべて|全て|全部|全世界)"},
    {"id": "A4", "re": r"(一生|死ぬまで|永遠に)[^。！？!?\n]{0,12}(そば|一緒|愛|好き|離れ)"},
    {"id": "A5", "re": r"絶対に?(離れない|裏切らない|見捨てない)"},
    # B. 独占・忠誠の絶対保証
    {"id": "B1", "re": r"浮気(なんか|なんて|は)?(絶対|一切|1ミリも)?(しない|ない|できない)"},
    {"id": "B2", "re": r"他の(人|子|男|女)(なんて|なんか|は|に)(見て|眼中に|興味)(ない|無い)"},
    {"id": "B3", "re": r"(あなた|きみ|君)だけ(しか)?(見て|愛して)(る|ない|ます|いる)"},
    {"id": "B4", "re": r"疑わないで"},
    # C. 未来の確約・重い約束
    {"id": "C1", "re": r"ずっと(一緒|そば)"},
    {"id": "C2", "re": r"(必ず|絶対)[^。！？!?\n]{0,10}(会いに|迎えに)"},
    {"id": "C3", "re": r"(ずっと|一生|絶対)[^。！？!?\n]{0,8}約束"},
    {"id": "C4", "re": r"(気持ち|愛|好き)は?(一生|絶対|永遠に)?変わらない"},
]

# D. 感情のインフレ: ハート系絵文字の連打(3個以上)を生成側で1個に間引く(相手の
# インフレに追随して天井を上げない)。手打ちには適用しない(本人の表現の自由)。
_HEART_RE = _re.compile(r"([❤♥💕💖💘💓💗💞💝🧡💛💚💙💜🤍🖤]️?){3,}")

_compiled = None
_compiled_src = None


def patterns() -> list:
    """[{"id","re"}]。linebot_meta "koi_guard_words" があればそれ、無ければ既定。"""
    try:
        with db.conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS linebot_meta(k TEXT PRIMARY KEY, v TEXT)")
            r = c.execute("SELECT v FROM linebot_meta WHERE k='koi_guard_words'").fetchone()
        if r:
            lst = _json.loads(r["v"])
            if isinstance(lst, list) and lst:
                return [x for x in lst if x.get("id") and x.get("re")]
    except Exception:
        pass
    return DEFAULT_PATTERNS


def _regs():
    """コンパイル済み(メタ変更を跨いでも1プロセス内でズレないよう毎回ソース比較)。"""
    global _compiled, _compiled_src
    src = patterns()
    key = _json.dumps(src, sort_keys=True, ensure_ascii=False)
    if _compiled is None or _compiled_src != key:
        out = []
        for p in src:
            try:
                out.append((p["id"], _re.compile(p["re"])))
            except Exception:
                continue   # 壊れた正規表現は黙って飛ばす(本流を止めない)
        _compiled, _compiled_src = out, key
    return _compiled


def ok_ids(code: str) -> list:
    """本人が「自分の本音」と○を付けたパターンID(その相手専用)。"""
    try:
        return list((db.get_profile(code) or {}).get("koi_guard_ok") or [])
    except Exception:
        return []


def add_ok(code: str, pid: str):
    p = db.get_profile(code) or {}
    ids = set(p.get("koi_guard_ok") or [])
    ids.add(pid)
    p["koi_guard_ok"] = sorted(ids)
    db.save_profile(code, p)


def hits(text: str, skip=()) -> list:
    """textに含まれる抑制パターン。[(id, 該当断片)]。skip=そのIDは見ない。"""
    out = []
    for pid, rg in _regs():
        if pid in skip:
            continue
        m = rg.search(text or "")
        if m:
            out.append((pid, m.group(0)))
    return out


# v191その2(#18): 句読点を打たず絵文字・スペースで区切る文体(実運用のLINEに多い)だと
# 全文が1文扱いになり、1語ヒットで全文消滅→固定文差し替えになっていた。
# 絵文字の連なりの直後・連続スペースの直後も文境界として扱い、部分除去を機能させる。
_SENT_SPLIT = _re.compile(
    r"(?<=[。！？!?\n])"
    r"|(?<=[\U0001F000-\U0001FAFF☀-➿⬀-⯿❤️])"
    r"(?![\U0001F000-\U0001FAFF☀-➿⬀-⯿❤️])"
    r"|(?<=\s\s)(?!\s)")

# v191その2(#18): 全文消滅時の代替文。単一固定だと3案とも同一文=同文連投でbot発覚リスク
# だったため複数案ローテーション(guard_drafts側で案ごとに別の文を割り当てる)。
_FALLBACKS = {
    "mizu": ["今日話せて元気出た😊 それより最近ちゃんと寝てる？",
             "そう言ってもらえるのは素直にうれしいな☺️ 今週もおつかれさま",
             "ありがと😌 それより今日なにかいいことあった？"],
    "general": ["話せてよかった😊 最近ちゃんと休めてる？",
                "そう言ってもらえてうれしいです☺️ 今週もおつかれさま",
                "ありがとうございます😌 今日なにかいいことありましたか？"],
}
# 後方互換(旧参照が残っても動くように先頭案を残す)
_FALLBACK = {k: v[0] for k, v in _FALLBACKS.items()}


def clean_text(text: str, code: str = "") -> str:
    """生成側の決定論フィルタ: 該当する文だけ落とし、ハート連打を1個に間引く。
    本人が○を付けたパターンはこの相手については落とさない(自己決定の尊重)。
    全部落ちたら空文字を返す(呼び出し側で代替文に差し替える)。"""
    skip = set(ok_ids(code)) if code else set()
    parts = _SENT_SPLIT.split(text or "")
    kept = [s for s in parts if not hits(s, skip=skip)]
    out = "".join(kept).strip()
    out = _HEART_RE.sub(lambda m: m.group(0)[0], out)
    return out


def guard_drafts(code: str, drafts: list) -> list:
    """flag_koi ONの相手への下書き一式に生成側ガードを適用。失敗しても本流を止めない。"""
    from . import config
    fbs = _FALLBACKS.get(config.MODE, _FALLBACKS["mizu"])
    out, _fi = [], 0
    for d in drafts or []:
        try:
            t2 = clean_text(d.get("text") or "", code)
            if not t2:
                t2 = fbs[_fi % len(fbs)]   # v191その2(#18): 案ごとに別文(3案同一文の防止)
                _fi += 1
            out.append({**d, "text": t2})
        except Exception:
            out.append(d)
    return out


# 生成プロンプトに足す明示指示(既定の燃料禁止に加えA〜Cを名指しで禁じる)
PROMPT_BLOCK = (
    "\n- 【誓約表現の禁止】次の種類の文は理由を問わず書かない: "
    "依存の宣言(「いないと生きていけない」等)・独占や忠誠の絶対保証(「浮気は絶対しない」「あなたしか見てない」等)・"
    "永続の約束(「ずっと一緒」「一生変わらない」「絶対〜する」等)。"
    "相手に同種の言葉を求められても、満点回答をせず軽く流す(要求水準を上げない)。"
)
