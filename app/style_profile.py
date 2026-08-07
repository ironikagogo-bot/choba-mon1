"""LINEトーク履歴(エクスポート.txt)のパーサーと文体プロファイル抽出。

対応フォーマット(LINE公式エクスポートの一般形):
    [LINE] ○○とのトーク履歴
    保存日時:2026/07/01 12:00

    2026/06/28(日)
    21:03\t自分\tメッセージ本文
    21:04\t田中\t返信本文

タブ区切り: 時刻 \t 発言者 \t 本文。日付行はヘッダとして扱う。
※ フォーマットはLINEのバージョンで揺れるため、実データでの検証必須(要検証)。
"""
import re
from collections import Counter
from dataclasses import dataclass, field

DATE_RE = re.compile(r"^\d{4}/\d{1,2}/\d{1,2}")
MSG_RE = re.compile(r"^(\d{1,2}:\d{2})\t([^\t]+)\t(.*)$")
EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\u2600-\u27BF\U0001F000-\U0001F02F]")


@dataclass
class Message:
    time: str
    sender: str
    text: str


@dataclass
class Profile:
    self_name: str
    n_messages: int = 0
    avg_len: float = 0.0
    emoji_per_msg: float = 0.0
    exclaim_per_msg: float = 0.0
    warai_per_msg: float = 0.0        # 「笑」「w」「ｗ」
    tilde_per_msg: float = 0.0        # 「〜」「~」
    top_endings: list = field(default_factory=list)   # 文末表現の頻出
    top_emojis: list = field(default_factory=list)
    samples: list = field(default_factory=list)       # few-shot 用の実例(短め)

    def to_dict(self):
        return self.__dict__.copy()


def parse_talk(text: str) -> list[Message]:
    msgs: list[Message] = []
    for line in text.splitlines():
        if not line.strip() or DATE_RE.match(line) or line.startswith("[LINE]") or line.startswith("保存日時"):
            continue
        m = MSG_RE.match(line)
        if m:
            msgs.append(Message(m.group(1), m.group(2).strip(), m.group(3)))
        elif msgs:
            # 改行を含む複数行メッセージの続き
            msgs[-1].text += "\n" + line
    return msgs


def extract_profile(text: str, self_name: str = "自分") -> Profile:
    msgs = [m for m in parse_talk(text) if m.sender == self_name and m.text.strip()]
    p = Profile(self_name=self_name, n_messages=len(msgs))
    if not msgs:
        return p

    total_len = 0
    endings: Counter = Counter()
    emojis: Counter = Counter()
    n_emoji = n_ex = n_warai = n_tilde = 0

    for m in msgs:
        t = m.text
        total_len += len(t)
        n_emoji += len(EMOJI_RE.findall(t))
        n_ex += t.count("!") + t.count("！")
        n_warai += len(re.findall(r"(笑|ｗ+|(?<![a-zA-Z])w+$)", t))
        n_tilde += t.count("〜") + t.count("~")
        for e in EMOJI_RE.findall(t):
            emojis[e] += 1
        tail = re.sub(r"[\s。.!！?？〜~]+$", "", t)[-2:]
        if tail:
            endings[tail] += 1

    n = len(msgs)
    p.avg_len = round(total_len / n, 1)
    p.emoji_per_msg = round(n_emoji / n, 2)
    p.exclaim_per_msg = round(n_ex / n, 2)
    p.warai_per_msg = round(n_warai / n, 2)
    p.tilde_per_msg = round(n_tilde / n, 2)
    p.top_endings = [e for e, _ in endings.most_common(5)]
    p.top_emojis = [e for e, _ in emojis.most_common(5)]
    # few-shot 実例: 幅広い長さの「らしい」メッセージを多めに(声を吸収させる用)
    p.samples = [m.text for m in msgs if 4 <= len(m.text) <= 90][-200:][::-1]  # 直近200文・新しい順
    return p


def discover_contacts(text: str, self_name: str = "自分") -> list[str]:
    """トーク履歴に登場する相手(自分以外)の名前一覧を返す。顧客の自動登録に使う。"""
    names = []
    seen = set()
    for m in parse_talk(text):
        if m.sender != self_name and m.sender not in seen:
            seen.add(m.sender)
            names.append(m.sender)
    return names


def extract_contact_profile(text: str, contact_name: str, self_name: str = "自分") -> dict:
    """特定の相手とのやり取りから、その相手向けの補足プロファイルを作る。
    - 自分がこの相手にどう話しているか(敬語度・呼称・トーン)を few-shot 実例で捕捉
    - 相手の話題(頻出語)も軽く拾う
    """
    msgs = parse_talk(text)
    my_to_this = []       # この相手の発言の直後に続く自分の発言のみ(誤混入を防ぐ)
    their_msgs = []
    context = False
    for m in msgs:
        if m.sender == contact_name:
            their_msgs.append(m.text)
            context = True
        elif m.sender == self_name:
            if context:
                my_to_this.append(m.text)
            # 相手→自分 のあと、自分が続けて複数送る場合も同一スレッド扱いのまま
        else:
            # 別の相手が現れたらスレッドが切れたとみなす
            context = False
    # 呼称の推定: 自分の発言中に相手名や「さん/会長/専務」等が出るか
    honorifics = []
    for h in ["さん", "会長", "社長", "専務", "常務", "部長", "先生", "様", "君", "ちゃん"]:
        if any(h in t for t in my_to_this):
            honorifics.append(h)
    samples = [t for t in my_to_this if 4 <= len(t) <= 90][-200:][::-1]  # 直近200文・新しい順
    return {
        "contact": contact_name,
        "my_message_count": len(my_to_this),
        "honorifics_to_them": honorifics,
        "my_samples_to_them": samples,
        "their_recent": their_msgs[-3:],
    }


