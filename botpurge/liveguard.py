"""Live Guard: a moderator that removes bots from live chat as they show up (Protect).

The judge keeps a short memory per chatter and decides, per message:
    none | delete | timeout | ban
using the same behaviour-only signals as the inbox scanner (never viewpoint),
plus live-only evidence: repeats and flooding in the last minutes, the same
line from several accounts at once, a canary phrase, an account already
flagged in the streamer's lists.

Safety rails:
  * never acts on the broadcaster, moderators, VIPs, or anyone the streamer
    whitelisted, follows, or marked a real person;
  * "watch" mode (the default for a new stream) only logs what it would do;
  * escalates gently: delete, then timeout, and a ban only on strong evidence
    (canary, known bot, coordinated script) or repeat offences;
  * caps actions per minute so a bug can't mass-ban a chat.

Connectors: Twitch (chat over IRC-TLS, moderation via the Helix API) and
YouTube Live (Data API v3). TikTok/Instagram Live have no moderation API; the
done-for-you agent feeds their chat into the same judge from a moderator
account the streamer added.
"""
from __future__ import annotations

import re
import socket
import ssl
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import httpx

from . import rules
from .messages import WEIGHTS, Message, MessageScorer, _norm

PROTECTED_BADGES = {"broadcaster", "moderator", "vip", "owner", "staff", "admin"}


@dataclass
class ChatMessage:
    platform: str
    author_id: str
    text: str
    author_name: str = ""
    message_id: str = ""
    at: Optional[datetime] = None
    badges: set[str] = field(default_factory=set)   # broadcaster, moderator, vip, subscriber, member...


@dataclass
class Decision:
    action: str            # none | delete | timeout | ban
    score: float
    reasons: list[str]
    duration_s: int = 0


@dataclass
class Policy:
    mode: str = "watch"            # watch (log only) | protect (act)
    delete_at: float = 60
    timeout_at: float = 80
    ban_at: float = 95
    timeout_s: int = 600
    max_actions_per_min: int = 30
    trust_subscribers: bool = True
    protected_names: list = field(default_factory=list)   # the streamer's and moderators' names, to catch impersonators
    # The streamer's own chat rules. Bot and scam detection never looks at opinions; these are
    # topic and conduct rules the streamer switches on, and they apply the same to every side.
    no_politics: bool = False                              # keep political talk out of chat, from any side
    no_abuse: bool = False                                 # insults and personal attacks, aimed at anyone
    blocked_phrases: list = field(default_factory=list)    # the streamer's own words and phrases


POLITICS = re.compile(
    r"\b(trump\w*|biden|harris|kamala|obama|maga|democrats?|dems|republicans?|gop|liberals?|libs|libtards?|conservatives?|"
    r"leftists?|right[- ]?wing\w*|left[- ]?wing\w*|antifa|woke|potus|president|congress|senate|elections?|impeach\w*|"
    r"vote\s+(blue|red)|(blue|red)\s+wave|epstein|release\s+the\s+(files|list)|deep\s+state|the\s+party\s+whose|"
    # slogans from every side
    r"defund(\s+the)?\s*(police|cops|ice)?|abolish\s+(the\s+)?(ice|police)|ice\s+(raids?|agents?)|no\s+kings|the\s+resistance|"
    r"back\s+the\s+blue|build\s+the\s+wall|deport\s+(them|em)\s+all|stop\s+the\s+steal|let'?s\s+go\s+brandon|"
    r"lock\s+(her|him)\s+up|drain\s+the\s+swamp|blue\s+lives|all\s+lives|black\s+lives|free\s+palestine|stand\s+with\s+israel)\b")
ABUSE = re.compile(
    r"\b(pedo\w*|p\s*d\s*files?|pdf\s*files?|g?rapists?|groomers?|traitors?|nazis?|kys|kill\s+your\s*self|scumbags?|"
    r"whores?|sluts?|retard\w*|pieces?\s+of\s+(shit|trash))\b")


