"""Bots and scammers in your inbox, comments, live chat and email.

Every item (a DM, a comment, a live-chat line, an email) is checked for bot
behaviour. Senders are then scored on everything they sent, because the sender
is what the user acts on: block, report, remove or unsubscribe.

Signals are about *behaviour*, never opinions. Someone flooding a live stream
with the same line gets flagged whatever side the line is on; someone arguing
a view in their own words never does.

Sources, all read locally from files the user chose:
  * DMs from TikTok, Instagram, Facebook and X data exports
  * comments and live chat pasted in or uploaded as CSV/JSON (exports don't include them)
  * email as .mbox (Gmail Takeout, Thunderbird, Apple Mail) or .eml files
"""
from __future__ import annotations

import csv
import email
import email.policy
import hashlib
import io
import json
import mailbox
import re
import tempfile
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Iterable, Optional
from urllib.parse import urlparse

from .importers import ImportError_, _ts
from .models import label_for

# ---------------------------------------------------------------------------------
# Items

@dataclass
class Message:
    kind: str                      # dm | comment | live | email
    platform: str                  # tiktok | instagram | facebook | x | linkedin | email | other
    sender_id: str
    text: str
    sender_name: str = ""
    sender_handle: str = ""
    at: Optional[datetime] = None
    context: str = ""              # post/video/live id, or email subject
    headers: dict = field(default_factory=dict)   # email only
    links: list[tuple[str, str]] = field(default_factory=list)  # email: (shown text, real href)

    @property
    def id(self) -> str:
        raw = f"{self.kind}|{self.platform}|{self.sender_id}|{self.at}|{self.context}|{self.text[:200]}"
        return hashlib.sha1(raw.encode()).hexdigest()[:20]


# ---------------------------------------------------------------------------------
# Social signals

WEIGHTS = {
    # social
    "bracket_emoji": 0.60,
    "solicitation": 0.45,
    "link_drop": 0.30,
    "repeat_sender": 0.45,
    "template_across_senders": 0.60,
    "flooding": 0.55,
    "generic_comment": 0.20,
    "fake_giveaway": 0.85,
    "ai_artifact": 0.55,
    "canary": 0.97,
    "known_bot": 0.50,
    "faceless_avatar": 0.20,
    # email
    "auth_fail": 0.45,
    "reply_to_mismatch": 0.35,
    "brand_spoof": 0.70,
    "lookalike_domain": 0.75,
    "phishing_language": 0.40,
    "link_mismatch": 0.55,
    "raw_ip_link": 0.45,
    "free_mail_brand": 0.45,
}

# TikTok/Douyin emoji shortcodes that bots paste as text instead of the emoji: [tearsofjoy], [smile], [wronged]...
BRACKET = re.compile(r"\[(?:[a-z]{3,20}|[A-Z][a-z]{2,20})\]")
SOLICIT = re.compile(
    r"(check (out )?my (profile|bio|page|link)|link in (my )?bio|dm me|message me|text me|add me on|"
    r"whats ?app|telegram|t\.me/|snap(chat)?:|kik|cash ?app|venmo|zelle|paypal me|"
    r"invest(ment|ing)? (with|opportunit)|forex|crypto (signal|trad)|bitcoin|profit daily|earn \$?\d|"
    r"sugar (daddy|mommy|baby)|onlyfans|of link|18\+|hot (pics|singles)|lonely|"
    r"promot(e|ion|er) (your|ur) (account|page|stream|channel|videos?|music)|grow your (account|page|followers|stream|channel)|"
    r"free (followers|viewers|likes)|dm @\w+|"
    r"collab\?? (dm|send)|brand ambassador|ambassador program|send (me )?(a )?dm)",
    re.I,
)
URL = re.compile(r"(https?://\S+|www\.\S+|\b(bit\.ly|tinyurl\.com|t\.co|linktr\.ee|t\.me|wa\.me)/\S+)", re.I)
PHONE = re.compile(r"(\+?\d[\d\s().-]{8,}\d)")
GENERIC = re.compile(
    r"^\W*(nice|great|good|amazing|awesome|cool|wow|beautiful|lovely|so cute|love (this|it)|"
    r"great (content|video|post)|nice (video|post|pic)|keep it up|keep going|well done|so beautiful|"
    r"i love your (content|videos?)|you are (so )?(beautiful|amazing)|interesting|so true|agree)"
    r"[\s!.,❤️🔥😍👍💯🙌✨😊👏]*$",
    re.I,
)
# "The first 10 people to type WIN get $10,000", "I'm giving away $5,000 to everyone who comments",
# "gifting 20 followers", "claim at the link in my bio": prize bait from someone who isn't the host.
GIVEAWAY = re.compile(
    r"((first|next) \d+ (people|persons|viewers|followers|to)|everyone who (comments?|types?|dm|messages?)|"
    r"(giving|give) ?away|giveaway|gifting|donat(e|ing)|airdrop|double your|send \$?\d+ (and|&) (get|receive))"
    r".{0,80}(\$ ?\d[\d,.]*k?|\d[\d,.]* ?(dollars|usd|usdt|btc|eth|bitcoin|coins|gifts?)|iphone|ps5|gift ?card|cash)"
    r"|(\$ ?\d[\d,.]*k?|\d[\d,.]* ?(dollars|usd|usdt|btc|eth))\s.{0,60}(first|next) \d+",
    re.I,
)
CLAIM = re.compile(r"(claim|dm me|message me|text me|type|comment|link in (my )?bio|telegram|whats ?app|t\.me)", re.I)

