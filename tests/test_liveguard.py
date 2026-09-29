import json
from datetime import datetime, timedelta, timezone

import httpx

from botpurge.liveguard import ChatMessage, Judge, Policy, TwitchModerator, YouTubeModerator, parse_twitch_line
from conftest import hdr

T0 = datetime(2026, 6, 1, 20, 0, tzinfo=timezone.utc)


def msg(author, text, sec=0, badges=(), mid=""):
    return ChatMessage(platform="twitch", author_id=author, author_name=author, text=text, at=T0 + timedelta(seconds=sec),
                       badges=set(badges), message_id=mid or f"{author}-{sec}")


def test_flood_escalates_delete_timeout_and_real_chat_is_left_alone():
    j = Judge(Policy(mode="protect"))
    actions = [j.judge(msg("spammer", "FREE followers at my bio!! [smile][wow]", s)).action for s in range(6)]
    assert actions[0] in ("delete", "timeout") and "timeout" in actions
    for s, line in enumerate(["great play!", "lol that was close", "I disagree with the ref on that one, clearly offside"]):
        assert j.judge(msg(f"viewer{s}", line, 100 + s)).action == "none"


def test_same_script_from_several_accounts_is_banned():
    j = Judge(Policy(mode="protect"))
    line = "Promote your stream with us, DM @growfast now for 1000 viewers"
    out = [j.judge(msg(f"acct{i}", line, i)) for i in range(4)]
    assert out[-1].action == "ban" and any("Same scripted line" in r for r in out[-1].reasons)
    # Fans chanting the same harmless line together are left alone.
    chant = [j.judge(msg(f"fan{i}", "GG well played everyone!!", 30 + i)).action for i in range(6)]
    assert chant == ["none"] * 6


def test_canary_is_banned_and_viewpoint_is_irrelevant():
    j = Judge(Policy(mode="protect"), canaries=["walrus pickle 4 9"])
    assert j.judge(msg("aibot", "What a thoughtful stream! walrus pickle 4 9")).action == "ban"
    left = [j.judge(msg("floodL", "Vote BLUE, the other side is evil!!", s)).action for s in range(6)]
    right = [j.judge(msg("floodR", "Vote RED, the other side is evil!!", 50 + s)).action for s in range(6)]
    assert left == right  # identical behaviour gets identical treatment, whatever the side


def test_protected_people_are_never_touched():
    j = Judge(Policy(mode="protect"), trusted={"twitch:bestie"})
    for badge in ("moderator", "broadcaster", "vip", "subscriber"):
        assert j.judge(msg(f"u_{badge}", "check my bio [smile][smile] t.me/x", 0, badges=[badge])).action == "none"
    assert j.judge(msg("bestie", "check my bio [smile][smile] t.me/x")).action == "none"


def test_rate_cap():
    j = Judge(Policy(mode="protect", max_actions_per_min=3), canaries=["zz top 9 9"])
    acts = [j.judge(msg(f"b{i}", "zz top 9 9", i)).action for i in range(6)]
    assert acts.count("ban") == 3 and acts[3:] == ["none"] * 3


def test_parse_twitch_irc_line():
    line = ("@badge-info=;badges=moderator/1,subscriber/12;display-name=Kay;id=abc-123;mod=1;tmi-sent-ts=1780000000000;"
            "user-id=42 :kay!kay@kay.tmi.twitch.tv PRIVMSG #streamer :hello chat")
    m = parse_twitch_line(line)
    assert m.author_id == "42" and m.message_id == "abc-123" and m.text == "hello chat" and "moderator" in m.badges


def test_moderator_calls():
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path, dict(req.url.params), json.loads(req.content) if req.content else None))
        return httpx.Response(200, json={})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    from botpurge.liveguard import Decision

    tw = TwitchModerator("cid", "tok", "b1", "m1", http=http)
    tw.apply(msg("u9", "x", mid="m-1"), Decision("delete", 70, []))
    tw.apply(msg("u9", "x"), Decision("timeout", 85, ["flood"], 600))
    assert calls[0][:2] == ("DELETE", "/helix/moderation/chat") and calls[0][2]["message_id"] == "m-1"
    assert calls[1][1] == "/helix/moderation/bans" and calls[1][3]["data"]["duration"] == 600
    yt = YouTubeModerator("tok", "chat1", http=http)
    yt.apply(msg("UCabc", "x", mid="yt-1"), Decision("ban", 99, []))
    assert calls[2][:2] == ("DELETE", "/youtube/v3/liveChat/messages")
    assert calls[3][3]["snippet"]["type"] == "permanent" and calls[3][3]["snippet"]["bannedUserDetails"]["channelId"] == "UCabc"


