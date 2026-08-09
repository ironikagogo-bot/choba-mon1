"""顧客の会社ニュース連携(📰ネタ帳)。

- カードの「会社名」が入っている顧客について、Google News RSS検索を毎朝1回クロール
- 新着ヒット時のみ、AIが「今夜使える一言」を生成(APIキー無しなら見出しのみ)
- 本文・顧客名は検索クエリに入れない(会社名のみ)。個人名の外部送信を避ける設計
"""
import hashlib
import json as _json
import re as _re
import threading
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import requests

from . import config, db

_MAX_PER_DAY = 10        # 1日の新規ネタ上限(コスト・ノイズ抑制)
_MAX_PER_CONTACT = 2     # 1顧客1日あたり
_FRESH_DAYS = 3          # 何日前までの記事を「新しい」とみなすか


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
        # v125: キーワード紐づけ(趣味・お酒など)。kw=キーワード / who=該当顧客名のJSON
        for ddl in ("kw TEXT DEFAULT ''", "who TEXT DEFAULT ''"):
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


def _fetch_rss(query: str) -> list[dict]:
    r = requests.get(
        "https://news.google.com/rss/search",
        params={"q": query, "hl": "ja", "gl": "JP", "ceid": "JP:ja"},
        headers={"User-Agent": "Mozilla/5.0 (chouba-neta)"},
        timeout=15)
    r.raise_for_status()
    return parse_rss(r.content)


def parse_rss(xml_bytes: bytes) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        ts = 0.0
        try:
            ts = parsedate_to_datetime(pub).timestamp()
        except Exception:
            pass
        if title:
            out.append({"title": title, "link": link, "ts": ts})
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
    "出力は本文のみ。"
)


def _style_hint() -> str:
    """本人の文体実例(あれば)。ネタの一言も本人の声で出す(v169・bot感対策の本丸)。"""
    try:
        prof = db.get_profile("_global") or {}
        samples = prof.get("samples") or []
        if not samples:
            return ""
        import random as _rnd
        picks = samples[:30]
        picks = _rnd.sample(picks, min(5, len(picks)))
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


_REFRESH_LOCK = threading.Lock()


def refresh(force: bool = False) -> dict:
    """朝バッチ本体。1日1回(JST日付で判定)。force=Trueで即時実行。
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
        with db.conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT code, company, company_note FROM contacts "
                "WHERE linked!=0 AND company IS NOT NULL AND company!=''")]
        added = 0
        failed = 0
        for ct in rows:
            if added >= _MAX_PER_DAY:
                break
            try:
                items = _fetch_rss(ct["company"])
            except Exception:
                failed += 1
                continue
            per = 0
            for it in items:
                if per >= _MAX_PER_CONTACT or added >= _MAX_PER_DAY:
                    break
                if it["ts"] and (now - it["ts"]) > _FRESH_DAYS * 86400:
                    continue
                h = hashlib.sha1((ct["code"] + "|" + it["title"]).encode("utf-8")).hexdigest()
                with db.conn() as c:
                    dup = c.execute("SELECT 1 FROM news_items WHERE hash=?", (h,)).fetchone()
                if dup:
                    continue
                opener = _make_opener(ct["code"], ct["company"], ct.get("company_note") or "", it["title"])
                with db.conn() as c:
                    c.execute("INSERT OR IGNORE INTO news_items(contact,company,title,link,opener,hash,created_ts) "
                              "VALUES(?,?,?,?,?,?,?)",
                              (ct["code"], ct["company"], it["title"], it["link"], opener, h, now))
                per += 1
                added += 1
        # v125: キーワード紐づけ(趣味・好きなお酒など)。実在見出しのみ・1キーワード1件/日
        kw_added = 0
        try:
            kw_added = _refresh_keywords(now)
        except Exception as e:
            print(f"[news kw] {e}", flush=True)
        # 全社失敗(ネットワーク断など)の日はマークせず次周回で再挑戦。一部でも取れたら完了扱い
        if not rows or failed < len(rows):
            _meta_set("last_day", jst_day)
        return {"ran": True, "added": added + kw_added, "companies": len(rows), "failed": failed}
    finally:
        _REFRESH_LOCK.release()


def _refresh_keywords(now: float) -> int:
    """v125: 顧客カードの趣味・好きなお酒からキーワードを集め、該当ニュースを紐づける。
    該当顧客が多いキーワード優先・最大5キーワード/日・1キーワード1件。"""
    from . import crm
    kw_map: dict = {}
    for ct in db.list_contacts():
        if (ct.get("kind") or "customer") != "customer" or ct.get("linked") == 0:
            continue
        a = crm.get_attrs(ct["code"]) or {}
        for field in ("趣味・関心", "好きなお酒"):
            for tok in _re.split(r"[、,・/／()（）\s]+", (a.get(field) or "")):
                tok = tok.strip()
                if 2 <= len(tok) <= 12 and not tok.isdigit():
                    kw_map.setdefault(tok, set()).add(ct["code"])
    added = 0
    for kw, whos in sorted(kw_map.items(), key=lambda x: -len(x[1]))[:5]:
        try:
            items = _fetch_rss(kw)
        except Exception:
            continue
        for it in items[:4]:
            if it["ts"] and (now - it["ts"]) > _FRESH_DAYS * 86400:
                continue
            h = hashlib.sha1(("kw:" + kw + "|" + it["title"]).encode("utf-8")).hexdigest()
            with db.conn() as c:
                if c.execute("SELECT 1 FROM news_items WHERE hash=?", (h,)).fetchone():
                    continue
            opener = _kw_opener(kw, it["title"])
            with db.conn() as c:
                c.execute("INSERT OR IGNORE INTO news_items"
                          "(contact,company,title,link,opener,hash,created_ts,kw,who) "
                          "VALUES('','',?,?,?,?,?,?,?)",
                          (it["title"], it["link"], opener, h, now, kw,
                           _json.dumps(sorted(whos)[:6], ensure_ascii=False)))
            added += 1
            break
    return added


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


def list_items(limit: int = 20) -> list[dict]:
    ensure()
    with db.conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM news_items WHERE dismissed=0 ORDER BY created_ts DESC, id DESC LIMIT ?",
            (limit,))]


def dismiss(nid: int):
    ensure()
    with db.conn() as c:
        c.execute("UPDATE news_items SET dismissed=1 WHERE id=?", (nid,))


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
