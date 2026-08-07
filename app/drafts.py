"""返信下書き生成。
ANTHROPIC_API_KEY があれば Claude API、なければテンプレートにフォールバック
(APIキーなしでもパイロットの動線検証ができるように)。
"""
import json
import re
import threading

import requests

from . import config, db
from .style_profile import profile_prompt_block, contact_profile_block

SYSTEM = """あなたは本人の「返信の下書き係」。本人になりきって、届いたLINEへの返信案を作る。相手は基本、気心の知れた友人・知り合い。既定は"友達へのLINE"の温度で書く。

【既定トーン＝かなりくだけた友人感】
- タメ口ベース。短く、崩して、句読点は少なめ。きれいな作文にしない。
- 水商売・接客・営業の定型句を使わない。禁止例:「お待ちしております」「ご用意しておきます」「楽しみにしております」「〜させていただきます」の連発、「嬉しいです」の多用、過剰な持ち上げ、堅い時候の挨拶。
- 友達に送るくらいの軽さ。「〜だね」「〜しよ」「りょ」「おっけー」みたいな自然な口語。絵文字は本人の実例に出る範囲で控えめに(数合わせで盛らない)。
- 一言で済むならそれでいい。長くしない。

【最優先ルール(既定トーンより上)】
- 「本人が実際に書いた文」の実例があれば、既定トーンより実例を優先して声を写す。数値プロファイルは"参考"で、当てにいく目標ではない(数字合わせはわざとらしくなる)。
- 【距離感】の指定があれば絶対に従う。とくに"敬語厳守"は安全のため何より優先し、タメ口・友達口調の案を一切出さない。

わざとらしさを消す:
- 実例より丁寧・完全に書かない。！や「笑」を数合わせで盛らない。決まり文句を実例に無いのに足さない。
- 気の利いた一言を無理に作らない。滑るくらいなら短く自然な受けにする。
- 方言・脱字風の崩し(「だて」「〜やで」等)を発明しない。実例に繰り返し(2回以上)出てくる場合だけ真似てよい。実例1件だけの誤字・特殊な語尾は偶発とみなして標準的な口語にする。
- 事実や約束を捏造しない。日時・場所・金額は本人が確定できない限り断定せず、ふわっと。
- 相手の言葉を1つ拾うと自然。

- 相手の「地雷・注意(ネガ)」は"避ける配慮"にのみ使い、その語句や否定的評価(例:ケチ・恐妻家)を本文に絶対書かない。
- 相手の「喜ぶ・強み(ポジ)」は事実がある範囲で自然に活かす(不自然に持ち上げない)。

出力はJSONのみ: {"plan":"この相手・この文への返し方の作戦を1〜2文(相手には見せない)","drafts":[{"tone":"...","text":"..."},{"tone":"...","text":"..."}]}
必ずplanを先に書き、作戦を決めてから本文を書く。2案は毛色を少し変える(例: 片方は最小限の一言、もう片方は少しだけ足す)。toneは短い日本語ラベル。前置き・説明・コードブロック記号は禁止。"""

# いなしモードの発火条件(相手のネガ欄 or 受信本文)
_KAWASU_NEG = re.compile(r"(下ネタ|エロ|セクハラ|際どい|卑猥)")
_KAWASU_MSG = re.compile(r"(下着|ハイレグ|Tバック|エッチ|えっち|裸|セクシー|おっぱい|パンチラ|どぎつい|水着姿|脱いで)")

REGISTER_RULE = {
    "keigo_only": "【距離感=敬語厳守・最優先】この相手には必ず敬語。タメ口・砕けた表現・友達口調は一切禁止。2案とも敬語で(堅い相手への事故防止)。",
    "keigo": "【距離感=敬語】この相手は敬語基調で丁寧に。友達口調にはしない。",
    "mix": "【距離感=混在】敬語とタメ口が混ざる間柄。2案のうち片方を少し砕けた案に。",
    "casual": "【距離感=タメ口】親しい相手。友達口調で崩してよい。",
}


