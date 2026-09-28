"""Payments and licenses.

Bot Purge is a desktop app, so a payment can't be confirmed on the buyer's own
computer. A small hosted store (this same server, run with Stripe keys)
takes the payment and issues a **license key**: a short token signed with the
store's Ed25519 private key. The desktop app checks the signature offline
against the matching public key, so a key can't be forged or edited, and no
card details ever touch the app.

* Cleanup ($20 once): a license that never expires.
* Protect ($60/month): a license valid until the paid period ends (plus a few
  days' grace). The app refreshes it from the store once a day; renewals
  extend it, cancellations let it run out at the end of the paid month.

Store settings: ``STRIPE_SECRET_KEY``, ``STRIPE_WEBHOOK_SECRET``,
``BOTPURGE_LICENSE_PRIVATE`` (from ``python -m botpurge.billing keygen``) and
``BOTPURGE_PUBLIC_URL``. App setting: the public key, in
``botpurge/license_public.txt`` or ``BOTPURGE_LICENSE_PUBLIC``, and
``BOTPURGE_STORE_URL`` or ``release.json`` (where the Buy buttons go).

While the beta is on (``BOTPURGE_BETA=1``) none of this is needed: every plan
is free.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import security as sec
from .db import DB
from .plans import PLANS

STRIPE_API = "https://api.stripe.com/v1"
GRACE = timedelta(days=3)          # a late renewal payment doesn't switch protection off
PUBLIC_FILE = Path(__file__).with_name("license_public.txt")


class LicenseError(ValueError):
    pass


class BillingError(RuntimeError):
    pass


# ---- license keys -------------------------------------------------------------------

def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def keygen() -> tuple[str, str]:
    """A new (private, public) key pair, both base64."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    k = Ed25519PrivateKey.generate()
    raw = serialization.Encoding.Raw
    priv = k.private_bytes(raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub = k.public_key().public_bytes(raw, serialization.PublicFormat.Raw)
    return base64.b64encode(priv).decode(), base64.b64encode(pub).decode()


def public_key() -> Optional[str]:
    env = os.environ.get("BOTPURGE_LICENSE_PUBLIC")
    if env:
        return env.strip()
    if PUBLIC_FILE.exists():
        k = PUBLIC_FILE.read_text().strip()
        return k or None
    return None


def sign_license(payload: dict, private_b64: str) -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    sig = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_b64)).sign(body)
    return f"BP1.{_b64(body)}.{_b64(sig)}"


def verify_license(token: str, public_b64: Optional[str] = None, now: Optional[datetime] = None,
                   allow_expired: bool = False) -> dict:
    """The license's contents if the signature is genuine (and it hasn't run out)."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    pub = public_b64 or public_key()
    if not pub:
        raise LicenseError("This copy of Bot Purge can't check license keys yet (no store key is set)")
    try:
        tag, body, sig = token.strip().split(".")
        if tag != "BP1":
            raise ValueError
        Ed25519PublicKey.from_public_bytes(base64.b64decode(pub)).verify(_unb64(sig), _unb64(body))
        lic = json.loads(_unb64(body))
    except InvalidSignature:
        raise LicenseError("That license key isn't genuine") from None
    except Exception:
        raise LicenseError("That doesn't look like a Bot Purge license key") from None
    if lic.get("plan") not in PLANS or not PLANS[lic["plan"]]["price_cents"]:
        raise LicenseError("That license key is for an unknown plan")
    exp = sec.parse_iso(lic.get("expires"))
    if exp and exp < (now or sec.now()) and not allow_expired:
        raise LicenseError("That license has run out. Renew Protect to keep it going")
    return lic


# ---- Stripe ------------------------------------------------------------------------------

def verify_stripe_signature(payload: bytes, header: str, secret: str, tolerance: int = 300,
                            now: Optional[float] = None) -> None:
    """Stripe-Signature: t=<unix>,v1=<hex hmac of "t.payload">[,v1=...]."""
    parts = [p.split("=", 1) for p in (header or "").split(",") if "=" in p]
    ts = next((v for k, v in parts if k == "t"), None)
    sigs = [v for k, v in parts if k == "v1"]
    if not ts or not sigs:
        raise BillingError("missing Stripe signature")
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in sigs):
        raise BillingError("bad Stripe signature")
    if abs((now or time.time()) - int(ts)) > tolerance:
        raise BillingError("Stripe signature is too old")


def _form(d: dict, prefix: str = "") -> list[tuple[str, str]]:
    """Stripe's nested form encoding: a[b][0][c]=v."""
    out: list[tuple[str, str]] = []
    for k, v in d.items():
        key = f"{prefix}[{k}]" if prefix else str(k)
        if isinstance(v, dict):
            out += _form(v, key)
        elif isinstance(v, list):
            for i, x in enumerate(v):
                out += _form(x, f"{key}[{i}]") if isinstance(x, dict) else [(f"{key}[{i}]", str(x))]
        elif v is not None:
            out.append((key, str(v).lower() if isinstance(v, bool) else str(v)))
    return out


