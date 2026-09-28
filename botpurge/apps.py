"""Connected apps: third-party apps holding access to the user's social accounts.

Follower-growth, auto-like and "who unfollowed me / who viewed my profile" apps
are bots that act through *your* account: they follow, like and DM in your
name, and some steal the access outright. Each app found in a data export is
scored and explained, with steps to revoke it.

Sources: X archive ``data/connected-application.js``; Facebook/Instagram
"Apps and websites" JSON (file and key names vary, so both are matched
loosely); or a CSV (name, permissions, approved_at, url, description).
"""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .importers import ImportError_, _ts

STRENGTH = {"weak": 0.2, "medium": 0.4, "strong": 0.65, "near_certain": 0.9}

GROWTH = re.compile(
    r"\b(follow(er)?s?\s*(pro|plus|boost|gain|track(er)?)|unfollow(er)?s?|un-?follow|who\s*unfollowed|ghost\s*followers?|"
    r"auto[\s-]?(like|follow|liker|follower|comment|dm|post)|likes?\s*(boost|pro|get|4|for)|get\s*(likes|followers)|booster?|"
    r"growth|gainer|insta(gram)?[\s-]?(boost|likes|follow)|tik\s*(boost|fans|likes)|fans?\s*(boost|plus)|engagement\s*(pod|boost)|"
    r"mass\s*(follow|unfollow|dm)|nakrutka|накрутка|free\s*followers)\b", re.I)
VIEWERS = re.compile(r"(who\s*(viewed|visited|stalk(s|ed)?|sees?)\s*(my|your)?\s*(profile|account)|profile\s*(viewers?|visitors?|stalkers?)|stalker\s*(finder|checker))", re.I)
CRYPTO = re.compile(r"(airdrop|giveaway|nft\s*(mint|drop)|wallet\s*(connect|verify)|free\s*(btc|eth|crypto|robux|v-?bucks))", re.I)
QUIZ = re.compile(r"(quiz|which .* are you|personality|horoscope|face\s*(app|swap|age)|photo\s*(edit|effect|filter))", re.I)
FREE_HOSTS = re.compile(r"(gmail\.com|yahoo\.|outlook\.com|hotmail\.|wixsite\.com|blogspot\.|weebly\.com|000webhost|github\.io|netlify\.app|vercel\.app|pages\.dev|workers\.dev)", re.I)
WRITE = re.compile(r"\b(write|tweet\.write|follows\.write|like\.write|list\.write|block\.write|publish|manage)\b", re.I)
DMS = re.compile(r"(direct\s*messages?|dm\.(read|write)|read_mailbox|pages_messaging|instagram_manage_messages)", re.I)


@dataclass
class ConnectedApp:
    platform: str
    name: str
    permissions: list[str] = field(default_factory=list)
    approved_at: Optional[datetime] = None
    org_name: str = ""
    org_url: str = ""
    description: str = ""
    status: str = "active"          # active | expired | removed (Facebook/Instagram)


@dataclass
class AppVerdict:
    app: ConnectedApp
    score: float
    label: str
    reasons: list[dict]


def score_app(a: ConnectedApp, others: list[ConnectedApp], now: Optional[datetime] = None) -> AppVerdict:
    now = now or datetime.now(timezone.utc)
    text = f"{a.name} {a.org_name} {a.description}"
    perms = " ".join(a.permissions)
    hits: list[tuple[str, str, str]] = []
    growth, write, dms = bool(GROWTH.search(text)), bool(WRITE.search(perms)), bool(DMS.search(perms))
    if VIEWERS.search(text):
        hits.append(("profile_viewer_app", "near_certain", "Claims to show who viewed your profile. No platform allows that, so it's a scam"))
    if growth:
        hits.append(("growth_app", "strong" if (write or dms) else "medium",
                     "A follower-growth or auto-like app: it follows, likes or posts as you" if write else "A follower-growth or unfollow-tracker app"))
    if CRYPTO.search(text):
        hits.append(("crypto_giveaway_app", "strong" if write else "medium", "Crypto, NFT or free-currency giveaway app"))
    if dms:
        hits.append(("dm_access", "strong" if growth else "medium", "Can read or send your direct messages"))
    elif write and not growth:
        hits.append(("write_access", "weak", "Can post, follow or like as you"))
    if QUIZ.search(text):
        hits.append(("quiz_app", "weak", "Quiz or photo-effect app (a common way to harvest profile data)"))
    if a.platform == "x" and (not a.org_url or FREE_HOSTS.search(a.org_url)) and len(a.description.strip()) < 20:
        hits.append(("thin_publisher", "weak", "Publisher gives no real website or description"))
    if a.approved_at and (now - a.approved_at).days > 730 and a.status == "active":
        hits.append(("dormant_access", "weak", f"You approved it {(now - a.approved_at).days // 365} years ago and it still has access"))
    if growth:
        twins = [o for o in others if o is not a and GROWTH.search(f"{o.name} {o.description}")]
        if twins:
            hits.append(("repeat_growth_apps", "medium", f"One of {len(twins) + 1} growth apps you've connected"))
    reasons = sorted(({"code": c, "text": t, "weight": STRENGTH[s]} for c, s, t in hits), key=lambda r: -r["weight"])
    p = 1.0
    for r in reasons:
        p *= 1 - r["weight"]
    score = round(100 * (1 - p), 1)
    label = "likely_bot" if score >= 85 else "suspicious" if score >= 60 else "low_confidence" if score >= 40 else "looks_real"
    return AppVerdict(a, score, label, reasons)