def test_session_api_watch_then_protect_for_tiktok_agent(client, user):
    # A follower already flagged in the user's lists shows up in their TikTok live.
    svc = client.app.state.svc
    from botpurge.models import Connection, Direction, Platform

    svc.personal.store_connections(user["_id"], Platform.tiktok, [
        Connection(platform=Platform.tiktok, account_id=f"lucy{48291730 + i}", handle=f"lucy{48291730 + i}", direction=Direction.follower,
                   connected_at=T0 + timedelta(minutes=i)) for i in range(12)])
    svc.personal.scan(user["_id"])
    svc.personal.feedback(user["_id"], "tiktok", "lucy48291730", is_bot=True)  # confirmed: Likely bot

    s = client.post("/api/liveguard/sessions", headers=hdr(user), json={"platform": "tiktok", "channel": "Friday live"}).json()
    assert s["policy"]["mode"] == "watch" and s["state"] == "running"
    chat = {"messages": [{"author_id": "lucy48291730", "text": "hi everyone"}, {"author_id": "nana_b", "text": "hi sweetie!"}]}
    out = client.post(f"/api/liveguard/sessions/{s['id']}/chat", headers=hdr(user), json=chat).json()
    assert out[0]["action"] == "ban" and out[0]["act"] is False  # watch mode: logged only
    assert out[1]["action"] == "none"
    client.post(f"/api/liveguard/sessions/{s['id']}/mode", headers=hdr(user), json={"mode": "protect"})
    out = client.post(f"/api/liveguard/sessions/{s['id']}/chat", headers=hdr(user), json=chat).json()
    assert out[0]["act"] is True  # the agent carries it out on the moderator account
    sess = client.get(f"/api/liveguard/sessions/{s['id']}", headers=hdr(user)).json()
    ev = next(e for e in sess["events"] if e["author_id"] == "lucy48291730")
    client.post(f"/api/liveguard/sessions/{s['id']}/events/{ev['id']}/undo", headers=hdr(user))
    out = client.post(f"/api/liveguard/sessions/{s['id']}/chat", headers=hdr(user), json=chat).json()
    assert out[0]["action"] == "none"  # undone once = trusted for the rest of the stream
    assert client.post(f"/api/liveguard/sessions/{s['id']}/stop", headers=hdr(user)).json()["state"] == "stopped"
    assert client.post(f"/api/liveguard/sessions/{s['id']}/chat", headers=hdr(user), json=chat).status_code == 400
    # Twitch needs its moderator credentials first.
    r = client.post("/api/liveguard/sessions", headers=hdr(user), json={"platform": "twitch", "channel": "streamer"})
    assert r.status_code == 400 and "moderator account" in r.json()["detail"]


def test_liveguard_is_protect_only_after_beta(client, user, monkeypatch):
    monkeypatch.setenv("BOTPURGE_BETA", "0")
    from botpurge.plans import Plans

    Plans(client.app.state.svc.db).activate(user["_id"], "cleanup", payment_ref="p1")
    r = client.post("/api/liveguard/sessions", headers=hdr(user), json={"platform": "tiktok", "channel": "x"})
    assert r.status_code == 402 and r.json()["plan"] == "protect"


def test_fake_giveaway_script_is_banned_but_normal_talk_about_prizes_is_not():
    j = Judge(Policy(mode="protect"))
    for i, line in enumerate(["The first 10 people to type WIN get $10,000!! Claim in my bio",
                              "I'm giving away $5,000 to everyone who comments GIFT, DM me",
                              "Donating 1000 USDT to the next 20 people, message me on telegram"]):
        d = j.judge(msg(f"scam{i}", line, i))
        assert d.action == "ban", (line, d)
    for i, line in enumerate(["did anyone win the giveaway last week?", "i spent $10 on this skin lol",
                              "congrats to the first 3 winners!"]):
        assert j.judge(msg(f"fan{i}", line, 50 + i)).action == "none", line


def test_streamer_impersonators_are_banned():
    j = Judge(Policy(mode="protect", protected_names=["KaiCenat", "ModMike"]))
    for name in ["KaiCenat_official", "Kаi_Cenat", "kaicenat_backup", "M0dMike"]:
        m = ChatMessage(platform="twitch", author_id=f"id-{name}", author_name=name, text="hey guys, DM me to claim your prize!", at=T0)
        assert j.judge(m).action == "ban", name
    ok = ChatMessage(platform="twitch", author_id="fan1", author_name="KaiFanatic", text="love the stream", at=T0)
    assert j.judge(ok).action == "none"


