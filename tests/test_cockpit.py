"""The agent cockpit: visible cursor, take over and resume, ask for help, teach mode.

The "person" is simulated with real (trusted) mouse and keyboard input while the agent waits.
Opt-in like the other browser tests: BOTPURGE_UI_TESTS=1.
"""
import os

import pytest

from botpurge.agent.cockpit import Cockpit, learned_program, steps_from_recording
from botpurge.agent.programs import compile_steps, fill
from botpurge.agent.runner import Executor, Pacing

browser = pytest.mark.skipif(os.environ.get("BOTPURGE_UI_TESTS") != "1", reason="set BOTPURGE_UI_TESTS=1")


@pytest.fixture
def page():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        exe = os.environ.get("CHROMIUM_PATH")
        br = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        pg = br.new_context(viewport={"width": 1200, "height": 700}).new_page()
        yield pg
        br.close()


def test_recording_becomes_editable_steps_that_compile_back():
    raw = [{"op": "click", "text": "Options"}, {"op": "click", "text": "Options"}, {"op": "click", "text": "Block"},
           {"op": "type", "value": "l", "placeholder": "Search"}, {"op": "type", "value": "lucy1", "placeholder": "Search", "flush": True},
           {"op": "press", "keys": "Enter"}, {"op": "type", "value": "lucy1", "placeholder": "Search"},
           {"op": "secret"}, {"op": "press", "keys": "Ctrl+K"}, {"op": "select", "option": "It's spam", "label": "Reason"},
           {"op": "switch", "to": "new"}, {"op": "click", "text": "Done"}, {"op": "close"}, {"op": "dialog", "action": "accept"}]
    lines = steps_from_recording(raw, {"handle": "@Lucy1"})
    assert lines == ['Open their profile.', 'Click "Options".', 'Click "Block".', 'Type "{handle}" in the "Search" box.',
                     'Press Enter.', 'Press Ctrl+K.', 'Select "It\'s spam" from the "Reason" dropdown.', 'Switch to the new window.',
                     'Click "Done".', 'Close this tab.', 'Go back to the main window.', 'Accept the confirmation.']
    ops = [s["op"] for s in compile_steps(lines)]
    assert ops == ["goto", "click", "click", "type", "press", "press", "select", "switch", "click", "close", "switch", "dialog"]


def test_learned_labels_become_alternatives(svc):
    prog = [{"op": "goto", "url": "{profile_url}"}, {"op": "click", "text": "Remove"}]
    assert learned_program(prog, {1: "Delete follower"})[1]["any"] == ["Delete follower", "Remove"]
    uid = svc.personal.register("a@b.co")[0]
    svc.agent.learn(uid, "instagram", "remove_follower", 1, "Delete follower")
    assert svc.agent.learned(uid, "instagram") == {"remove_follower": {1: "Delete follower"}}


PAGE = """<!doctype html><html><body><h1>@lucy1</h1>
<button onclick="window.one=true">Step one</button> <button onclick="window.two=true">Step two</button>
<button onclick="window.deleted=true">Delete follower</button>
<div style="height:400px"></div></body></html>"""


def _page(page, html=PAGE):
    page.route("https://www.instagram.com/**", lambda r: r.fulfill(body=html, content_type="text/html; charset=utf-8"))
    cp = Cockpit(page.context, help_timeout=10).install()
    return cp


@browser
def test_person_takes_over_and_hands_back(page):
    cp = _page(page)
    seen = []

    def between_steps(_s):             # the person clicks the page while the agent is between steps
        if not seen:
            page.mouse.click(600, 350)
            seen.append("clicked")

    def person(pg, state):
        seen.append(state)
        if state == "paused" and seen.count("paused") == 3:
            assert "in control" in pg.inner_text("#__bpBar")
            pg.click("#__bpGo")        # Resume

    cp.person = person
    ex = Executor(Pacing(0, 0, 0, 0, sleep=between_steps), timeout_ms=2000, cockpit=cp)
    prog = [{"op": "goto", "url": "https://www.instagram.com/lucy1/"}, {"op": "click", "text": "Step one"}, {"op": "click", "text": "Step two"}]
    res = ex.run(page, prog)
    assert res.ok, res.error
    assert "paused" in seen and page.evaluate("window.one && window.two")
    assert page.is_visible("#__bpCur")                    # the agent's own cursor is on screen


@browser
def test_agent_asks_for_help_and_learns(page):
    cp = _page(page)

    def person(pg, state):
        if state == "help":
            assert "Remove" in pg.inner_text("#__bpBar")
            pg.get_by_text("Delete follower").click()          # show the agent the renamed button

    cp.person = person
    ex = Executor(Pacing(0, 0, 0, 0, sleep=lambda s: None), timeout_ms=600, cockpit=cp)
    res = ex.run(page, [{"op": "goto", "url": "https://www.instagram.com/lucy1/"}, {"op": "click", "text": "Remove"}])
    assert res.ok, res.error
    assert res.learned == {1: "Delete follower"} and page.evaluate("window.deleted")
    # Next time the learned label is tried first, so no help is needed.
    page.evaluate("window.deleted = false")
    cp.person = lambda pg, st: pytest.fail("should not need help again")
    res = ex.run(page, learned_program([{"op": "goto", "url": "https://www.instagram.com/lucy2/"}, {"op": "click", "text": "Remove"}], res.learned))
    assert res.ok and page.evaluate("window.deleted")