def chat_rule(policy: Policy, text: str) -> Optional[str]:
    """Which of the streamer's own chat rules a message breaks, if any (disguised spellings included)."""
    from . import rules

    c = rules.normalize(text)
    t = " ".join(re.sub(r"[^\w\s']", " ", (c.leet or "") + " " + (c.text or "").lower()).split())
    for phrase in policy.blocked_phrases or []:
        p = " ".join(re.sub(r"[^\w\s']", " ", rules.normalize(str(phrase)).leet or "").split())
        if p and re.search(r"(?<!\w)" + re.escape(p) + r"(?!\w)", t):
            return f"Your chat rule: blocked words (\"{phrase}\")"
    if policy.no_abuse and ABUSE.search(t):
        return "Your chat rule: no insults or personal attacks"
    if policy.no_politics and POLITICS.search(t):
        return "Your chat rule: no political talk in this chat"
    return None


class Judge:
    def __init__(self, policy: Policy, canaries=(), known_bots=(), trusted=(), confirmed_bots=()):
        self.policy = policy
        self.scorer = MessageScorer(canaries=canaries, known_bots=known_bots)
        self.confirmed = set(confirmed_bots)          # "Likely bot" in the streamer's lists: blocked on sight
        self.trusted = set(trusted)                   # "platform:author_id"
        self.history: dict[str, deque] = defaultdict(lambda: deque(maxlen=30))
        self.recent_lines: deque = deque(maxlen=400)  # (time, normalized text, author)
        self.strikes: dict[str, int] = defaultdict(int)
        self.actions: deque = deque(maxlen=200)       # timestamps, for the rate cap

    def _protected(self, m: ChatMessage) -> bool:
        if m.badges & PROTECTED_BADGES or f"{m.platform}:{m.author_id}" in self.trusted:
            return True
        return self.policy.trust_subscribers and bool(m.badges & {"subscriber", "member", "founder"})

    def judge(self, m: ChatMessage) -> Decision:
        now = m.at or datetime.now(timezone.utc)
        key = f"{m.platform}:{m.author_id}"
        norm = _norm(m.text)
        hist = self.history[key]
        hist.append((now, norm))
        self.recent_lines.append((now, norm, key))
        if self._protected(m):
            return Decision("none", 0, [])

        msg = Message(kind="live", platform=m.platform, sender_id=m.author_id, text=m.text, at=now)
        sigs = {c: (s, t) for c, s, t in self.scorer._item_signals(msg)}
        # The same line from this chatter, recently.
        repeats = sum(1 for _, n in hist if n == norm and len(n) >= 4)
        if repeats >= 3:
            sigs["repeat_sender"] = (min(1.0, 0.6 + repeats / 20), f"Sent the same message {repeats} times")
        last_min = [t for t, _ in hist if now - t <= timedelta(seconds=60)]
        if len(last_min) >= 5:
            sigs["flooding"] = (1.0, f"{len(last_min)} messages in the last minute (flooding the chat)")
        # The same line from several accounts in the last two minutes.
        if len(norm) >= 12:
            authors = {a for t, n, a in self.recent_lines if n == norm and now - t <= timedelta(minutes=2)}
            if len(authors) >= 3:
                # A pitch or link posted by several accounts at once is a bot network; fans
                # chanting the same harmless line ("GG well played everyone!") is not.
                pitch = "solicitation" in sigs or "link_drop" in sigs or "fake_giveaway" in sigs
                sigs["coordinated_script"] = (1.0 if pitch else 0.5,
                                              f"Same scripted line as {len(authors) - 1} other accounts just now")
        rule = chat_rule(self.policy, m.text)
        if rule:
            sigs["chat_rule"] = (1.0, rule)
        bait = rules.evaluate_username(m.author_name or m.author_id)
        if bait:
            sigs["bait_name"] = (1.0, bait[0].reason)
        imp = impersonates(m.author_name or m.author_id, self.policy.protected_names)
        if imp:
            sigs["impersonation"] = (1.0, f"Name imitates {imp}, but it's a different account")
        if key in self.confirmed:
            sigs["confirmed_bot"] = (1.0, "A likely bot from your followers showed up in chat")
        elif key in self.scorer.known_bots:
            sigs["known_bot"] = (1.0, "Already flagged as a bot in your followers")

        if rules.keyboard_mash(m.author_name or m.author_id):   # "ynvpxs": weak alone, adds up with scam text
            sigs["random_name"] = (1.0, "Name is random letters")
        weights = {**WEIGHTS, "confirmed_bot": 0.85, "coordinated_script": 0.8, "impersonation": 0.85, "bait_name": 0.5, "chat_rule": 0.7,
                   "random_name": 0.25}
        reasons = sorted(((weights[c] * s, t, c) for c, (s, t) in sigs.items()), reverse=True)
        p = 1.0
        for w, _, _ in reasons:
            p *= 1 - w
        score = round(100 * (1 - p), 1)
        codes = {c for _, _, c in reasons}
        texts = [t for _, t, _ in reasons[:3]]

        pol = self.policy
        strong = bool(codes & {"canary", "confirmed_bot", "impersonation"}) or sigs.get("coordinated_script", (0,))[0] >= 1.0 \
            or sigs.get("fake_giveaway", (0,))[0] >= 1.0
        if (strong and score >= pol.timeout_at) or (score >= pol.ban_at and self.strikes[key] >= 2):
            action = "ban"
        elif score >= pol.timeout_at or (score >= pol.delete_at and self.strikes[key] >= 2):
            action = "timeout"
        elif score >= pol.delete_at:
            action = "delete"
        else:
            action = "none"
        if action != "none":
            self.strikes[key] += 1
            recent = [t for t in self.actions if time.monotonic() - t < 60]
            if len(recent) >= pol.max_actions_per_min:
                return Decision("none", score, texts + ["Rate cap reached: logged, not acted on"])
            self.actions.append(time.monotonic())
        return Decision(action, score, texts, pol.timeout_s if action == "timeout" else 0)


