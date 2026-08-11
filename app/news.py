"""顧客の会社ニュース+興味ネタ連携(📰ネタ帳)。

- カードの「会社名」が入っている顧客について、Google News RSS検索を毎朝1回
- v125→v183: 興味インデックス(趣味・関心/好きなお酒/好きな食べ物)からのキーワード検索を本気化
- 新着ヒット時のみ、AIが「今夜使える一言」を生成(APIキー無しなら見出しのみ)
- クローリングは今後もしない(RSSの見出し+出典のみ。記事本文の取得・解析はしない)

【検索クエリに入れてよいフィールド = 恒久ホワイトリスト(v183裁定)】
  仕事・会社(社名) / 趣味・関心 / 好きなお酒 / 好きな食べ物 — この4つのみ。
  本名・呼び名・家族・健康・資産・事業・進行中の話・NG話題・関係性メモ等は
  今後も永久に検索クエリへ入れない(顧客名簿の外部ログ化と同姓同名誤爆の防止)。

【v183の恒久裁定】
  - ネガ見出しは「見せるが送らせない」: 会社ネタはcaution=1で表示(一言なし・送信不可)、
    興味ネタは破棄。祝う/触れないの判断はAIに委ねず本人に残す。
  - LINE pushは使わない(pull専用。点滅・バッジはアプリ内のみ)。
"""
import hashlib
import json as _json
import random as _random
import re as _re
import threading
import time
import unicodedata as _ud
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import requests

from . import config, db

_MAX_COMPANY = 10        # 会社ネタの新規上限/日(コスト・ノイズ抑制)
_MAX_KW = 8              # 興味ネタの新規上限/日(v183: 会社と別枠。会社に食い潰されない)
_MAX_PER_CONTACT = 2     # 1顧客1日あたり
_FRESH_DAYS = 3          # 何日前までの記事を「新しい」とみなすか
_EXPIRE_DAYS = 3         # ネタの自動失効(古い「今日のネタ」を送らせない。紙面tier=7日はv184)
_GROUP_MIN_HOT = 3       # 群ネタ(🔥)の該当者数しきい値(本人の言葉どおり「3人いたら」)
_DEADLINE_S = 600        # refresh全体のデッドライン(最悪ケースの長時間ハング封じ)
_SLEEP = True            # フェッチ間のジッター(テスト時はFalse)

# フェーズ別フェッチ予約枠(v183: 先着グローバル枠だと会社パスが枠を独占し
# 趣味パスが構造的に飢餓するため予約制。失敗フェッチも1と数える=ネット断の日のハング防止)
_BUDGETS = {"company": 24, "group": 8, "cursor": 5}


