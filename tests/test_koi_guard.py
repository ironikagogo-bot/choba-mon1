"""app/koi_guard.py (v186/191) のテスト。

観点:
- DEFAULT_PATTERNS A/B/C 各系統の代表が hits で検出される
- tolerance: add_ok → ok_ids → clean_text でその相手のみ抑制解除
- clean_text: 句読点あり/なし(絵文字・連続スペース区切り #18)でも該当文だけ落とす
- ハート3連以上が1個に間引かれる(2個は触らない)
- 全文消滅時 guard_drafts が3案に別々の代替文(_FALLBACKSローテ)
- linebot_meta "koi_guard_words" で上書き・壊れた正規表現は黙ってスキップ
- code なし(flag_koi OFF相当の呼び方)では tolerance が効かない
- general モードの代替文は run_in_mode で別プロセス検証

契約者コード: t_koiguard_<n>(他テストと衝突させない)
"""
import json

import pytest

from tests.conftest import run_in_mode

from app import db, koi_guard


@pytest.fixture(scope="module", autouse=True)
def _dbinit():
    """clientを使わない純モジュールテストなので、schema(style_profile等)を自前で確保。"""
    db.init()


def _hit_ids(text):
    return {pid for pid, _ in koi_guard.hits(text)}


# ---- 1〜3. DEFAULT_PATTERNS 系統別代表 ----

@pytest.mark.parametrize("text,pid", [
    ("君がいないと生きていけない", "A1"),
    ("あなたなしでは無理", "A2"),
    ("君だけがすべてだよ", "A3"),
    ("一生そばにいるから", "A4"),
    ("絶対に離れないよ", "A5"),
])
def test_hits_family_a(text, pid):
    assert pid in _hit_ids(text)


@pytest.mark.parametrize("text,pid", [
    ("浮気なんか絶対しないよ", "B1"),
    ("他の子なんて見てないって", "B2"),
    ("君だけ見てるから", "B3"),
    ("疑わないでね", "B4"),
])
def test_hits_family_b(text, pid):
    assert pid in _hit_ids(text)


@pytest.mark.parametrize("text,pid", [
    ("ずっと一緒だよ", "C1"),
    ("絶対会いに行くから", "C2"),
    ("一生の約束ね", "C3"),
    ("気持ちは一生変わらない", "C4"),
])
def test_hits_family_c(text, pid):
    assert pid in _hit_ids(text)


# ---- 4. hits の戻り形式と skip ----

def test_hits_returns_fragment_and_respects_skip():
    got = koi_guard.hits("ずっと一緒だよ")
    assert ("C1", "ずっと一緒") in got
    assert koi_guard.hits("ずっと一緒だよ", skip=("C1",)) == []
    assert koi_guard.hits("") == []
    assert koi_guard.hits(None) == []


# ---- 5. tolerance: add_ok → ok_ids → clean_text(その相手だけ黙る) ----

def test_add_ok_and_ok_ids_per_contact():
    code = "t_koiguard_1"
    assert koi_guard.ok_ids(code) == []          # 未登録相手はエラーなく空
    koi_guard.add_ok(code, "C1")
    koi_guard.add_ok(code, "C1")                 # 二重登録しても増えない
    koi_guard.add_ok(code, "A1")
    assert koi_guard.ok_ids(code) == ["A1", "C1"]  # sorted
    assert koi_guard.ok_ids("t_koiguard_other") == []


def test_clean_text_tolerance_only_for_that_contact():
    code = "t_koiguard_2"
    koi_guard.add_ok(code, "C1")
    text = "ずっと一緒にいたいな。また来てね。"
    # ○を付けた相手: C1文は残る
    assert koi_guard.clean_text(text, code) == "ずっと一緒にいたいな。また来てね。"
    # 別の相手: C1文は落ちる
    assert koi_guard.clean_text(text, "t_koiguard_3") == "また来てね。"


def test_clean_text_without_code_ignores_tolerance():
    """codeなし(flag_koi OFF相当のガード外呼び出し形)ではskipは常に空。"""
    code = "t_koiguard_4"
    koi_guard.add_ok(code, "C1")
    assert koi_guard.clean_text("ずっと一緒にいたいな。また来てね。") == "また来てね。"
    assert koi_guard.clean_text("ずっと一緒にいたいな。また来てね。", "") == "また来てね。"


# ---- 6. 文境界: 句読点あり/なし(#18) ----

def test_clean_text_drops_only_offending_sentence_with_punctuation():
    out = koi_guard.clean_text("今日ありがとう！ずっと一緒にいたいな。また来てね。", "t_koiguard_5")
    assert out == "今日ありがとう！また来てね。"


def test_clean_text_emoji_boundary_no_punctuation():
    """句読点なし・絵文字区切り文体でも全文消滅せず該当セグメントのみ落ちる(#18)。"""
    out = koi_guard.clean_text("今日ありがとう😊ずっと一緒にいたいな😊また来てね", "t_koiguard_5")
    assert out == "今日ありがとう😊また来てね"


