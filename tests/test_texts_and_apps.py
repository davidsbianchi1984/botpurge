import json
import sqlite3
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from botpurge import rules
from botpurge.apps import import_apps, score_app
from botpurge.messages import Message, MessageScorer, import_messages, parse_email_bytes
from conftest import hdr

T0 = datetime(2026, 5, 1, tzinfo=timezone.utc)


def codes(r):
    return {x["code"] for x in r.reasons}


def test_android_sms_backup_and_known_contacts():
    xml = b"""<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<smses count="4">
  <sms protocol="0" address="+13055550123" date="1780000000000" type="1" body="E-ZPass: outstanding toll balance of $12.51. Pay now to avoid a $50 late fee https://ezpass-pay.top/x" contact_name="(Unknown)" />
  <sms protocol="0" address="+13055550199" date="1780000100000" type="1" body="Are we still on for golf tomorrow?" contact_name="Uncle Bob" />
  <sms protocol="0" address="+13055550199" date="1780000200000" type="2" body="yep see you" contact_name="Uncle Bob" />
  <mms date="1780000300000" msg_box="1" address="scammer1234@icloud.com"><parts><part ct="text/plain" text="Please reply Y, then exit the text message, reopen the text message activation link" /></parts></mms>
</smses>"""
    msgs = import_messages("sms", "sms-2026.xml", xml)
    assert len(msgs) == 3 and all(m.kind == "sms" for m in msgs)
    bob = next(m for m in msgs if m.sender_name == "Uncle Bob")
    assert bob.known_contact
    res = {r.sender_id: r for r in MessageScorer().score(msgs)}
    assert res["+13055550123"].label == "likely_bot" and "toll_smish" in codes(res["+13055550123"])
    assert res["scammer1234@icloud.com"].label == "likely_bot"
    assert res["+13055550199"].label == "looks_real"  # a saved contact's plans aren't a "wrong number"


def test_iphone_sms_db(tmp_path):
    db = tmp_path / "sms.db"
    con = sqlite3.connect(db)
    con.executescript("""CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, attributedBody BLOB, date INTEGER, is_from_me INTEGER, handle_id INTEGER);
        INSERT INTO handle VALUES (1, '+447700900123');""")
    ns = int((T0.timestamp() - 978307200) * 1e9)
    con.execute("INSERT INTO message VALUES (1, ?, NULL, ?, 0, 1)", ("I'm trying to log into WhatsApp and the code went to you by mistake. Please send it quickly.", ns))
    con.execute("INSERT INTO message VALUES (2, 'my own reply', NULL, ?, 1, 1)", (ns,))
    con.commit()
    con.close()
    msgs = import_messages("sms", "sms.db", db.read_bytes())
    assert len(msgs) == 1 and msgs[0].at.date() == T0.date()
    [r] = MessageScorer().score(msgs)
    assert "code_theft" in codes(r) and r.label == "likely_bot"


def test_pig_butchering_arc_across_a_conversation():
    lines = ["Hi, is this Jenny?", "Oh sorry, wrong number! But nice to meet you, where are you from?",
             "Let's chat on WhatsApp, add me on whatsapp +44 7700 900123",
             "My uncle works at a trading platform, USDT liquidity mining gives guaranteed profit"]
    msgs = [Message(kind="sms", platform="sms", sender_id="+15555550100", text=t, at=T0 + timedelta(days=i)) for i, t in enumerate(lines)]
    [r] = MessageScorer().score(msgs)
    assert "pig_butchering_arc" in codes(r) and r.label == "likely_bot"
    # The opener alone is only a hint.
    [r1] = MessageScorer().score(msgs[:1])
    assert r1.label == "looks_real"


def test_obfuscation_is_seen_through():
    for text in ["Best viewers on s t r e a m b o o . com", "buy followers at bigfollows (dot) com", "Buy fоllowers now"]:
        hits = {h.code for h in rules.evaluate(text, "live")}
        assert hits & {"growth_service_ad", "obfuscated_link", "mixed_script"}, text


