"""Androidの通知(LINE)を帳場の受信に変換する層。

Android側(サブ端末)の転送アプリ or 専用アプリが、LINEの着信通知を
{package, title, text} として帳場に POST してくる。それを (contact, message) に
解釈し、重複を除いて受信パイプラインに渡す。

LINEの通知フォーマットはバージョン・端末で揺れるので、パーサーは実データで調整前提
(生の title/text はデスクコンソールに出して、AXツリーの時と同じく実物合わせする)。

このモジュールにも送信機能は無い(読み取り→取り込みのみ)。
"""
import re
import time

LINE_PACKAGES = ("jp.naver.line.android",)   # LINE (Android) のパッケージ名

# 無視すべき要約/集約通知の本文パターン(相手・本文が特定できないもの)
_SUMMARY_PATTERNS = [
    re.compile(r"^\s*\d+\s*件"),                 # 「3件の…」
    re.compile(r"新しいメッセージ"),
    re.compile(r"メッセージが届いています"),
    re.compile(r"新着メッセージ"),
]

# 通話系通知(不在着信/着信中/通話中/応答なし)は取り込まない:
# 着信はiPhone本体が鳴る＋LINE本体に不在着信表示が残るため、帳場で二重管理しない(本人判断 2026-07-18)。
_CALL_TITLE = re.compile(r"(不在着信|着信中|通話中|応答なし)")
# LINE本体のシステム通知(本文がこの定型のみ)。通常メッセージに混ざる場合は通す
_SYS_TEXT = re.compile(r"^.{1,30}が(?:友だちになりました|あなたを友だち追加しました|グループに参加しました)。?$")
# 本文が通話開始の定型文"だけ"のとき(グループ通話等)。通常メッセージ内に
# 「不在着信」等の語が含まれるだけでは捨てない(誤爆防止)。
_CALL_TEXT = re.compile(r"^(?:.{1,30}が)?(?:LINE)?(?:グループ通話|ビデオ通話|音声通話|通話)(?:を開始しました|に招待しました|に参加しました|を?着信中.{0,3}|応答なし)$")


def is_call_notice(text: str) -> bool:
    """本文が通話系通知そのもの("LINE音声通話を着信中…"等)か。過去に取り込んだ残骸の掃除にも使う。"""
    t = re.sub(r"^【[^】]*】", "", (text or "").strip()).strip()
    return bool(_CALL_TEXT.match(t) or re.match(r"^LINE(?:不在着信|着信中)$", t))

# グループトークの本文が "送信者: 本文" 形式のとき分離する
_GROUP_SPLIT = re.compile(r"^(?P<sender>[^:：！？。、,.\d\n]{1,20})[:：]\s*(?P<msg>.+)$", re.S)
# メディア/アクション通知: "送信者 が 写真/スタンプ等 を送信しました" から送信者を抽出
_MEDIA_SENDER = re.compile(r"^(?P<sender>.{1,30}?)が(?P<action>(?:写真|スタンプ|動画|画像|ファイル|ボイスメッセージ|アルバム|位置情報|連絡先|ギフト)を送信しました)$")