AI_ARTIFACT = re.compile(
    r"(as an ai( language model)?|i('m| am) an ai|i cannot (comply|provide|help with)|"
    r"^certainly!|^sure! here('s| is)|here('s| is) a (comment|reply|response)|"
    r"automated system|this transcript|\bgenerate a comment\b|as requested,? here)",
    re.I,
)


def _norm(text: str) -> str:
    t = text.lower()
    t = URL.sub("<url>", t)
    t = re.sub(r"@\w+", "<user>", t)
    t = re.sub(r"\d+", "<n>", t)
    t = re.sub(r"[^\w<> ]", "", t)
    return " ".join(t.split())


# ---------------------------------------------------------------------------------
# Email signals

BRANDS = {
    "paypal": ["paypal.com"], "apple": ["apple.com", "icloud.com"], "amazon": ["amazon.com", "amazon.co.uk", "amazon.ca"],
    "microsoft": ["microsoft.com", "outlook.com", "live.com", "office.com"], "netflix": ["netflix.com"],
    "google": ["google.com", "gmail.com", "youtube.com"], "facebook": ["facebook.com", "facebookmail.com", "meta.com"],
    "instagram": ["instagram.com", "mail.instagram.com"], "tiktok": ["tiktok.com"], "usps": ["usps.com"],
    "fedex": ["fedex.com"], "ups": ["ups.com"], "dhl": ["dhl.com"], "irs": ["irs.gov"], "chase": ["chase.com"],
    "wells fargo": ["wellsfargo.com"], "bank of america": ["bankofamerica.com", "bofa.com"], "coinbase": ["coinbase.com"],
    "venmo": ["venmo.com"], "cash app": ["cash.app", "square.com"], "geek squad": ["geeksquad.com", "bestbuy.com"],
    "norton": ["norton.com"], "mcafee": ["mcafee.com"], "linkedin": ["linkedin.com"], "x": ["x.com", "twitter.com"],
}
FREE_MAIL = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "aol.com", "icloud.com", "proton.me", "protonmail.com", "gmx.com", "mail.com"}
PHISH = re.compile(
    r"(verify your (account|identity)|account (has been |will be )?(suspended|locked|limited|disabled)|unusual (sign.?in|activity)|"
    r"confirm your (password|payment|details)|update your (payment|billing)|gift ?card|wire transfer|"
    r"you('ve| have) won|claim your (prize|reward|refund)|final notice|immediate(ly)? action|within 24 hours|"
    r"invoice (attached|#)|payment (failed|declined)|package (could not|couldn't) be delivered|"
    r"your subscription (has|will) (expire|renew)|refund of \$|bitcoin|seed phrase|recovery phrase)",
    re.I,
)


def _domain(addr: str) -> str:
    return addr.rsplit("@", 1)[-1].lower().strip(">").strip() if "@" in addr else ""


def _base(domain: str) -> str:
    parts = domain.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain


def _brand_domains() -> list[str]:
    return [d for ds in BRANDS.values() for d in ds]


