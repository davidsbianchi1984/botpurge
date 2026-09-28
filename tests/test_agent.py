"""Done-for-you agent: consent and program compilation always run; real-browser runs are opt-in
(BOTPURGE_UI_TESTS=1), like the other browser tests."""
import os

import pytest

from botpurge.agent.programs import compile_steps, fill, program_for
from conftest import hdr
from test_personal_api import burst_export, flagged, ig_export, upload


def test_consent_is_required_and_recorded(client, user):
    c = client.get("/api/agent/consent/instagram", headers=hdr(user)).json()
    assert not c["consented"] and "suspend" in c["text"] and "Assisted" in c["text"]
    upload(client, user, ig_export(burst_export())[0])
    bots = flagged(client, user)[:2]
    body = {"platform": "instagram", "mode": "agent", "accounts": [{"account_id": b["account_id"], "direction": "follower"} for b in bots]}
    r = client.post("/api/removals", headers=hdr(user), json=body)
    assert r.status_code == 400 and "notice" in r.json()["detail"]
    client.post("/api/agent/consent/instagram", headers=hdr(user))
    assert client.post("/api/removals", headers=hdr(user), json=body).json()["mode"] == "agent"
    row = client.app.state.svc.db.one("SELECT consent_text FROM agent_consents")
    assert "Instagram does not allow automated actions" in row["consent_text"]  # exactly what they agreed to


def test_agent_is_protect_only(client, user, monkeypatch):
    monkeypatch.setenv("BOTPURGE_BETA", "0")
    from botpurge.plans import Plans

    Plans(client.app.state.svc.db).activate(user["_id"], "cleanup", payment_ref="p1")
    assert client.post("/api/agent/consent/instagram", headers=hdr(user)).status_code == 402


def test_programs():
    prog = fill(program_for("instagram", "unfollow"), {"profile_url": "https://www.instagram.com/lucy1/", "handle": "lucy1", "own_followers_url": ""})
    assert prog[0] == {"op": "goto", "url": "https://www.instagram.com/lucy1/"}
    assert compile_steps(["Open their profile.", "Tap 'Following'.", "Tap Unfollow to confirm."])[1:] == [
        {"op": "click", "text": "Following"}, {"op": "click", "text": "Unfollow"}]
    with pytest.raises(ValueError):
        program_for("instagram", "unfriend")


# ---- real browser -------------------------------------------------------------------

browser = pytest.mark.skipif(os.environ.get("BOTPURGE_UI_TESTS") != "1", reason="set BOTPURGE_UI_TESTS=1 to run browser tests")

PROFILE = """<!doctype html><html><body><h1>@{h}</h1>
<button id="f" onclick="document.getElementById('dlg').hidden=false">Following</button>
<div id="dlg" hidden><button onclick="document.getElementById('f').textContent='Follow';document.getElementById('dlg').hidden=true;window.unfollowed=(window.unfollowed||0)+1">Unfollow</button><button>Cancel</button></div>
</body></html>"""
BLOCKED = "<!doctype html><html><body><h2>Action Blocked</h2><p>We restrict certain activity to protect our community. Try again later.</p></body></html>"


@pytest.fixture
def page():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        exe = os.environ.get("CHROMIUM_PATH")
        br = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        pg = br.new_page()
        yield pg
        br.close()


@browser
def test_executor_unfollows_and_verifies(page):
    from botpurge.agent.runner import Executor, Pacing

    page.route("https://www.instagram.com/**", lambda r: r.fulfill(body=PROFILE.format(h="lucy1"), content_type="text/html"))
    ex = Executor(Pacing(0, 0, 0, 0, sleep=lambda s: None), timeout_ms=2000)
    res = ex.run(page, fill(program_for("instagram", "unfollow"), {"profile_url": "https://www.instagram.com/lucy1/", "handle": "lucy1", "own_followers_url": ""}))
    assert res.ok and res.verified and page.evaluate("window.unfollowed") == 1


@browser
def test_executor_stops_on_pushback_and_uses_planner(page):
    from botpurge.agent.runner import Executor, Pacing

    page.route("https://www.instagram.com/**", lambda r: r.fulfill(body=BLOCKED, content_type="text/html"))
    ex = Executor(Pacing(0, 0, 0, 0, sleep=lambda s: None), timeout_ms=1000)
    res = ex.run(page, [{"op": "goto", "url": "https://www.instagram.com/x/"}, {"op": "click", "text": "Following"}])
    assert not res.ok and res.blocked and "action blocked" in res.error.lower()
    # Planner fallback: the button was renamed, the planner finds it.
    page.unroute("https://www.instagram.com/**")
    page.route("https://www.instagram.com/**", lambda r: r.fulfill(body=PROFILE.format(h="a").replace(">Following<", ">Connected<"), content_type="text/html"))
    ex = Executor(Pacing(0, 0, 0, 0, sleep=lambda s: None), timeout_ms=800,
                  planner=lambda text, step: {"op": "click", "text": "Connected"} if "Connected" in text and step.get("text") == "Following" else None)
    res = ex.run(page, [{"op": "goto", "url": "https://www.instagram.com/a/"}, {"op": "click", "text": "Following"}, {"op": "click", "text": "Unfollow"}])
    assert res.ok and any("planner" in l for l in res.log)


@browser
def test_run_job_end_to_end_and_pause_on_block(client, user, page):
    from botpurge.agent.runner import Executor, Pacing

    svc = client.app.state.svc
    upload(client, user, ig_export(burst_export())[0])
    client.post("/api/agent/consent/instagram", headers=hdr(user))
    bots = flagged(client, user)[:4]
    job = client.post("/api/removals", headers=hdr(user), json={"platform": "instagram", "mode": "agent",
                      "accounts": [{"account_id": b["account_id"], "direction": b["direction"]} for b in bots]}).json()
    seen = []

    def serve(route):
        seen.append(route.request.url)
        route.fulfill(body=BLOCKED if len(seen) == 3 else PROFILE.format(h="x"), content_type="text/html")

    page.route("https://www.instagram.com/**", serve)
    # Note: removing a *follower* needs the own-followers page; these fixtures unfollow for simplicity.
    ex = Executor(Pacing(0, 0, 0, 0, sleep=lambda s: None), timeout_ms=1500)
    out = svc.agent.run_job(user["_id"], job["id"], page, executor=ex, custom_steps={"remove_follower": ["Open their profile.", "Tap 'Following'.", "Tap 'Unfollow'."]})
    assert out["counts"].get("pending", 0) + out["counts"].get("removed", 0) == 2
    assert out["counts"].get("failed") == 1 and out["state"] == "paused" and "blocked" in out["stopped"]
    assert any(a["kind"] == "agent" and "pushed back" in a["text"] for a in client.get("/api/alerts", headers=hdr(user)).json())
