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

    page.route("https://www.instagram.com/**", lambda r: r.fulfill(body=PROFILE.format(h="lucy1"), content_type="text/html; charset=utf-8"))
    ex = Executor(Pacing(0, 0, 0, 0, sleep=lambda s: None), timeout_ms=2000)
    res = ex.run(page, fill(program_for("instagram", "unfollow"), {"profile_url": "https://www.instagram.com/lucy1/", "handle": "lucy1", "own_followers_url": ""}))
    assert res.ok and res.verified and page.evaluate("window.unfollowed") == 1


@browser
def test_executor_stops_on_pushback_and_uses_planner(page):
    from botpurge.agent.runner import Executor, Pacing

    page.route("https://www.instagram.com/**", lambda r: r.fulfill(body=BLOCKED, content_type="text/html; charset=utf-8"))
    ex = Executor(Pacing(0, 0, 0, 0, sleep=lambda s: None), timeout_ms=1000)
    res = ex.run(page, [{"op": "goto", "url": "https://www.instagram.com/x/"}, {"op": "click", "text": "Following"}])
    assert not res.ok and res.blocked and "action blocked" in res.error.lower()
    # Planner fallback: the button was renamed, the planner finds it.
    page.unroute("https://www.instagram.com/**")
    page.route("https://www.instagram.com/**", lambda r: r.fulfill(body=PROFILE.format(h="a").replace(">Following<", ">Connected<"), content_type="text/html; charset=utf-8"))
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


# ---- shortcuts, multi-screen navigation, reports --------------------------------------

def test_compiler_understands_shortcuts_windows_and_wizards():
    from botpurge.agent.runner import Executor

    prog = compile_steps([
        "Open their profile.",
        'Press Ctrl+K, then type "lucy1" in the Search box, then press Enter.',
        "Hover over the name, then tap Options.",
        'Scroll down until you see "Report", then tap "Report".',
        "Select \"It's spam\" from the \"Reason\" dropdown.",
        "Tap Submit, then accept the confirmation.",
        'Switch to the new window. Tap "Done". Close this tab. Go back to the main window.',
        "Tap 'Next' if it's shown.",
        "Hit Esc.", "Press Remove to confirm.",
    ])
    ops = [(p["op"], p.get("keys") or p.get("text") or p.get("value") or p.get("option") or p.get("until") or p.get("to") or p.get("action") or "")
           for p in prog]
    assert ops == [("goto", ""), ("press", "Ctrl+K"), ("type", "lucy1"), ("press", "Enter"), ("hover", "name"), ("click", "Options"),
                   ("scroll", "Report"), ("click", "Report"), ("select", "It's spam"), ("click", "Submit"), ("dialog", "accept"),
                   ("switch", "new"), ("click", "Done"), ("close", ""), ("switch", "main"), ("click", "Next"), ("press", "Esc"),
                   ("click", "Remove")]
    assert next(p for p in prog if p.get("text") == "Next")["optional"] is True
    assert Executor.key_combo("cmd + shift + p") == "Meta+Shift+p" and Executor.key_combo("Esc") == "Escape"


