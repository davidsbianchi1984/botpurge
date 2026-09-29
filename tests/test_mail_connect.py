"""Live inbox connections (Gmail, Outlook, Yahoo Mail): read-only OAuth, fetched mail goes through the email scanner."""
import base64
from email.message import EmailMessage
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from botpurge.api import create_app


def scam_email(i=0):
    m = EmailMessage()
    m["From"], m["Subject"] = '"Geek Squad" <renewals.team88@gmail.com>', f"Your subscription renewed #{i}"
    m["Message-ID"] = f"<scam{i}@example.com>"
    m.set_content("Your Geek Squad subscription has been renewed and $399.99 charged. To cancel or request a refund call +1 (808) 555-0147 within 24 hours.")
    return bytes(m)


def fake_provider(req: httpx.Request):
    u = req.url
    if u.host in ("oauth2.googleapis.com", "login.microsoftonline.com") or u.path == "/oauth2/get_token":
        form = parse_qs(req.content.decode())
        assert form["client_id"] == ["cid"] and form["grant_type"][0] in ("authorization_code", "refresh_token")
        if form["grant_type"] == ["authorization_code"]:
            assert form["code_verifier"][0]
        return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt2"})
    assert req.headers["authorization"] == "Bearer at"
    if u.host == "gmail.googleapis.com":
        if u.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "g1"}, {"id": "g2"}]})
        raw = base64.urlsafe_b64encode(scam_email(int(u.path[-1]))).decode().rstrip("=")
        return httpx.Response(200, json={"raw": raw})
    if u.host == "graph.microsoft.com":
        if "mailFolders/inbox/messages" in u.path:
            return httpx.Response(200, json={"value": [{"id": "o1"}]})
        return httpx.Response(200, content=scam_email(7))
    if u.host == "api.login.yahoo.com" and u.path.endswith("/userinfo"):
        return httpx.Response(200, json={"email": "me@yahoo.com"})
    return httpx.Response(404)


class FakeYahooImap:
    """Yahoo's IMAP server: XOAUTH2 sign-in, a read-only inbox with three recent messages."""
    def __init__(self, *a, **k):
        self.readonly = None

    def authenticate(self, mech, cb):
        assert mech == "XOAUTH2" and cb(b"") == b"user=me@yahoo.com\x01auth=Bearer at\x01\x01"

    def select(self, box, readonly=False):
        self.readonly = readonly
        return "OK", [b"3"]

    def search(self, charset, *crit):
        assert crit[0] == "SINCE"
        return "OK", [b"1 2 3"]

    def fetch(self, mid, what):
        assert self.readonly and "PEEK" in what          # never marks mail read
        return "OK", [(mid + b" (BODY[] {10}", scam_email(int(mid))), b")"]

    def logout(self):
        pass


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setattr("botpurge.connectors.mail.imaplib.IMAP4_SSL", FakeYahooImap)
    for k in ("GOOGLE_CLIENT_ID", "MS_CLIENT_ID", "YAHOO_CLIENT_ID"):
        monkeypatch.setenv(k, "cid")
    app = create_app(str(tmp_path / "m.sqlite3"), worker=False, x_http=httpx.Client(transport=httpx.MockTransport(fake_provider)))
    client = TestClient(app)
    u = client.post("/api/signup", json={"email": "me@example.com"}).json()
    return client, {"Authorization": f"Bearer {u['token']}"}, app


@pytest.mark.parametrize("provider,scope,expect", [("gmail", "gmail.readonly", 2), ("outlook", "Mail.Read", 1), ("yahoo", "mail-r", 3)])
def test_connect_read_only_and_scan(c, provider, scope, expect):
    client, h, app = c
    assert {m["provider"]: m["available"] for m in client.get("/api/mail", headers=h).json()} == {"gmail": True, "outlook": True, "yahoo": True}
    url = client.post(f"/api/mail/{provider}/connect", headers=h).json()["authorize_url"]
    q = parse_qs(urlparse(url).query)
    assert scope in q["scope"][0] and q["code_challenge_method"] == ["S256"]
    assert "send" not in q["scope"][0].lower() and "modify" not in q["scope"][0].lower()      # read-only
    page = client.get(f"/api/mail/callback?state={q['state'][0]}&code=abc")
    assert "connected" in page.text
    links = {m["provider"]: m for m in client.get("/api/mail", headers=h).json()}
    assert links[provider]["connected"] and links[provider]["last_sync"]
    senders = client.get("/api/inbox/senders?tab=flagged", headers=h).json()
    assert any(s["sender_id"] == "renewals.team88@gmail.com" for s in senders)
    assert client.post(f"/api/mail/{provider}/sync", headers=h).json()["fetched"] == expect
    # The rotated refresh token replaced the old one, sealed at rest.
    assert app.state.svc.get_secret(client.app.state.svc.db.one("SELECT id FROM users")["id"], f"mail_{provider}_refresh") == "rt2"
    client.delete(f"/api/mail/{provider}", headers=h)
    assert not {m["provider"]: m for m in client.get("/api/mail", headers=h).json()}[provider]["connected"]


def test_callback_rejects_unknown_state_and_denied_consent(c):
    client, h, _ = c
    assert client.get("/api/mail/callback?state=gmail.nope&code=x").status_code == 400
    url = client.post("/api/mail/gmail/connect", headers=h).json()["authorize_url"]
    state = parse_qs(urlparse(url).query)["state"][0]
    assert "wasn't connected" in client.get(f"/api/mail/callback?state={state}&error=access_denied").text
    assert client.get(f"/api/mail/callback?state={state}&code=x").status_code == 400     # one use only


def test_not_configured_and_paid_feature(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    client = TestClient(create_app(str(tmp_path / "n.sqlite3"), worker=False))
    h = {"Authorization": "Bearer " + client.post("/api/signup", json={"email": "a@b.co"}).json()["token"]}
    assert client.post("/api/mail/gmail/connect", headers=h).status_code == 503
    monkeypatch.setenv("BOTPURGE_BETA", "0")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    assert client.post("/api/mail/gmail/connect", headers=h).status_code == 402