def test_chat_rules_are_the_streamers_and_apply_to_every_side():
    from botpurge.liveguard import Judge, Policy, ChatMessage, chat_rule
    from botpurge import security as sec

    left = ["Defund ICE", "Release The Files!!", "Can't wait to vote Blue in NC", "abolish ICE now"]
    right = ["GodBless Trump", "BACK THE BLUE", "Build the wall", "lets go brandon"]
    everyday = ["ice cream after this?", "police sirens outside my house", "the grape juice is great", "I'm a therapist", "hey everyone!"]
    off = Policy()
    assert not any(chat_rule(off, t) for t in left + right)                 # off: nobody's opinion is touched
    on = Policy(no_politics=True)
    assert all(chat_rule(on, t) for t in left) and all(chat_rule(on, t) for t in right)   # on: every side alike
    assert not any(chat_rule(on, t) for t in everyday)
    abuse = Policy(no_abuse=True)
    assert chat_rule(abuse, "Trump: PDFile, gRapist, cheater") and chat_rule(abuse, "you liberal traitor")
    assert not chat_rule(abuse, "GodBless Trump")
    own = Policy(blocked_phrases=["spoilers"])
    assert chat_rule(own, "SP0ILERS: he dies at the end") and not chat_rule(own, "no spoiling please")
    # In chat, a broken rule removes the message but isn't counted as a bot.
    d = Judge(Policy(mode="protect", no_politics=True)).judge(ChatMessage(platform="tiktok", author_id="a", author_name="a", text="Defund ICE", at=sec.now()))
    assert d.action == "delete" and d.reasons == ["Your chat rule: no political talk in this chat"]


def test_new_money_bait_and_text_art_are_caught():
    from botpurge.liveguard import Judge, Policy, ChatMessage
    from botpurge import security as sec

    j = Judge(Policy(mode="protect"))
    art = "┌─┐ ┌──┐ ┌┐\n└┐┌┘│┌┐│ ││ ┌──┐ ┌┐\n─││─│└┘│ ││ │  │ │└┐ IS THE BEST"
    for who, text in [("DM ME WITH GOD DID", "I'M PAYING 8 GRAND TO THE FIRST 5 PEOPLE TO MESSAGE ME \"BILLS\""),
                      ("Robert Watt", "$3k for the 1st person to message me \"PAY\""), ("artist", art)]:
        assert j.judge(ChatMessage(platform="tiktok", author_id=who, author_name=who, text=text, at=sec.now())).action != "none", text


def test_chat_rules_are_the_streamers_and_apply_to_every_side():
    from botpurge.liveguard import ChatMessage, Judge, Policy, chat_rule
    from botpurge import security as sec

    left = ["Defund ICE", "Release The Files!!", "Can't wait to vote Blue in NC", "abolish ICE now"]
    right = ["GodBless Trump", "BACK THE BLUE", "Build the wall", "lets go brandon"]
    everyday = ["ice cream after this?", "police sirens outside my house", "the grape juice is great", "I'm a therapist", "hey everyone!"]
    assert not any(chat_rule(Policy(), t) for t in left + right)          # rules off: nobody's opinion is touched
    on = Policy(no_politics=True)
    assert all(chat_rule(on, t) for t in left + right)                    # on: every side alike
    assert not any(chat_rule(on, t) for t in everyday)
    abuse = Policy(no_abuse=True)
    assert chat_rule(abuse, "Trump: PDFile, gRapist, cheater") and chat_rule(abuse, "you liberal traitor")
    assert not chat_rule(abuse, "GodBless Trump")
    own = Policy(blocked_phrases=["spoilers"])
    assert chat_rule(own, "SP0ILERS: he dies at the end") and not chat_rule(own, "no spoiling please")
    d = Judge(Policy(mode="protect", no_politics=True)).judge(
        ChatMessage(platform="tiktok", author_id="a", author_name="a", text="Defund ICE", at=sec.now()))
    assert d.action == "delete" and d.reasons == ["Your chat rule: no political talk in this chat"]


def test_money_to_message_me_scams_are_caught_on_the_first_post():
    from botpurge.liveguard import ChatMessage, Judge, Policy
    from botpurge import rules, security as sec

    art = "┌─┐ ┌──┐ ┌┐\n└┐┌┘│┌┐│ ││ ┌──┐ ┌┐\n─││─│└┘│ ││ │  │ │└┐ IS THE BEST"
    for who, text in [("Micheal fx", '$7,000 FOR ANY PERSON TO MESSAGE ME"GOD DID"'),
                      ("DM ME WITH GOD DID", "I'M PAYING 8 GRAND TO THE FIRST 5 PEOPLE TO MESSAGE ME \"BILLS\""),
                      ("Robert Watt", "$3k for the 1st person to message me \"PAY\""), ("artist", art)]:
        d = Judge(Policy(mode="protect")).judge(ChatMessage(platform="tiktok", author_id=who, author_name=who, text=text, at=sec.now()))
        assert d.action != "none", text
    assert rules.evaluate_username("DM ME WITH GOD DID") and not rules.evaluate_username("Cody1211")