def test_bait_usernames_and_stolen_comments():
    assert {h.code for h in rules.evaluate_username("P і n n e d  by  MrBeast")} == {"pinned_by_impersonation"}
    assert {h.code for h in rules.evaluate_username("anna_on_telegram")} == {"bait_username"}
    assert rules.evaluate_username("anna.smith92") == []
    top = "This is the funniest thing I've seen all week, my cat does exactly the same thing"
    msgs = [Message(kind="comment", platform="youtube", sender_id="orig", text=top, context="vid1", at=T0),
            Message(kind="comment", platform="youtube", sender_id="thief", text=top, context="vid1", at=T0 + timedelta(hours=3))]
    res = {r.sender_id: r for r in MessageScorer().score(msgs)}
    assert "stolen_comment" in codes(res["thief"]) and "stolen_comment" not in codes(res["orig"])


def test_email_attachments_smuggling_and_free_hosting():
    m = EmailMessage()
    m["From"], m["Subject"] = '"Accounts" <billing@vendor-portal.net>', "Invoice attached"
    m.set_content("Please see the attached invoice")
    m.add_alternative('<a href="https://login-portal.pages.dev/o365">View invoice</a><script>var d=atob("AAA");var b=new Blob([d]);</script>', subtype="html")
    m.add_attachment(b"MZ", maintype="application", subtype="octet-stream", filename="invoice.pdf.exe")
    [r] = MessageScorer().score([parse_email_bytes(bytes(m))])
    assert {"dangerous_attachment", "html_smuggling", "url_free_hosting"} <= codes(r)
    callback = EmailMessage()
    callback["From"], callback["Subject"] = '"Geek Squad" <renewals.team88@gmail.com>', "Your subscription renewed"
    callback.set_content("Your Geek Squad subscription has been renewed and $399.99 charged. To cancel or request a refund call +1 (808) 555-0147 within 24 hours.")
    [r] = MessageScorer().score([parse_email_bytes(bytes(callback))])
    assert "callback_phishing" in codes(r) and r.label == "likely_bot"


def test_connected_apps_from_x_archive_and_scoring(client, user):
    js = "window.YTD.connected_application.part0 = " + json.dumps([
        {"connectedApplication": {"organization": {"name": "GrowFast", "url": "https://growfast.wixsite.com"}, "name": "InstaFollowers Pro Boost",
                                  "description": "Get followers fast", "permissions": ["read", "write", "directmessages"], "approvedAt": "2025-01-01T00:00:00.000Z", "id": "1"}},
        {"connectedApplication": {"organization": {"name": "Who Viewed Me"}, "name": "Who viewed my profile",
                                  "description": "", "permissions": ["read"], "approvedAt": "2025-02-01T00:00:00.000Z", "id": "2"}},
        {"connectedApplication": {"organization": {"name": "Buffer", "url": "https://buffer.com"}, "name": "Buffer",
                                  "description": "Schedule your posts across social networks", "permissions": ["read", "write"],
                                  "approvedAt": "2025-06-01T00:00:00.000Z", "id": "3"}},
    ])
    apps = import_apps("x", "connected-application.js", js.encode())
    v = {a.name: score_app(a, apps) for a in apps}
    assert v["InstaFollowers Pro Boost"].label == "likely_bot"
    assert v["Who viewed my profile"].label == "likely_bot"
    assert v["Buffer"].label == "looks_real"  # a real scheduler with write access is fine
    r = client.post("/api/apps/import/x", headers=hdr(user), files={"file": ("connected-application.js", js.encode(), "text/javascript")})
    assert r.json()["apps"] == 3
    listed = client.get("/api/apps", headers=hdr(user)).json()
    assert listed[0]["revoke_steps"] and listed[0]["reasons"]
    assert client.get("/api/me/summary", headers=hdr(user)).json()["categories"]["risky_apps"] == 2
    client.post("/api/apps/status", headers=hdr(user), json={"platform": "x", "name": "Who viewed my profile", "status": "revoked"})
    assert client.get("/api/me/summary", headers=hdr(user)).json()["categories"]["risky_apps"] == 1


def test_facebook_apps_loose_keys():
    obj = {"installed_apps_v2": [{"name": "Auto Liker for Pages", "added_timestamp": 1500000000}],
           "inactive_apps_v2": [{"name": "Which Disney Princess Are You Quiz", "added_timestamp": 1400000000}]}
    apps = import_apps("facebook", "apps_and_websites.json", json.dumps(obj).encode())
    assert {a.name: a.status for a in apps} == {"Auto Liker for Pages": "active", "Which Disney Princess Are You Quiz": "expired"}
    liker = next(a for a in apps if a.name.startswith("Auto"))
    assert score_app(liker, apps).label in ("suspicious", "low_confidence", "likely_bot")

