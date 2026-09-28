from datetime import datetime, timedelta, timezone

from botpurge.models import Connection, Direction, Platform
from conftest import hdr

T0 = datetime(2026, 5, 1, tzinfo=timezone.utc)


def conns(extra_bots=0):
    real = [Connection(platform=Platform.x, account_id=f"r{i}", handle=f"friend.{i}", name=f"Friend {i}", direction=Direction.follower,
                       connected_at=T0 - timedelta(days=40 * i, hours=i), created_at=T0 - timedelta(days=900 + i),
                       followers_count=300, following_count=280, bio="hi") for i in range(30)]
    bots = [Connection(platform=Platform.x, account_id=f"b{i}", handle=f"lucy{48291730 + i}", name="Lucy", direction=Direction.follower,
                       connected_at=T0 + timedelta(minutes=i), created_at=T0 - timedelta(days=3), followers_count=1,
                       following_count=4000, bio="DM for crypto signals", has_default_avatar=True) for i in range(extra_bots)]
    return real + bots


def test_new_follower_screening_and_weekly_report(client, user):
    svc = client.app.state.svc
    uid = user["_id"]
    svc.personal.store_connections(uid, Platform.x, conns(0))
    svc.personal.scan(uid)
    assert not [a for a in client.get("/api/alerts", headers=hdr(user)).json() if a["kind"] == "new_threats"]
    svc.personal.store_connections(uid, Platform.x, conns(6))
    svc.personal.scan(uid)
    alerts = [a for a in client.get("/api/alerts", headers=hdr(user)).json() if a["kind"] == "new_threats"]
    assert alerts and alerts[0]["text"].startswith("6 new accounts in your x lists")

    r = client.get("/api/me/report", headers=hdr(user)).json()
    assert r["new_threats"] == 6 and "6 new threats found" in r["headline"]
    assert svc.tick()["reports"] == 1 and svc.tick()["reports"] == 0  # once a week
    assert any(a["kind"] == "weekly_report" for a in client.get("/api/alerts", headers=hdr(user)).json())
    assert client.post("/api/rescan", headers=hdr(user), json={"cadence": "daily"}).json()["rescan"] == "daily"


def test_daily_monitoring_is_protect_only(client, user, monkeypatch):
    monkeypatch.setenv("BOTPURGE_BETA", "0")
    assert client.post("/api/rescan", headers=hdr(user), json={"cadence": "daily"}).status_code == 402
    assert client.get("/api/me/report", headers=hdr(user)).status_code == 402
    assert client.post("/api/rescan", headers=hdr(user), json={"cadence": "weekly"}).status_code == 200