def test_report_guards(client, user):
    upload(client, user, ig_export(burst_export())[0])
    svc = client.app.state.svc
    rows = flagged(client, user)
    sus = rows[0]  # the Instagram burst bots are rated Suspicious, not Likely bot
    body = lambda r, **kw: {"platform": "instagram", "mode": "guided", "accounts": [{"account_id": r["account_id"], "direction": r["direction"], "action": "report", **kw}]}  # noqa: E731
    r = client.post("/api/removals", headers=hdr(user), json=body(sus))
    assert r.status_code == 400 and "Likely bot" in r.json()["detail"]
    client.post("/api/feedback", headers=hdr(user), json={"platform": "instagram", "account_id": sus["account_id"], "is_bot": True})
    assert client.post("/api/removals", headers=hdr(user), json=body(sus, reason="nonsense")).status_code == 400
    job = client.post("/api/removals", headers=hdr(user), json=body(sus, reason="fake")).json()
    assert job["items"][0]["action"] == "report" and job["items"][0]["reason"] == "fake"
    g = client.get(f"/api/removals/{job['id']}/guided", headers=hdr(user)).json()
    assert "Report" in " ".join(g["steps"]["report"]["steps"])
    client.post(f"/api/removals/{job['id']}/items/0/confirm", headers=hdr(user), json={"outcome": "done"})
    j = client.get(f"/api/removals/{job['id']}", headers=hdr(user)).json()
    assert j["counts"] == {"reported": 1}
    # Still in their lists (reporting isn't removing), and never reported twice.
    assert svc.db.one("SELECT status FROM flags WHERE account_id=?", (sus["account_id"],))["status"] != "removed"
    assert "already reported" in client.post("/api/removals", headers=hdr(user), json=body(sus)).json()["detail"]
    assert client.get("/api/undo-log?event=reported", headers=hdr(user)).json()


REPORT_WIZARD = """<!doctype html><html><body><h1>@bot</h1>
<button onclick="document.getElementById('menu').hidden=false">…</button>
<div id="menu" hidden role="menu"><button role="menuitem" onclick="document.getElementById('w1').hidden=false">Report</button></div>
<div id="w1" hidden><button onclick="document.getElementById('w2').hidden=false">Report account</button></div>
<div id="w2" hidden role="listbox"><div role="option" onclick="window.reason='spam';document.getElementById('w3').hidden=false">It's spam</div>
  <div role="option" onclick="window.reason='fake';document.getElementById('w3').hidden=false">It's a fake account</div></div>
<div id="w3" hidden><button onclick="window.submitted=true;document.body.insertAdjacentHTML('beforeend','<p>Thanks for reporting</p>')">Submit report</button></div>
</body></html>"""

SHORTCUT_PAGE = """<!doctype html><html><body><h1>Inbox</h1>
<input placeholder="Search" id="q" hidden>
<a href="https://www.instagram.com/popup" target="_blank">Open details</a>
<button onclick="if (confirm('Really remove?')) window.removed=true">Remove</button>
<script>document.addEventListener('keydown', e => { if (e.ctrlKey && e.key === 'k') { const q = document.getElementById('q'); q.hidden = false; q.focus(); }
  if (e.key === 'Enter' && document.activeElement.id === 'q') window.searched = document.getElementById('q').value; });</script>
<div style="height:3000px"></div><button onclick="window.far=true">Far away button</button>
</body></html>"""
POPUP = "<!doctype html><html><body><h2>Details</h2><button onclick=\"window.done=true\">Done</button></body></html>"


@browser
def test_executor_runs_a_report_wizard(page):
    from botpurge.agent.runner import Executor, Pacing

    page.route("https://www.instagram.com/**", lambda r: r.fulfill(body=REPORT_WIZARD, content_type="text/html; charset=utf-8"))
    ex = Executor(Pacing(0, 0, 0, 0, sleep=lambda s: None), timeout_ms=1500)
    prog = fill(program_for("instagram", "report"), {"profile_url": "https://www.instagram.com/bot/", "handle": "bot", "own_followers_url": "", "reason": "fake"})
    res = ex.run(page, prog)
    assert res.ok, res.error
    assert page.evaluate("window.reason") == "fake" and page.evaluate("window.submitted") is True


@browser
def test_executor_shortcuts_scroll_dialogs_and_windows(page):
    from botpurge.agent.runner import Executor, Pacing

    def serve(route):
        route.fulfill(body=POPUP if route.request.url.endswith("/popup") else SHORTCUT_PAGE, content_type="text/html; charset=utf-8")

    page.context.route("https://www.instagram.com/**", serve)
    ex = Executor(Pacing(0, 0, 0, 0, sleep=lambda s: None), timeout_ms=2000)
    prog = compile_steps(["Open the profile.",
                          'Press Ctrl+K, then type "lucy1" in the Search box, then press Enter.',
                          'Scroll down until you see "Far away button", then click "Far away button".',
                          "Click Remove, then accept the confirmation.",
                          'Click "Open details". Switch to the new window. Tap "Done". Close this tab.',
                          "Tap 'Not here' if it's shown."])
    res = ex.run(page, fill(prog, {"profile_url": "https://www.instagram.com/me/", "handle": "", "own_followers_url": ""}))
    assert res.ok, (res.error, res.log)
    assert page.evaluate("window.searched") == "lucy1" and page.evaluate("window.far") is True and page.evaluate("window.removed") is True
    assert any("optional" in line for line in res.log)