def _skeleton(name: str) -> str:
    from .rules import normalize

    t = normalize(name or "").leet
    t = re.sub(r"(official|real|backup|team|support|admin|mod|tv|live|_?\d{1,4})$", "", re.sub(r"[\s_.\-]+", "", t))
    return t


def impersonates(name: str, protected: list) -> Optional[str]:
    """The protected name this chatter imitates (lookalike letters, separators, "official"/"backup"...)."""
    from difflib import SequenceMatcher

    me = _skeleton(name)
    if len(me) < 4:
        return None
    for p in protected:
        if not p or p.lower() == (name or "").lower():
            continue  # the real account (badge checks also protect it)
        other = _skeleton(p)
        if len(other) >= 4 and (me == other or (abs(len(me) - len(other)) <= 2 and SequenceMatcher(None, me, other).ratio() >= 0.85)):
            return p
    return None


# ------------------------------------------------------------------------------------
# Moderators: apply a decision on the platform

class TwitchModerator:
    API = "https://api.twitch.tv/helix"

    def __init__(self, client_id: str, token: str, broadcaster_id: str, moderator_id: str, http: Optional[httpx.Client] = None):
        self.http = http or httpx.Client(timeout=15)
        self.h = {"Client-Id": client_id, "Authorization": f"Bearer {token}"}
        self.b, self.m = broadcaster_id, moderator_id

    def apply(self, msg: ChatMessage, d: Decision) -> None:
        if d.action == "delete" and msg.message_id:
            self.http.delete(f"{self.API}/moderation/chat", headers=self.h,
                             params={"broadcaster_id": self.b, "moderator_id": self.m, "message_id": msg.message_id}).raise_for_status()
        elif d.action in ("timeout", "ban"):
            data = {"user_id": msg.author_id, "reason": ("Bot Purge: " + "; ".join(d.reasons))[:500]}
            if d.action == "timeout":
                data["duration"] = d.duration_s
            self.http.post(f"{self.API}/moderation/bans", headers=self.h, params={"broadcaster_id": self.b, "moderator_id": self.m},
                           json={"data": data}).raise_for_status()

    def undo(self, author_id: str) -> None:
        self.http.delete(f"{self.API}/moderation/bans", headers=self.h,
                         params={"broadcaster_id": self.b, "moderator_id": self.m, "user_id": author_id}).raise_for_status()


