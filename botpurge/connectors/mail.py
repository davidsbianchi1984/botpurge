"""Live inbox connections: Gmail and Outlook, read-only.

Both use OAuth 2.0 with PKCE, so Bot Purge never sees the email password, and
both ask only for read access:

* Gmail: ``https://www.googleapis.com/auth/gmail.readonly``. Google treats it
  as a *restricted* scope: before the public can connect, the Google Cloud app
  needs Google's verification (and a yearly security assessment). Until then
  only test users added in the Google Cloud console can connect.
* Outlook / Microsoft 365: ``Mail.Read offline_access`` through Microsoft
  Graph, from an app registered in Microsoft Entra (publisher verification
  recommended).

Each sync fetches the last 30 days of the inbox as raw MIME and hands it to
the same email scanner as uploaded mailbox exports, so every email rule
(SPF/DKIM/DMARC, lookalike domains, attachments, callback phishing...) applies.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from urllib.parse import urlencode

import httpx

PROVIDERS = {
    "gmail": {
        "name": "Gmail",
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "scope": "https://www.googleapis.com/auth/gmail.readonly",
        "client_env": "GOOGLE_CLIENT_ID", "secret_env": "GOOGLE_CLIENT_SECRET",
        "extra": {"access_type": "offline", "prompt": "consent"},
    },
    "outlook": {
        "name": "Outlook",
        "authorize": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scope": "offline_access https://graph.microsoft.com/Mail.Read",
        "client_env": "MS_CLIENT_ID", "secret_env": "MS_CLIENT_SECRET",
        "extra": {"response_mode": "query"},
    },
}
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
GRAPH = "https://graph.microsoft.com/v1.0/me"


class MailError(RuntimeError):
    pass


def authorize_url(provider: str, client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    p = PROVIDERS[provider]
    return p["authorize"] + "?" + urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "scope": p["scope"],
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256", **p["extra"]})


def _token(provider: str, data: dict, client_id: str, client_secret: Optional[str], http: httpx.Client) -> dict:
    p = PROVIDERS[provider]
    body = {"client_id": client_id, **({"client_secret": client_secret} if client_secret else {}), **data}
    if provider == "outlook":
        body["scope"] = p["scope"]
    r = http.post(p["token"], data=body)
    if r.status_code >= 400:
        raise MailError(f"{p['name']} sign-in failed: {r.text[:200]}")
    return r.json()


def exchange_code(provider: str, code: str, redirect_uri: str, verifier: str, client_id: str,
                  client_secret: Optional[str] = None, http: Optional[httpx.Client] = None) -> dict:
    return _token(provider, {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                             "code_verifier": verifier}, client_id, client_secret, http or httpx.Client(timeout=20))


def refresh(provider: str, refresh_token: str, client_id: str, client_secret: Optional[str] = None,
            http: Optional[httpx.Client] = None) -> dict:
    return _token(provider, {"grant_type": "refresh_token", "refresh_token": refresh_token},
                  client_id, client_secret, http or httpx.Client(timeout=20))


def _get(http: httpx.Client, url: str, token: str, **params) -> httpx.Response:
    r = http.get(url, params=params or None, headers={"Authorization": f"Bearer {token}"})
    if r.status_code == 401:
        raise MailError("The mailbox connection expired. Reconnect it")
    if r.status_code >= 400:
        raise MailError(f"Mailbox error {r.status_code}: {r.text[:200]}")
    return r


def fetch_gmail(token: str, days: int = 30, limit: int = 300, http: Optional[httpx.Client] = None) -> list[bytes]:
    http = http or httpx.Client(timeout=30)
    ids: list[str] = []
    page = None
    while len(ids) < limit:
        params = {"q": f"newer_than:{days}d", "labelIds": "INBOX", "maxResults": min(100, limit - len(ids))}
        if page:
            params["pageToken"] = page
        data = _get(http, GMAIL + "/messages", token, **params).json()
        ids += [m["id"] for m in data.get("messages", [])]
        page = data.get("nextPageToken")
        if not page:
            break
    out = []
    for mid in ids[:limit]:
        raw = _get(http, f"{GMAIL}/messages/{mid}", token, format="raw").json().get("raw", "")
        out.append(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    return out


def fetch_outlook(token: str, days: int = 30, limit: int = 300, http: Optional[httpx.Client] = None) -> list[bytes]:
    http = http or httpx.Client(timeout=30)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url: Optional[str] = f"{GRAPH}/mailFolders/inbox/messages?" + urlencode(
        {"$select": "id", "$top": 100, "$filter": f"receivedDateTime ge {since}"})
    ids: list[str] = []
    while url and len(ids) < limit:
        data = _get(http, url, token).json()
        ids += [m["id"] for m in data.get("value", [])]
        url = data.get("@odata.nextLink")
    return [_get(http, f"{GRAPH}/messages/{mid}/$value", token).content for mid in ids[:limit]]


FETCHERS: dict[str, Callable[..., list[bytes]]] = {"gmail": fetch_gmail, "outlook": fetch_outlook}
