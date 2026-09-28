import io
import json
import zipfile
from email.message import EmailMessage

from botpurge.messages import Message, MessageScorer, parse_paste
from conftest import hdr


def sender(results, sid):
    return next(r for r in results if r.sender_id == sid)


def codes(r):
    return {x["code"] for x in r.reasons}


def test_bracket_emoji_and_solicitation_dm():
    msgs = [Message(kind="dm", platform="tiktok", sender_id="luna_x93", text="Hi dear [smile][tearsofjoy] you look so kind, add me on telegram"),
            Message(kind="dm", platform="tiktok", sender_id="aunt_jo", text="See you at dinner Sunday! Bring the pie 😂")]
    res = MessageScorer().score(msgs)
    bot, aunt = sender(res, "luna_x93"), sender(res, "aunt_jo")
    assert {"bracket_emoji", "solicitation"} <= codes(bot) and bot.label in ("likely_bot", "suspicious")
    assert aunt.score < 40


def test_live_chat_flooding_and_repeats_are_viewpoint_neutral():
    # Two floods pushing opposite political lines, and one real viewer arguing a view in their own words.
    chat = "\n".join(
        [f"flood_left: Vote BLUE or lose everything!!" for _ in range(6)]
        + [f"flood_right: Vote RED or lose everything!!" for _ in range(6)]
        + ["maria_k: I actually think the officer was wrong here, the video shows him stepping back first",
           "maria_k: but I get why people disagree"]
    )
    res = {r.sender_id: r for r in MessageScorer().score(parse_paste(chat, "tiktok", "live"))}
    assert {"repeat_sender", "flooding"} <= codes(res["flood_left"])
    assert codes(res["flood_left"]) == codes(res["flood_right"]) and res["flood_left"].score == res["flood_right"].score
    assert res["maria_k"].score < 40


def test_template_network_and_generic_comments():
    lines = [f"user{i}: Promote your account with us, DM @growfast{i} now" for i in range(4)] + ["realfan: Nice video!"]
    res = {r.sender_id: r for r in MessageScorer().score(parse_paste("\n".join(lines), "tiktok", "comment"))}
    assert "template_across_senders" in codes(res["user0"]) and res["user0"].label in ("suspicious", "likely_bot")
    assert codes(res["realfan"]) == {"generic_comment"} and res["realfan"].label == "looks_real"


def test_canary_phrase_is_a_confirmed_bot():
    msgs = parse_paste("smartbot: What a moving story, blueberry 6 7, truly inspiring\nhuman1: lol what was that weird line at the end",
                       "tiktok", "comment")
    res = {r.sender_id: r for r in MessageScorer(canaries=["blueberry 6 7"]).score(msgs)}
    assert "canary" in codes(res["smartbot"]) and res["smartbot"].label == "likely_bot"
    assert res["human1"].label == "looks_real"


def test_ai_artifacts():
    [r] = MessageScorer().score([Message(kind="comment", platform="tiktok", sender_id="gpt_fan",
                                         text="Certainly! Here's a comment: This video beautifully captures the joy of cooking.")])
    assert "ai_artifact" in codes(r)


def email_bytes(frm, subject, body_html, reply_to=None, auth=None):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, "me@example.com", subject
    m["Date"] = "Mon, 01 Jun 2026 10:00:00 +0000"
    if reply_to:
        m["Reply-To"] = reply_to
    if auth:
        m["Authentication-Results"] = auth
    m.set_content("plain version")
    m.add_alternative(body_html, subtype="html")
    return bytes(m)


def test_email_scams_are_caught_and_real_mail_is_not():
    from botpurge.messages import parse_email_bytes

    scam = parse_email_bytes(email_bytes(
        '"PayPal Security" <alerts@paypa1-secure.com>', "Your account has been suspended",
        '<p>Unusual sign-in activity. <a href="http://185.22.1.9/login">https://www.paypal.com/verify</a></p>',
        reply_to="collect@gmail.com", auth="mx.example.com; spf=fail; dkim=fail; dmarc=fail"))
    real = parse_email_bytes(email_bytes(
        '"Grandma Rose" <rose.smith@gmail.com>', "Pie recipe", "<p>Here it is, love you!</p>",
        auth="mx.example.com; spf=pass; dkim=pass; dmarc=pass"))
    res = {r.sender_id: r for r in MessageScorer().score([scam, real])}
    s = res["alerts@paypa1-secure.com"]
    assert {"auth_fail", "reply_to_mismatch", "brand_spoof", "lookalike_domain", "phishing_language"} <= codes(s)
    assert s.label == "likely_bot" and ("raw_ip_link" in codes(s) or "link_mismatch" in codes(s))
    assert res["rose.smith@gmail.com"].score < 40


