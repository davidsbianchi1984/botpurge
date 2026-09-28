from conftest import hdr
from test_personal_api import burst_export, flagged, ig_export, upload


def test_catalog_prices():
    from botpurge.plans import catalog

    c = {p["id"]: p for p in catalog()["plans"]}
    assert c["cleanup"]["price"] == "$20 one-time" and c["protect"]["price"] == "$60/month"
    assert "agent" in c["protect"]["features"] and "remove" in c["cleanup"]["features"]
    assert c["free"]["features"] == ["scan", "review"]


def test_beta_everything_free(client, user):
    cat = client.get("/api/plans").json()
    assert cat["beta"] and all(p["free_now"] for p in cat["plans"])
    me = client.get("/api/me/plan", headers=hdr(user)).json()
    assert me["plan"] == "free" and "agent" in me["features"]  # beta unlocks everything
    r = client.post("/api/me/plan", headers=hdr(user), json={"plan": "protect"}).json()
    assert r["plan"] == "protect" and r["renews_at"]


def test_scan_summary_counts_threats(client, user):
    s = client.get("/api/me/summary", headers=hdr(user)).json()
    assert s["threats"] == 0 and s["last_scan"] is None
    upload(client, user, ig_export(burst_export())[0])
    s = client.get("/api/me/summary", headers=hdr(user)).json()
    assert s["threats"] == 12 and s["categories"]["fake_followers"] == 12 and s["status"] in ("review", "at_risk")
    assert s["scanned"] == 52


def test_paywall_after_beta(client, user, monkeypatch):
    monkeypatch.setenv("BOTPURGE_BETA", "0")
    upload(client, user, ig_export(burst_export())[0])
    bots = flagged(client, user)[:2]
    body = {"platform": "instagram", "mode": "guided", "accounts": [{"account_id": b["account_id"], "direction": "follower"} for b in bots]}
    # Free users can scan and review (every reason shown) but not remove.
    assert client.get("/api/flags?tab=suspicious", headers=hdr(user)).status_code == 200
    r = client.post("/api/removals", headers=hdr(user), json=body)
    assert r.status_code == 402 and r.json()["plan"] == "cleanup" and "$20" in r.json()["detail"]
    assert client.get("/api/export.csv", headers=hdr(user)).status_code == 402
    # No payment, no plan.
    assert client.post("/api/me/plan", headers=hdr(user), json={"plan": "cleanup"}).status_code == 402
    # A completed payment unlocks it.
    from botpurge.plans import Plans

    Plans(client.app.state.svc.db).activate(user["_id"], "cleanup", payment_ref="pay_123")
    assert client.post("/api/removals", headers=hdr(user), json=body).status_code == 200
    me = client.get("/api/me/plan", headers=hdr(user)).json()
    assert "remove" in me["features"] and "agent" not in me["features"]