class YouTubeModerator:
    API = "https://www.googleapis.com/youtube/v3"

    def __init__(self, token: str, live_chat_id: str, http: Optional[httpx.Client] = None):
        self.http = http or httpx.Client(timeout=15)
        self.h = {"Authorization": f"Bearer {token}"}
        self.chat = live_chat_id

    def apply(self, msg: ChatMessage, d: Decision) -> None:
        if msg.message_id and d.action in ("delete", "timeout", "ban"):
            self.http.delete(f"{self.API}/liveChat/messages", headers=self.h, params={"id": msg.message_id}).raise_for_status()
        if d.action in ("timeout", "ban"):
            snippet = {"liveChatId": self.chat, "type": "temporary" if d.action == "timeout" else "permanent",
                       "bannedUserDetails": {"channelId": msg.author_id}}
            if d.action == "timeout":
                snippet["banDurationSeconds"] = d.duration_s
            r = self.http.post(f"{self.API}/liveChat/bans", headers=self.h, params={"part": "snippet"}, json={"snippet": snippet})
            r.raise_for_status()

    def undo(self, ban_id: str) -> None:
        self.http.delete(f"{self.API}/liveChat/bans", headers=self.h, params={"id": ban_id}).raise_for_status()


# ------------------------------------------------------------------------------------
# Chat readers

TAGS = re.compile(r"^@(\S+) :(\w+)!\S+ PRIVMSG #(\w+) :(.*)$")


def parse_twitch_line(line: str) -> Optional[ChatMessage]:
    m = TAGS.match(line.rstrip("\r\n"))
    if not m:
        return None
    tags = dict(kv.split("=", 1) for kv in m.group(1).split(";") if "=" in kv)
    badges = {b.split("/")[0] for b in tags.get("badges", "").split(",") if b}
    if tags.get("mod") == "1":
        badges.add("moderator")
    return ChatMessage(platform="twitch", author_id=tags.get("user-id", m.group(2)), author_name=tags.get("display-name") or m.group(2),
                       text=m.group(4), message_id=tags.get("id", ""), badges=badges,
                       at=datetime.fromtimestamp(int(tags["tmi-sent-ts"]) / 1000, timezone.utc) if tags.get("tmi-sent-ts") else None)


def twitch_chat(channel: str, login: str, token: str, on_message: Callable[[ChatMessage], None], stop: threading.Event) -> None:
    """Read a Twitch channel's chat over IRC (TLS) until ``stop`` is set."""
    raw = socket.create_connection(("irc.chat.twitch.tv", 6697), timeout=10)
    sock = ssl.create_default_context().wrap_socket(raw, server_hostname="irc.chat.twitch.tv")
    send = lambda s: sock.sendall((s + "\r\n").encode())  # noqa: E731
    send(f"PASS oauth:{token}")
    send(f"NICK {login}")
    send("CAP REQ :twitch.tv/tags twitch.tv/commands")
    send(f"JOIN #{channel.lower()}")
    buf = ""
    sock.settimeout(1.0)
    try:
        while not stop.is_set():
            try:
                chunk = sock.recv(65536).decode("utf-8", "replace")
            except socket.timeout:
                continue
            if not chunk:
                break
            buf += chunk
            *lines, buf = buf.split("\r\n")
            for line in lines:
                if line.startswith("PING"):
                    send("PONG" + line[4:])
                elif (msg := parse_twitch_line(line)):
                    on_message(msg)
    finally:
        sock.close()


