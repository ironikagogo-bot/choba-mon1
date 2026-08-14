"""v216: ペルソナ分析の長文対策 — 途中切れJSONの決定論サルベージ(_json_salvage)と
analyze_personaの実経路(LLMをモックして、v212のmyself配列込みで保存されるか)。
背景: 木村曜カード(66KB・964行)でペルソナ分析が失敗した本人報告(2026-08-12)。
"""
import json


def test_salvage_truncated_in_myself_array(client):
    """v212の4配列目(myself)の途中で切れたケース: 完結分まで残して閉じ直せる。"""
    from app import linebot
    full = {"summary": "テスト", "sections": [{"k": "価値観の核", "v": "v1", "src": "s", "conf": "高"}],
            "tolerance": [{"k": "冗談・軽口", "v": "v2", "src": "s", "conf": "中"}],
            "myself": [{"k": "口調・距離", "v": "v3", "src": "s", "conf": "高"},
                       {"k": "演じている役", "v": "v4", "src": "s", "conf": "中"}]}
    t = json.dumps(full, ensure_ascii=False)
    cut = t[:t.index('"演じている役"') + 8]   # 2要素目の途中でぶった切る
    obj = linebot._json_salvage(cut)
    assert obj is not None and obj["summary"] == "テスト"
    assert obj["sections"][0]["v"] == "v1"
    assert obj["myself"][0]["k"] == "口調・距離"   # 完結している1要素目は生きる


def test_salvage_handles_escaped_quotes_and_none(client):
    from app import linebot
    t = '{"a": "引用\\"内側\\"つき", "b": ["x", "y"'
    obj = linebot._json_salvage(t)
    assert obj is not None and obj["a"].startswith("引用")
    assert linebot._json_salvage("そもそもJSONじゃない") is None
    assert linebot._json_salvage("") is None


def test_analyze_persona_end_to_end_with_mock(client, monkeypatch):
    """実経路: 取り込み済みトーク+モックLLM(途中切れ応答)→サルベージ→保存まで通る。"""
    from app import linebot, db, config

    code = "t_v216_kimura"
    db.upsert_contact(code, "B")
    lines = ["[LINE] t_v216_kimura とのトーク履歴", "2026/08/01(土)"]
    for i in range(120):
        lines.append(f"10:{i % 60:02d}\tt_v216_kimura\tこんにちは、資料の件よろしくお願いします{i}")
        lines.append(f"10:{i % 60:02d}\t自分\tはい、確認します!{i}")
    linebot.ensure()
    linebot.save_talk(code, "\n".join(lines))

    full = {"summary": "仕事の相談相手", "sections": [
                {"k": "価値観の核", "v": "期限と段取りを重視", "src": "締め切りがあります", "conf": "高"}],
            "tolerance": [{"k": "冗談・軽口", "v": "軽口は少なめ", "src": "よろしくお願いします", "conf": "中"}],
            "myself": [{"k": "口調・距離", "v": "敬語ベースで短文", "src": "はい、確認します!", "conf": "高"}]}
    t = json.dumps(full, ensure_ascii=False)
    truncated = t[:t.index('"口調・距離"') + 30]   # myselfの途中で切れた応答を再現

    class _R:
        status_code = 200
        def json(self):
            return {"content": [{"type": "text", "text": truncated}]}

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(linebot.requests, "post", lambda *a, **k: _R())
    p, err = linebot.analyze_persona(code)
    assert err is None, f"err={err}"
    assert p["summary"] == "仕事の相談相手"
    assert p["sections"][0]["k"] == "価値観の核"


def test_v221_myself_is_saved_from_llm(client, monkeypatch):
    """v221回帰: AIが返したmyselfが保存結果に残る(v212で捨てていた実バグの固定)。"""
    import json as _j
    from app import linebot, db, config
    code = "t_v221_my"
    db.upsert_contact(code, "B")
    linebot.ensure()
    lines = ["[LINE] t_v221_my とのトーク履歴", "2026/08/01(土)"]
    for i in range(60):
        lines.append(f"10:{i % 60:02d}\tt_v221_my\tこんにちは{i}")
        lines.append(f"10:{i % 60:02d}\t自分\tはーい!{i}")
    linebot.save_talk(code, "\n".join(lines))
    full = {"summary": "テスト", "sections": [{"k": "価値観の核", "v": "v1", "src": "s", "conf": "高"}],
            "tolerance": [{"k": "冗談・軽口", "v": "v2", "src": "s", "conf": "中"}],
            "myself": [{"k": "口調・距離", "v": "タメ口で短文", "src": "はーい!", "conf": "高"},
                       {"k": "演じている役", "v": "聞き役", "src": "はーい!", "conf": "中"}]}

    class _R:
        status_code = 200
        def json(self):
            return {"content": [{"type": "text", "text": _j.dumps(full, ensure_ascii=False)}]}

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(linebot.requests, "post", lambda *a, **k: _R())
    p, err = linebot.analyze_persona(code)
    assert err is None
    assert p.get("myself") and p["myself"][0]["k"] == "口調・距離"
    assert p["myself"][0]["v"] == "タメ口で短文"


def test_v227_myself_followup_recovers(client, monkeypatch):
    """1回目の応答にmyselfが無い(途中切れ等)→追撃リクエストで回収される。"""
    import json as _j
    from app import linebot, db, config
    code = "t_v227_fu"
    db.upsert_contact(code, "B")
    linebot.ensure()
    lines = ["[LINE] t_v227_fu とのトーク履歴", "2026/08/01(土)"]
    for i in range(80):
        lines.append(f"10:{i % 60:02d}\tt_v227_fu\tながいはなし{i}")
        lines.append(f"10:{i % 60:02d}\t自分\tうんうん{i}")
    linebot.save_talk(code, "\n".join(lines))
    first = {"summary": "S", "sections": [{"k": "価値観の核", "v": "v", "src": "s", "conf": "高"}],
             "tolerance": []}   # myself無し=途中切れ相当
    second = {"myself": [{"k": "口調・距離", "v": "聞き役で相槌多め", "src": "うんうん", "conf": "高"}]}
    calls = []

    class _R:
        def __init__(self, payload):
            self.status_code = 200
            self._p = payload
        def json(self):
            return {"content": [{"type": "text", "text": _j.dumps(self._p, ensure_ascii=False)}]}

    def _post(*a, **k):
        calls.append(1)
        return _R(first if len(calls) == 1 else second)

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(linebot.requests, "post", _post)
    p, err = linebot.analyze_persona(code)
    assert err is None
    assert len(calls) == 2                       # 追撃が1回だけ走った
    assert p["myself"][0]["v"] == "聞き役で相槌多め"
