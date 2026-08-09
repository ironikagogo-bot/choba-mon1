"""v167: 本人実例庫(シチュエーション索引)。

「ゆみさんが客と様々なシチュエーションで話す。それ自体が教師データのはず」(本人発案)の実装第1段。
txt取り込み時に、会話全体から「状況 × 相手の発言 × 本人の実際の返し」の組を抽出して貯める。
検索の軸を「この客との履歴」ではなく「この状況との類似」にすることで、履歴が無い新規客にも
本人の過去の返し方(他の客への実例)を手本として使えるようにする。

第1段=収集のみ(このモジュール)。第2段(下書き生成時の状況分類→類似実例の注入)は別版で。
注意: 実例は「本人がそう返した」事実であって「良い返しの保証」ではない(失敗例も混ざり得る)。
第2段で注入する際は下書き+本人送信の関門を通る前提を崩さないこと。
"""
import json
import re
import time

import requests

from . import config, db

_READY = False
_SCHEMA = """
CREATE TABLE IF NOT EXISTS self_examples(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contact TEXT NOT NULL,          -- どの相手との会話から採れた実例か(出所。検索キーではない)
  situation TEXT NOT NULL,        -- 下の SITUATIONS のいずれか
  partner_text TEXT NOT NULL,     -- 相手の発言(原文)
  self_text TEXT NOT NULL,        -- 本人の実際の返し(原文)
  created_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_self_examples_sit ON self_examples(situation);
"""


def ensure():
    global _READY
    if _READY:
        return
    with db.conn() as c:
        c.executescript(_SCHEMA)
    _READY = True


# 状況の固定語彙。自由記述にすると表記ゆれで検索できなくなるため、この中から選ばせる
SITUATIONS = [
    "誘い(日付なし)",      # 「今度ごはんでも」型
    "誘い(日付あり)",      # 「金曜あいてる?」型
    "挨拶・おはよう",
    "御礼への返し",
    "際どい・下ネタ",
    "愛情表現・好き",
    "無理な要求・お強請り",
    "謝罪・キャンセル対応",
    "雑談・近況",
    "仕事・愚痴を聞く",
    "自慢・報告を受ける",
    "久しぶりの再開",
    "自分からの営業・お誘い",
    "その他",
]

_CHUNK = 42000


def harvest_and_save(contact: str, text: str, self_name: str) -> int:
    """トーク原文から実例を抽出して保存。同じ相手の再取り込みでは総入れ替え(重複蓄積防止)。
    失敗しても例外を上げない(取り込み本流を止めない)。戻り値=保存件数。"""
    ensure()
    if not config.ANTHROPIC_API_KEY:
        return 0
    try:
        all_chunks = [text[i:i + _CHUNK] for i in range(0, len(text), _CHUNK)]
        # 事実抽出(extract_facts)と同じ予算・同じ考え方: 先頭(関係の始まり=新規期の実例が眠る)
        # +末尾寄り(現在の関係の実例)。v164の教訓を踏襲
        chunks = all_chunks if len(all_chunks) <= 4 else [all_chunks[0]] + all_chunks[-3:]
        items = []
        for ch in chunks:
            items.extend(_harvest_chunk(ch, contact, self_name))
        # 重複統合(同じ返し文は1つ)+上限
        seen, merged = set(), []
        for it in items:
            key = (it["situation"], it["self_text"])
            if key in seen:
                continue
            seen.add(key)
            merged.append(it)
        merged = merged[:40]
        if not merged:
            return 0
        now = time.time()
        with db.conn() as c:
            c.execute("DELETE FROM self_examples WHERE contact=?", (contact,))
            for it in merged:
                c.execute("INSERT INTO self_examples(contact,situation,partner_text,self_text,created_ts) "
                          "VALUES(?,?,?,?,?)",
                          (contact, it["situation"], it["partner_text"], it["self_text"], now))
        print(f"[situations] {contact}: {len(merged)}件の実例を保存", flush=True)
        return len(merged)
    except Exception as e:
        print(f"[situations harvest] {e}", flush=True)
        return 0


