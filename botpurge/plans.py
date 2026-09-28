"""Plans, prices and what each one unlocks.

The scan is always free: it finds and counts the threats, and shows every one
with its reasons (so nobody pays to remove a real friend by mistake).
Removing them is Cleanup ($20 once). Ongoing protection is Protect ($60/month).

While ``BOTPURGE_BETA`` is on (the default), every plan is free: the price is
shown, marked free for the beta, and activating a plan needs no payment.
"""
from __future__ import annotations

import os
from typing import Optional

from . import security as sec
from .db import DB

FEATURES = {
    "scan": "Scan friends, followers and following on every platform",
    "review": "See every flagged account with its reasons",
    "remove": "Remove all threats (one-click, assisted or guided)",
    "undo": "Undo log and CSV export",
    "messages": "Scan your inbox, comments and email for bots and scams",
    "canary": "Canary traps that expose AI comment bots",
    "monitor": "Daily automatic rescans and new-follower screening",
    "impersonation": "Impersonation watch with instant alerts",
    "agent": "Done-for-you removal by the Bot Purge agent",
    "email": "Email scam and spam monitoring",
    "liveguard": "Live Guard: removes bots from your live chat as they appear",
    "reports": "Weekly protection report",
    "priority": "Priority support",
}

PLANS = {
    "free": {
        "name": "Free scan",
        "price_cents": 0,
        "interval": None,
        "features": ["scan", "review"],
        "pitch": "Find out how many bots, fakes and impersonators are in your lists.",
    },
    "cleanup": {
        "name": "Bot Purge Cleanup",
        "price_cents": 2000,
        "interval": "once",
        "features": ["scan", "review", "remove", "undo", "messages"],
        "pitch": "Remove every threat we found, on every platform. Pay once.",
    },
    "protect": {
        "name": "Bot Purge Protect",
        "price_cents": 6000,
        "interval": "month",
        "features": list(FEATURES),
        "pitch": "Always-on protection: daily scans, inbox and email monitoring, canary traps, "
                 "impersonation watch and done-for-you removal.",
    },
}


def beta() -> bool:
    return os.environ.get("BOTPURGE_BETA", "1") == "1"


def price_label(plan: str) -> str:
    p = PLANS[plan]
    if not p["price_cents"]:
        return "Free"
    dollars = f"${p['price_cents'] // 100}"
    return dollars + (" one-time" if p["interval"] == "once" else "/month")


def catalog() -> dict:
    return {
        "beta": beta(),
        "plans": [
            {"id": k, **v, "price": price_label(k), "free_now": beta() or not v["price_cents"],
             "feature_text": [FEATURES[f] for f in v["features"]]}
            for k, v in PLANS.items()
        ],
    }


class PlanRequired(PermissionError):
    """The user's plan doesn't include this feature (HTTP 402)."""

    def __init__(self, feature: str):
        need = next(k for k in ("cleanup", "protect") if feature in PLANS[k]["features"])
        super().__init__(f"{FEATURES[feature]} is part of {PLANS[need]['name']} ({price_label(need)}).")
        self.feature, self.plan = feature, need


class Plans:
    def __init__(self, db: DB):
        self.db = db

    def current(self, user_id: str) -> dict:
        r = self.db.one("SELECT plan, plan_activated_at, plan_renews_at FROM users WHERE id=?", (user_id,))
        plan = r["plan"] if r and r["plan"] in PLANS else "free"
        renews = sec.parse_iso(r["plan_renews_at"]) if r else None
        if plan == "protect" and renews and renews < sec.now() and not beta():
            plan = "cleanup" if self._bought_cleanup(user_id) else "free"  # subscription lapsed
        return {"plan": plan, "name": PLANS[plan]["name"], "activated_at": r["plan_activated_at"] if r else None,
                "renews_at": r["plan_renews_at"] if r else None, "features": sorted(self.features(user_id, plan)),
                "beta": beta()}

    def _bought_cleanup(self, user_id: str) -> bool:
        return bool(self.db.one("SELECT 1 FROM purchases WHERE user_id=? AND plan IN ('cleanup','protect')", (user_id,)))

    def features(self, user_id: str, plan: Optional[str] = None) -> set[str]:
        if beta():
            return set(FEATURES)  # everything is free while the beta runs
        plan = plan or self.current(user_id)["plan"]
        return set(PLANS[plan]["features"])

    def require(self, user_id: str, feature: str) -> None:
        if feature not in self.features(user_id):
            raise PlanRequired(feature)

    def activate(self, user_id: str, plan: str, payment_ref: Optional[str] = None) -> dict:
        """Switch plan. During the beta this is free; afterwards it needs a completed payment."""
        if plan not in PLANS:
            raise ValueError(f"unknown plan {plan}")
        if PLANS[plan]["price_cents"] and not beta() and not payment_ref:
            raise PlanRequired(PLANS[plan]["features"][-1])
        now = sec.now()
        from datetime import timedelta

        renews = sec.iso(now + timedelta(days=30)) if PLANS[plan]["interval"] == "month" else None
        self.db.x("UPDATE users SET plan=?, plan_activated_at=?, plan_renews_at=? WHERE id=?",
                  (plan, sec.iso(now), renews, user_id))
        self.db.x("INSERT INTO purchases(user_id,plan,amount_cents,at,payment_ref) VALUES (?,?,?,?,?)",
                  (user_id, plan, 0 if beta() else PLANS[plan]["price_cents"], sec.iso(now), payment_ref or ("beta" if beta() else None)))
        return self.current(user_id)