def _template_drafts(contact: dict, text: str, reason: str) -> list[dict]:
    """オフライン用の素朴なテンプレート(APIキー無し=デモの動線検証用)。
    対応モード(ガチ恋/いなし)と敬語設定を必ず反映する。定型文がモードを無視すると
    デモで「ガチ恋客に'また会お！'」のような信頼を壊す返答が出るため(2026-07-29実バグ)。"""
    # ガチ恋・線引き: 燃料を足さない・突き放さない・話題転換 or 店へ
    if int(contact.get("flag_koi") or 0) == 1:
        return [
            {"tone": "線引き・流す", "text": "ふふ、ありがと😊 それより最近ちゃんと寝てる？無理しないでね"},
            {"tone": "線引き・店へ", "text": "考えすぎ🤣 お店でゆっくり話そ、待ってるね！"},
            {"tone": "長文・ていねい", "text": "いつも丁寧に送ってくれてありがと😊 お仕事のほう、前に話してた件は落ち着いた？"
                     "季節の変わり目だから体調だけは気をつけてね。私は相変わらずお店でばたばたしてるけど元気にやってます。"
                     "ちゃんとご飯食べて、あったかくして寝ること！また来てくれた時にゆっくり聞かせてね"},
        ]
    # いなし: 乗らない・拒まない・軽く外す
    if int(contact.get("flag_ero") or 0) == 1 or _KAWASU_MSG.search(text or ""):
        return [
            {"tone": "いなし・軽く", "text": "なにそれ🤣 その話は聞かなかったことにするね！最近どうしてるの？"},
            {"tone": "いなし・自虐", "text": "見ても後悔するだけだって🤣 それより元気にしてた？"},
        ]
    # 敬語系の相手には友達口調のテンプレを出さない(敬語厳守事故の防止)
    if (contact.get("register") or "") in ("keigo_only", "keigo"):
        return [
            {"tone": "丁寧・軽く", "text": "ご連絡ありがとうございます！その件、ぜひ今度ゆっくりお聞かせください"},
            {"tone": "丁寧・最小", "text": "ありがとうございます。またお会いできるのを楽しみにしています！"},
        ]
    if "来店" in reason or "席" in reason:
        return [
            {"tone": "軽く", "text": "ほんと！？来てくれるの嬉しい〜、席とっとくね。何人くらい？"},
            {"tone": "最小", "text": "おっけー、空けとく！何時ごろ来れそう？"},
        ]
    if "日程" in reason or "同伴" in reason:
        return [
            {"tone": "軽く", "text": "いいね、行こ〜。今週なら火曜か木曜がわたし動きやすいけどどう？"},
            {"tone": "最小", "text": "りょ！日にち決まったら教えて、空けとくね"},
        ]
    return [
        {"tone": "軽く", "text": "おつかれ〜！その話、今度ゆっくり聞かせてよ"},
        {"tone": "共感", "text": "そうなんだ〜。連絡くれてありがと、また近いうち会お！"},
    ]


_FUEL_RE = re.compile(r"(会いたい|寂しい|さみしい|大好き|愛して|楽しみにして|二人で|ふたりで|❤️|💕|💗|❣️|😍|🥰|💘|💝)")
_STIFF_RE = re.compile(r"(えー、|ございます|でしたか？|いたします|くださいませ)")

def _needs_review(incoming: str, drafts: list) -> bool:
    """AI検品を呼ぶ前のローカル即時チェック。明白な問題が無ければ検品を省略=速度優先。"""
    for d in drafts:
        t = (d.get("text") or "")
        if _FUEL_RE.search(t) or _STIFF_RE.search(t):
            return True
        # 復唱チェック: 相手の文の6文字以上の断片をそのまま含む
        inc = (incoming or "").strip()
        for i in range(0, max(0, len(inc) - 6)):
            frag = inc[i:i + 7]
            if len(frag) >= 7 and frag in t:
                return True
    return False


