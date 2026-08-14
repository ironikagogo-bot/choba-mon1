"""一般モード分岐(v191監査A/B系)のテスト。

観点:
- general起動で healthz ver=v191 / /api/liff/hello mode=general
- sittings.orei_text: general では夜職語彙(同伴・アフター・お席・来店・お店・ヘルプ・ママ)を
  出さない(v191その2 一般B1)。mizu では夜職文言のまま(退行なし)。
- sittings.role_label: general は ROLE_LABEL_GENERAL(中立語)、mizu は従来ラベル(一般A4)。
- koi_guard._FALLBACKS: general は敬体寄り(です・ます)、mizu は口語のまま。
  guard_drafts の全文消滅時フォールバックがモード別リストをローテーションする。
- campaign の生成プロンプト: general は「銀座の一流ホステス」「来店」を出さない(v158)。

config.MODE は import 時決定のため、general 固有の検証は conftest.run_in_mode で
別プロセス起動して行う(規約どおり)。mizu 側は同一プロセスで直接検証。
"""
import json

from tests.conftest import run_in_mode

# 一般モードの送信文・画面ラベルに出てはいけない夜職語彙(v191 B1/A4)
NIGHT_WORDS_TEXT = ["同伴", "アフター", "お席", "来店", "お店", "ヘルプ", "ママ", "ホステス", "銀座"]
NIGHT_WORDS_LABEL = ["客", "ヘルプ", "ママ", "アフター"]


# ---------- mizu(既定・同一プロセス): 退行なし確認 ----------

def test_mode_default_is_mizu_and_healthz_v191(client):
    from app import config
    assert config.MODE == "mizu"
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    from app.main import APP_VER
    assert body["ver"] == APP_VER


def test_liff_hello_reports_mizu_mode(client):
    # /api/liff/hello は認証不要の起動確認。mode=表示プロファイル(v157)
    r = client.get("/api/liff/hello")
    assert r.status_code == 200
    assert r.json()["mode"] == "mizu"


def test_mizu_orei_customer_keeps_night_vocab(client):
    """mizu の主賓客向け御礼は同伴・お席・アフターの夜職文言のまま(退行なし)。"""
    from app import sittings
    t = sittings.orei_text("customer", "senior", "田中さん", "田中さん",
                           stype="", venue="", dohan_venue="鮨処すず", after_venue="Bar K")
    assert "同伴の鮨処すず" in t
    assert "お席" in t
    assert "アフター" in t
    assert "心よりお待ちしております" in t


def test_mizu_orei_gaiso_invites_to_store(client):
    """mizu の店外のみ(gaiso)は締めが「今度はお店にもぜひ。」(営業導線)のまま。"""
    from app import sittings
    t = sittings.orei_text("customer", "equal", "佐藤さん", "佐藤さん",
                           stype="gaiso", venue="ゴルフ")
    assert "今度はお店にもぜひ" in t


def test_mizu_role_labels_unchanged(client):
    from app import sittings
    assert sittings.role_label("customer") == "主賓客"
    assert sittings.role_label("help") == "ヘルプ"
    assert sittings.role_label("report") == "担当ママへ共有"
    assert sittings.role_label("after") == "アフター先のお店"
    assert sittings.role_label("unknown_role") == "unknown_role"   # 未知はそのまま


def test_koi_guard_fallbacks_mizu_casual_and_distinct(client):
    """mizu のフォールバック: 3案・全て異なる・口語(です/ます無し)。後方互換_FALLBACKは先頭案。"""
    from app import koi_guard
    fbs = koi_guard._FALLBACKS["mizu"]
    assert len(fbs) == 3
    assert len(set(fbs)) == 3
    assert all(("です" not in f and "ます" not in f) for f in fbs)
    assert koi_guard._FALLBACK["mizu"] == fbs[0]


def test_koi_guard_fallbacks_general_polite(client):
    """general のフォールバック: 3案・全て異なる・敬体寄り(です/ます)で mizu と別文。"""
    from app import koi_guard
    fbs = koi_guard._FALLBACKS["general"]
    assert len(fbs) == 3
    assert len(set(fbs)) == 3
    polite = sum(1 for f in fbs if ("です" in f or "ます" in f))
    assert polite >= 2   # 注: 先頭案「…休めてる？」のみ口語(実装どおり)
    assert not set(fbs) & set(koi_guard._FALLBACKS["mizu"])
    assert koi_guard._FALLBACK["general"] == fbs[0]


def test_guard_drafts_mizu_rotates_mizu_fallbacks(client):
    """mizu 実行: 全文が抑制対象の3案 → mizu フォールバック3案が順に割り当たり同文連投しない。"""
    from app import koi_guard
    drafts = [{"text": "ずっと一緒だよ"}, {"text": "浮気なんか絶対しない"},
              {"text": "一生そばにいるって約束する"}]
    out = koi_guard.guard_drafts("t_general_mode_1", drafts)
    texts = [d["text"] for d in out]
    assert texts == koi_guard._FALLBACKS["mizu"]
    assert len(set(texts)) == 3


def test_mizu_campaign_prompts_keep_sales_wording(client):
    from app import campaign
    assert "銀座の一流ホステス" in campaign.GREETING_SYSTEM
    assert "来店" in campaign.THANKS_SYSTEM


# ---------- general(別プロセス・run_in_mode) ----------

