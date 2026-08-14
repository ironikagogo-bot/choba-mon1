"""v211: 呼び名の決定論抽出(敬称込み・3層フィルタ)。実測4本(南さん/文ちゃん/宮澤くん/材料なし)で
検証済みの挙動を合成データで固定する。自動確定はしない=○✕関門(pending facts)へ。
"""
from tests.conftest import mk_contact


def _talk(header, lines):
    return f"[LINE] {header}とのトーク履歴\n" + "\n".join(lines)


def test_yobina_honorific_and_dispersion(client):
    from app import linebot
    t = _talk("山口 文太郎", [
        "2026/01/01(木)", "10:00\t自分\t文ちゃん、あけおめ!",
        "2026/01/05(月)", "10:00\t自分\t文ちゃん今週どう?",
        "2026/01/09(金)", "10:00\t自分\t文ちゃん、飲もう", "10:01\t自分\tえりちゃんも誘う?",
        "2026/01/10(土)", "10:00\t自分\t文ちゃんありがとう",
        "10:01\t自分\t明日焼肉行くんですけど来ます?",   # 動詞+ん誤マッチgarde
        "2026/01/12(月)", "10:00\t自分\t文ちゃん、おつかれ", "10:01\t自分\t文ちゃん、また今度"])
    r = linebot.extract_yobina_calls(t, "自分")
    assert r["v"] == "文ちゃん" and r["conf"] == "高"
    assert "6回" in r["src"] and "5日" in r["src"]


def test_yobina_romaji_display_freq_path(client):
    """漢字候補×ローマ字表示名: 照合不一致でも頻度・分散が強ければ中確信で出す(南さん型)。"""
    from app import linebot
    t = _talk("minamitoshiro", [
        "2026/01/01(木)", "10:00\t自分\t南さん、あけおめ!",
        "2026/01/03(土)", "10:00\t自分\t南さん今夜どうですか",
        "2026/01/05(月)", "10:00\t自分\t南さん、席ありますか",
        "2026/01/07(水)", "10:00\t自分\t南さんお疲れさまです",
        "2026/01/09(金)", "10:00\t自分\t南さん、また"])
    r = linebot.extract_yobina_calls(t, "自分")
    assert r["v"] == "南さん" and r["conf"] in ("高", "中")


def test_yobina_topic_and_thirdparty_rejected(client):
    """話題形(助詞)と低頻度の第三者は候補にしない=材料が無ければNone。"""
    from app import linebot
    t = _talk("木村", [
        "2026/01/01(木)", "10:00\t自分\t了解です!",
        "10:01\t自分\t田中さんが来るって", "10:02\t自分\t田中さんは元気?",
        "10:03\t自分\t武田くんも来ます"])
    assert linebot.extract_yobina_calls(t, "自分") is None


def test_yobina_kun_variants_merged(client):
    from app import linebot
    t = _talk("宮澤将史", [
        "2026/01/01(木)", "10:00\t自分\t宮澤くん、おつ",
        "2026/01/03(土)", "10:00\t自分\t宮澤君どう?",
        "2026/01/05(月)", "10:00\t自分\t宮澤くん、飲む?"])
    r = linebot.extract_yobina_calls(t, "自分")
    assert r["v"] == "宮澤くん" and "3回" in r["src"]


def test_yobina_lands_in_pending_facts_via_import(client, tok):
    """取り込み経路の統合: AIキー無しでも呼び名factがpendingに積まれる(自動確定はしない)。"""
    import io
    from app import db
    txt = _talk("山口 文太郎", [
        "2026/01/01(木)", "10:00\t大山\t文ちゃん、あけおめ!", "10:01\t山口 文太郎\tあけおめ!",
        "2026/01/05(月)", "10:00\t大山\t文ちゃん今週どう?",
        "2026/01/09(金)", "10:00\t大山\t文ちゃん、飲もう",
        "2026/01/10(土)", "10:00\t大山\t文ちゃんありがとう",
        "2026/01/12(月)", "10:00\t大山\t文ちゃん、おつかれ"])
    db.save_profile("_selfname", {"name": "大山"})
    r = client.post("/api/liff/import", headers=tok,
                    files=[("files", ("[LINE] 山口 文太郎とのトーク.txt", io.BytesIO(txt.encode("utf-8")), "text/plain"))])
    assert r.status_code == 200
    import json as _json
    import time as _t
    from app import linebot
    found = None
    for _ in range(50):   # ジョブは別スレッド
        # v187検疫: 新規カードの事実は種別確定まで保留箱(quarantine)に入るのが正しい挙動
        q = _json.loads(linebot._meta_get("quarantine_山口 文太郎") or "[]")
        found = next((f for f in q if f.get("k") == "呼び名"), None)
        if found:
            break
        _t.sleep(0.2)
    assert found and found["v"] == "文ちゃん" and "呼びかけ" in found["src"]
    # 自動確定していないこと(カード属性は未設定のまま)
    from app import crm
    assert (crm.get_attrs("山口 文太郎") or {}).get("呼び名") != "文ちゃん"