def _review_pass(contact: dict, incoming: str, drafts: list, base_prompt: str) -> list | None:
    """ガチ恋/いなし用の検品: 生成した案を自己審査し、違反やぎこちなさがあれば書き直して返す。"""
    review = (
        "あなたは下書きの検品係。以下の返信案を審査し、問題があれば書き直した最終版を返す。\n"
        "審査基準(1つでも該当したら書き直し):\n"
        "1. 恋愛の燃料(会いたい/寂しい/大好き/ハート絵文字/特別扱い/将来の匂わせ/二人きりの約束)\n"
        "2. 相手の愛情表現や要求の復唱(「考えてくれてたんですね」等=増幅するのでNG)\n"
        "3. 接客敬語・不自然な相槌(「えー、」等)・説明口調のぎこちなさ。友達のLINEらしい自然な軽さか\n"
        "4. 質問への不用意な直答(好き?への回答/写真を送る送らないの確約)\n"
        "5. 冷たさ・突き放し・説教\n"
        f"相手からのメッセージ:「{incoming}」\n"
        f"返信案: {json.dumps(drafts, ensure_ascii=False)}\n"
        '出力はJSONのみ: {"drafts":[{"tone":"...","text":"..."},{"tone":"...","text":"..."}]}(問題なければ原文のまま返す)'
    )
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": config.ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": getattr(config, "REVIEW_MODEL", config.ANTHROPIC_MODEL),
            "max_tokens": 500,
            "system": SYSTEM,
            "messages": [{"role": "user", "content": base_prompt},
                          {"role": "assistant", "content": json.dumps({"drafts": drafts}, ensure_ascii=False)},
                          {"role": "user", "content": review}],
        },
        timeout=30,
    )
    r.raise_for_status()
    out = "".join(b.get("text", "") for b in r.json().get("content", []))
    out = re.sub(r"```(json)?", "", out).strip()
    fixed = json.loads(out).get("drafts", [])[:3]
    return fixed or None


# v72(9-5): 二重生成ガード。事前生成スレッド(受信時)と本処理(通知タップ時)が同じmidを
# 同時に生成してAPI呼び出しが2倍になる問題の修正。既存下書きがあれば再生成せず返し、
# 生成中なら完了を待って結果を共有する。
_GEN_LOCK = threading.Lock()
_GEN_INFLIGHT: dict = {}


def _saved_drafts(message_id: int):
    rows = db.get_drafts(message_id) or []
    return [{"text": d["text"], "tone": d["tone"]} for d in rows]


def generate(message_id: int) -> list[dict]:
    existing = _saved_drafts(message_id)
    if existing:
        return existing
    with _GEN_LOCK:
        ev = _GEN_INFLIGHT.get(message_id)
        owner = ev is None
        if owner:
            ev = threading.Event()
            _GEN_INFLIGHT[message_id] = ev
    if not owner:
        ev.wait(timeout=45)
        return _saved_drafts(message_id)
    try:
        return _generate_inner(message_id)
    finally:
        with _GEN_LOCK:
            _GEN_INFLIGHT.pop(message_id, None)
        ev.set()