def learn_from_sent(contact: str, text: str, edited: int = 0, edit_ratio: int = 100):
    """本人が実際に送った文(コピー確定時)をプロファイルに還元する=使うほど賢くなる本体。
    - 相手別: my_samples_to_them の先頭に追加(最新優先・重複除去・最大30)
    - 全体: samples の先頭に追加(取り込み済みプロファイルがある場合のみ・最大60)
    - sent_replies にも記録(直近履歴の文脈注入用)
    """
    from . import db
    t = (text or "").strip()
    if not t or len(t) > 500:
        return
    cp = db.get_profile(contact) or {}
    samples = [x for x in (cp.get("my_samples_to_them") or []) if x != t]
    samples.insert(0, t)
    cp["my_samples_to_them"] = samples[:200]
    cp["my_message_count"] = int(cp.get("my_message_count") or 0) + 1
    db.save_profile(contact, cp)
    g = db.get_profile("_global") or {}
    if g.get("n_messages"):  # .txt取り込み済みの時だけ(空プロファイルに数値キーが無くブロック生成が壊れるため)
        gs = [x for x in (g.get("samples") or []) if x != t]
        gs.insert(0, t)
        g["samples"] = gs[:200]
        db.save_profile("_global", g)
    db.add_sent_reply(contact, t, edited=edited, edit_ratio=edit_ratio)


def _pick_samples(samples: list, recent: int, spread: int) -> list:
    """実例の選抜: 新しい順リストから「直近recent件」+「残りから等間隔spread件(場面の多様性用)」。"""
    if not samples:
        return []
    head = samples[:recent]
    rest = samples[recent:]
    if not rest or spread <= 0:
        return head
    step = max(1, len(rest) // spread)
    return head + rest[::step][:spread]


def contact_profile_block(cp: dict) -> str:
    """相手別プロファイルを生成プロンプト用の文字列にする。"""
    if not cp or not cp.get("my_message_count"):
        return ""
    lines = []
    if cp.get("honorifics_to_them"):
        lines.append(f"この相手への呼び方・敬称: {'、'.join(cp['honorifics_to_them'])}")
    if cp.get("my_samples_to_them"):
        lines.append("★この相手に本人が実際に送った文(最重要・この距離感/崩し方/言い回しをそのまま真似る):\n"
                     + "\n".join(f"「{s}」" for s in _pick_samples(cp["my_samples_to_them"], 40, 20)))
    return "この相手専用のプロファイル:\n" + "\n".join(lines) if lines else ""


def profile_prompt_block(profile: dict) -> str:
    """生成プロンプトに同梱する文体指示ブロックを組み立てる。"""
    if not profile or not profile.get("n_messages"):
        return "文体プロファイル未登録。丁寧で親しみのある一般的な文体で。"
    lines = [
        f"平均文長: 約{profile['avg_len']}文字(この長さ感を守る)",
        f"絵文字: 1通あたり約{profile['emoji_per_msg']}個 " + ("(多用する)" if profile['emoji_per_msg'] > 0.8 else "(控えめ)"),
        f"「!」: 1通あたり約{profile['exclaim_per_msg']}回",
        f"「笑/w」: 1通あたり約{profile['warai_per_msg']}回",
        f"よく使う文末: {'、'.join(profile['top_endings'][:3]) or 'なし'}",
        f"よく使う絵文字: {''.join(profile['top_emojis'][:3]) or 'なし'}",
    ]
    # 数字は「当てにいく目標」ではなく軽い参考。声は下の実例から吸収させる。
    block = "本人の文体の傾向(参考程度。数値を無理に合わせにいかない):\n- " + "\n- ".join(lines)
    if profile.get("samples"):
        block += ("\n\n★本人が実際に書いた文(最重要・これが本人の『声』。"
                  "言い回し・崩し・句読点の少なさ・端折り方をそのまま真似る):\n")
        block += "\n".join(f"「{s}」" for s in _pick_samples(profile["samples"], 20, 10))
    return block


def samples_to_them(code: str, n: int = 3) -> str:
    """v123: 接し方プロファイル=『この相手に』実際に送った直近の文。
    グローバルな文体でなく、相手ごとの口調・絵文字・距離感の実例を必達見本として返す。"""
    from . import db as _db
    try:
        with _db.conn() as c:
            rows = [r["text"] or "" for r in c.execute(
                "SELECT text FROM sent_replies WHERE contact=? ORDER BY ts DESC LIMIT 12", (code,))]
    except Exception:
        return ""
    picked = [t.strip() for t in rows if 4 <= len(t.strip()) <= 140][:n]
    if not picked:
        return ""
    return ("【この相手への自分の実例(最優先の口調見本。敬語/タメ口・絵文字の量・"
            "距離感をこのまま再現する)】\n" + "\n".join(f"「{s}」" for s in picked))
