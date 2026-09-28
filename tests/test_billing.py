"""Payments: Stripe checkout on the store, signed license keys checked offline by the app."""
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from botpurge import billing
from botpurge.api import create_app
from conftest import hdr

PRIV, PUB = billing.keygen()


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A store server with Stripe faked out; returns (client, calls)."""
    monkeypatch.setenv("BOTPURGE_BETA", "0")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_x")
    monkeypatch.setenv("BOTPURGE_LICENSE_PRIVATE", PRIV)
    monkeypatch.setenv("BOTPURGE_LICENSE_PUBLIC", PUB)
    monkeypatch.setenv("BOTPURGE_PUBLIC_URL", "https://store.example")
    calls = []

    def stripe(req: httpx.Request):
        form = parse_qs(req.content.decode())
        calls.append((req.url.path, form))
        if req.url.path == "/v1/checkout/sessions":
            return httpx.Response(200, json={"id": "cs_1", "url": "https://checkout.stripe.com/c/cs_1"})
        if req.url.path == "/v1/billing_portal/sessions":
            return httpx.Response(200, json={"url": "https://billing.stripe.com/p/1"})
        return httpx.Response(404, json={"error": {"message": "nope"}})

    app = create_app(str(tmp_path / "store.sqlite3"), worker=False, x_http=httpx.Client(transport=httpx.MockTransport(stripe)))
    return TestClient(app), calls


def hook(client, event, secret="whsec_x", ts=None):
    body = json.dumps(event).encode()
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return client.post("/api/billing/webhook", content=body, headers={"Stripe-Signature": f"t={ts},v1={sig}"})


def paid(session_id="cs_1", plan="protect", email="Buyer@Example.com", **extra):
    return {"id": "evt_" + session_id, "type": "checkout.session.completed", "data": {"object": {
        "id": session_id, "payment_status": "paid", "metadata": {"plan": plan}, "customer": "cus_1",
        "subscription": "sub_1" if plan == "protect" else None, "payment_intent": None if plan == "protect" else "pi_1",
        "customer_details": {"email": email}, **extra}}}


def test_license_keys_are_signed_and_checked_offline():
    key = billing.sign_license({"id": "lic_1", "email": "a@b.c", "plan": "cleanup", "expires": None}, PRIV)
    assert billing.verify_license(key, PUB)["plan"] == "cleanup"
    tag, body, sig = key.split(".")
    forged = json.loads(billing._unb64(body)) | {"plan": "protect"}
    with pytest.raises(billing.LicenseError, match="genuine"):
        billing.verify_license(".".join([tag, billing._b64(json.dumps(forged).encode()), sig]), PUB)
    _, other_pub = billing.keygen()
    with pytest.raises(billing.LicenseError):
        billing.verify_license(key, other_pub)
    old = billing.sign_license({"id": "lic_2", "email": "a@b.c", "plan": "protect", "expires": "2020-01-01T00:00:00+00:00"}, PRIV)
    with pytest.raises(billing.LicenseError, match="run out"):
        billing.verify_license(old, PUB)
    with pytest.raises(billing.LicenseError, match="doesn't look like"):
        billing.verify_license("hello", PUB)


def test_stripe_signatures():
    body = b'{"id":"evt"}'
    ts = int(time.time())
    good = hmac.new(b"whsec", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    billing.verify_stripe_signature(body, f"t={ts},v1=bad,v1={good}", "whsec")
    for header in ("", f"t={ts},v1=deadbeef", f"t={ts - 3600},v1={hmac.new(b'whsec', f'{ts - 3600}.'.encode() + body, hashlib.sha256).hexdigest()}"):
        with pytest.raises(billing.BillingError):
            billing.verify_stripe_signature(body, header, "whsec")


def test_checkout_webhook_and_license_on_the_store(store):
    client, calls = store
    r = client.get("/buy/protect?email=buyer@example.com", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://checkout.stripe.com/")
    path, form = calls[-1]
    assert form["mode"] == ["subscription"] and form["line_items[0][price_data][unit_amount]"] == ["6000"]
    assert form["line_items[0][price_data][recurring][interval]"] == ["month"] and form["metadata[plan]"] == ["protect"]
    assert form["success_url"] == ["https://store.example/billing/success?session_id={CHECKOUT_SESSION_ID}"]
    client.post("/api/billing/checkout", json={"plan": "cleanup"})
    assert calls[-1][1]["mode"] == ["payment"] and calls[-1][1]["line_items[0][price_data][unit_amount]"] == ["2000"]
    assert client.post("/api/billing/checkout", json={"plan": "free"}).status_code == 400

    assert client.get("/api/billing/license?session_id=cs_1").status_code == 404      # not paid yet
    assert hook(client, paid(), secret="wrong").status_code == 400                  # forged webhook
    assert hook(client, paid()).json()["handled"]
    assert hook(client, paid()).json()["duplicate"]                                 # Stripe retries are ignored
    key = client.get("/api/billing/license?session_id=cs_1").json()["license"]
    lic = billing.verify_license(key)
    assert lic["plan"] == "protect" and lic["email"] == "buyer@example.com" and lic["expires"]
    assert "Thank you" in client.get("/billing/success?session_id=cs_1").text

    # Renewal pushes the expiry out; refresh hands the app a new key.
    end = int(time.time()) + 70 * 86400
    hook(client, {"id": "evt_inv", "type": "invoice.paid", "data": {"object": {"subscription": "sub_1", "lines": {"data": [{"period": {"end": end}}]}}}})
    renewed = billing.verify_license(client.post("/api/billing/refresh", json={"license": key}).json()["license"])
    assert renewed["expires"] > lic["expires"]
    assert client.post("/api/billing/portal", json={"license": key}).json()["url"].startswith("https://billing.stripe.com/")
    # Cancelling keeps the paid time; it just isn't extended any more.
    hook(client, {"id": "evt_del", "type": "customer.subscription.deleted", "data": {"object": {"id": "sub_1"}}})
    assert billing.verify_license(client.post("/api/billing/refresh", json={"license": key}).json()["license"])["expires"] == renewed["expires"]


def test_refund_revokes_and_web_accounts_upgrade_at_once(store):
    client, _ = store
    u = client.post("/api/signup", json={"email": "web@example.com"}).json()
    h = {"Authorization": f"Bearer {u['token']}"}
    assert "remove" not in client.get("/api/me/plan", headers=h).json()["features"]
    hook(client, paid("cs_2", plan="cleanup", email="web@example.com", client_reference_id=u["user_id"], metadata={"plan": "cleanup", "user_id": u["user_id"]}))
    mine = client.get("/api/me/plan", headers=h).json()
    assert mine["plan"] == "cleanup" and "remove" in mine["features"]
    key = client.get("/api/billing/license?session_id=cs_2").json()["license"]
    hook(client, {"id": "evt_ref", "type": "charge.refunded", "data": {"object": {"payment_intent": "pi_1", "amount": 2000, "amount_refunded": 2000}}})
    r = client.post("/api/billing/refresh", json={"license": key})
    assert r.status_code == 400 and "refunded" in r.json()["detail"]


def test_desktop_enters_key_and_refreshes_daily(tmp_path, monkeypatch):
    monkeypatch.setenv("BOTPURGE_BETA", "0")
    monkeypatch.setenv("BOTPURGE_LICENSE_PUBLIC", PUB)
    monkeypatch.delenv("BOTPURGE_LICENSE_PRIVATE", raising=False)
    monkeypatch.setenv("BOTPURGE_STORE_URL", "https://store.example")
    soon = billing.sign_license({"id": "lic_9", "email": "me@example.com", "plan": "protect", "expires": "2099-01-01T00:00:00+00:00"}, PRIV)
    later = billing.sign_license({"id": "lic_9", "email": "me@example.com", "plan": "protect", "expires": "2099-03-01T00:00:00+00:00"}, PRIV)
    seen = []

    def store(req: httpx.Request):
        seen.append(req.url.path)
        if req.url.path == "/api/billing/refresh":
            return httpx.Response(200, json={"license": later})
        if req.url.path == "/api/billing/portal":
            return httpx.Response(200, json={"url": "https://billing.stripe.com/p/9"})
        if req.url.path == "/api/billing/send-keys":
            return httpx.Response(200, json={"ok": True, "detail": "sent"})
        return httpx.Response(404)

    app = create_app(str(tmp_path / "d.sqlite3"), worker=False, x_http=httpx.Client(transport=httpx.MockTransport(store)))
    c = TestClient(app)
    u = c.post("/api/signup", json={"email": "me@example.com"}).json()
    h = {"Authorization": f"Bearer {u['token']}"}
    cat = c.get("/api/plans").json()
    assert cat["can_buy"] and cat["store_url"] == "https://store.example"
    assert c.post("/api/me/plan", headers=h, json={"plan": "protect"}).status_code == 402   # paying is required after the beta
    assert c.post("/api/me/license", headers=h, json={"license": "BP1.fake.key"}).status_code == 400
    mine = c.post("/api/me/license", headers=h, json={"license": soon}).json()
    assert mine["plan"] == "protect" and "agent" in mine["features"] and mine["renews_at"].startswith("2099-01")
    assert c.post("/api/me/billing/portal", headers=h).json()["url"].endswith("/p/9")
    assert c.post("/api/billing/send-keys", json={"email": "me@example.com"}).json()["ok"]
    assert app.state.svc.refresh_licenses() == 1 and app.state.svc.refresh_licenses() == 0   # once a day
    assert c.get("/api/me/plan", headers=h).json()["renews_at"].startswith("2099-03")
    assert seen.count("/api/billing/refresh") == 1


def test_beta_needs_no_payment(client, user):
    assert client.get("/api/plans").json()["beta"]
    assert client.post("/api/me/plan", headers=hdr(user), json={"plan": "protect"}).json()["plan"] == "protect"
