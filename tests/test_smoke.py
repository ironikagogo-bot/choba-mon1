"""ハーネスの煙テスト: client起動・mk_contact・run_in_mode の3本。"""
from tests.conftest import mk_contact, run_in_mode


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    from app.main import APP_VER
    assert r.json() == {"ok": True, "ver": APP_VER}   # 版数固定にしない(梱包ごとに更新されるため)


def test_mk_contact(client, tok):
    card = mk_contact(client, tok, "t_smoke_1", rank="A", cycle_days=14,
                      tags="VIP", birthday="07-20")
    assert card is not None
    assert card["code"] == "t_smoke_1"
    assert card["rank"] == "A"
    assert card["cycle_days"] == 14
    # kind指定つき
    card2 = mk_contact(client, tok, "t_smoke_2", kind="staff")
    assert card2["kind"] == "staff"


def test_run_in_mode_general():
    rc, out, err = run_in_mode(
        "general",
        "from app import config; print(config.MODE)")
    assert rc == 0, f"stderr: {err}"
    assert out.strip() == "general"