def _lookalike(domain: str) -> Optional[str]:
    """A domain one or two edits from a known brand's (paypa1.com, arnazon.com), but not the real one."""
    b = _base(domain)
    if not b or b in _brand_domains():
        return None
    swapped = b.replace("0", "o").replace("1", "l").replace("rn", "m").replace("vv", "w")
    for real in _brand_domains():
        if swapped == real or (abs(len(b) - len(real)) <= 2 and SequenceMatcher(None, b, real).ratio() >= 0.85):
            return real
    name = swapped.split(".")[0]
    for real in _brand_domains():
        rn = real.split(".")[0]
        # paypal-security-alerts.com, paypa1-secure.com, secure-paypal.com (not pineapple-shop.com)
        if len(rn) >= 5 and name != rn and re.search(rf"(^|-){re.escape(rn)}", name):
            return real
    return None


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: Optional[str] = None
        self._text: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href") or ""
            self._text = []

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append(("".join(self._text).strip(), self._href))
            self._href = None

    def handle_data(self, data):
        self.text.append(data)
        if self._href is not None:
            self._text.append(data)


# ---------------------------------------------------------------------------------
# Scoring

@dataclass
class SenderResult:
    platform: str
    sender_id: str
    sender_name: str
    sender_handle: str
    items: int
    score: float
    label: str
    reasons: list[dict]
    first_at: Optional[datetime]
    last_at: Optional[datetime]
    item_scores: dict[str, tuple[float, list[dict]]]