def test_clean_text_double_space_boundary():
    out = koi_guard.clean_text("今日ありがとう  ずっと一緒にいたいな  また来てね", "t_koiguard_5")
    assert "ずっと一緒" not in out
    assert "今日ありがとう" in out and "また来てね" in out


# ---- 7. ハート連打の間引き ----

def test_heart_run_thinned_to_one():
    assert koi_guard.clean_text("会いたいな❤️❤️❤️", "t_koiguard_5") == "会いたいな❤"
    # 異種ハート混在の3連も1個(先頭)に
    assert koi_guard.clean_text("おつかれさま💕💖💘", "t_koiguard_5") == "おつかれさま💕"
    # 2個までは本人の表現として触らない
    assert koi_guard.clean_text("会えてうれしい❤️❤️", "t_koiguard_5") == "会えてうれしい❤️❤️"


# ---- 8. 全文消滅と guard_drafts のフォールバックローテ ----

def test_clean_text_all_removed_returns_empty():
    assert koi_guard.clean_text("ずっと一緒だよ", "t_koiguard_5") == ""


def test_guard_drafts_fallback_rotation_mizu():
    """3案とも全文消滅 → _FALLBACKS[mizu] の3案が順に割り当てられ同文連投しない。"""
    drafts = [{"text": "ずっと一緒だよ"}, {"text": "浮気は絶対しない"}, {"text": "一生の約束ね"}]
    out = koi_guard.guard_drafts("t_koiguard_6", drafts)
    texts = [d["text"] for d in out]
    assert texts == koi_guard._FALLBACKS["mizu"]
    assert len(set(texts)) == 3


def test_guard_drafts_mixed_keeps_clean_and_preserves_keys():
    drafts = [
        {"text": "ずっと一緒だよ", "kind": "short"},
        {"text": "今日ありがとう！ずっと一緒にいたいな。", "kind": "mid"},
        {"text": "疑わないでね", "kind": "long"},
    ]
    out = koi_guard.guard_drafts("t_koiguard_6", drafts)
    fbs = koi_guard._FALLBACKS["mizu"]
    # 1案目: 全消滅→fb[0] / 2案目: 部分除去 / 3案目: 全消滅→fb[1](ローテは消滅案のみ進む)
    assert out[0]["text"] == fbs[0]
    assert out[1]["text"] == "今日ありがとう！"
    assert out[2]["text"] == fbs[1]
    assert [d["kind"] for d in out] == ["short", "mid", "long"]


def test_guard_drafts_tolerance_applies():
    code = "t_koiguard_7"
    koi_guard.add_ok(code, "C1")
    out = koi_guard.guard_drafts(code, [{"text": "ずっと一緒だよ"}])
    assert out[0]["text"] == "ずっと一緒だよ"


# ---- 9. linebot_meta "koi_guard_words" 上書き・壊れ正規表現スキップ ----

def test_meta_override_and_broken_regex_skipped():
    meta = [
        {"id": "X1", "re": "ぴよぴよ"},
        {"id": "BAD", "re": "("},        # 壊れた正規表現 → 黙ってスキップ
        {"re": "idなし"},                # id欠落 → patterns()で除外
        {"id": "Y1"},                    # re欠落 → patterns()で除外
    ]
    try:
        with db.conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS linebot_meta(k TEXT PRIMARY KEY, v TEXT)")
            c.execute(
                "INSERT INTO linebot_meta(k,v) VALUES('koi_guard_words',?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (json.dumps(meta, ensure_ascii=False),))
        pats = koi_guard.patterns()
        assert [p["id"] for p in pats] == ["X1", "BAD"]   # id/re両方あるものだけ
        # 上書き語彙で検出・既定語彙は不発・壊れ正規表現は例外を出さない
        assert koi_guard.hits("ぴよぴよだよ") == [("X1", "ぴよぴよ")]
        assert koi_guard.hits("ずっと一緒だよ") == []
    finally:
        with db.conn() as c:
            c.execute("DELETE FROM linebot_meta WHERE k='koi_guard_words'")
    # メタ削除後は既定に戻る(コンパイル済みキャッシュがソース比較で追随する)
    assert koi_guard.patterns() == koi_guard.DEFAULT_PATTERNS
    assert "C1" in _hit_ids("ずっと一緒だよ")


# ---- 10. general モードの代替文(別プロセス検証) ----

def test_guard_drafts_fallbacks_general_mode():
    code_str = (
        "import json\n"
        "from app import db, config, koi_guard\n"
        "db.init()\n"
        "assert config.MODE == 'general', config.MODE\n"
        "out = koi_guard.guard_drafts('t_koiguard_g1', "
        "[{'text': 'ずっと一緒だよ'}, {'text': '浮気は絶対しない'}, {'text': '一生の約束ね'}])\n"
        "print(json.dumps([d['text'] for d in out], ensure_ascii=False))\n"
    )
    rc, out, err = run_in_mode("general", code_str)
    assert rc == 0, f"stderr={err}"
    texts = json.loads(out.strip().splitlines()[-1])
    assert texts == koi_guard._FALLBACKS["general"]
    assert len(set(texts)) == 3
