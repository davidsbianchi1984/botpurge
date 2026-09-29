"""Live Guard as a live-stream moderator: the chat feed, and the agent moderating TikTok-style live chat."""
import os

import pytest

from botpurge.agent.cockpit import Cockpit
from botpurge.agent.livewatch import watch
from botpurge.agent.runner import Executor, Pacing
from botpurge.liveguard import ChatMessage
from botpurge import security as sec

browser = pytest.mark.skipif(os.environ.get("BOTPURGE_UI_TESTS") != "1", reason="set BOTPURGE_UI_TESTS=1")


def _session(svc, mode="protect"):
    uid = svc.personal.register("streamer@example.com")[0]
    return uid, svc.liveguard.start(uid, "tiktok", "Friday live", {"mode": mode}, connect=False)["id"]


def test_chat_feed_counts_everything_and_keeps_recent(svc):
    uid, sid = _session(svc)
    lg = svc.liveguard
    msgs = [ChatMessage(platform="tiktok", author_id=f"fan{i}", author_name=f"fan{i}", text=f"hello {i}", at=sec.now()) for i in range(310)]
    msgs.append(ChatMessage(platform="tiktok", author_id="free_gift_99", author_name="free_gift_99",
                            text="first 10 people to type WIN get $10,000 check my bio", at=sec.now()))
    out = lg.feed(uid, sid, msgs)
    bot = out[-1]
    assert bot["act"] and bot["event_id"] and bot["author_name"] == "free_gift_99"
    x = lg.session(uid, sid)
    assert x["checked"] == 311 and len(x["chat"]) == 80                     # every message counted, recent ones shown
    assert x["chat"][-1]["author_id"] == "free_gift_99" and x["chat"][-1]["action"] != "none"
    assert svc.db.one("SELECT COUNT(*) n FROM lg_chat WHERE session_id=?", (sid,))["n"] <= 301   # old chat is dropped
    assert "none" not in x["counts"]
    lg.mark_applied(uid, sid, bot["event_id"])
    assert svc.db.one("SELECT applied FROM lg_events WHERE id=?", (bot["event_id"],))["applied"] == 1


def test_watch_endpoint_rules(client, user):
    uid_h = {"Authorization": user["Authorization"]}
    sid = client.post("/api/liveguard/sessions", headers=uid_h, json={"platform": "tiktok", "channel": "live", "policy": {"mode": "protect"}}).json()["id"]
    assert client.post(f"/api/liveguard/sessions/{sid}/watch", headers=uid_h, json={"url": "https://evil.example/live"}).status_code == 400
    r = client.post(f"/api/liveguard/sessions/{sid}/watch", headers=uid_h, json={"url": "https://www.tiktok.com/@me/live"})
    assert r.status_code == 403 and "notice" in r.json()["detail"]           # the agent notice comes first


LIVE = """<!doctype html><html><body><h1>@me LIVE</h1><div id="chat"></div>
<div id="menu" hidden><button onclick="window.blocked.push(window.who); document.getElementById('confirm').hidden=false; this.parentNode.hidden=true">Block</button></div>
<div id="confirm" hidden><p>Block this person?</p><button onclick="this.parentNode.hidden=true">Block</button></div>
<script>
window.blocked = [];
const say = (who, text) => {
  const m = document.createElement('div'); m.setAttribute(ATTR_ITEM, '');
  const n = document.createElement('span'); n.setAttribute(ATTR_AUTHOR, ''); n.textContent = who;
  n.onclick = () => { window.who = who; document.getElementById('menu').hidden = false; };
  m.append(n, document.createTextNode(' ' + text)); document.getElementById('chat').append(m);
};
const lines = [["gamer_joe", "this stream is fire"], ["free_gift_99", "first 10 people to type WIN get $10,000 check my bio"],
               ["nana_b", "hi from Ohio!"], ["free_gift_98", "first 10 people to type WIN get $10,000 check my bio"]];
lines.forEach(([w, t], i) => setTimeout(() => say(w, t), 300 + i * 400));
</script></body></html>"""