def test_general_healthz_v191_and_hello_mode():
    """general 起動でも healthz ver=v191、/api/liff/hello が mode=general を返す。"""
    code = """
import json
from app import news
news._SLEEP = False
news._fetch_rss = lambda query, require_in_title="": []
from fastapi.testclient import TestClient
from app.main import app
with TestClient(app) as c:
    h = c.get("/healthz").json()
    hello = c.get("/api/liff/hello").json()
print(json.dumps({"h": h, "hello": hello}))
"""
    rc, out, err = run_in_mode("general", code)
    assert rc == 0, f"stderr={err}"
    d = json.loads(out.strip().splitlines()[-1])
    assert d["h"]["ok"] is True
    from app.main import APP_VER
    assert d["h"]["ver"] == APP_VER
    assert d["hello"]["mode"] == "general"


def test_general_orei_no_night_vocab_all_roles():
    """v191その2(一般B1): general の御礼文は全役割×立場×同伴/アフター/店外の組合せで
    夜職語彙を一切含まない。"""
    code = """
import json
from app import sittings
roles = ["customer", "intro", "guest", "after", "peer", "help", "report"]
stands = ["senior", "equal", "junior"]
combos = [
    dict(stype="", venue="", dohan_venue="", after_venue=""),
    dict(stype="", venue="", dohan_venue="鮨処すず", after_venue="Bar K"),
    dict(stype="gaiso", venue="ゴルフ", dohan_venue="", after_venue=""),
]
texts = []
for r in roles:
    for st in stands:
        for kw in combos:
            texts.append(sittings.orei_text(r, st, "田中さん", "田中さん", **kw))
print(json.dumps(texts, ensure_ascii=False))
"""
    rc, out, err = run_in_mode("general", code)
    assert rc == 0, f"stderr={err}"
    texts = json.loads(out.strip().splitlines()[-1])
    assert texts and all(isinstance(t, str) and t for t in texts)
    for t in texts:
        for w in NIGHT_WORDS_TEXT:
            assert w not in t, f"夜職語彙 {w!r} が general 御礼に混入: {t!r}"


def test_general_orei_customer_uses_neutral_phrasing():
    """general の主賓向け: 同伴→「一次会の◯◯」、アフター→「◯◯の二次会」、
    gaiso締めは「またぜひ誘ってください」(営業導線なし)。"""
    code = """
import json
from app import sittings
a = sittings.orei_text("customer", "equal", "田中さん", "田中さん",
                       dohan_venue="鮨処すず", after_venue="Bar K")
b = sittings.orei_text("customer", "equal", "佐藤さん", "佐藤さん",
                       stype="gaiso", venue="ゴルフ")
print(json.dumps({"a": a, "b": b}, ensure_ascii=False))
"""
    rc, out, err = run_in_mode("general", code)
    assert rc == 0, f"stderr={err}"
    d = json.loads(out.strip().splitlines()[-1])
    assert "一次会の鮨処すず" in d["a"]
    assert "Bar Kの二次会" in d["a"]
    assert "またぜひ誘ってください" in d["b"]
    assert "お店" not in d["b"]


def test_general_role_labels_neutral():
    """v191その2(一般A4): general の役割ラベルは ROLE_LABEL_GENERAL(中立語)。"""
    code = """
import json
from app import sittings
roles = ["customer", "intro", "guest", "peer", "after", "help", "report"]
print(json.dumps({r: sittings.role_label(r) for r in roles}, ensure_ascii=False))
"""
    rc, out, err = run_in_mode("general", code)
    assert rc == 0, f"stderr={err}"
    labels = json.loads(out.strip().splitlines()[-1])
    assert labels == {"customer": "主賓", "intro": "紹介者", "guest": "同席の方",
                      "peer": "同席の同僚", "after": "二次会のお店",
                      "help": "手伝ってくれた人", "report": "上司へ共有"}
    for r, lab in labels.items():
        for w in NIGHT_WORDS_LABEL:
            assert w not in lab, f"夜職語彙 {w!r} が general ラベルに混入: {r}={lab!r}"


def test_general_guard_drafts_uses_general_fallbacks():
    """general 実行の guard_drafts: 全文消滅時は general のフォールバック3案をローテーション。"""
    code = """
import json
from app import koi_guard
drafts = [{"text": "ずっと一緒だよ"}, {"text": "浮気なんか絶対しない"},
          {"text": "一生そばにいるって約束する"}]
out = koi_guard.guard_drafts("t_general_mode_2", drafts)
print(json.dumps({"texts": [d["text"] for d in out],
                  "fbs": koi_guard._FALLBACKS["general"]}, ensure_ascii=False))
"""
    rc, out, err = run_in_mode("general", code)
    assert rc == 0, f"stderr={err}"
    d = json.loads(out.strip().splitlines()[-1])
    assert d["texts"] == d["fbs"]
    assert len(set(d["texts"])) == 3


def test_general_campaign_prompts_rewritten():
    """v158: general の営業プロンプトはホステス・来店語彙を挨拶・近況に書き換え済み。"""
    code = """
import json
from app import campaign
print(json.dumps({
    "greet_hostess": "ホステス" in campaign.GREETING_SYSTEM,
    "greet_kai": "挨拶・近況LINE" in campaign.GREETING_SYSTEM,
    "thanks_raiten": "来店" in campaign.THANKS_SYSTEM,
    "thanks_saikai": "再会" in campaign.THANKS_SYSTEM,
}, ensure_ascii=False))
"""
    rc, out, err = run_in_mode("general", code)
    assert rc == 0, f"stderr={err}"
    d = json.loads(out.strip().splitlines()[-1])
    assert d["greet_hostess"] is False
    assert d["greet_kai"] is True
    assert d["thanks_raiten"] is False
    assert d["thanks_saikai"] is True


def test_general_config_mode_import_time():
    """config.MODE は環境変数から import 時決定(general が反映される)。"""
    rc, out, err = run_in_mode("general", "from app import config; print(config.MODE)")
    assert rc == 0, f"stderr={err}"
    assert out.strip() == "general"