def test_inbox_api_end_to_end(client, user):
    # Instagram DMs from a data export.
    thread = {"participants": [{"name": "Me Myself"}, {"name": "Crypto Coach"}],
              "messages": [{"sender_name": "Crypto Coach", "timestamp_ms": 1780000000000, "content": "Hi [smile] I can teach you forex, text me on whatsapp +1 555 123 4567"},
                           {"sender_name": "Me Myself", "timestamp_ms": 1780000100000, "content": "no thanks"}]}
    thread2 = {"participants": [{"name": "Me Myself"}, {"name": "Sam Lee"}],
               "messages": [{"sender_name": "Sam Lee", "timestamp_ms": 1780000000000, "content": "Are we still on for Saturday?"}]}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("your_instagram_activity/messages/inbox/coach_1/message_1.json", json.dumps(thread))
        z.writestr("your_instagram_activity/messages/inbox/sam_2/message_1.json", json.dumps(thread2))
    r = client.post("/api/inbox/import/instagram", headers=hdr(user), files={"file": ("ig.zip", buf.getvalue(), "application/zip")})
    assert r.status_code == 200 and r.json()["senders"] == 2
    flagged = client.get("/api/inbox/senders", headers=hdr(user)).json()
    assert [f["name"] for f in flagged] == ["Crypto Coach"]

    # Canary trap on a TikTok video, then paste the comments.
    c = client.post("/api/canaries", headers=hdr(user), json={"label": "cooking video"}).json()
    assert c["phrase"] in c["instruction"] and len(c["how_to_use"]) == 4
    paste = f"aibot_99: Such a heartwarming recipe! {c['phrase']}\nnana_b: Making this tonight"
    client.post("/api/inbox/paste", headers=hdr(user), json={"platform": "tiktok", "kind": "comment", "text": paste})
    hits = client.get("/api/canaries", headers=hdr(user)).json()[0]["hits"]
    assert [h["sender_id"] for h in hits] == ["aibot_99"]
    flagged = {f["sender_id"]: f for f in client.get("/api/inbox/senders", headers=hdr(user)).json()}
    assert flagged["aibot_99"]["canary_hit"] and "nana_b" not in flagged

    # Summary counts spam messengers; whitelisting clears them.
    s = client.get("/api/me/summary", headers=hdr(user)).json()
    assert s["categories"]["spam_messengers"] == 2
    coach = next(f for f in client.get("/api/inbox/senders", headers=hdr(user)).json() if f["name"] == "Crypto Coach")
    items = client.get(f"/api/inbox/senders/instagram/{coach['sender_id']}", headers=hdr(user)).json()
    assert items and items[0]["reasons"]
    client.post("/api/inbox/feedback", headers=hdr(user), json={"platform": "instagram", "sender_id": coach["sender_id"], "is_bot": False})
    assert client.get("/api/me/summary", headers=hdr(user)).json()["categories"]["spam_messengers"] == 1

    # Email from an .mbox
    mbox = b"From x@y Mon Jun  1 10:00:00 2026\n" + email_bytes(
        '"Amazon" <no-reply@amazon-orders-help.com>', "Payment declined", "<p>Update your payment within 24 hours</p>") + b"\n"
    r = client.post("/api/inbox/import/email", headers=hdr(user), files={"file": ("Takeout.mbox", mbox, "application/mbox")})
    assert r.status_code == 200
    assert client.get("/api/me/summary", headers=hdr(user)).json()["categories"]["scam_emails"] == 1


def test_messages_paywalled_after_beta(client, user, monkeypatch):
    monkeypatch.setenv("BOTPURGE_BETA", "0")
    r = client.post("/api/inbox/paste", headers=hdr(user), json={"text": "a: hi"})
    assert r.status_code == 402
    assert client.post("/api/canaries", headers=hdr(user), json={}).status_code == 402


def test_lookalike_domains():
    from botpurge.messages import _lookalike

    assert _lookalike("paypa1-secure.com") and _lookalike("secure-paypal.com") and _lookalike("arnazon.com")
    assert not _lookalike("pineapple-shop.com") and not _lookalike("paypal.com") and not _lookalike("mail.paypal.com")