def youtube_chat(token: str, live_chat_id: str, on_message: Callable[[ChatMessage], None], stop: threading.Event,
                 http: Optional[httpx.Client] = None) -> None:
    http = http or httpx.Client(timeout=15)
    page = None
    while not stop.is_set():
        params = {"liveChatId": live_chat_id, "part": "snippet,authorDetails", "maxResults": 2000}
        if page:
            params["pageToken"] = page
        r = http.get(f"{YouTubeModerator.API}/liveChat/messages", headers={"Authorization": f"Bearer {token}"}, params=params)
        if r.status_code != 200:
            time.sleep(10)
            continue
        body = r.json()
        for it in body.get("items", []):
            a, sn = it.get("authorDetails", {}), it.get("snippet", {})
            badges = {k for k, v in (("owner", a.get("isChatOwner")), ("moderator", a.get("isChatModerator")),
                                     ("member", a.get("isChatSponsor"))) if v}
            on_message(ChatMessage(platform="youtube", author_id=a.get("channelId", ""), author_name=a.get("displayName", ""),
                                   text=sn.get("displayMessage", ""), message_id=it.get("id", ""), badges=badges,
                                   at=datetime.fromisoformat(sn["publishedAt"].replace("Z", "+00:00")) if sn.get("publishedAt") else None))
        page = body.get("nextPageToken")
        stop.wait(max(2.0, body.get("pollingIntervalMillis", 5000) / 1000))


# ------------------------------------------------------------------------------------
# Sessions

LIVE_PLATFORMS = ("twitch", "youtube", "tiktok", "instagram", "facebook", "kick", "x")


