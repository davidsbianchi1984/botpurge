"""Your agent: talk it through (settings, directions, objectives), saved sign-ins, teaching live moderation."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from botpurge import evaluation as ev
from botpurge.agent.chat import Toolbox, converse, interpret
from botpurge.api import create_app
from botpurge.models import Platform
from conftest import hdr


def say(client, user, text, **ctx):
    r = client.post("/api/agent/chat", json={"text": text, **ctx}, headers=hdr(user))
    assert r.status_code == 200, r.text
    return r.json()


def test_plain_words_change_settings(client, user):
    r = say(client, user, "Please go slower and only do 20 at a time")
    assert r["prefs"]["pace"] == "gentle" and r["prefs"]["max_per_run"] == 20 and r["engine"] == "built-in"
    assert "Pace set to gentle" in r["reply"] and "20 accounts" in r["reply"]
    assert say(client, user, "always block them too")["prefs"]["default_actions"] == ["remove", "block"]
    assert say(client, user, "don't report anyone")["prefs"]["default_actions"] == ["remove", "block"]
    r = say(client, user, "My goal is to clear out crypto scammers")
    assert r["prefs"]["objectives"] == ["clear out crypto scammers"]
    assert "crypto" in say(client, user, "what are my settings?")["reply"]
    hist = client.get("/api/agent/chat", headers=hdr(user)).json()
    assert len(hist["messages"]) == 10 and hist["messages"][0]["role"] == "user"
    assert "live_ban" in hist["teachable"]["tiktok"] and "unfriend" in hist["teachable"]["facebook"]


def test_unknown_request_gets_examples_not_changes(client, user):
    r = say(client, user, "hello there")
    assert r["changes"] == [] and "go slower" in r["reply"]


def test_directions_in_plain_words_become_the_persons_steps(client, user, svc):
    r = say(client, user, 'On TikTok, to block someone: click "Share", then click "Block", then click "Block" again')
    assert r["changes"] and "Saved your steps" in r["reply"]
    best = svc.instructions.best("tiktok", "web", "block", user["_id"])
    assert best["author_id"] == user["_id"] and best["steps"][0] == 'click "Share".'
    # Came from "Talk it through" on a live action: the context says what the steps are for.
    r = say(client, user, 'click "{author_name}", then click "Remove from chat"', platform="tiktok", action="live_ban")
    assert svc.instructions.best("tiktok", "web", "live_ban", user["_id"])["author_id"] == user["_id"]
    # Passwords are never part of directions.
    r = say(client, user, 'On TikTok, to report: click "Report", then type "hunter2" in the "Password" box', )
    assert "couldn't" in r["reply"] and "password" in r["reply"]


def test_trust_account_by_name(client, user, svc):
    net = ev.seeded_network(n_real=40, n_bots=8, n_clones=2, n_following_bad=2, platform=Platform.tiktok, seed=3)
    svc.personal.store_connections(user["_id"], Platform.tiktok, net.conns)
    svc.personal.scan(user["_id"], "tiktok")
    row = svc.db.one("SELECT handle, account_id FROM flags WHERE user_id=? AND label='likely_bot' LIMIT 1", (user["_id"],))
    r = say(client, user, f"never remove @{row['handle']}")
    assert "marked as a real person" in r["reply"]
    assert svc.db.one("SELECT status FROM flags WHERE user_id=? AND account_id=?", (user["_id"], row["account_id"]))["status"] == "whitelisted"
    assert "couldn't find" in say(client, user, "never remove @nobody_here_123")["reply"]


def test_live_rules_become_live_guard_defaults(client, user):
    r = say(client, user, 'Keep politics out of my live chat, no insults, and block the words "giveaway" and "telegram"')
    assert r["prefs"]["live"] == {"no_politics": True, "no_abuse": True, "blocked_phrases": ["giveaway", "telegram"]}
    s = client.post("/api/liveguard/sessions", json={"platform": "tiktok", "channel": "me"}, headers=hdr(user)).json()
    pol = json.loads(client.app.state.svc.db.one("SELECT policy_json FROM lg_sessions WHERE id=?", (s["id"],))["policy_json"])
    assert pol["no_politics"] and pol["no_abuse"] and pol["blocked_phrases"] == ["giveaway", "telegram"]
    # Choices made on the Live Guard screen still win.
    s = client.post("/api/liveguard/sessions", json={"platform": "tiktok", "channel": "me2", "policy": {"no_politics": False}}, headers=hdr(user)).json()
    assert not json.loads(client.app.state.svc.db.one("SELECT policy_json FROM lg_sessions WHERE id=?", (s["id"],))["policy_json"])["no_politics"]


def test_claude_understands_when_the_server_has_a_key(svc, monkeypatch):
    uid = svc.personal.register("c@d.co")[0]
    calls = []

    def fake(req: httpx.Request):
        body = json.loads(req.content)
        calls.append(body)
        assert req.headers["x-api-key"] == "k" and body["tools"]
        if len(calls) == 1:
            return httpx.Response(200, json={"content": [{"type": "tool_use", "id": "t1", "name": "set_pace", "input": {"pace": "quick"}}]})
        assert body["messages"][-1]["content"][0]["content"].startswith("Pace set to quick")
        return httpx.Response(200, json={"content": [{"type": "text", "text": "Done: I'll work faster now."}]})

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    r = converse(svc, uid, "can you hurry it up a bit", http=httpx.Client(transport=httpx.MockTransport(fake)))
    assert r["engine"] == "claude" and r["prefs"]["pace"] == "quick" and r["reply"] == "Done: I'll work faster now."
    # If Claude can't be reached, the built-in reader still answers.
    r = converse(svc, uid, "go slower", http=httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(500))))
    assert r["engine"] == "built-in" and r["prefs"]["pace"] == "gentle"


def test_saved_signin_is_desktop_only_sealed_and_masked(client, user, svc, monkeypatch):
    body = {"username": "david@botpurge.online", "password": "s3cret!"}
    monkeypatch.delenv("BOTPURGE_DESKTOP", raising=False)
    assert client.put("/api/agent/signins/tiktok", json=body, headers=hdr(user)).status_code == 403
    assert client.get("/api/agent/signins", headers=hdr(user)).json()["available"] is False
    monkeypatch.setenv("BOTPURGE_DESKTOP", "1")
    r = client.put("/api/agent/signins/tiktok", json=body, headers=hdr(user)).json()
    assert r["saved"] and r["username"] == "da•••@botpurge.online"
    listed = {x["platform"]: x for x in client.get("/api/agent/signins", headers=hdr(user)).json()["signins"]}
    assert listed["tiktok"]["saved"] and "s3cret" not in json.dumps(listed)
    raw = svc.db.one("SELECT sealed FROM secrets WHERE name='agent_signin_tiktok'")["sealed"]
    assert b"s3cret" not in (raw if isinstance(raw, bytes) else raw.encode())          # sealed at rest
    client.delete("/api/agent/signins/tiktok", headers=hdr(user))
    assert not {x["platform"]: x for x in client.get("/api/agent/signins", headers=hdr(user)).json()["signins"]}["tiktok"]["saved"]


def test_teaching_live_moderation_needs_the_live_and_a_chatter(client, user):
    base = {"platform": "tiktok", "action": "live_ban"}
    assert client.post("/api/agent/teach", json=base, headers=hdr(user)).status_code == 400
    assert client.post("/api/agent/teach", json={**base, "start_url": "https://evil.example/live"}, headers=hdr(user)).status_code == 400
    r = client.post("/api/agent/teach", json={**base, "start_url": "https://www.tiktok.com/@me/live"}, headers=hdr(user))
    assert r.status_code == 400 and "chatter" in r.json()["detail"]
    assert client.post("/api/agent/teach", json={"platform": "linkedin", "action": "live_ban"}, headers=hdr(user)).status_code == 400


def test_scan_scope_in_plain_words(client, user, svc):
    net = ev.seeded_network(n_real=120, n_bots=20, n_clones=3, n_following_bad=5, platform=Platform.tiktok, seed=5)
    svc.personal.store_connections(user["_id"], Platform.tiktok, net.conns)
    fb = ev.seeded_network(n_real=60, n_bots=8, n_clones=1, n_following_bad=2, platform=Platform.facebook, seed=6)
    svc.personal.store_connections(user["_id"], Platform.facebook, fb.conns)
    r = say(client, user, "Only scan my followers on TikTok for bots, spam and fraud accounts")
    assert r["prefs"]["scope"] == {"platforms": ["tiktok"], "directions": ["follower"], "kinds": ["bots", "spam", "fraud"]}
    assert r["reply"].startswith("Scanning only your TikTok followers for bots, spam and fraud accounts: checked ")
    checked = svc.db.one("SELECT COUNT(*) n FROM flags WHERE user_id=? AND platform='tiktok' AND direction='follower'", (user["_id"],))["n"]
    assert f"checked {checked}," in r["reply"] and checked > 0
    assert "scanning your TikTok followers" in say(client, user, "what are my settings?")["reply"]