class MessageScorer:
    def __init__(self, canaries: Iterable[str] = (), known_bots: Iterable[str] = (),
                 faceless: Iterable[str] = (), multipliers: Optional[dict] = None):
        self.canaries = [c.lower() for c in canaries if c]
        self.known_bots = set(known_bots)     # "platform:sender_id" already flagged in the user's lists
        self.faceless = set(faceless)         # "platform:sender_id" with a faceless / back-of-head avatar
        self.mult = multipliers or {}

    def _w(self, code: str) -> float:
        return min(0.97, WEIGHTS[code] * self.mult.get(code, 1.0))

    def _item_signals(self, m: Message) -> list[tuple[str, float, str]]:
        out: list[tuple[str, float, str]] = []
        add = lambda c, s, t: out.append((c, max(0.0, min(1.0, s)), t))  # noqa: E731
        text = m.text or ""
        low = text.lower()
        for tok in self.canaries:
            if tok in low:
                add("canary", 1.0, "Repeated your hidden canary phrase — only an AI following instructions does that")
                break
        if m.kind == "email":
            self._email_signals(m, add)
            return out
        brackets = BRACKET.findall(text)
        if brackets:
            add("bracket_emoji", 1.0 if len(brackets) >= 2 or len(text) < 60 else 0.8,
                f"Uses emoji codes a person would never type ({', '.join(brackets[:3])})")
        if SOLICIT.search(text) or (PHONE.search(text) and m.kind != "email"):
            add("solicitation", 1.0 if m.kind == "dm" else 0.8, "Solicits: pushes you to a profile, app, number or money offer")
        if URL.search(text):
            add("link_drop", 1.0 if m.kind in ("comment", "live") else 0.6, "Drops a link")
        if GIVEAWAY.search(text):
            add("fake_giveaway", 1.0 if CLAIM.search(text) else 0.7,
                "Prize bait (\"first 10 people to type ... get $10,000\"): a classic fake-giveaway script")
        if m.kind in ("comment", "live") and GENERIC.match(text.strip()):
            add("generic_comment", 1.0, "A one-size-fits-all comment that could go under any video")
        if AI_ARTIFACT.search(text):
            add("ai_artifact", 1.0, "Reads like an AI writing on autopilot (it followed an instruction or talks like a chatbot)")
        return out

    def _email_signals(self, m: Message, add) -> None:
        h = {k.lower(): v for k, v in m.headers.items()}
        from_name, from_addr = parseaddr(h.get("from", ""))
        dom = _domain(from_addr)
        auth = h.get("authentication-results", "").lower()
        fails = [k for k in ("spf", "dkim", "dmarc") if re.search(rf"\b{k}=(fail|softfail|permerror)", auth)]
        if fails:
            add("auth_fail", 1.0 if "dmarc" in fails else 0.7, f"Failed sender checks ({', '.join(fails).upper()}): it may not be from who it says")
        reply = parseaddr(h.get("reply-to", ""))[1]
        if reply and _base(_domain(reply)) != _base(dom):
            add("reply_to_mismatch", 1.0, f"Replies go to a different address ({_domain(reply)}) than the sender ({dom})")
        for brand, domains in BRANDS.items():
            if re.search(rf"\b{re.escape(brand)}\b", from_name.lower()):
                if _base(dom) not in {_base(d) for d in domains}:
                    if dom in FREE_MAIL:
                        add("free_mail_brand", 1.0, f"Claims to be {brand.title()} but writes from a free {dom} address")
                    else:
                        add("brand_spoof", 1.0, f"Claims to be {brand.title()} but comes from {dom}")
                break
        look = _lookalike(dom)
        if look:
            add("lookalike_domain", 1.0, f"Sender domain {dom} imitates {look}")
        if PHISH.search(m.text) or PHISH.search(m.context):
            add("phishing_language", 1.0, "Uses scam pressure (account locked, verify now, prize, gift card, final notice)")
        for shown, href in m.links:
            host = urlparse(href).hostname or ""
            if re.fullmatch(r"[\d.]+", host):
                add("raw_ip_link", 1.0, "Links to a bare IP address instead of a website")
                break
            shown_host = re.search(r"([a-z0-9-]+\.)+[a-z]{2,}", shown.lower())
            if shown_host and host and _base(shown_host.group(0)) != _base(host.lower()):
                add("link_mismatch", 1.0, f"A link says {shown_host.group(0)} but really goes to {host}")
                break

    def score(self, messages: list[Message]) -> list[SenderResult]:
        by_sender: dict[tuple[str, str], list[Message]] = defaultdict(list)
        for m in messages:
            by_sender[(m.platform, m.sender_id)].append(m)

        # Templates shared by several different senders (a bot network), social only.
        tmpl_senders: dict[str, set] = defaultdict(set)
        for m in messages:
            if m.kind != "email":
                t = _norm(m.text)
                if len(t) >= 12:
                    tmpl_senders[t].add((m.platform, m.sender_id))

        results: list[SenderResult] = []
        for (platform, sid), items in by_sender.items():
            best: dict[str, tuple[float, str]] = {}
            item_scores: dict[str, tuple[float, list[dict]]] = {}
            for m in items:
                sigs = self._item_signals(m)
                reasons = [{"code": c, "text": t, "weight": self._w(c) * s} for c, s, t in sigs]
                p = 1.0
                for r in reasons:
                    p *= 1 - r["weight"]
                item_scores[m.id] = (round(100 * (1 - p), 1), sorted(reasons, key=lambda r: -r["weight"]))
                for c, s, t in sigs:
                    if c not in best or s > best[c][0]:
                        best[c] = (s, t)

            social = [m for m in items if m.kind != "email"]
            # The same message over and over from this sender.
            counts = Counter(_norm(m.text) for m in social if len(_norm(m.text)) >= 4)
            if counts:
                text, n = counts.most_common(1)[0]
                if n >= 3:
                    best["repeat_sender"] = (min(1.0, 0.6 + n / 20), f"Sent the same message {n} times")
            # Flooding: bursts of messages in a live chat or comment section.
            times = sorted(m.at for m in social if m.at and m.kind in ("live", "comment"))
            if len(times) >= 5:
                window = [(b - a).total_seconds() for a, b in zip(times, times[4:])]
                if window and min(window) <= 60:
                    best["flooding"] = (1.0, "Posted 5+ messages within a minute (flooding the chat)")
            # Shared with other senders.
            shared, pitch = 0, False
            for m in social:
                t = _norm(m.text)
                if len(t) >= 12 and len(tmpl_senders[t]) > shared:
                    shared, pitch = len(tmpl_senders[t]), bool(SOLICIT.search(m.text) or URL.search(m.text))
            if shared >= 3:
                # A sales pitch or link posted word for word by several accounts is a coordinated network.
                best["template_across_senders"] = (1.0 if pitch else min(1.0, 0.6 + shared / 30),
                                                   f"Sent the same scripted message as {shared - 1} other accounts")
            key = f"{platform}:{sid}"
            if key in self.known_bots:
                best["known_bot"] = (1.0, "Already flagged as a bot in your followers or friends")
            if key in self.faceless:
                best["faceless_avatar"] = (1.0, "Profile photo shows no face (back of head, object or blank)")

            reasons = [{"code": c, "text": t, "weight": self._w(c) * s} for c, (s, t) in best.items()]
            reasons.sort(key=lambda r: -r["weight"])
            p = 1.0
            for r in reasons:
                p *= 1 - r["weight"]
            score = round(100 * (1 - p), 1)
            ats = [m.at for m in items if m.at]
            results.append(SenderResult(
                platform=platform, sender_id=sid,
                sender_name=next((m.sender_name for m in items if m.sender_name), ""),
                sender_handle=next((m.sender_handle for m in items if m.sender_handle), ""),
                items=len(items), score=score, label=label_for(score).value, reasons=reasons,
                first_at=min(ats) if ats else None, last_at=max(ats) if ats else None, item_scores=item_scores,
            ))
        return results