class Billing:
    """The store: Stripe checkout, webhooks, and license keys."""

    def __init__(self, db: DB, http=None):
        self.db = db
        self.http = http

    # configuration
    @staticmethod
    def store_configured() -> bool:
        return bool(os.environ.get("STRIPE_SECRET_KEY") and os.environ.get("BOTPURGE_LICENSE_PRIVATE"))

    def _stripe(self, method: str, path: str, data: Optional[dict] = None) -> dict:
        import httpx

        key = os.environ.get("STRIPE_SECRET_KEY")
        if not key:
            raise BillingError("Payments aren't set up on this server")
        client = self.http or httpx.Client(timeout=20)
        from urllib.parse import urlencode

        r = client.request(method, STRIPE_API + path, content=urlencode(_form(data or {})),
                           headers={"Authorization": f"Bearer {key}", "Content-Type": "application/x-www-form-urlencoded"})
        if r.status_code >= 400:
            try:
                msg = r.json()["error"]["message"]
            except Exception:
                msg = r.text[:200]
            raise BillingError(f"Stripe: {msg}")
        return r.json()

    # checkout
    def checkout(self, plan: str, email: Optional[str] = None, user_id: Optional[str] = None) -> str:
        """A Stripe Checkout page for the plan; returns its URL."""
        if plan not in PLANS or not PLANS[plan]["price_cents"]:
            raise ValueError("choose cleanup or protect")
        p = PLANS[plan]
        base = os.environ.get("BOTPURGE_PUBLIC_URL", "http://127.0.0.1:8000").rstrip("/")
        price = {"currency": "usd", "unit_amount": p["price_cents"], "product_data": {"name": p["name"]}}
        env_price = os.environ.get(f"STRIPE_PRICE_{plan.upper()}")   # optional: a Price made in the Stripe dashboard
        item = {"price": env_price, "quantity": 1} if env_price else \
            {"price_data": price | ({"recurring": {"interval": "month"}} if p["interval"] == "month" else {}), "quantity": 1}
        meta = {"plan": plan, **({"user_id": user_id} if user_id else {})}
        body = {
            "mode": "subscription" if p["interval"] == "month" else "payment",
            "line_items": [item],
            "success_url": f"{base}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
            "cancel_url": f"{base}/#plans",
            "client_reference_id": user_id,
            "customer_email": email,
            "metadata": meta,
            "allow_promotion_codes": True,
        }
        if p["interval"] == "month":
            body["subscription_data"] = {"metadata": meta}
        return self._stripe("POST", "/checkout/sessions", body)["url"]

    def portal(self, token: str) -> str:
        """Stripe's page for managing a subscription (card, invoices, cancel)."""
        lic = verify_license(token, public_key() or self._own_public(), allow_expired=True)
        row = self.db.one("SELECT stripe_customer FROM licenses WHERE id=?", (lic["id"],))
        if not row or not row["stripe_customer"]:
            raise BillingError("No subscription found for that license")
        base = os.environ.get("BOTPURGE_PUBLIC_URL", "http://127.0.0.1:8000").rstrip("/")
        return self._stripe("POST", "/billing_portal/sessions", {"customer": row["stripe_customer"], "return_url": base + "/"})["url"]

    # webhooks
    def handle_webhook(self, payload: bytes, signature: str) -> dict:
        secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
        if not secret:
            raise BillingError("STRIPE_WEBHOOK_SECRET isn't set")
        verify_stripe_signature(payload, signature, secret)
        ev = json.loads(payload)
        with self.db.tx() as tx:
            if tx.execute("SELECT 1 FROM billing_events WHERE id=?", (ev["id"],)).fetchone():
                return {"ok": True, "duplicate": True}      # Stripe retries; handle each event once
            tx.execute("INSERT INTO billing_events(id,type,at) VALUES (?,?,?)", (ev["id"], ev["type"], sec.iso()))
        obj = ev["data"]["object"]
        handler = {"checkout.session.completed": self._paid_checkout, "invoice.paid": self._renewed,
                   "customer.subscription.deleted": self._cancelled, "charge.refunded": self._refunded}.get(ev["type"])
        result = handler(obj) if handler else None
        return {"ok": True, "handled": bool(handler), **({"license_id": result} if isinstance(result, str) else {})}

    def _paid_checkout(self, s: dict) -> Optional[str]:
        if s.get("payment_status") not in ("paid", "no_payment_required"):
            return None
        meta = s.get("metadata") or {}
        plan = meta.get("plan")
        if plan not in PLANS:
            return None
        email = (s.get("customer_details") or {}).get("email") or s.get("customer_email") or ""
        expires = None
        if PLANS[plan]["interval"] == "month":
            expires = sec.iso(sec.now() + timedelta(days=31) + GRACE)
        lic_id = sec.new_id("lic_")
        with self.db.tx() as tx:
            if tx.execute("SELECT 1 FROM licenses WHERE session_id=?", (s["id"],)).fetchone():
                return None
            tx.execute("INSERT INTO licenses(id,email,plan,session_id,stripe_customer,stripe_subscription,payment_intent,"
                       "expires_at,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (lic_id, email.lower(), plan, s["id"], s.get("customer"), s.get("subscription"), s.get("payment_intent"),
                        expires, "active", sec.iso()))
        if email and os.environ.get("BOTPURGE_SMTP_URL"):
            try:
                self.send_keys(email)
            except Exception:
                pass                      # the success page shows the key anyway
        # Bought from inside a hosted (web) account: switch the plan on right away.
        uid = meta.get("user_id") or s.get("client_reference_id")
        if uid and self.db.one("SELECT 1 FROM users WHERE id=?", (uid,)):
            from .plans import Plans

            Plans(self.db).activate(uid, plan, payment_ref=lic_id, renews_at=expires)
        return lic_id

    def _renewed(self, inv: dict) -> None:
        sub = inv.get("subscription") or ((inv.get("parent") or {}).get("subscription_details") or {}).get("subscription")
        if not sub:
            return
        ends = [ln.get("period", {}).get("end") for ln in (inv.get("lines") or {}).get("data", [])]
        end = max([e for e in ends if e] or [int(time.time()) + 31 * 86400])
        expires = sec.iso(datetime.fromtimestamp(end, timezone.utc) + GRACE)
        self.db.x("UPDATE licenses SET expires_at=?, status='active' WHERE stripe_subscription=? AND (expires_at IS NULL OR expires_at<?)",
                  (expires, sub, expires))
        self._sync_accounts(sub)

    def _cancelled(self, sub: dict) -> None:
        # Paid time isn't taken away: the license simply stops being extended.
        self.db.x("UPDATE licenses SET status='cancelled' WHERE stripe_subscription=?", (sub["id"],))

    def _refunded(self, ch: dict) -> None:
        if ch.get("amount_refunded", 0) >= ch.get("amount", 1):
            now = sec.iso()
            self.db.x("UPDATE licenses SET status='refunded', expires_at=? WHERE payment_intent=? OR (stripe_customer=? AND stripe_subscription IS NULL)",
                      (now, ch.get("payment_intent"), ch.get("customer")))

    def _sync_accounts(self, sub: str) -> None:
        for r in self.db.q("SELECT l.id, l.plan, l.expires_at, u.id uid FROM licenses l JOIN users u ON u.license_id=l.id "
                           "WHERE l.stripe_subscription=?", (sub,)):
            self.db.x("UPDATE users SET plan_renews_at=? WHERE id=?", (r["expires_at"], r["uid"]))

    # license keys
    def _own_public(self) -> Optional[str]:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        priv = os.environ.get("BOTPURGE_LICENSE_PRIVATE")
        if not priv:
            return None
        k = Ed25519PrivateKey.from_private_bytes(base64.b64decode(priv))
        raw = serialization.Encoding.Raw
        return base64.b64encode(k.public_key().public_bytes(raw, serialization.PublicFormat.Raw)).decode()

    def _issue(self, row) -> str:
        priv = os.environ.get("BOTPURGE_LICENSE_PRIVATE")
        if not priv:
            raise BillingError("BOTPURGE_LICENSE_PRIVATE isn't set")
        return sign_license({"id": row["id"], "email": row["email"], "plan": row["plan"], "expires": row["expires_at"],
                             "issued": sec.iso()}, priv)

    def license_for_session(self, session_id: str) -> Optional[str]:
        row = self.db.one("SELECT * FROM licenses WHERE session_id=?", (session_id,))
        return self._issue(row) if row else None

    def refresh(self, token: str) -> str:
        """A fresh key with the latest expiry (after a renewal), or an error if it was refunded."""
        lic = verify_license(token, self._own_public() or public_key(), allow_expired=True)
        row = self.db.one("SELECT * FROM licenses WHERE id=?", (lic["id"],))
        if not row:
            raise LicenseError("Unknown license")
        if row["status"] == "refunded":
            raise LicenseError("That purchase was refunded")
        return self._issue(row)

    def send_keys(self, email: str, mailer=None) -> int:
        """Email every license bought with this address (for "I lost my key"). Keys only ever go to
        the buyer's inbox, never back to whoever asked, so nobody can fetch someone else's key."""
        rows = self.db.q("SELECT * FROM licenses WHERE email=? AND status!='refunded' ORDER BY created_at", (email.strip().lower(),))
        if rows:
            keys = "\n\n".join(f"{PLANS[r['plan']]['name']}:\n{self._issue(r)}" for r in rows)
            (mailer or send_mail)(email, "Your Bot Purge license key",
                                  "Paste this into Bot Purge > Plans > Enter license key:\n\n" + keys)
        return len(rows)