def test_block_by_choice(client, user):
    upload(client, user, ig_export(burst_export())[0])
    rows = flagged(client, user)
    a, b = rows[0], rows[1]
    # Blocking is your own choice for any flagged account (it only affects you), and it can go with a report.
    client.post("/api/feedback", headers=hdr(user), json={"platform": "instagram", "account_id": a["account_id"], "is_bot": True})
    job = client.post("/api/removals", headers=hdr(user), json={"platform": "instagram", "mode": "guided", "accounts": [
        {"account_id": a["account_id"], "direction": a["direction"], "action": "report", "reason": "spam"},
        {"account_id": a["account_id"], "direction": a["direction"], "action": "block"},
        {"account_id": b["account_id"], "direction": b["direction"], "action": "block"}]}).json()
    assert [i["action"] for i in job["items"]] == ["report", "block", "block"]
    g = client.get(f"/api/removals/{job['id']}/guided", headers=hdr(user)).json()
    assert "Block" in " ".join(g["steps"]["block"]["steps"])
    for idx in (0, 1, 2):
        client.post(f"/api/removals/{job['id']}/items/{idx}/confirm", headers=hdr(user), json={"outcome": "done"})
    j = client.get(f"/api/removals/{job['id']}", headers=hdr(user)).json()
    assert j["counts"] == {"reported": 1, "blocked": 2} and j["state"] == "done"
    assert {r["event"] for r in client.get("/api/undo-log", headers=hdr(user)).json()} >= {"blocked", "reported"}
    # Blocked accounts wait for the next scan to confirm they've gone, like removals.
    pending = client.get("/api/flags?platform=instagram&tab=pending", headers=hdr(user)).json()
    assert {r["account_id"] for r in pending} >= {a["account_id"], b["account_id"]}


def test_block_programs_and_instructions_everywhere():
    from botpurge.instructions import PLATFORM_ACTIONS, builtin_sets

    have = {(s.platform, s.device, s.action) for s in builtin_sets()}
    for platform, actions in PLATFORM_ACTIONS.items():
        assert "block" in actions and "report" in actions
        assert {(platform, d, "block") for d in ("web", "ios", "android")} <= have
        prog = program_for(platform, "block")
        assert prog[0]["op"] == "goto" and any("Block" in (s.get("text", "") + " ".join(s.get("any", []))) for s in prog)


BLOCK_PAGE = """<!doctype html><html><body><h1>@bot</h1>
<button aria-label="Options" onclick="document.getElementById('m').hidden=false">…</button>
<div id="m" hidden><button onclick="document.getElementById('c').hidden=false">Block</button></div>
<div id="c" hidden><p>Block bot?</p><button onclick="window.blocked=true">Block bot</button></div></body></html>"""


@browser
def test_executor_blocks(page):
    from botpurge.agent.runner import Executor, Pacing

    page.route("https://www.instagram.com/**", lambda r: r.fulfill(body=BLOCK_PAGE, content_type="text/html; charset=utf-8"))
    ex = Executor(Pacing(0, 0, 0, 0, sleep=lambda s: None), timeout_ms=1500)
    res = ex.run(page, fill(program_for("instagram", "block"), {"profile_url": "https://www.instagram.com/bot/", "handle": "bot", "own_followers_url": ""}))
    assert res.ok, (res.error, res.log)
    assert page.evaluate("window.blocked") is True