def _live(page, item='data-e2e="chat-message"', author='data-e2e="message-owner-name"'):
    html = LIVE.replace("ATTR_ITEM", repr(item.split("=")[0])).replace("ATTR_AUTHOR", repr(author.split("=")[0]))
    if "=" in item:   # TikTok's attributes carry values
        html = html.replace("m.setAttribute('data-e2e', '')", "m.setAttribute('data-e2e', 'chat-message')")
        html = html.replace("n.setAttribute('data-e2e', '')", "n.setAttribute('data-e2e', 'message-owner-name')")
    page.route("https://www.tiktok.com/**", lambda r: r.fulfill(body=html, content_type="text/html; charset=utf-8"))
    page.goto("https://www.tiktok.com/@me/live")


@pytest.fixture
def page():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        exe = os.environ.get("CHROMIUM_PATH")
        br = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        yield br.new_context(viewport={"width": 1100, "height": 700}).new_page()
        br.close()


@browser
def test_agent_moderates_tiktok_live_chat(svc, page):
    uid, sid = _session(svc)
    _live(page)
    cp = Cockpit(page.context).install()
    ex = Executor(Pacing(0, 0, 0, 0, sleep=lambda s: None), timeout_ms=2000, cockpit=cp)
    stats = watch(svc.liveguard, uid, sid, page, "tiktok", executor=ex, cockpit=cp, poll=0.3, max_seconds=5)
    assert sorted(page.evaluate("window.blocked")) == ["free_gift_98", "free_gift_99"]     # the fans stay
    assert stats["read"] == 4 and stats["removed"] == 2
    x = svc.liveguard.session(uid, sid)
    assert x["checked"] == 4 and all(e["applied"] for e in x["events"] if e["action"] != "none")


@browser
def test_agent_learns_an_unfamiliar_chat_layout(svc, page):
    uid, sid = _session(svc, mode="watch")
    _live(page, item="data-msg", author="data-who")          # markup it doesn't know
    cp = Cockpit(page.context, help_timeout=5).install()
    learned = []

    def person(pg, state):
        if state == "help" and not learned:
            learned.append(1)
            pg.locator("[data-msg]").nth(1).click(position={"x": 200, "y": 5})     # "this is a chat message"

    cp.person = person
    stats = watch(svc.liveguard, uid, sid, page, "tiktok", cockpit=cp, poll=0.3, max_seconds=6, find_timeout=1.5)
    assert stats["read"] >= 3
    flagged = [e["author_id"] for e in svc.liveguard.session(uid, sid)["events"] if e["action"] != "none"]
    assert "free_gift_99" in flagged and "gamer_joe" not in flagged


@browser
def test_streamers_own_steps_win_over_the_built_in_ones(svc, page):
    """TikTok moved "Block" into a "Manage" menu for this streamer; they taught it once, and the agent follows."""
    uid, sid = _session(svc)
    _live(page)
    page.evaluate("""() => { const m = document.getElementById('menu');
        m.innerHTML = '<button onclick="document.getElementById(\\'sub\\').hidden=false">Manage</button>' +
                      '<div id="sub" hidden><button onclick="window.blocked.push(window.who)">Remove from chat</button></div>'; }""")
    cp = Cockpit(page.context).install()
    ex = Executor(Pacing(0, 0, 0, 0, sleep=lambda s: None), timeout_ms=1500, cockpit=cp, ask_help=False)
    own = {"ban": ['Click "{author_name}".', 'Click "Manage".', 'Click "Remove from chat".']}
    stats = watch(svc.liveguard, uid, sid, page, "tiktok", executor=ex, cockpit=cp, poll=0.3, max_seconds=5, custom=own)
    assert "free_gift_99" in page.evaluate("window.blocked") and stats["removed"] >= 1
