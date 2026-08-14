"""v240: 配信のAI生成が「失敗することが多い」への対策。

本人報告(2026-08-14)「アナウンスでAI使った定型文生成に失敗することが多い」。
配信は人数分を連続で叩くのに、1回きりの呼び出し+素のjson.loadsだったため、
レート制限・一時的な過負荷・AIのひとことの前置き・max_tokens切れ、どれか1つで
黙って定型文に落ちていた(理由はログにも残らなかった)。
"""
import json

import pytest

from tests.conftest import mk_contact

H = {"X-Ingest-Token": "tk"}


class _Resp:
    def __init__(self, status=200, text='{"text":"こんばんは、今週も待ってます"}'):
        self.status_code = status
        self._t = text

    def json(self):
        return {"content": [{"text": self._t}]}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


# ── JSONの取り出しが頑丈になったか ───────────────────────────
@pytest.mark.parametrize("raw,want", [
    ('{"text":"こんばんは"}', "こんばんは"),
    ('はい、承知しました。\n```json\n{"text":"今週は木曜おります"}\n```\nいかがでしょう', "今週は木曜おります"),
    ('{"tone":"やわらかめ","text":"「玉響」入りました"}', "「玉響」入りました"),
    ('{"text":"1行目\\n2行目"}', "1行目\n2行目"),
    # max_tokens切れ(閉じ引用符ごと欠け)。長い断片は救出する=本人が直せる形で見せる
    ('{"tone":"a","text":"宝条さん、玉響入りました！今週おります', "宝条さん、玉響入りました！今週おります"),
])
def test_json_text_is_robust(raw, want):
    from app import campaign
    assert campaign._json_text(raw) == want


@pytest.mark.parametrize("raw", [
    "すみません、生成できませんでした",     # JSONですらない
    '{"text":"宝条さ',                      # 短すぎる断片は拾わない(ちぎれた文を送らせない)
    "",
])
def test_json_text_gives_up_cleanly(raw):
    from app import campaign
    with pytest.raises(Exception):
        campaign._json_text(raw)


# ── レート制限・過負荷を再試行するか ──────────────────────────
def test_retries_on_rate_limit(client, tok, monkeypatch):
    """429の次に200が返れば、定型文に落とさずAI文を返す(従来は1発で諦めていた)。"""
    from app import campaign, config
    mk_contact(client, tok, "t_v240c_a", rank="A")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key", raising=False)
    monkeypatch.setattr(campaign.time, "sleep", lambda *_: None)   # 待ちは飛ばす
    calls = []

    def _post(url, **kw):
        calls.append(1)
        return _Resp(429) if len(calls) == 1 else _Resp()

    monkeypatch.setattr(campaign.requests, "post", _post)
    r = campaign.generate(mode="greeting", ranks=["S", "A", "B"], codes=["t_v240c_a"],
                          template="今週は木曜おります")
    assert len(calls) == 2, "再試行していない"
    assert r["items"][0]["ai"] is True
    assert r["ai_failed"] == 0


def test_gives_up_after_three_tries(client, tok, monkeypatch):
    """ずっと429なら定型文に落ちる。落ちた人数は数えて返す(黙って消えない)。"""
    from app import campaign, config
    mk_contact(client, tok, "t_v240c_b", rank="A")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key", raising=False)
    monkeypatch.setattr(campaign.time, "sleep", lambda *_: None)
    calls = []

    def _post(url, **kw):
        calls.append(1)
        return _Resp(429)

    monkeypatch.setattr(campaign.requests, "post", _post)
    r = campaign.generate(mode="greeting", ranks=["S", "A", "B"], codes=["t_v240c_b"],
                          template="今週は木曜おります")
    assert len(calls) == 3
    assert r["items"][0]["ai"] is False        # UIの琥珀注意が出る
    assert r["ai_failed"] == 1
    assert r["items"][0]["text"]               # 定型文は必ず出る(空にはしない)


def test_preamble_no_longer_fails(client, tok, monkeypatch):
    """AIが一言添えただけで全滅していた経路が通る。"""
    from app import campaign, config
    mk_contact(client, tok, "t_v240c_c", rank="A")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key", raising=False)
    monkeypatch.setattr(campaign.time, "sleep", lambda *_: None)
    monkeypatch.setattr(campaign.requests, "post",
                        lambda url, **kw: _Resp(text='了解です。\n{"text":"今週も待ってます"}'))
    r = campaign.generate(mode="greeting", ranks=["S", "A", "B"], codes=["t_v240c_c"],
                          template="今週は木曜おります")
    assert r["items"][0]["ai"] is True
    assert "今週も待ってます" in r["items"][0]["text"]


def test_max_tokens_raised(client, tok, monkeypatch):
    """注入が増えた分、上限を上げてある(450では閉じ手前で切れることがあった)。"""
    from app import campaign, config
    mk_contact(client, tok, "t_v240c_d", rank="A")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key", raising=False)
    seen = {}

    def _post(url, **kw):
        seen["body"] = kw.get("json") or {}
        return _Resp()

    monkeypatch.setattr(campaign.requests, "post", _post)
    campaign.generate(mode="greeting", ranks=["S", "A", "B"], codes=["t_v240c_d"],
                      template="今週は木曜おります")
    assert seen["body"]["max_tokens"] >= 700


def test_ai_failed_is_exposed(client, tok, monkeypatch):
    """何人が定型文になったかを呼び出し側が知れる(将来UIに出すため)。"""
    from app import campaign, config
    for i in range(3):
        mk_contact(client, tok, f"t_v240c_e{i}", rank="A")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key", raising=False)
    monkeypatch.setattr(campaign.time, "sleep", lambda *_: None)
    monkeypatch.setattr(campaign.requests, "post", lambda url, **kw: _Resp(500))
    r = campaign.generate(mode="greeting", ranks=["S", "A", "B"],
                          codes=[f"t_v240c_e{i}" for i in range(3)],
                          template="今週は木曜おります")
    assert r["ai_failed"] == 3 and r["count"] == 3


def test_client_offers_regenerate():
    """定型文になった時、その相手だけ作り直せるボタンが出る。"""
    import os
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "app", "static", "liff.html")
    html = open(p, encoding="utf-8").read()
    assert "annRegen()" in html
    assert 'window.annRegen' in html
    assert 'delete draftCache["a" + p.code]' in html   # prefetchAnnと同じ鍵
    _ = json  # noqa: F841
