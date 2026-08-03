"""テキスト→下書き のワンショット生成(iOSショートカット/クイック返信用)。

保存済みメッセージ(message_id)を前提とする drafts.generate() と違い、
相手の本文テキストを直接受け取って下書き2案を返す。
既存の文体プロファイル・顧客プロファイル・ランクをそのまま再利用する。

距離感(register)・呼び方(nickname)は、DB列が無くてもリクエストで明示指定できる
(指定があればプロンプトにハードルールとして注入する)。指定が無ければ従来通り。
"""
import json
import re

import requests

from . import config, db
from .drafts import SYSTEM, _template_drafts
from .style_profile import profile_prompt_block, contact_profile_block

_REGISTER_RULE = {
    "keigo_only": "【距離感=敬語厳守・ハードルール】この相手には必ず敬語で書く。タメ口・砕けた表現は一切禁止。2案とも敬語で作ること(堅い客への事故防止)。",
    "keigo": "【距離感=敬語】この相手には敬語基調で丁寧に。",
    "mix": "【距離感=混在】敬語とタメ口が混ざる間柄。2案のうち片方を少しだけ砕けた案にしてよい。",
    "casual": "【距離感=タメ口OK】砕けた口調(タメ口)でよい親しい相手。親しげに、崩して。",
}


def draft_from_text(text: str, contact_code: str | None = None,
                    reason: str = "ラリー", register: str | None = None,
                    nickname: str | None = None) -> list[dict]:
    contact = db.get_contact(contact_code) if contact_code else None
    if not contact:
        contact = {"code": contact_code or "", "rank": "B"}

    # APIキー未設定時はテンプレにフォールバック(動線検証用)
    if not config.ANTHROPIC_API_KEY:
        return _template_drafts(contact if contact.get("code") else None, text, reason)

    profile = db.get_profile("_global") or {}
    per_contact = db.get_profile(contact["code"]) or {} if contact.get("code") else {}
    cp_block = contact_profile_block(per_contact)

    parts = [profile_prompt_block(profile)]
    if cp_block:
        parts.append(cp_block)
    rule = _REGISTER_RULE.get((register or "").strip())
    if rule:
        parts.append(rule)
    if nickname:
        parts.append(f"呼び方は「{nickname}」を使う(不自然にならない範囲で)。")
    _pos = (contact.get("note_pos") or "").strip()
    _neg = (contact.get("note_neg") or "").strip()
    if _pos:
        parts.append(f"この相手が喜ぶ・強み(自然に活かす): {_pos}")
    if _neg:
        parts.append(f"この相手の地雷・注意(触れず避ける。本文にこの語句を絶対書かない): {_neg}")
    parts.append(
        f"相手: {contact.get('code','') or '不明'}(ランク{contact.get('rank','B')})\n"
        f"受信区分: {reason}\n"
        f"相手からのメッセージ:「{text}」\n\n"
        "返信下書きを2案、JSONで。"
    )
    user_prompt = "\n\n".join(parts)

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
        return drafts
    except Exception:
        return _template_drafts(contact if contact.get("code") else None, text, reason)