# ---------------------------------------------------------------------------------
# Importers

def _self_name(names: Iterable[str]) -> str:
    """The export's owner is whoever appears in the most conversations."""
    c = Counter(names)
    return c.most_common(1)[0][0] if c else ""


def parse_meta_inbox(platform: str, files: list[tuple[str, bytes]]) -> list[Message]:
    """Instagram / Facebook Messenger: messages/inbox/<thread>/message_N.json"""
    threads = []
    for name, blob in files:
        if re.search(r"message_\d+\.json$", name):
            threads.append((name, json.loads(blob.decode("utf-8"))))
    me = _self_name(p.get("name", "") for _, t in threads for p in t.get("participants", []))
    out = []
    for name, t in threads:
        for msg in t.get("messages", []):
            sender = msg.get("sender_name", "")
            if not sender or sender == me or "content" not in msg:
                continue
            try:
                sender = sender.encode("latin-1").decode("utf-8")
                content = msg["content"].encode("latin-1").decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                content = msg["content"]
            out.append(Message(kind="dm", platform=platform, sender_id=hashlib.sha1(sender.lower().encode()).hexdigest()[:16],
                               sender_name=sender, text=content, context=name.split("/")[-2] if "/" in name else "",
                               at=_ts(msg.get("timestamp_ms", 0) / 1000 if msg.get("timestamp_ms") else None)))
    return out


def parse_tiktok_dms(obj: dict) -> list[Message]:
    out = []
    for key, chats in _walk_key(obj, "ChatHistory"):
        for title, msgs in (chats or {}).items():
            other = re.sub(r"^Chat History with\s*", "", title).rstrip(":").strip()
            for m in msgs or []:
                frm = m.get("From", "")
                if frm and frm != other:
                    continue  # the user's own messages
                out.append(Message(kind="dm", platform="tiktok", sender_id=(other or frm).lower(), sender_handle=other or frm,
                                   text=m.get("Content", ""), at=_ts(m.get("Date"))))
    return out


