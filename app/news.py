"""顧客の会社ニュース連携(📰ネタ帳)。

- カードの「会社名」が入っている顧客について、Google News RSS検索を毎朝1回クロール
- 新着ヒット時のみ、AIが「今夜使える一言」を生成(APIキー無しなら見出しのみ)
- 本文・顧客名は検索クエリに入れない(会社名のみ)。個人名の外部送信を避ける設計
"""
import hashlib
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


def _make_opener(contact_code: str, company: str, note: str, title: str) -> str:
    """見出し→今夜使える一言。営業くさくせず、事実を断定しない。"""
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
                      "銀座のクラブのホステスが、顧客との会話やLINEで使う「一言ネタ」を1つ作る。\n"
                      f"顧客の会社: {company}" + (f"（{note}）" if note else "") + "\n"
                      f"今日のニュース見出し: {title}\n"
                      "条件: 1〜2文・営業くさくしない・見出し以上の事実を断定しない(「〜みたいですね」程度)・"
                      "相手が気持ちよく話し始められる振り方にする。出力は一言ネタの本文のみ。"}]},
            timeout=30)
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", [])).strip()[:200]
    except Exception:
        return ""


def refresh(force: bool = False) -> dict:
    """朝バッチ本体。1日1回(JST日付で判定)。force=Trueで即時実行。"""
    ensure()
    now = time.time()
    jst_day = time.strftime("%Y-%m-%d", time.gmtime(now + 9 * 3600))
    if not force and _meta_get("last_day") == jst_day:
        return {"ran": False, "added": 0}
    _meta_set("last_day", jst_day)
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT code, company, company_note FROM contacts "
            "WHERE linked!=0 AND company IS NOT NULL AND company!=''")]
    added = 0
    for ct in rows:
        if added >= _MAX_PER_DAY:
            break
        try:
            items = _fetch_rss(ct["company"])
        except Exception:
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
    return {"ran": True, "added": added, "companies": len(rows)}


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
