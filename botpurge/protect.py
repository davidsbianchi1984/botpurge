"""Protect tier: the weekly protection report and the always-on schedule."""
from __future__ import annotations

from datetime import timedelta

from . import security as sec
from .db import DB


def weekly_report(db: DB, user_id: str, days: int = 7) -> dict:
    since = sec.iso(sec.now() - timedelta(days=days))
    one = lambda sql, *a: db.one(sql, (user_id, *a))["n"] or 0  # noqa: E731
    live = db.one("SELECT COUNT(*) n, SUM(e.action='ban') bans, SUM(e.action IN ('delete','timeout')) other FROM lg_events e "
                  "JOIN lg_sessions s ON s.id=e.session_id WHERE s.user_id=? AND e.at>=? AND e.action!='none'", (user_id, since))
    report = {
        "days": days,
        "new_threats": one("SELECT COUNT(*) n FROM undo_log WHERE user_id=? AND event='flagged' AND at>=?", since),
        "removed": one("SELECT COUNT(*) n FROM undo_log WHERE user_id=? AND event='removed' AND at>=?", since),
        "impersonators": one("SELECT COUNT(*) n FROM alerts WHERE user_id=? AND kind='clone' AND at>=?", since),
        "scam_messages": one("SELECT COUNT(*) n FROM msg_senders WHERE user_id=? AND label IN ('likely_bot','suspicious') AND last_at>=?", since),
        "canary_catches": one("SELECT COUNT(DISTINCT m.sender_id) n FROM msg_items m JOIN canaries c ON c.user_id=m.user_id "
                              "WHERE m.user_id=? AND m.imported_at>=? AND lower(m.text) LIKE '%' || c.token || '%'", since),
        "live_actions": live["n"] or 0, "live_bans": live["bans"] or 0,
        "risky_apps": one("SELECT COUNT(*) n FROM apps WHERE user_id=? AND status='active' AND label IN ('likely_bot','suspicious')"),
    }
    parts = []
    if report["new_threats"]:
        parts.append(f"{report['new_threats']} new threats found")
    if report["removed"]:
        parts.append(f"{report['removed']} removed")
    if report["live_actions"]:
        parts.append(f"{report['live_actions']} bots stopped in your live chats")
    if report["canary_catches"]:
        parts.append(f"{report['canary_catches']} AI bots caught by your canaries")
    if report["risky_apps"]:
        parts.append(f"{report['risky_apps']} risky apps still connected")
    report["headline"] = ("This week: " + ", ".join(parts) + ".") if parts else "A quiet week: nothing new got through."
    return report


def send_weekly_reports(db: DB, features_for) -> int:
    """Called by the worker. Protect users get a report alert once a week."""
    sent = 0
    week_ago = sec.iso(sec.now() - timedelta(days=7))
    for u in db.q("SELECT id FROM users"):
        if "reports" not in features_for(u["id"]):
            continue
        if db.one("SELECT 1 FROM alerts WHERE user_id=? AND kind='weekly_report' AND at>=?", (u["id"], week_ago)):
            continue
        if not db.one("SELECT 1 FROM scans WHERE user_id=?", (u["id"],)):
            continue
        r = weekly_report(db, u["id"])
        db.x("INSERT INTO alerts VALUES (?,?,?,?,?,?,?,?,0)", (sec.new_id("al_"), u["id"], sec.iso(), "weekly_report", r["headline"], None, None, None))
        sent += 1
    return sent