def send_mail(to: str, subject: str, body: str) -> None:
    """Plain-text email through BOTPURGE_SMTP_URL (smtp[s]://user:password@host:port, sender in BOTPURGE_MAIL_FROM)."""
    import smtplib
    from email.message import EmailMessage
    from urllib.parse import unquote, urlparse

    url = os.environ.get("BOTPURGE_SMTP_URL")
    if not url:
        raise BillingError("Email isn't set up on this server (BOTPURGE_SMTP_URL)")
    u = urlparse(url)
    m = EmailMessage()
    m["From"] = os.environ.get("BOTPURGE_MAIL_FROM", "Bot Purge <no-reply@localhost>")
    m["To"], m["Subject"] = to, subject
    m.set_content(body)
    cls = smtplib.SMTP_SSL if u.scheme == "smtps" else smtplib.SMTP
    with cls(u.hostname, u.port or (465 if u.scheme == "smtps" else 587), timeout=20) as smtp:
        if u.scheme != "smtps":
            smtp.starttls()
        if u.username:
            smtp.login(unquote(u.username), unquote(u.password or ""))
        smtp.send_message(m)


def cli() -> None:  # pragma: no cover - operator tool
    import sys

    if sys.argv[1:] == ["keygen"]:
        priv, pub = keygen()
        print("Keep this secret, on the store server only:\n  BOTPURGE_LICENSE_PRIVATE=" + priv)
        print("Public (ship it in the app): put this in botpurge/license_public.txt\n  " + pub)
    else:
        print("usage: python -m botpurge.billing keygen")


if __name__ == "__main__":  # pragma: no cover
    cli()