REVOKE_STEPS = {
    "x": ["Open X > Settings and privacy > Security and account access > Apps and sessions > Connected apps.",
          "Select the app and choose Revoke app permissions."],
    "facebook": ["Open Facebook > Settings & privacy > Settings > Apps and websites.",
                 "Select the app, choose Remove, and tick the option to delete what it posted if offered."],
    "instagram": ["Open Instagram > Settings > Accounts Center > Your information and permissions (or Website permissions > Apps and websites).",
                  "Select the app and choose Remove."],
    "tiktok": ["Open TikTok > Profile > Menu > Settings and privacy > Security > Manage app permissions.", "Select the app and choose Remove access."],
    "google": ["Open myaccount.google.com > Security > Your connections to third-party apps & services.", "Select the app and choose Delete all connections."],
}


# ---------------------------------------------------------------------------------
# Importers

def parse_x_connected(text: str) -> list[ConnectedApp]:
    text = re.sub(r"^\s*window\.YTD\.\w+\.part\d+\s*=\s*", "", text)
    out = []
    for it in json.loads(text):
        a = it.get("connectedApplication", it)
        org = a.get("organization") or {}
        approved = a.get("approvedAt") or (int(a["approvedAtMsec"]) / 1000 if a.get("approvedAtMsec") else None)
        out.append(ConnectedApp(platform="x", name=a.get("name", ""), permissions=[str(p) for p in a.get("permissions", [])],
                                approved_at=_ts(approved), org_name=org.get("name", ""), org_url=org.get("url", "") or "",
                                description=a.get("description", "") or ""))
    return out


def parse_meta_apps(platform: str, obj) -> list[ConnectedApp]:
    """Facebook/Instagram 'Apps and websites': key names vary by export version, so match loosely."""
    out = []

    def walk(o, status="active"):
        if isinstance(o, dict):
            for k, v in o.items():
                kl = k.lower()
                st = "expired" if ("expired" in kl or "inactive" in kl) else "removed" if "removed" in kl else status
                if isinstance(v, list) and any(isinstance(x, dict) and ("name" in x or "title" in x) for x in v) and \
                        re.search(r"apps?|websites?|installed|active|expired|removed", kl):
                    for x in v:
                        if isinstance(x, dict) and (x.get("name") or x.get("title")):
                            out.append(ConnectedApp(platform=platform, name=x.get("name") or x.get("title"),
                                                    approved_at=_ts(x.get("added_timestamp") or x.get("timestamp")), status=st,
                                                    permissions=[str(p) for p in x.get("permissions", [])] if isinstance(x.get("permissions"), list) else []))
                else:
                    walk(v, st)
        elif isinstance(o, list):
            for x in o:
                walk(x, status)

    walk(obj)
    return out


def parse_apps_csv(text: str, platform: str) -> list[ConnectedApp]:
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        if row.get("name"):
            out.append(ConnectedApp(platform=row.get("platform") or platform, name=row["name"],
                                    permissions=[p for p in re.split(r"[|,;]\s*", row.get("permissions", "")) if p],
                                    approved_at=_ts(row.get("approved_at")), org_url=row.get("url", ""), description=row.get("description", "")))
    return out


def import_apps(platform: str, filename: str, data: bytes) -> list[ConnectedApp]:
    files = []
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                n = info.filename.lower()
                if re.search(r"(connected-application\.js|apps?_and_websites[^/]*\.json|connected_apps[^/]*\.json|installed_apps[^/]*\.json)$", n):
                    files.append((info.filename, zf.read(info)))
    else:
        files.append((filename, data))
    out: list[ConnectedApp] = []
    try:
        for name, blob in files:
            t = blob.decode("utf-8-sig")
            if name.lower().endswith(".csv"):
                out += parse_apps_csv(t, platform)
            elif platform == "x" or "connected-application" in name:
                out += parse_x_connected(t)
            else:
                out += parse_meta_apps(platform, json.loads(t))
    except (ValueError, KeyError, TypeError) as exc:
        raise ImportError_(f"Could not read connected apps from that file: {exc}") from exc
    if not out:
        raise ImportError_("No connected apps were found in that file")
    return out