def _walk_key(obj, key):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                yield k, v
            else:
                yield from _walk_key(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_key(v, key)


def parse_x_dms(text: str) -> list[Message]:
    text = re.sub(r"^\s*window\.YTD\.\w+\.part\d+\s*=\s*", "", text)
    convs = json.loads(text)
    senders = Counter()
    rows = []
    for c in convs:
        for m in (c.get("dmConversation") or {}).get("messages", []):
            mc = m.get("messageCreate")
            if mc:
                senders[mc.get("senderId")] += 1
                rows.append(mc)
    # The export's owner is in every conversation, so they're the top sender.
    me = senders.most_common(1)[0][0] if senders else None
    out = []
    for mc in rows:
        if mc.get("senderId") == me:
            continue
        out.append(Message(kind="dm", platform="x", sender_id=str(mc.get("senderId")), text=mc.get("text", ""),
                           at=_ts(mc.get("createdAt"))))
    return out


def parse_email_bytes(raw: bytes) -> Optional[Message]:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    return _email_to_message(msg)


def _email_to_message(msg) -> Optional[Message]:
    from_hdr = str(msg.get("From", ""))
    name, addr = parseaddr(from_hdr)
    if not addr:
        return None
    text_parts, links = [], []
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_content()
        except (LookupError, KeyError, AssertionError):
            payload = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
        if ctype == "text/html":
            p = _Links()
            p.feed(payload)
            links.extend(p.links)
            text_parts.append(" ".join(p.text))
        else:
            text_parts.append(payload)
    try:
        at = parsedate_to_datetime(str(msg.get("Date"))) if msg.get("Date") else None
        if at and not at.tzinfo:
            at = at.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        at = None
    headers = {k: str(msg.get(k, "")) for k in ("From", "Reply-To", "Authentication-Results", "Return-Path", "List-Unsubscribe")}
    return Message(kind="email", platform="email", sender_id=addr.lower(), sender_name=name, sender_handle=addr.lower(),
                   text=" ".join(text_parts)[:20000], at=at, context=str(msg.get("Subject", ""))[:300],
                   headers=headers, links=links[:200])


def parse_mbox(data: bytes) -> list[Message]:
    with tempfile.NamedTemporaryFile(suffix=".mbox") as f:
        f.write(data)
        f.flush()
        box = mailbox.mbox(f.name, factory=lambda fp: email.message_from_binary_file(fp, policy=email.policy.default))
        out = [m for m in (_email_to_message(msg) for msg in box) if m]
        box.close()
    return out


def parse_csv(text: str, platform: str = "other") -> list[Message]:
    """Columns: kind, platform, sender, sender_name, text, at, context (only sender and text required)."""
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        sender = row.get("sender") or row.get("sender_id") or row.get("username") or row.get("handle") or ""
        body = row.get("text") or row.get("comment") or row.get("message") or ""
        if not sender or not body:
            continue
        out.append(Message(kind=row.get("kind") or "comment", platform=row.get("platform") or platform,
                           sender_id=sender.lstrip("@").lower(), sender_handle=sender.lstrip("@"),
                           sender_name=row.get("sender_name", ""), text=body, at=_ts(row.get("at") or row.get("date")),
                           context=row.get("context", "")))
    return out


PASTE_LINE = re.compile(r"^\s*@?([\w.]{2,40})\s*[:\-–—]\s+(.+)$")


def parse_paste(text: str, platform: str, kind: str, context: str = "") -> list[Message]:
    """Comments or live chat copied from the app, one per line as "handle: message".

    Also accepts the two-line layout the apps copy as (handle on one line, comment on the next).
    """
    lines = [l.rstrip() for l in text.splitlines() if l.strip()]
    out, i = [], 0
    base = datetime.now(timezone.utc)
    while i < len(lines):
        m = PASTE_LINE.match(lines[i])
        if m:
            handle, body = m.group(1), m.group(2)
            i += 1
        elif i + 1 < len(lines) and re.fullmatch(r"@?[\w.]{2,40}", lines[i].strip()):
            handle, body = lines[i].strip(), lines[i + 1]
            i += 2
        else:
            i += 1
            continue
        out.append(Message(kind=kind, platform=platform, sender_id=handle.lstrip("@").lower(), sender_handle=handle.lstrip("@"),
                           text=body.strip(), context=context,
                           # Pasted live chat is a fast stream: keep its order, one second apart, so
                           # flooding is measurable. Pasted comments have no reliable time.
                           at=datetime.fromtimestamp(base.timestamp() + len(out), timezone.utc) if kind == "live" else None))
    return out


def import_messages(source: str, filename: str, data: bytes) -> list[Message]:
    """source: tiktok | instagram | facebook | x | email | csv"""
    files: list[tuple[str, bytes]] = []
    if data[:2] == b"PK":
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise ImportError_("Not a valid zip file") from exc
        for info in zf.infolist():
            if not info.is_dir() and info.file_size < 200 * 1024 * 1024:
                n = info.filename.lower()
                if re.search(r"(message_\d+\.json|user_data(_tiktok)?\.json|direct-messages?(-group)?\.js|\.mbox|\.eml)$", n):
                    files.append((info.filename, zf.read(info)))
    else:
        files.append((filename, data))
    out: list[Message] = []
    try:
        if source in ("instagram", "facebook"):
            out = parse_meta_inbox(source, files)
        elif source == "tiktok":
            for _, blob in files:
                out += parse_tiktok_dms(json.loads(blob.decode("utf-8")))
        elif source == "x":
            for _, blob in files:
                out += parse_x_dms(blob.decode("utf-8"))
        elif source == "email":
            for name, blob in files:
                if name.lower().endswith(".eml") or (not name.lower().endswith(".mbox") and not blob.startswith(b"From ")):
                    m = parse_email_bytes(blob)
                    out += [m] if m else []
                else:
                    out += parse_mbox(blob)
        elif source == "csv":
            for _, blob in files:
                out += parse_csv(blob.decode("utf-8-sig"))
        else:
            raise ImportError_(f"Unknown source {source}")
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError) as exc:
        raise ImportError_(f"Could not read that file as {source} messages: {exc}") from exc
    if not out:
        raise ImportError_("No messages from other people were found in that file")
    return out