def parse_line_notification(title: str, text: str, package: str | None = None,
                            known_title: bool = False) -> dict | None:
    """LINE通知 → {"contact","message"} を返す。取り込むべきでなければ None。

    - package が LINE 以外なら None(パッケージ指定がある場合のみ判定)
    - 要約/集約通知(相手不明)は None
    - 1対1: title=相手名, text=本文
    - グループ: title=グループ名, text="送信者: 本文" → contact=送信者, message=本文
    - known_title=True: titleが既存カード/エイリアスに解決済み=確実に1:1。
      本文の「語: 残り」をグループ発言と誤認して偽カード(「集合」等)を作らない(v177)。
    """
    title = (title or "").strip()
    text = (text or "").strip()

    if package:
        p = package.lower()
        if not any(pkg in p for pkg in LINE_PACKAGES) and "line" not in p:
            return None

    if not title and not text:
        return None
    # アプリ名だけの通知や要約は捨てる
    if title in ("LINE", "") and (not text or _is_summary(text)):
        return None
    if _is_summary(text):
        return None
    # 通話系(不在着信等)は取り込まない。title側("LINE不在着信")か、
    # 本文が通話開始の定型文だけの場合のみ(本文に語が混ざる通常文は通す)。
    if _CALL_TITLE.search(title) or _CALL_TEXT.match(text):
        return None
    # LINEのシステム通知(友だち追加など)は人からのメッセージではないので捨てる。
    # 「LINE」という偽の連絡先が未紐付けトレイに湧く実運用バグ(2026-08-05 mon1)の対策。
    if _SYS_TEXT.match(text.strip()):
        return None

    # グループ判定: (a)"送信者: 本文"  (b)"送信者 が 写真等を送信しました"
    # known_title(既存カードに解決済みの1:1)ではスキップ=「集合: 19時に」の誤分離を防ぐ
    sender = None
    body = None
    if not known_title:
        m = _GROUP_SPLIT.match(text)
        if m and title and m.group("sender").strip() != title:
            sender, body = m.group("sender").strip(), m.group("msg").strip()
        else:
            mm = _MEDIA_SENDER.match(text)
            if mm and title and mm.group("sender").strip() != title:
                sender, body = mm.group("sender").strip(), mm.group("action").strip()
    if sender:
        # 相手=送信者(本人)。グループ名は文脈として本文頭に残す(簡易対応・スキーマ変更なし)
        gname = title.strip()
        msg = ("【" + gname + "】" + body) if gname else body
        return {"contact": sender, "message": msg}

    # 名無しグループ: タイトルがメンバー名の羅列("Eri, Sangeetha Roka, Aki"等)。
    # 送信者が本文から取れない形式の通知では誰の発言か特定できない→グループ印を付けて
    # フロントで「分類を迫らない」専用カードにする(根本解決はC5=APKでsender分離送信)。
    if ("," in title or "、" in title):
        _parts = [x.strip() for x in re.split(r"[,、]", title) if x.strip()]
        if len(_parts) >= 2 and all(len(x) <= 20 for x in _parts):
            return {"contact": title, "message": "【グループ】" + text}

    # 1対1: title=相手名, text=本文
    return {"contact": title or "(不明)", "message": text}


def _is_summary(text: str) -> bool:
    return any(p.search(text) for p in _SUMMARY_PATTERNS)


class NotifyDedup:
    """通知の"再掲"(同一メッセージの重複POST)だけを弾く。本物の連投は通す。

    設計方針(本人の要望「本物のメッセージを消さない」を最優先):
    - 通知の一意ID(アプリが送る key+ts)が既出なら、同一通知の再掲とみなして弾く。
    - ts が端末で揺れる対策として、同一(相手,本文)を"短い窓(既定3秒)"内に見た場合も再掲扱い。
      → LINEの再掲はほぼ同時(ミリ秒〜数秒)なので拾える。
    - 旧「相手の直前と同じ本文なら常に弾く」ルールは廃止。
      本物の『はい』『はい』を消していたため。3秒より離れた同一本文は通す。
    """

    LONG_TEXT_MIN = 15      # この文字数以上は「同一本文の再掲」を長い窓で弾く
    LONG_WINDOW = 30 * 60   # 長文の同一本文は30分以内なら再掲とみなす(実測: ロック解除時に分単位で再掲される)

    def __init__(self, window_sec: float = 3.0):
        self.window_sec = window_sec
        self._seen = {}        # (contact, message) -> ts   短文用(3秒窓)
        self._seen_long = {}   # (contact, message) -> ts   長文用(30分窓)
        self._seen_ids = {}    # msg_id(key+ts) -> ts

    def should_process(self, contact: str, message: str,
                       now: float | None = None, msg_id: str | None = None) -> bool:
        now = now if now is not None else time.time()
        # 古い記録を掃除(ID側は少し長めに保持=同一通知の再掲は間隔が空くこともある)
        for k, ts in list(self._seen.items()):
            if now - ts > self.window_sec:
                del self._seen[k]
        for k, ts in list(self._seen_long.items()):
            if now - ts > self.LONG_WINDOW:
                del self._seen_long[k]
        for k, ts in list(self._seen_ids.items()):
            if now - ts > 60:
                del self._seen_ids[k]
        # 1) 通知の一意ID(key+ts)が既出 → 同一通知の再掲
        if msg_id:
            if msg_id in self._seen_ids:
                return False
            self._seen_ids[msg_id] = now
        # 2) ts揺れ対策: 同一(相手,本文)を短い窓内に見た → 再掲とみなす
        #    短文(「はい」等)は3秒窓のみ=本物の連投を消さない。
        #    長文(15字以上)の同一本文は30分窓=通知の再掲(ロック解除時等)を弾く。
        pair = (contact, message)
        if pair in self._seen:
            return False
        self._seen[pair] = now
        if len(message) >= self.LONG_TEXT_MIN:
            if pair in self._seen_long:
                return False
            self._seen_long[pair] = now
        return True