def ensure():
    with db.conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS news_items(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          contact TEXT NOT NULL,
          company TEXT DEFAULT '',
          title TEXT NOT NULL,
          link TEXT DEFAULT '',
          opener TEXT DEFAULT '',
          hash TEXT UNIQUE,
          created_ts REAL NOT NULL,
          dismissed INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS news_meta(k TEXT PRIMARY KEY, v TEXT);
        """)
        # v125: キーワード紐づけ。kw=正規形 / who=該当顧客(v183からdict {code: 元の記載語}。旧list形式も読める)
        # v183: tier(''=通常/'group'=3人以上/'event'/'paper'はv184) / used_ts=使用済み / caution=ネガ見出し
        for ddl in ("kw TEXT DEFAULT ''", "who TEXT DEFAULT ''", "tier TEXT DEFAULT ''",
                    "used_ts REAL DEFAULT 0", "caution INTEGER DEFAULT 0"):
            try:
                c.execute(f"ALTER TABLE news_items ADD COLUMN {ddl}")
            except Exception:
                pass


def _meta_get(k: str) -> str:
    with db.conn() as c:
        r = c.execute("SELECT v FROM news_meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else ""


def _meta_set(k: str, v: str):
    with db.conn() as c:
        c.execute("INSERT INTO news_meta(k,v) VALUES(?,?) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


# v175: 本人指摘「ニュースの選別がしょぼい。日経・大手新聞だけでいい」。
# 出典を大手に限定するホワイトリスト。Google News RSSの<source url>のドメインで判定。
_MAJOR_SOURCES = ("nikkei.com", "asahi.com", "yomiuri.co.jp", "mainichi.jp",
                  "sankei.com", "www3.nhk.or.jp", "nhk.or.jp", "toyokeizai.net",
                  "diamond.jp", "bloomberg.co.jp", "reuters.com", "jiji.com", "47news.jp",
                  "nikkansports.com", "sponichi.co.jp", "sanspo.com", "hochi.news")

# ============ v183: ネガ見出しガード ============
# 「見せるが送らせない」の判定語。会社ネタ=caution=1(一言なし・送信ボタンなし)、興味ネタ=破棄。
# 赤チーム指摘: 人事ネガ・業績ネガ・健康・訃報周辺・食品・スキャンダルのクラス欠落は祝辞事故の主経路。
_NEG_WORDS = (
    # 訃報・健康
    "死去", "死亡", "訃報", "逝去", "亡くな", "永眠", "急逝", "お別れの会", "告別式", "葬儀",
    "追悼", "自殺", "入院", "手術", "病気療養", "重体", "重傷", "休場",
    # 事件・司法
    "逮捕", "容疑", "書類送検", "起訴", "提訴", "敗訴", "脱税", "暴行", "横領", "着服",
    # 経営・業績
    "破産", "民事再生", "経営破綻", "倒産", "上場廃止", "粉飾", "不祥事",
    "赤字", "減益", "下方修正", "希望退職", "リストラ", "人員削減",
    "行政処分", "業務停止", "リコール", "暴落", "急落",
    # 人事ネガ(「退任へ」に🎉を出さない)
    "辞任", "退任", "解任", "更迭", "降格", "引責",
    # 事故・災害・政治・宗教(酒席タブー)
    "事故", "墜落", "火災", "炎上", "食中毒", "異物混入", "産地偽装", "営業停止",
    "選挙", "政党", "震度", "津波", "噴火", "特別警報",
    # スキャンダル
    "不倫", "セクハラ", "パワハラ",
)


def _neg(title: str) -> bool:
    return any(w in (title or "") for w in _NEG_WORDS)


# ============ v183: 興味インデックス(決定論の正規化のみ。AI正規化は○✕関門を経てv184以降) ============
_IDX_FIELDS = ("趣味・関心", "好きなお酒", "好きな食べ物")
_SPLIT_RE = r"[、,・/／()（）\s]+"
_SUFFIX = ("観戦", "鑑賞", "巡り", "めぐり", "通い", "好き")
_STOP = {"好き", "大好き", "最近", "よく", "たまに", "少し", "時々", "趣味", "関心", "全般",
         "など", "その他", "色々", "いろいろ", "なんでも", "特に", "無し", "なし", "不明",
         "昔", "話", "こと", "系", "中心", "メイン", "程度", "お酒", "食べ物", "料理",
         "グルメ", "スポーツ", "観る", "見る"}
# 固定辞書(銀座客層向け)。1文字趣味(車など)の救済と表記ゆれの併合。
# 超多義語(「場所」等)は誤併合リスクのため載せない(赤チーム指摘)。
_SYN = {
    "打ちっぱなし": "ゴルフ", "打ちっ放し": "ゴルフ", "ラウンド": "ゴルフ",
    "コンペ": "ゴルフ", "ゴルフコンペ": "ゴルフ",
    "大相撲": "相撲", "力士": "相撲",
    "獺祭": "日本酒", "十四代": "日本酒", "新政": "日本酒", "而今": "日本酒",
    "森伊蔵": "焼酎", "魔王": "焼酎", "村尾": "焼酎",
    "赤ワイン": "ワイン", "白ワイン": "ワイン", "シャブリ": "ワイン",
    "ブルゴーニュ": "ワイン", "ボルドー": "ワイン",
    "ドンペリ": "シャンパン", "アルマンド": "シャンパン",
    "プロ野球": "野球", "メジャーリーグ": "野球", "巨人": "野球", "阪神": "野球",
    "Jリーグ": "サッカー",
    "整う": "サウナ", "サ活": "サウナ",
    "車": "クルマ", "外車": "クルマ", "ポルシェ": "クルマ", "フェラーリ": "クルマ",
    "お寿司": "寿司", "鮨": "寿司", "おすし": "寿司",
    "鰻": "うなぎ", "犬": "イヌ", "猫": "ネコ",
}
# キュレート済みクエリ(精度の本丸)。汎用語は素の検索だと無関係ヒットが多いため
# クエリ側で絞る。ここに載る語は require_in_title を外す(クエリで十分絞れている)。
_KW_QUERY = {
    "ゴルフ": "ゴルフ (優勝 OR ツアー) when:2d",
    "相撲": "大相撲 when:2d",
    "野球": "プロ野球 when:1d",
    "サッカー": "Jリーグ OR サッカー日本代表 when:2d",
    "競馬": "競馬 (G1 OR 重賞) when:2d",
    "テニス": "テニス 優勝 when:2d",
    "ワイン": "ワイン (ヴィンテージ OR 品評会 OR 醸造) when:3d",
    "日本酒": "日本酒 (鑑評会 OR 新酒 OR 酒蔵) when:3d",
    "シャンパン": "シャンパン when:3d",
    "サウナ": "サウナ when:3d",
    "クルマ": "新型車 発表 when:3d",
    "寿司": "寿司 when:3d",
}


def _canon(tok: str) -> str:
    """興味トークン→正規形。空文字=採用しない。辞書照合は長さ判定より先(1文字趣味の救済)。"""
    t = _ud.normalize("NFKC", (tok or "").strip())
    # 数字を含むトークンは棄却(「ゴルフは月1」「週3」等の頻度メモの混入を防ぐ。
    # 分割regexは助詞を切らないため、数字入り=キーワードでなくメモと判定する)
    if not t or any(ch.isdigit() for ch in t):
        return ""
    for suf in _SUFFIX:
        if t.endswith(suf) and len(t) > len(suf):
            t = t[: -len(suf)]
            break
    if t in _STOP:
        return ""
    if t in _SYN:
        return _SYN[t]
    if 2 <= len(t) <= 12:
        return t
    return ""


def _ng_hit(kw: str, ng_text: str) -> bool:
    """NG話題との衝突判定。NG側トークンにも同じ正規化を通し、正規形同士でも照合する
    (「阪神の話NG」×「野球」の素通りを防ぐ。赤チームmustFix)。"""
    if not ng_text or not kw:
        return False
    for tok in _re.split(_SPLIT_RE, ng_text):
        tok = tok.strip()
        if not tok:
            continue
        if tok in kw or kw in tok:
            return True
        cn = _SYN.get(_ud.normalize("NFKC", tok)) or ""
        if cn and (cn == kw or cn in kw or kw in cn):
            return True
    # 埋め込み語照合: 「阪神の話」のように助詞つきで書かれたNGも辞書キーの包含で拾う
    ngn = _ud.normalize("NFKC", ng_text)
    for k, v in _SYN.items():
        if k in ngn and v and (v == kw or v in kw or kw in v):
            return True
    return False


def _interest_index() -> dict:
    """{正規形: {"who": {code: 元の記載語}, "field": 初出フィールド}}。
    素材は本人確認済みのカード属性(_IDX_FIELDS)のみ。NG話題該当者はwhoから個人単位で除外。"""
    from . import crm
    idx: dict = {}
    for ct in db.list_contacts():
        if (ct.get("kind") or "customer") != "customer" or ct.get("linked") == 0:
            continue
        try:
            a = crm.get_attrs(ct["code"]) or {}
        except Exception:
            continue
        ng = a.get("NG話題") or ""
        for field in _IDX_FIELDS:
            for tok in _re.split(_SPLIT_RE, (a.get(field) or "")):
                cn = _canon(tok)
                if not cn or (ng and _ng_hit(cn, ng)):
                    continue
                e = idx.setdefault(cn, {"who": {}, "field": field})
                e["who"].setdefault(ct["code"], tok.strip())
    return idx


def _solo_ok(code: str) -> bool:
    """該当1人だけの興味を検索してよいか(v183裁定: ランクA以上 or 1ヶ月以内の来店)。"""
    try:
        ct = db.get_contact(code) or {}
        if (ct.get("rank") or "B") in ("S", "A"):
            return True
        lim = time.time() - 31 * 86400
        with db.conn() as c:
            if c.execute("SELECT 1 FROM sitting_members m JOIN sittings s ON s.id=m.sitting_id "
                         "WHERE m.contact=? AND s.created_ts>=? LIMIT 1", (code, lim)).fetchone():
                return True
            return bool(c.execute("SELECT 1 FROM sittings WHERE main_contact=? AND created_ts>=? LIMIT 1",
                                  (code, lim)).fetchone())
    except Exception:
        return False


def who_codes(item: dict) -> list:
    """who列(v183=dict{code:元語} / 旧=list)の両対応パーサ。"""
    try:
        w = _json.loads(item.get("who") or "null")
    except Exception:
        return []
    if isinstance(w, dict):
        return list(w.keys())
    if isinstance(w, list):
        return [str(x) for x in w]
    return []


# ============ v183: kwクールダウン(news_metaのJSON1キー。新テーブルは作らない) ============
_KW_STATE_LOCK = threading.Lock()


def _kw_cooled(canon: str, now: float) -> bool:
    try:
        st = _json.loads(_meta_get("kw_state") or "{}")
        return float((st.get(canon) or {}).get("cool_until") or 0) > now
    except Exception:
        return False


def _kw_cool(canon: str, days: float):
    """used=3日 / dismiss=7日。読み-変更-書きはロックで守る(refreshスレッドと競合し得る)。"""
    if not canon:
        return
    with _KW_STATE_LOCK:
        try:
            st = _json.loads(_meta_get("kw_state") or "{}")
        except Exception:
            st = {}
        cur = float((st.get(canon) or {}).get("cool_until") or 0)
        st[canon] = {"cool_until": max(cur, time.time() + days * 86400)}
        _meta_set("kw_state", _json.dumps(st, ensure_ascii=False))


def _major(src_url: str) -> bool:
    from urllib.parse import urlparse
    try:
        host = (urlparse(src_url).netloc or "").lower()
        return any(host == d or host.endswith("." + d) for d in _MAJOR_SOURCES)
    except Exception:
        return False


def _fetch_rss(query: str, require_in_title: str = "") -> list:
    r = requests.get(
        "https://news.google.com/rss/search",
        params={"q": query, "hl": "ja", "gl": "JP", "ceid": "JP:ja"},
        headers={"User-Agent": "Mozilla/5.0 (chouba-neta)"},
        timeout=15)
    r.raise_for_status()
    items = parse_rss(r.content)
    # v175: ①大手出典のみ ②require_in_title=見出しに実際に含まれるものだけ
    out = [it for it in items if _major(it.get("src_url", ""))]
    if require_in_title:
        out = [it for it in out if require_in_title in it["title"]]
    return out


def _fetch_budgeted(budget: dict, phase: str, query: str, require_in_title: str = ""):
    """フェーズ別予約枠つきfetch。枠切れ=None(スキップ)。失敗も予算1として消費する
    (ネット断の日に全枠×15sタイムアウトでハングする穴を塞ぐ)。例外は呼び出し側でcatch。"""
    if budget.get(phase, 0) <= 0:
        return None
    budget[phase] -= 1
    if _SLEEP:
        time.sleep(1.0 + _random.random())   # レート礼儀(逐次+ジッター)
    return _fetch_rss(query, require_in_title=require_in_title)


def parse_rss(xml_bytes: bytes) -> list:
    root = ET.fromstring(xml_bytes)
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        src = it.find("source")
        src_url = (src.get("url") if src is not None else "") or ""
        ts = 0.0
        try:
            ts = parsedate_to_datetime(pub).timestamp()
        except Exception:
            pass
        if title:
            out.append({"title": title, "link": link, "ts": ts, "src_url": src_url})
    return out


# v169: 本人指摘「文章が硬くbot感がすごい」への対処。原因は3つあった。
# ①プロンプトが夜職文面のまま(v158の既知の残り)で一般モードでも「お客様」が出る
# ②「振り方にする」の指示だけで文体の手がかりが無く、AIが毎回「要約→持ち上げ→丁寧な質問」の
#   インタビュー型に落ちていた ③名前を知らないAIが「◯◯さん」プレースホルダを勝手に書く。
# 対処: MODE分岐+口語指定+型の禁止+宛名禁止+本人の文体実例(あれば)を注入して声を写す。
_OPENER_RULES = (
    "条件:\n"
    "- 1〜2文・LINEでそのまま送れる軽い口語。書き言葉・ニュースキャスター調・インタビュー調にしない\n"
    "- 「記事の要約→相手を持ち上げる→丁寧な質問」の型を使わない。質問で締めなくてよい"
    "(「〜だって！」「〜らしいよ」のような感想・共有だけで終えてよい)\n"
    "- 相手の名前・宛名・「◯◯さん」等の穴埋めを書かない(本文だけ。呼びかけ無しで自然に読める文)\n"
    "- 「お客様」「〜でらっしゃいます」等の接客敬語にしない\n"
    "- 見出し以上の事実を断定しない(「〜みたいですね」「〜らしい」程度)・営業くさくしない\n"
    "- スポーツの勝敗に肩入れしない(事実の共有と問いかけのみ)\n"
    "出力は本文のみ。"
)


def _style_hint() -> str:
    """本人の文体実例(あれば)。ネタの一言も本人の声で出す(v169・bot感対策の本丸)。"""
    try:
        prof = db.get_profile("_global") or {}
        samples = prof.get("samples") or []
        if not samples:
            return ""
        picks = samples[:30]
        picks = _random.sample(picks, min(5, len(picks)))
        return ("本人が実際に書いたLINE文の実例(この人の声・砕け方・句読点の癖を真似る):\n"
                + "\n".join(f"「{x}」" for x in picks) + "\n")
    except Exception:
        return ""


def _make_opener(contact_code: str, company: str, note: str, title: str) -> str:
    """見出し→今夜使える一言。営業くさくせず、事実を断定しない。"""
    if not config.ANTHROPIC_API_KEY:
        return ""
    if config.MODE == "general":   # v169: 一般モードは夜職語彙を使わない
        head = ("知り合いにLINEで送る、ニュースきっかけの軽い一言を1つ作る。\n"
                f"相手の会社・仕事: {company}" + (f"（{note}）" if note else "") + "\n")
    else:
        head = ("銀座のクラブのホステスが、顧客とのLINEや会話で使う「一言ネタ」を1つ作る。\n"
                f"顧客の会社: {company}" + (f"（{note}）" if note else "") + "\n")
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": config.ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": config.ANTHROPIC_MODEL, "max_tokens": 200,
                  "messages": [{"role": "user", "content":
                      head + _style_hint()
                      + f"今日のニュース見出し: {title}\n" + _OPENER_RULES}]},
            timeout=30)
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", [])).strip()[:200]
    except Exception:
        return ""


def _kw_opener(kw: str, title: str) -> str:
    """キーワードニュース→今夜の一言。見出しの範囲だけ・断定しない。"""
    if not config.ANTHROPIC_API_KEY:
        return ""
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": config.ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": config.ANTHROPIC_MODEL, "max_tokens": 200,
                  "messages": [{"role": "user", "content":
                      ((f"「{kw}」が好きな知り合いにLINEで送る、ニュースきっかけの軽い一言を1つ作る。\n"
                        if config.MODE == "general" else
                        f"銀座のホステスが「{kw}」好きのお客様とのLINEや会話で使う一言ネタを1つ作る。\n")
                       + _style_hint()
                       + f"今日のニュース見出し: {title}\n" + _OPENER_RULES)}]},
            timeout=30)
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", [])).strip()[:200]
    except Exception:
        return ""


_REFRESH_LOCK = threading.Lock()


def refresh(force: bool = False) -> dict:
    """朝バッチ本体。1日1回(JST日付で判定)。force=Trueで即時実行(呼び出し側で別スレッド推奨)。
    v72(9-6): 実行済みマーク(last_day)は処理成功後に書く。途中失敗した日は
    次回スケジューラ周回(30分後)で再実行される。再入はロックで防止。"""
    ensure()
    now = time.time()
    jst_day = time.strftime("%Y-%m-%d", time.gmtime(now + 9 * 3600))
    if not force and _meta_get("last_day") == jst_day:
        return {"ran": False, "added": 0}
    if not _REFRESH_LOCK.acquire(blocking=False):
        return {"ran": False, "added": 0, "busy": True}
    try:
        from . import crm
        budget = dict(_BUDGETS)
        deadline = now + _DEADLINE_S
        with db.conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT code, company, company_note FROM contacts "
                "WHERE linked!=0 AND IFNULL(kind,'customer')='customer' "
                "AND company IS NOT NULL AND company!=''")]
        added = 0
        failed = 0
        for ct in rows:
            if added >= _MAX_COMPANY or budget["company"] <= 0 or time.time() > deadline:
                break
            # v183: NG話題に社名か「仕事」を含む相手はスキップ(仕事の話がNGの人に会社ネタを勧めない)
            try:
                _ng = (crm.get_attrs(ct["code"]) or {}).get("NG話題") or ""
                if _ng and (ct["company"] in _ng or "仕事" in _ng):
                    continue
            except Exception:
                pass
            try:
                items = _fetch_budgeted(budget, "company", ct["company"],
                                        require_in_title=ct["company"])
            except Exception:
                failed += 1
                continue
            if items is None:
                break
            per = 0
            for it in items:
                if per >= _MAX_PER_CONTACT or added >= _MAX_COMPANY:
                    break
                if it["ts"] and (now - it["ts"]) > _FRESH_DAYS * 86400:
                    continue
                h = hashlib.sha1((ct["code"] + "|" + it["title"]).encode("utf-8")).hexdigest()
                with db.conn() as c:
                    dup = c.execute("SELECT 1 FROM news_items WHERE hash=?", (h,)).fetchone()
                if dup:
                    continue
                # v183: ネガ見出しは「見せるが送らせない」= caution=1・一言は生成しない
                caution = 1 if _neg(it["title"]) else 0
                opener = "" if caution else _make_opener(
                    ct["code"], ct["company"], ct.get("company_note") or "", it["title"])
                with db.conn() as c:
                    c.execute("INSERT OR IGNORE INTO news_items"
                              "(contact,company,title,link,opener,hash,created_ts,caution) "
                              "VALUES(?,?,?,?,?,?,?,?)",
                              (ct["code"], ct["company"], it["title"], it["link"],
                               opener, h, now, caution))
                per += 1
                added += 1
        # v125→v183: 興味キーワード(趣味・お酒・食べ物)。実在見出しのみ・1キーワード1件/日
        kw_added, kw_failed = 0, 0
        try:
            kw_added, kw_failed = _refresh_keywords(now, budget, deadline)
        except Exception as e:
            print(f"[news kw] {e}", flush=True)
            kw_failed += 1
        # 全社失敗(ネットワーク断など)の日はマークせず次周回で再挑戦。一部でも取れたら完了扱い
        # v191その2(#17): 会社ネタ0件の構成ではkwフェーズの成否も見る。従来は rows が空だと
        # ネット全滅日でも当日完了マークが立ち、その日のネタ供給ゼロが確定していた。
        # 「1件も取れず、かつ失敗があった日」はマークせず次周回で再挑戦。
        _company_ok = (not rows) or (failed < len(rows))
        _all_zero_with_fail = ((added + kw_added) == 0 and (failed + kw_failed) > 0)
        if _company_ok and not _all_zero_with_fail:
            _meta_set("last_day", jst_day)
        # v183: 観測点(実クエリ消費とかかった秒数)。v184の設計判断の根拠データ
        try:
            _meta_set("last_run_stats", _json.dumps(
                {"day": jst_day, "added": added, "kw_added": kw_added,
                 "budget_left": budget, "secs": round(time.time() - now, 1)}))
        except Exception:
            pass
        return {"ran": True, "added": added + kw_added, "companies": len(rows), "failed": failed}
    finally:
        _REFRESH_LOCK.release()


def _refresh_keywords(now: float, budget: dict = None, deadline: float = None) -> tuple:
    """v183: 興味インデックスから検索。群語(3人以上)は毎日、その他は日替わりローテで週内一巡
    (旧v125の「該当者数トップ5固定」は他の興味が永久に検索されない飢餓バグ)。
    v191その2(#17): 戻りは (added, failed)。失敗数を完了判定(last_day)に使う。"""
    budget = budget if budget is not None else dict(_BUDGETS)
    deadline = deadline or (now + _DEADLINE_S)
    idx = _interest_index()
    group = sorted([cn for cn, e in idx.items() if len(e["who"]) >= _GROUP_MIN_HOT])
    rest = []
    for cn in sorted(idx):
        if cn in group:
            continue
        who = idx[cn]["who"]
        if len(who) >= 2:
            rest.append(cn)
        elif len(who) == 1 and _solo_ok(next(iter(who))):
            # v183裁定: 1人だけの興味は「ランクA以上 or 1ヶ月以内来店」の相手なら検索する
            rest.append(cn)
    # 日替わりローテ(kw_cursor)。群語は毎日必ず・その他は数日で全興味を巡回
    picks = [(cn, "group") for cn in group[:_BUDGETS["group"]]]
    if rest:
        cur = int(_meta_get("kw_cursor") or 0) % len(rest)
        take = min(len(rest), _BUDGETS["cursor"])
        picks += [(rest[(cur + i) % len(rest)], "cursor") for i in range(take)]
        _meta_set("kw_cursor", str((cur + take) % len(rest)))
    added = 0
    openers = 0
    kw_failed = 0   # v191その2(#17): 取得失敗数(完了判定用)
    for cn, phase in picks:
        if time.time() > deadline:
            break
        if _kw_cooled(cn, now):
            continue
        # 前日分が未使用・未dismissのまま残っている興味は当日の追い足しをしない(同文連発とノイズ抑制)
        with db.conn() as c:
            if c.execute("SELECT 1 FROM news_items WHERE kw=? AND dismissed=0 AND IFNULL(used_ts,0)=0 "
                         "AND created_ts>=?", (cn, now - _EXPIRE_DAYS * 86400)).fetchone():
                continue
        e = idx[cn]
        if cn in _KW_QUERY:
            q, rit = _KW_QUERY[cn], ""
        else:
            ctx = {"好きなお酒": " 酒", "好きな食べ物": " グルメ"}.get(e["field"], "")
            q, rit = f"{cn}{ctx} when:2d", cn
        try:
            items = _fetch_budgeted(budget, phase, q, require_in_title=rit)
        except Exception:
            kw_failed += 1   # v191その2(#17)
            continue
        if items is None:
            continue
        for it in items[:4]:
            if it["ts"] and (now - it["ts"]) > _FRESH_DAYS * 86400:
                continue
            if _neg(it["title"]):
                continue   # 興味ネタにネガ見出しは無価値=破棄(会社ネタと違い見落とし防止の意味がない)
            h = hashlib.sha1(("kw:" + cn + "|" + it["title"]).encode("utf-8")).hexdigest()
            with db.conn() as c:
                if c.execute("SELECT 1 FROM news_items WHERE hash=?", (h,)).fetchone():
                    continue
                # 会社流と同一見出しの重複ガード(当日)
                if c.execute("SELECT 1 FROM news_items WHERE title=? AND created_ts>=?",
                             (it["title"], now - 86400)).fetchone():
                    continue
            opener = ""
            if openers < _MAX_KW:
                opener = _kw_opener(cn, it["title"])
                openers += 1
            who = dict(sorted(e["who"].items())[:8])
            with db.conn() as c:
                c.execute("INSERT OR IGNORE INTO news_items"
                          "(contact,company,title,link,opener,hash,created_ts,kw,who,tier) "
                          "VALUES('','',?,?,?,?,?,?,?,?)",
                          (it["title"], it["link"], opener, h, now, cn,
                           _json.dumps(who, ensure_ascii=False),
                           "group" if len(e["who"]) >= _GROUP_MIN_HOT else ""))
            added += 1
            break
    return added, kw_failed


def list_items(limit: int = 20) -> list:
    ensure()
    now = time.time()
    with db.conn() as c:
        # v175: v169以前に生成された「◯◯さん」プレースホルダ入りの一言が残り、
        # コピペ時に◯◯ごと送られる実害(本人指摘)。残存分を非表示化(新規生成はv169で禁止済み)
        c.execute("UPDATE news_items SET dismissed=1 WHERE dismissed=0 AND "
                  "(opener LIKE '%◯◯%' OR opener LIKE '%〇〇%' OR opener LIKE '%○○%')")
        # v183: 自動失効。古い「今日のネタ」を送らせない(紙面tier=7日保持はv184で追加)
        c.execute("UPDATE news_items SET dismissed=1 WHERE dismissed=0 AND "
                  "((tier='paper' AND created_ts<?) OR (IFNULL(tier,'')!='paper' AND created_ts<?))",
                  (now - 7 * 86400, now - _EXPIRE_DAYS * 86400))
        # v183: 30日超のdismissed行は掃除(テーブルの平衡)
        c.execute("DELETE FROM news_items WHERE dismissed=1 AND created_ts<?", (now - 30 * 86400,))
        return [dict(r) for r in c.execute(
            "SELECT * FROM news_items WHERE dismissed=0 ORDER BY created_ts DESC, id DESC LIMIT ?",
            (limit,))]


def mark_used(nid: int):
    """「📤これで話しかける」の成立を記録。同じ興味は3日休む(同文使い回し事故の抑制)。"""
    ensure()
    with db.conn() as c:
        c.execute("UPDATE news_items SET used_ts=? WHERE id=?", (time.time(), nid))
        r = c.execute("SELECT kw FROM news_items WHERE id=?", (nid,)).fetchone()
    if r and r["kw"]:
        _kw_cool(r["kw"], 3)


def dismiss(nid: int):
    ensure()
    with db.conn() as c:
        c.execute("UPDATE news_items SET dismissed=1 WHERE id=?", (nid,))
        r = c.execute("SELECT kw FROM news_items WHERE id=?", (nid,)).fetchone()
    # v183: ✕は「この話題は興味なし」の弱い学習=7日休む
    if r and r["kw"]:
        _kw_cool(r["kw"], 7)


def start_scheduler():
    """毎朝8時(JST)以降の最初のチェックで当日分を実行。30分間隔の軽いループ。"""
    def loop():
        while True:
            try:
                hour_jst = int(time.strftime("%H", time.gmtime(time.time() + 9 * 3600)))
                if hour_jst >= 8:
                    refresh(force=False)
            except Exception:
                pass
            time.sleep(1800)
    threading.Thread(target=loop, daemon=True).start()