class LiveGuardService:
    """One session per stream. Chat arrives from a connector thread (Twitch, YouTube)
    or from the feed endpoint (the agent watching TikTok/Instagram from a mod account)."""

    def __init__(self, db, secrets_get: Callable[[str, str], Optional[str]], http: Optional[httpx.Client] = None):
        from .db import dumps, loads

        self.db, self.dumps, self.loads = db, dumps, loads
        self.secret = secrets_get
        self.http = http
        self.judges: dict[str, Judge] = {}
        self.moderators: dict[str, object] = {}
        self.stops: dict[str, threading.Event] = {}
        self.watchers: set[str] = set()      # sessions whose chat the desktop agent is reading

    def _judge_for(self, user_id: str, platform: str, policy: Policy) -> Judge:
        canaries = [r["token"] for r in self.db.q("SELECT token FROM canaries WHERE user_id=?", (user_id,))]
        bots = {f"{r['platform']}:{r['account_id']}" for r in self.db.q(
            "SELECT platform, account_id FROM flags WHERE user_id=? AND label IN ('likely_bot','suspicious') AND status!='whitelisted'", (user_id,))}
        bots |= {f"{r['platform']}:{r['sender_id']}" for r in self.db.q(
            "SELECT platform, sender_id FROM msg_senders WHERE user_id=? AND status IN ('active','removed') AND label IN ('likely_bot','suspicious')", (user_id,))}
        confirmed = {f"{r['platform']}:{r['account_id']}" for r in self.db.q(
            "SELECT platform, account_id FROM flags WHERE user_id=? AND label='likely_bot' AND status!='whitelisted'", (user_id,))}
        trusted = {f"{r['platform']}:{r['account_id']}" for r in self.db.q(
            "SELECT platform, account_id FROM flags WHERE user_id=? AND (status='whitelisted' OR (direction IN ('friend','following') AND label='looks_real'))", (user_id,))}
        trusted |= {f"{r['platform']}:{r['sender_id']}" for r in self.db.q(
            "SELECT platform, sender_id FROM msg_senders WHERE user_id=? AND status='whitelisted'", (user_id,))}
        return Judge(policy, canaries=canaries, known_bots=bots, trusted=trusted, confirmed_bots=confirmed)

    def start(self, user_id: str, platform: str, channel: str, policy: Optional[dict] = None, connect: bool = True) -> dict:
        from . import security as sec

        if platform not in LIVE_PLATFORMS:
            raise ValueError(f"platform must be one of {LIVE_PLATFORMS}")
        pol = Policy(**(policy or {}))
        if pol.mode not in ("watch", "protect"):
            raise ValueError("mode must be watch or protect")
        sid = sec.new_id("lg_")
        self.db.x("INSERT INTO lg_sessions(id,user_id,platform,channel,policy_json,state,started_at,stopped_at) VALUES (?,?,?,?,?,?,?,?)",
                  (sid, user_id, platform, channel, self.dumps(pol.__dict__), "running", sec.iso(), None))
        self.judges[sid] = self._judge_for(user_id, platform, pol)
        if connect and platform in ("twitch", "youtube"):
            self._connect(user_id, sid, platform, channel)
        return self.session(user_id, sid)

    def _connect(self, user_id: str, sid: str, platform: str, channel: str) -> None:
        import json as _json

        raw = self.secret(user_id, f"liveguard_{platform}")
        if not raw:
            raise ValueError(f"Connect your {platform.title()} moderator account first")
        cred = _json.loads(raw)
        stop = threading.Event()
        self.stops[sid] = stop
        if platform == "twitch":
            self.moderators[sid] = TwitchModerator(cred["client_id"], cred["token"], cred["broadcaster_id"], cred["moderator_id"], http=self.http)
            target = lambda: twitch_chat(channel, cred["login"], cred["token"], lambda m: self.feed(user_id, sid, [m]), stop)  # noqa: E731
        else:
            self.moderators[sid] = YouTubeModerator(cred["token"], channel, http=self.http)
            target = lambda: youtube_chat(cred["token"], channel, lambda m: self.feed(user_id, sid, [m]), stop, http=self.http)  # noqa: E731
        threading.Thread(target=target, daemon=True, name=f"liveguard-{sid}").start()

    def _row(self, user_id: str, sid: str):
        r = self.db.one("SELECT * FROM lg_sessions WHERE id=? AND user_id=?", (sid, user_id))
        if not r:
            raise LookupError("live session not found")
        return r

    def feed(self, user_id: str, sid: str, messages: list[ChatMessage]) -> list[dict]:
        from . import security as sec

        r = self._row(user_id, sid)
        if r["state"] != "running":
            raise ValueError("this Live Guard session has stopped")
        judge = self.judges.get(sid) or self.judges.setdefault(
            sid, self._judge_for(user_id, r["platform"], Policy(**self.loads(r["policy_json"]))))
        mod = self.moderators.get(sid)
        out = []
        for m in messages:
            d = judge.judge(m)
            applied, error = 0, None
            if d.action != "none" and judge.policy.mode == "protect" and mod is not None:
                try:
                    mod.apply(m, d)
                    applied = 1
                except Exception as exc:  # keep moderating even if one call fails
                    error = str(exc)[:300]
            event_id = None
            if d.action != "none" or d.score >= 40:
                with self.db.tx() as tx:
                    event_id = tx.execute("INSERT INTO lg_events(session_id,at,author_id,author_name,text,score,action,reasons_json,applied,error)"
                                          " VALUES (?,?,?,?,?,?,?,?,?,?)",
                                          (sid, sec.iso(m.at), m.author_id, m.author_name, m.text[:500], d.score, d.action,
                                           self.dumps(d.reasons), applied, error)).lastrowid
            # The chat as it scrolls by, with what Live Guard did to each message.
            self.db.x("INSERT INTO lg_chat(session_id,at,author_id,author_name,text,action,event_id) VALUES (?,?,?,?,?,?,?)",
                      (sid, sec.iso(m.at), m.author_id, m.author_name, m.text[:500], d.action, event_id))
            out.append({"message_id": m.message_id, "author_id": m.author_id, "author_name": m.author_name, "event_id": event_id,
                        "action": d.action, "score": d.score,
                        "reasons": d.reasons, "duration_s": d.duration_s,
                        # For callers that act themselves (the agent on TikTok/Instagram):
                        "act": d.action != "none" and judge.policy.mode == "protect" and mod is None})
        self.db.x("UPDATE lg_sessions SET checked=checked+? WHERE id=?", (len(messages), sid))
        # Keep only the recent chat; flagged messages stay in lg_events for the record.
        self.db.x("DELETE FROM lg_chat WHERE session_id=? AND id <= (SELECT id FROM lg_chat WHERE session_id=? ORDER BY id DESC LIMIT 1 OFFSET 300)",
                  (sid, sid))
        return out

    def mark_applied(self, user_id: str, sid: str, event_id: int, error: Optional[str] = None) -> None:
        """The agent reports it carried out an action on the platform (TikTok, Instagram…)."""
        self._row(user_id, sid)
        self.db.x("UPDATE lg_events SET applied=?, error=? WHERE id=? AND session_id=?", (0 if error else 1, error, event_id, sid))

    def set_mode(self, user_id: str, sid: str, mode: str) -> dict:
        if mode not in ("watch", "protect"):
            raise ValueError("mode must be watch or protect")
        r = self._row(user_id, sid)
        pol = self.loads(r["policy_json"])
        pol["mode"] = mode
        self.db.x("UPDATE lg_sessions SET policy_json=? WHERE id=?", (self.dumps(pol), sid))
        if sid in self.judges:
            self.judges[sid].policy.mode = mode
        return self.session(user_id, sid)

    def stop(self, user_id: str, sid: str) -> dict:
        from . import security as sec

        self._row(user_id, sid)
        if sid in self.stops:
            self.stops.pop(sid).set()
        self.judges.pop(sid, None)
        self.moderators.pop(sid, None)
        self.db.x("UPDATE lg_sessions SET state='stopped', stopped_at=? WHERE id=?", (sec.iso(), sid))
        return self.session(user_id, sid)

    def undo(self, user_id: str, sid: str, event_id: int) -> dict:
        self._row(user_id, sid)
        e = self.db.one("SELECT * FROM lg_events WHERE id=? AND session_id=?", (event_id, sid))
        if not e:
            raise LookupError("event")
        mod = self.moderators.get(sid)
        if e["applied"] and e["action"] in ("timeout", "ban") and isinstance(mod, TwitchModerator):
            mod.undo(e["author_id"])
        self.db.x("UPDATE lg_events SET undone=1 WHERE id=?", (event_id,))
        judge = self.judges.get(sid)
        if judge:
            r = self._row(user_id, sid)
            judge.trusted.add(f"{r['platform']}:{e['author_id']}")  # never touch them again this stream
        return dict(self.db.one("SELECT * FROM lg_events WHERE id=?", (event_id,)))

    def session(self, user_id: str, sid: str) -> dict:
        r = self._row(user_id, sid)
        counts = {x["action"]: x["n"] for x in self.db.q("SELECT action, COUNT(*) n FROM lg_events WHERE session_id=? GROUP BY action", (sid,))}
        events = [{**dict(e), "reasons": self.loads(e["reasons_json"], [])} for e in
                  self.db.q("SELECT * FROM lg_events WHERE session_id=? ORDER BY id DESC LIMIT 100", (sid,))]
        chat = [dict(c) for c in self.db.q(
            "SELECT id, at, author_id, author_name, text, action, event_id FROM lg_chat WHERE session_id=? ORDER BY id DESC LIMIT 80", (sid,))][::-1]
        counts.pop("none", None)
        return {"id": sid, "platform": r["platform"], "channel": r["channel"], "state": r["state"],
                "policy": self.loads(r["policy_json"]), "started_at": r["started_at"], "stopped_at": r["stopped_at"],
                "counts": counts, "checked": r["checked"], "connected": sid in self.moderators or sid in self.watchers,
                "watching": sid in self.watchers, "events": events, "chat": chat}

    def sessions(self, user_id: str) -> list[dict]:
        return [{k: r[k] for k in ("id", "platform", "channel", "state", "started_at", "stopped_at")}
                for r in self.db.q("SELECT * FROM lg_sessions WHERE user_id=? ORDER BY started_at DESC", (user_id,))]