def _generate_inner(message_id: int) -> list[dict]:
    msg = db.get_message(message_id)
    if not msg:
        return []
    contact = db.get_contact(msg["contact"]) or {"code": msg["contact"], "rank": "B"}

    # ラリー(連投)は相手からの一連の受信をまとめて1つの返信にする(最新1通だけに返さない)
    thread_text = msg["text"]
    if msg.get("category") == "rally":
        try:
            sibs = [x for x in db.open_messages()
                    if x["contact"] == msg["contact"] and x["category"] == "rally"]
            if len(sibs) > 1:
                sibs.sort(key=lambda x: x["ts"])
                thread_text = "\n".join(x["text"] for x in sibs)
        except Exception:
            pass

    if not config.ANTHROPIC_API_KEY:
        drafts = _template_drafts(contact, msg["text"], msg["reason"])
        db.save_drafts(message_id, drafts)
        return drafts

    profile = db.get_profile("_global") or {}
    per_contact = db.get_profile(contact["code"]) or {}
    cp_block = contact_profile_block(per_contact)
    user_prompt = (
        f"{profile_prompt_block(profile)}\n\n"
        f"{cp_block}\n\n" if cp_block else f"{profile_prompt_block(profile)}\n\n"
    )
    _pos = (contact.get("note_pos") or "").strip()
    _neg = (contact.get("note_neg") or "").strip()
    if _pos:
        user_prompt += f"\nこの相手が喜ぶ・強み(自然に活かす): {_pos}"
    if _neg:
        user_prompt += f"\nこの相手の地雷・注意(触れず避ける。本文にこの語句を絶対書かない): {_neg}"
    # v101: 顧客カード(整備/LIFFで貯めた属性)を生成に実接続
    try:
        from . import crm as _crm
        _cb = _crm.card_prompt_block(contact["code"])
    except Exception:
        _cb = ""
    if _cb:
        user_prompt += ("\n\n" + _cb +
                        "\n↑この相手の顧客カード。返信の文脈に合う事実が1つでもあれば自然に織り込む"
                        "(呼び名があれば呼びかけに使う)。文脈に合わない事実の無理な挿入はしない。")
    _reg_val = (contact.get("register") or "").strip()
    _reg = REGISTER_RULE.get(_reg_val)
    if _reg:
        user_prompt += "\n" + _reg
    elif not _reg_val:
        user_prompt += ("\n【距離感=自動】口調は『この相手に本人が実際に送った文』の実例に合わせる"
                        "(実例が敬語ならこちらも敬語を崩さない・タメ口ならタメ口)。"
                        "この相手への実例が無い場合は、砕けすぎない軽い丁寧語で様子を見る(初手から馴れ馴れしくしない)。")
    # v118: 第2層(関係性=ログ集計の事実)と第3層(許容レベル=本人確定済みのみ)を制約として注入
    try:
        from . import linebot as _lb
        _rel = _lb.relationship_prompt_block(contact["code"])
        if _rel:
            user_prompt += "\n\n" + _rel
        _tol = _lb.tolerance_prompt_block(contact["code"])
        if _tol:
            user_prompt += "\n\n" + _tol
    except Exception:
        pass

    # いなしモード: 下ネタ癖のある相手、または性的な冗談・要求を含む受信は「乗らず・拒まず」で受け流す
    # v30: 注入は「いなしON(flag_ero==1)」の相手のみ。検知→確認は受信箱の下ネタアラームが担う。
    # ノリOK(flag_ero==2)=プロレス型の相手には絶対に注入しない(実例学習がノリを再現する)。
    mode_prompt = ""
    if int(contact.get("flag_ero") or 0) == 1:
        mode_prompt += ("\n【いなしモード】相手の性的な冗談・写真や下着などの要求には絶対に乗らない"
                        "(送るとも送らないとも約束せず、具体的な返答をしない)。ただし拒否語(無理・やめて・嫌)も使わず、"
                        "(1)状況のせいにして外す(例:ホテルのプールだから追い出される) "
                        "(2)自虐や笑いで幻想を潰す(例:毛がめっちゃ出るよ？) "
                        "(3)軽く相手を立てて話題を閉じる・変える、のいずれかで受け流す。"
                        "要求の水位を上げさせない=次につながる含みを残さない。冗談の温度は保ち、説教・沈黙・急な敬語化はしない。")
    if int(contact.get("flag_koi") or 0):
        mode_prompt += ("\n【ガチ恋・線引きモード】相手は本気の恋愛感情を持っている(または傾向がある)。"
                        "目的=気持ちよく受け流して距離を一定に保つ。守ること:"
                        "\n- 恋愛の燃料を足さない: 「会いたい」「寂しい」「大好き」・ハート系絵文字・特別扱い・将来の匂わせ・二人きりの約束は禁止。"
                        "\n- 「好き？」等の直球には答えない。かつ、相手の愛情表現を復唱して受けない"
                        "(「考えててくれたんですね」のような繰り返しは気持ちを増幅させるのでNG)。"
                        "\n- 受け方は『軽い感謝 or 軽いツッコミ』を一言だけ→すぐ日常の話題へ。不自然な相槌(「えー、」等)や接客敬語にしない。"
                        "\n- 良い見本(トーンの参考。実際の口調は実例・距離感に従う):"
                        "「ありがと😊 そういえば今日お店でおもしろいことあってさ」"
                        "「もう、口うまいんだから🤣 ちゃんとご飯食べてる？」"
                        "「ふふ、ありがと。それより週末なにするの？」"
                        "\n- 会う話が出たら店に寄せる(「お店で待ってるね」)。"
                        "\n- 短い2案は従来どおり軽く短く。突き放し・説教・急な敬語化はしない。"
                        "\n- 【長文案を必ず追加】上の2案(短め)に加えて、3案目として『長文・線引き維持』を出す。"
                        "tone=\"長文・ていねい\"。長さは感情表現の増量ではなく**話題の回収と個別化**で出す:"
                        "(1)相手の直近の話題への反応から入る (2)未回答の質問があれば拾って答える(店の秘密や私生活の深入りはぼかす) "
                        "(3)会話履歴から具体的な話題を1〜2点だけ引用して気にかけを示す(3点以上詰め込まない) "
                        "(4)心配は健康・仕事・生活に限定(恋愛的な心配はしない) (5)自分の話は店・仕事の範囲で軽く "
                        "(6)締めは生活習慣か次の来店への言及で、恋愛的な余韻を残さない。"
                        "禁止事項(燃料・復唱・二人きりの約束・将来の匂わせ)は長文でも同じ。絵文字は実例準拠でノルマなし。")
    if (contact.get("kind") or "") == "staff":
        _st = (contact.get("stand") or "").strip()
        _tone = {"senior": "相手は店の先輩(ママ/黒服など)。丁寧め・敬語寄りで短く。",
                 "junior": "相手は後輩(ヘルプ)。タメ口で軽く、ねぎらいを一言。"}.get(_st, "相手は店の同僚。フラットに短く。")
        mode_prompt += ("\n【店内・同僚モード】これは客ではなく店の同僚への連絡。営業トーン・接客の定型句は禁止。"
                        "用件に即した短い実務返信にする。" + _tone +
                        " 時間・席・人数などの調整は断定せず『〜で大丈夫？』と確認で返す。")
    # 直近のやり取り(対応済みの受信＋自分が実際に送った返信)を文脈として渡す
    try:
        dlg = db.recent_dialogue(contact["code"], limit=8)
    except Exception:
        dlg = []
    if dlg:
        lines = "\n".join(f"{d['who']}「{(d['text'] or '')[:60]}」" for d in dlg)
        user_prompt += ("\n\nこの相手との直近のやり取り(文脈の参考。話題の続き・温度感を合わせる):\n" + lines)
    if mode_prompt:
        user_prompt = ("★最優先の特別指示(下の全情報より優先。この方針で書く):" + mode_prompt
                       + "\n\n---\n\n" + user_prompt)
    user_prompt += (
        f"\n相手: {contact['code']}(ランク{contact.get('rank','B')})\n"
        f"受信区分: {msg['reason']}\n"
        f"相手からのメッセージ:「{thread_text}」\n\n"
        "返信下書きを2案、JSONで。"
    )
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
                "max_tokens": 500,
                "system": SYSTEM,
                "messages": [{"role": "user", "content": user_prompt}],
            },
            timeout=30,
        )
        r.raise_for_status()
        out = "".join(b.get("text", "") for b in r.json().get("content", []))
        out = re.sub(r"```(json)?", "", out).strip()
        drafts = json.loads(out).get("drafts", [])[:3]
        if not drafts:
            raise ValueError("empty drafts")
        # ガチ恋/いなしは検品パス: 燃料・復唱・接客敬語・ぎこちなさを自己審査→必要なら書き直し
        if int(contact.get("flag_koi") or 0) == 1 or int(contact.get("flag_ero") or 0) == 1:
            if _needs_review(thread_text, drafts):   # 明白な問題がある時だけAI検品(速度優先)
                try:
                    drafts = _review_pass(contact, thread_text, drafts, user_prompt) or drafts
                except Exception:
                    pass
    except Exception:
        drafts = _template_drafts(contact, msg["text"], msg["reason"])
    db.save_drafts(message_id, drafts)
    return drafts
