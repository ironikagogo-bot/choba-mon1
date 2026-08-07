"""🌐 顧客ネット補強 (v125)。

公開情報を検索し、確信が持てるものだけを○✕確認つきでカードに提案する。
設計原則(2026-08-07 本人合意):
- 既定は「会社・肩書き」単位の検索。個人名検索は相手ごとのopt-in(検索範囲=個人名OK)。
- 同姓同名対策: カードの既知情報(会社・仕事)と一致が取れた結果だけ提示(アンカー照合)。
- 出典URL・引用・確信度を必ず添える。LLMは「本文にある事実の抽出」のみ(知識で補完しない)。
- ○を押すまでカードに入らない(v118許容レベルと同じ安全装置)。
"""
import hashlib
import html
import json
import re
import threading
import time

import requests

from . import config, db

_UA = {"User-Agent": "Mozilla/5.0 (chouba-enrich)"}


def ensure():
    with db.conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS enrich_suggestions(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          contact TEXT NOT NULL,
          k TEXT NOT NULL, v TEXT NOT NULL,
          quote TEXT DEFAULT '', src_title TEXT DEFAULT '', src_url TEXT DEFAULT '',
          conf TEXT DEFAULT '中',
          status TEXT DEFAULT 'pending',   -- pending/accepted/rejected
          hash TEXT UNIQUE, created_ts REAL)""")
        c.execute("CREATE TABLE IF NOT EXISTS enrich_meta(k TEXT PRIMARY KEY, v TEXT)")


def _meta_get(k):
    with db.conn() as c:
        r = c.execute("SELECT v FROM enrich_meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else ""


def _meta_set(k, v):
    with db.conn() as c:
        c.execute("INSERT INTO enrich_meta(k,v) VALUES(?,?) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


def scope(contact: str) -> str:
    """検索範囲: 'company'(既定) / 'person'(個人名OK)。"""
    from . import crm
    v = (crm.get_attrs(contact) or {}).get("検索範囲") or ""
    return "person" if v == "個人名OK" else "company"


def set_scope(contact: str, person_ok: bool):
    from . import crm
    crm.add_def("検索範囲")
    crm.set_attr(contact, "検索範囲", "個人名OK" if person_ok else "会社のみ")


def _ddg_search(q: str, n: int = 4) -> list:
    """DuckDuckGo liteの検索結果(タイトル+URL)。APIキー不要。失敗=[]。"""
    try:
        r = requests.post("https://lite.duckduckgo.com/lite/",
                          data={"q": q}, headers=_UA, timeout=12)
        r.raise_for_status()
        out = []
        for m in re.finditer(r'<a[^>]+href="(https?://[^"]+)"[^>]*class="result-link"[^>]*>(.*?)</a>',
                             r.text, re.S):
            url = html.unescape(m.group(1))
            title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            if "duckduckgo.com" in url:
                continue
            out.append({"url": url, "title": html.unescape(title)[:100]})
            if len(out) >= n:
                break
        return out
    except Exception as e:
        print(f"[enrich search] {e}", flush=True)
        return []


def _fetch_text(url: str, cap: int = 12000) -> str:
    """ページ本文の素朴な抽出(タグ除去)。失敗=空。"""
    try:
        r = requests.get(url, headers=_UA, timeout=10)
        r.raise_for_status()
        t = r.text
        t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", t, flags=re.S | re.I)
        t = re.sub(r"<[^>]+>", " ", t)
        t = html.unescape(re.sub(r"\s+", " ", t)).strip()
        return t[:cap]
    except Exception:
        return ""


def _extract(contact, anchors, person_ok, pages) -> list:
    """LLMでページ群から事実を抽出。アンカー照合と引用を強制。"""
    if not config.ANTHROPIC_API_KEY or not pages:
        return []
    src = "\n\n".join(f"[ページ{i+1}] {p['title']}\nURL: {p['url']}\n本文: {p['text'][:6000]}"
                      for i, p in enumerate(pages))
    anchor_txt = "／".join(f"{k}:{v}" for k, v in anchors.items() if v)
    rules = (
        "あなたは事実確認に厳格な調査係。以下のWebページ本文だけを根拠に、"
        f"顧客「{contact}」に関する公開情報を抽出する。\n"
        f"既知のアンカー情報: {anchor_txt}\n"
        "厳守ルール:\n"
        "- 本文に書いてあることだけ。知識・推測で補わない\n"
        "- アンカー(会社名・仕事)と明確に一致する記述だけ採用。一致が確認できない人物情報は"
        "同姓同名の可能性があるため全て捨てる\n"
        + ("" if person_ok else "- 個人名に関する情報は出力しない(会社・組織の情報のみ)\n") +
        "- 各項目: k(会社の正式名称/役職・肩書き/事業内容/受賞・メディア掲載/所在地/その他公開情報のいずれか)、"
        "v(80字以内)、quote(本文からの引用40字以内)、page(ページ番号)、conf(高/中)\n"
        "- 確信が持てないものは出さない。0件なら空配列でよい\n"
        '出力はJSONのみ: {"facts":[{"k":"役職・肩書き","v":"...","quote":"...","page":1,"conf":"高"}]}'
    )
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
                          headers={"x-api-key": config.ANTHROPIC_API_KEY,
                                   "anthropic-version": "2023-06-01",
                                   "content-type": "application/json"},
                          json={"model": config.ANTHROPIC_MODEL, "max_tokens": 900,
                                "messages": [{"role": "user", "content": rules + "\n\n---\n" + src}]},
                          timeout=90)
        r.raise_for_status()
        out = "".join(b.get("text", "") for b in r.json().get("content", []))
        t = out.replace("```json", "").replace("```", "").strip()
        obj = json.loads(t[t.index("{"):t.rindex("}") + 1])
        res = []
        for f in (obj.get("facts") or [])[:6]:
            pg = int(f.get("page", 0) or 0) - 1
            page = pages[pg] if 0 <= pg < len(pages) else {}
            res.append({"k": str(f.get("k", ""))[:20], "v": str(f.get("v", ""))[:80],
                        "quote": str(f.get("quote", ""))[:60],
                        "src_title": page.get("title", "")[:80],
                        "src_url": page.get("url", "")[:300],
                        "conf": f.get("conf") if f.get("conf") in ("高", "中") else "中"})
        return [x for x in res if x["k"] and x["v"]]
    except Exception as e:
        print(f"[enrich extract] {e}", flush=True)
        return []


# v149: Anthropic APIのWeb検索ツール(検索・ページ取得をAPIサーバー側で実行)。
# DDGスクレイピングがRender等のデータセンターIPで弾かれ「ほとんど返ってこない」問題の根治。
# 失敗(ツール未対応・エラー)時はNoneを返し、呼び出し側が従来のDDG方式にフォールバックする
def _api_search(contact, anchors, person_ok):
    if not config.ANTHROPIC_API_KEY:
        return None
    anchor_txt = "／".join(f"{k}:{v}" for k, v in anchors.items() if v)
    rules = (
        "あなたは事実確認に厳格な調査係。Web検索を使って、"
        f"顧客「{contact}」に関する公開情報を調べる。\n"
        f"既知のアンカー情報: {anchor_txt}\n"
        "検索方針: まずアンカーの会社・組織を正式名称で検索し、公式サイト・登記情報・"
        "ニュース・業界メディアを確認する。表記ゆれ(略称・ローマ字)も試す。最大4回まで検索してよい。\n"
        "厳守ルール:\n"
        "- 検索で実際に見つかったページの記述だけ。自分の知識・推測で補わない\n"
        "- アンカー(会社名・仕事)と明確に一致する記述だけ採用。一致が確認できない人物情報は"
        "同姓同名の可能性があるため全て捨てる\n"
        + ("" if person_ok else "- 個人名に関する情報は出力しない(会社・組織の情報のみ)\n") +
        "- 各項目: k(会社の正式名称/役職・肩書き/事業内容/受賞・メディア掲載/所在地/その他公開情報のいずれか)、"
        "v(80字以内)、quote(ページからの引用40字以内)、url(出典URL)、title(出典ページ名)、conf(高/中)\n"
        "- 確信が持てないものは出さない。見つからなければ空配列でよい\n"
        '最終出力はJSONのみ: {"facts":[{"k":"役職・肩書き","v":"...","quote":"...",'
        '"url":"https://...","title":"...","conf":"高"}]}'
    )
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
                          headers={"x-api-key": config.ANTHROPIC_API_KEY,
                                   "anthropic-version": "2023-06-01",
                                   "content-type": "application/json"},
                          json={"model": config.ANTHROPIC_MODEL, "max_tokens": 1600,
                                "tools": [{"type": "web_search_20250305",
                                           "name": "web_search", "max_uses": 4}],
                                "messages": [{"role": "user", "content": rules}]},
                          timeout=240)
        if r.status_code != 200:
            print(f"[enrich api-search] HTTP {r.status_code}: {r.text[:200]}", flush=True)
            return None
        out = "".join(b.get("text", "") for b in r.json().get("content", [])
                      if b.get("type") == "text")
        t = out.replace("```json", "").replace("```", "").strip()
        if "{" not in t:
            print(f"[enrich api-search] no json: {t[:120]!r}", flush=True)
            return []
        obj = json.loads(t[t.index("{"):t.rindex("}") + 1])
        res = []
        for f in (obj.get("facts") or [])[:6]:
            res.append({"k": str(f.get("k", ""))[:20], "v": str(f.get("v", ""))[:80],
                        "quote": str(f.get("quote", ""))[:60],
                        "src_title": str(f.get("title", ""))[:80],
                        "src_url": str(f.get("url", ""))[:300],
                        "conf": f.get("conf") if f.get("conf") in ("高", "中") else "中"})
        # v150: 出典URL・引用が無い提案は捨てる(「出典必須」の安全設計をAPI経路でも強制)
        return [x for x in res if x["k"] and x["v"]
                and x["src_url"].startswith("http") and x["quote"]]
    except Exception as e:
        print(f"[enrich api-search] {e}", flush=True)
        return None


def run(contact: str) -> dict:
    """検索→抽出→提案として保存。戻り: {"found": n} or {"error": ...}"""
    ensure()
    from . import crm
    a = crm.get_attrs(contact) or {}
    d = db.get_contact(contact) or {}
    company = (a.get("仕事・会社") or d.get("company") or "").strip()
    job = (a.get("仕事・会社") or "").strip()
    person_ok = scope(contact) == "person"
    name = (a.get("本名") or "").strip() or (contact if person_ok else "")
    if not company and not (person_ok and name):
        return {"error": "手がかりがありません。カードの「仕事・会社」を入れるか、検索範囲を確認してください"}
    _meta_set(f"stat_{contact}", f"running:{int(time.time())}")
    anchors = {"会社": company, "仕事": job}
    if person_ok and name:
        anchors["名前"] = name
    # v149: まずAPIのWeb検索(強い)。ダメな時だけ旧DDG方式(弱い)へ
    facts = _api_search(contact, anchors, person_ok)
    if facts is None:
        print("[enrich] api検索が使えないためDDG方式にフォールバック", flush=True)
        queries = []
        if company:
            queries.append(company + " 会社 概要")
            queries.append(company)
        if person_ok and name:
            queries.append((name + " " + company).strip())
        pages, seen = [], set()
        for q in queries[:3]:
            for hit in _ddg_search(q, 3):
                if hit["url"] in seen:
                    continue
                seen.add(hit["url"])
                txt = _fetch_text(hit["url"])
                if len(txt) > 120:
                    pages.append({"url": hit["url"], "title": hit["title"], "text": txt})
                if len(pages) >= 4:
                    break
            if len(pages) >= 4:
                break
        if not pages:
            _meta_set(f"stat_{contact}", "error:検索結果に読めるページがありませんでした")
            return {"error": "検索結果に読めるページがありませんでした(後でもう一度)"}
        facts = _extract(contact, anchors, person_ok, pages)
    now = time.time()
    n = 0
    with db.conn() as c:
        for f in facts:
            h = hashlib.sha1(f"{contact}|{f['k']}|{f['v']}".encode()).hexdigest()
            try:
                c.execute("INSERT OR IGNORE INTO enrich_suggestions"
                          "(contact,k,v,quote,src_title,src_url,conf,status,hash,created_ts) "
                          "VALUES(?,?,?,?,?,?,?,'pending',?,?)",
                          (contact, f["k"], f["v"], f["quote"], f["src_title"],
                           f["src_url"], f["conf"], h, now))
                n += c.execute("SELECT changes()").fetchone()[0]
            except Exception as e:
                print(f"[enrich save] {e}", flush=True)
    _meta_set(f"stat_{contact}", f"done:{n}")   # v149: 0件でも「探した結果0件」と分かるように
    return {"found": n}


def run_async(contact: str):
    threading.Thread(target=lambda: run(contact), daemon=True).start()


def status(contact: str) -> str:
    return _meta_get(f"stat_{contact}") or ""


def suggestions(contact: str) -> list:
    ensure()
    with db.conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM enrich_suggestions WHERE contact=? AND status='pending' ORDER BY id",
            (contact,))]


def act(sid: int, accept: bool) -> dict | None:
    ensure()
    with db.conn() as c:
        r = c.execute("SELECT * FROM enrich_suggestions WHERE id=?", (sid,)).fetchone()
        if not r:
            return None
        row = dict(r)
        c.execute("UPDATE enrich_suggestions SET status=? WHERE id=?",
                  ("accepted" if accept else "rejected", sid))
    if accept:
        from . import crm
        k = "🌐" + row["k"]     # ネット由来と分かる接頭辞(手入力と区別)
        crm.add_def(k)
        crm.set_attr(row["contact"], k, row["v"] + f"（出典:{row['src_title'][:30]}）")
    return row