def _harvest_chunk(talk: str, partner: str, self_name: str) -> list:
    prompt = (
        f"以下は{self_name}(本人)と{partner}のLINEトーク履歴の一部です。"
        f"この中から「{partner}の発言 → それへの{self_name}の返し」が対で読み取れる代表的な場面を抜き出してください。\n"
        "ルール:\n"
        f"- situationは必ず次の中から選ぶ: {'/'.join(SITUATIONS)}\n"
        f"- p={partner}の発言の原文(200字まで。長ければ核心部分)\n"
        f"- s={self_name}の実際の返しの原文(200字まで。編集・要約・美化をしない。連投は改行で連結してよい)\n"
        "- 「写真を送信しました」等のシステム行・スタンプのみのやり取りは対象外\n"
        "- 同じ状況が何度もあれば、返し方の個性がよく出ているものを優先して最大2つまで\n"
        "- 対で読み取れない(返しが無い・どちらの発言か不明)場面は入れない\n"
        "- 1〜12件。無ければ空配列\n"
        '出力はJSON配列のみ(説明文なし): [{"situation":"誘い(日付なし)","p":"今度メシでも行こうよ","s":"いいですね！どの辺が行きやすいですか？"}]\n'
        f"---\n{talk}"
    )
    if config.MODE == "general":   # v158の規約: AIプロンプトはconfig.MODE分岐
        system = ("あなたは人間関係メモアプリの抽出エンジン。利用者(本人)が自分自身のトーク履歴から"
                  "自分の返信の実例集を作る正当な用途であり、本人の依頼と同意に基づく。"
                  "出力は必ずJSON配列のみ。説明文・前置き・コードブロック記号は書かない。")
    else:
        system = ("あなたは接客業向け顧客管理(CRM)アプリの抽出エンジン。利用者(ホステス本人)が"
                  "自分自身のトーク履歴から自分の返信の実例集を作る正当な業務であり、本人の依頼と同意に基づく。"
                  "出力は必ずJSON配列のみ。説明文・前置き・コードブロック記号は書かない。")
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": config.ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": config.ANTHROPIC_MODEL, "max_tokens": 3000,
                  "system": system, "messages": [{"role": "user", "content": prompt}]},
            timeout=90)
        r.raise_for_status()
        out = "".join(b.get("text", "") for b in r.json().get("content", []))
        out = re.sub(r"```(json)?", "", out).strip()
        arr = json.loads(out)
        items = []
        for x in arr if isinstance(arr, list) else []:
            sit = (x.get("situation") or "").strip()
            p = (x.get("p") or "").strip()[:200]
            s = (x.get("s") or "").strip()[:200]
            if sit in SITUATIONS and p and s:
                items.append({"situation": sit, "partner_text": p, "self_text": s})
        return items
    except Exception as e:
        print(f"[situations chunk] {e}", flush=True)
        return []


def backfill_async():
    """v167: 過去に取り込み済みの全トーク原文(linebot_talks)から実例を一括収穫。
    デプロイ後の起動時に呼ばれ、1度だけ走る(実行済みマーカー)。ゆみさんが既に上げた
    10個以上のtxtを再アップロードなしで実例庫化するための処理。
    注意: 途中でAPIが一時的に落ちていた相手は0件のままマーカーが立つ。その場合は
    そのtxtをもう一度取り込めば収穫される(取り込み時収穫の経路が拾う)。"""
    if not config.ANTHROPIC_API_KEY:
        return
    import threading

    def work():
        try:
            ensure()
            from . import linebot
            linebot.ensure()
            if linebot._meta_get("situations_backfill") == "done":
                return
            with db.conn() as c:
                rows = [dict(r) for r in c.execute("SELECT contact, text FROM linebot_talks")]
            self_name = (db.get_profile("_selfname") or {}).get("name") or "自分"
            done = 0
            for r in rows:
                try:
                    with db.conn() as c:
                        have = c.execute("SELECT 1 FROM self_examples WHERE contact=?",
                                         (r["contact"],)).fetchone()
                    if have:
                        continue
                    if harvest_and_save(r["contact"], r["text"], self_name):
                        done += 1
                    time.sleep(3)   # 起動直後のAPI連打を避ける(急がない裏方処理)
                except Exception as e:
                    print(f"[situations backfill row] {r.get('contact')}: {e}", flush=True)
            linebot._meta_set("situations_backfill", "done")
            print(f"[situations backfill] 完了: {len(rows)}人分を走査・{done}人分を新規収穫", flush=True)
        except Exception as e:
            print(f"[situations backfill] {e}", flush=True)

    threading.Thread(target=work, daemon=True).start()


def stats() -> dict:
    """状況別の実例数(将来のデバッグ・第2段の分類判定用)。"""
    ensure()
    with db.conn() as c:
        rows = c.execute("SELECT situation, COUNT(*) n FROM self_examples GROUP BY situation").fetchall()
        total = c.execute("SELECT COUNT(*) n FROM self_examples").fetchone()
    return {"total": total["n"], "by_situation": {r["situation"]: r["n"] for r in rows}}