@browser
def test_stop_ends_the_run(page):
    from botpurge.agent.cockpit import Stopped

    cp = _page(page)
    cp.person = lambda pg, st: pg.click("#__bpStop") if st == "help" else None
    ex = Executor(Pacing(0, 0, 0, 0, sleep=lambda s: None), timeout_ms=400, cockpit=cp)
    with pytest.raises(Stopped):
        ex.run(page, [{"op": "goto", "url": "https://www.instagram.com/lucy1/"}, {"op": "click", "text": "Nowhere"}])


TEACH = """<!doctype html><html><body><h1>@lucy1</h1>
<input placeholder="Search"> <input type="password" placeholder="Password">
<select aria-label="Reason"><option>Pick one</option><option>It's spam</option></select>
<button onclick="document.getElementById('m').hidden=false">Options</button>
<div id="m" hidden><button onclick="window.blocked=true">Block</button></div>
<script>document.addEventListener('keydown', e => { if (e.ctrlKey && e.key === 'k') window.palette = (window.palette || 0) + 1; });</script>
</body></html>"""


@browser
def test_teach_mode_records_steps_the_agent_can_replay(page):
    cp = _page(page, TEACH)
    done = []

    def person(pg, state):
        if state != "rec" or done:
            return
        done.append(1)
        pg.get_by_placeholder("Password").fill("hunter2")            # never recorded
        pg.get_by_placeholder("Password").press("Tab")
        pg.get_by_placeholder("Search").click()
        pg.keyboard.type("lucy1")
        pg.keyboard.press("Enter")
        pg.keyboard.press("Control+k")
        pg.get_by_label("Reason").select_option("It's spam")
        pg.get_by_text("Options").click()
        pg.get_by_text("Block").click()
        pg.click("#__bpDone")

    cp.person = person
    raw = cp.record(page, start_url="https://www.instagram.com/lucy1/")
    lines = steps_from_recording(raw, {"handle": "lucy1"})
    assert "hunter2" not in " ".join(lines)
    assert lines == ['Open their profile.', 'Type "{handle}" in the "Search" box.', 'Press Enter.', 'Press Ctrl+K.',
                     'Select "It\'s spam" from the "Reason" dropdown.', 'Click "Options".', 'Click "Block".'], lines
    # The agent replays the lesson on another account.
    page.evaluate("window.blocked = false")
    cp.person = None
    ex = Executor(Pacing(0, 0, 0, 0, sleep=lambda s: None), timeout_ms=2000, cockpit=cp)
    res = ex.run(page, fill(compile_steps(lines), {"profile_url": "https://www.instagram.com/lucy2/", "handle": "lucy2", "own_followers_url": ""}))
    assert res.ok, (res.error, res.log)
    assert page.evaluate("window.blocked") and page.evaluate("window.palette") == 1
    assert page.get_by_placeholder("Search").input_value() == "lucy2"


LOGIN = """<!doctype html><html><body><h1>Log in</h1>
<form onsubmit="event.preventDefault(); window.sent = [this.username.value, this.p.value]; location.href = window.needCode ? '/2fa' : '/';">
<input name="username" autocomplete="username"><input name="p" type="password"><button>Log in</button></form>
<script>window.needCode = new URLSearchParams(location.search).has('code') || sessionStorage.code;</script></body></html>"""
HOME_IN = "<!doctype html><html><body><h1>For You</h1><button>Profile</button></body></html>"
CODE = "<!doctype html><html><body><h1>Enter the 6-digit code</h1><input name='otp'></body></html>"


def _tiktok(page, code=False):
    def serve(r):
        path = r.request.url.split("tiktok.com", 1)[1]
        html = CODE if path.startswith("/2fa") else LOGIN if "/login" in path else HOME_IN
        if code and "/login" in path:
            html = html.replace("window.needCode = ", "window.needCode = true || ")
        r.fulfill(body=html, content_type="text/html; charset=utf-8")
    page.route("https://www.tiktok.com/**", serve)


@browser
def test_saved_sign_in_types_into_the_login_page(page):
    from botpurge.agent.signin import ensure_signed_in, needs_sign_in

    _tiktok(page)
    cp = Cockpit(page.context).install()
    page.goto("https://www.tiktok.com/login/phone-or-email/email")
    assert needs_sign_in(page)
    assert ensure_signed_in(page, "tiktok", {"username": "david", "password": "pw1"}, cp) is True    # home shows no login: nothing to do
    from botpurge.agent.signin import sign_in
    sent = []
    page.expose_function("__sent", lambda u, p: sent.append((u, p)))
    page.add_init_script("document.addEventListener('submit', e => window.__sent(e.target.username.value, e.target.p.value), true)")
    assert sign_in(page, "tiktok", {"username": "david", "password": "pw1"}, cp, settle_ms=500)
    assert sent == [("david", "pw1")] and cp.state == "run"                                   # the agent's own typing isn't the person taking over
    assert not needs_sign_in(page)


@browser
def test_sign_in_code_is_handed_to_the_person(page):
    from botpurge.agent.signin import sign_in

    _tiktok(page, code=True)
    cp = Cockpit(page.context, help_timeout=5).install()
    seen = []

    def person(pg, state):
        if state == "paused" and not seen:
            seen.append(pg.evaluate("document.getElementById('__bpMsg').textContent"))
            pg.goto("https://www.tiktok.com/")               # they typed the code and got in
            cp._on({"type": "resume"})

    cp.person = person
    assert sign_in(page, "tiktok", {"username": "david", "password": "pw1"}, cp, settle_ms=500)
    assert seen and "Finish signing in" in seen[0]
