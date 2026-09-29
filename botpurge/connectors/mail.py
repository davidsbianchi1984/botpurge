"""Live inbox connections: Gmail, Outlook and Yahoo Mail. Read-only unless the person allows cleanup.

Both use OAuth 2.0 with PKCE, so Bot Purge never sees the email password, and
both ask only for read access:

* Gmail: ``https://www.googleapis.com/auth/gmail.readonly``. Google treats it
  as a *restricted* scope: before the public can connect, the Google Cloud app
  needs Google's verification (and a yearly security assessment). Until then
  only test users added in the Google Cloud console can connect.
* Outlook / Microsoft 365: ``Mail.Read offline_access`` through Microsoft
  Graph, from an app registered in Microsoft Entra (publisher verification
  recommended).
* Yahoo Mail: ``mail-r`` (read-only) from an app registered at
  developer.yahoo.com, then the inbox is read over Yahoo's IMAP with the same
  OAuth token (XOAUTH2). Yahoo grants the Mail scope to approved apps only.

Each sync fetches the last 30 days of the Inbox and Spam folders as raw MIME and
hands it to the same email scanner as uploaded mailbox exports, so every email
rule (SPF/DKIM/DMARC, lookalike domains, attachments, callback phishing...) applies.

Cleanup is a separate, optional permission (Gmail ``gmail.modify``, Outlook
``Mail.ReadWrite``, Yahoo ``mail-w``). With it, and only when the person presses
the button, the emails from senders Bot Purge flagged are moved to Trash or Spam.
Nothing is ever deleted for good, so every move can be undone in the mailbox.
"""
from __future__ import annotations

import base64
import imaplib
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
        "clean_scope": "https://www.googleapis.com/auth/gmail.modify",
        "client_env": "GOOGLE_CLIENT_ID", "secret_env": "GOOGLE_CLIENT_SECRET",
        "extra": {"access_type": "offline", "prompt": "consent"},
    },
    "outlook": {
        "name": "Outlook",
        "authorize": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scope": "offline_access https://graph.microsoft.com/Mail.Read",
        "clean_scope": "offline_access https://graph.microsoft.com/Mail.ReadWrite",
        "client_env": "MS_CLIENT_ID", "secret_env": "MS_CLIENT_SECRET",
        "extra": {"response_mode": "query"},
    },
    "yahoo": {
        "name": "Yahoo Mail",
        "authorize": "https://api.login.yahoo.com/oauth2/request_auth",
        "token": "https://api.login.yahoo.com/oauth2/get_token",
        "scope": "openid email mail-r",
        "clean_scope": "openid email mail-w",
        "client_env": "YAHOO_CLIENT_ID", "secret_env": "YAHOO_CLIENT_SECRET",
        "extra": {},
    },
}
YAHOO_USERINFO = "https://api.login.yahoo.com/openid/v1/userinfo"
YAHOO_IMAP = "imap.mail.yahoo.com"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
GRAPH = "https://graph.microsoft.com/v1.0/me"


class MailError(RuntimeError):
    pass


def scope_for(provider: str, cleanup: bool = False) -> str:
    p = PROVIDERS[provider]
    return p["clean_scope"] if cleanup else p["scope"]


def authorize_url(provider: str, client_id: str, redirect_uri: str, state: str, challenge: str, cleanup: bool = False) -> str:
    p = PROVIDERS[provider]
    return p["authorize"] + "?" + urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "scope": scope_for(provider, cleanup),
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256", **p["extra"]})


def _token(provider: str, data: dict, client_id: str, client_secret: Optional[str], http: httpx.Client,
           cleanup: bool = False) -> dict:
    p = PROVIDERS[provider]
    body = {"client_id": client_id, **({"client_secret": client_secret} if client_secret else {}), **data}
    if provider == "outlook":
        body["scope"] = scope_for(provider, cleanup)
    r = http.post(p["token"], data=body)
    if r.status_code >= 400:
        raise MailError(f"{p['name']} sign-in failed: {r.text[:200]}")
    return r.json()


def exchange_code(provider: str, code: str, redirect_uri: str, verifier: str, client_id: str,
                  client_secret: Optional[str] = None, http: Optional[httpx.Client] = None, cleanup: bool = False) -> dict:
    return _token(provider, {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                             "code_verifier": verifier}, client_id, client_secret, http or httpx.Client(timeout=20), cleanup)


def refresh(provider: str, refresh_token: str, client_id: str, client_secret: Optional[str] = None,
            http: Optional[httpx.Client] = None, cleanup: bool = False) -> dict:
    return _token(provider, {"grant_type": "refresh_token", "refresh_token": refresh_token},
                  client_id, client_secret, http or httpx.Client(timeout=20), cleanup)


def _get(http: httpx.Client, url: str, token: str, **params) -> httpx.Response:
    r = http.get(url, params=params or None, headers={"Authorization": f"Bearer {token}"})
    if r.status_code == 401:
        raise MailError("The mailbox connection expired. Reconnect it")
    if r.status_code >= 400:
        raise MailError(f"Mailbox error {r.status_code}: {r.text[:200]}")
    return r


def fetch_gmail(token: str, days: int = 30, limit: int = 300, http: Optional[httpx.Client] = None) -> list[tuple[bytes, str]]:
    """(raw email, folder) for the Inbox and Spam."""
    http = http or httpx.Client(timeout=30)
    out: list[tuple[bytes, str]] = []
    for label, folder in (("INBOX", "Inbox"), ("SPAM", "Spam")):
        ids: list[str] = []
        page = None
        while len(ids) < limit:
            params = {"q": f"newer_than:{days}d", "labelIds": label, "maxResults": min(100, limit - len(ids)),
                      "includeSpamTrash": "true" if label == "SPAM" else "false"}
            if page:
                params["pageToken"] = page
            data = _get(http, GMAIL + "/messages", token, **params).json()
            ids += [m["id"] for m in data.get("messages", [])]
            page = data.get("nextPageToken")
            if not page:
                break
        for mid in ids[:limit]:
            raw = _get(http, f"{GMAIL}/messages/{mid}", token, format="raw").json().get("raw", "")
            out.append((base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)), folder))
    return out


def fetch_outlook(token: str, days: int = 30, limit: int = 300, http: Optional[httpx.Client] = None) -> list[tuple[bytes, str]]:
    http = http or httpx.Client(timeout=30)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out: list[tuple[bytes, str]] = []
    for box, folder in (("inbox", "Inbox"), ("junkemail", "Spam")):
        url: Optional[str] = f"{GRAPH}/mailFolders/{box}/messages?" + urlencode(
            {"$select": "id", "$top": 100, "$filter": f"receivedDateTime ge {since}"})
        ids: list[str] = []
        while url and len(ids) < limit:
            data = _get(http, url, token).json()
            ids += [m["id"] for m in data.get("value", [])]
            url = data.get("@odata.nextLink")
        out += [(_get(http, f"{GRAPH}/messages/{mid}/$value", token).content, folder) for mid in ids[:limit]]
    return out


YAHOO_FOLDERS = (("INBOX", "Inbox"), ("Bulk", "Spam"))      # Yahoo's IMAP name for the Spam folder is "Bulk"


def _yahoo_box(token: str, http: httpx.Client, imap):
    email = _get(http, YAHOO_USERINFO, token).json().get("email")
    if not email:
        raise MailError("Yahoo didn't share the mailbox address. Reconnect Yahoo Mail")
    box = (imap or (lambda: imaplib.IMAP4_SSL(YAHOO_IMAP, 993, timeout=30)))()
    try:
        box.authenticate("XOAUTH2", lambda _: f"user={email}\x01auth=Bearer {token}\x01\x01".encode())
    except imaplib.IMAP4.error:
        raise MailError("The mailbox connection expired. Reconnect it")
    return box


def fetch_yahoo(token: str, days: int = 30, limit: int = 300, http: Optional[httpx.Client] = None,
                imap: Optional[Callable[[], imaplib.IMAP4]] = None) -> list[tuple[bytes, str]]:
    http = http or httpx.Client(timeout=30)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
    box = _yahoo_box(token, http, imap)
    out: list[tuple[bytes, str]] = []
    try:
        for name, folder in YAHOO_FOLDERS:
            if box.select(name, readonly=True)[0] != "OK":     # read-only: nothing is marked read or changed
                continue
            _, data = box.search(None, "SINCE", since)
            for mid in (data[0] or b"").split()[-limit:]:
                _, parts = box.fetch(mid, "(BODY.PEEK[])")
                out += [(p[1], folder) for p in parts if isinstance(p, tuple)]
        return out
    finally:
        try:
            box.logout()
        except Exception:
            pass


# ---- cleanup (only with the cleanup permission, only when the person presses the button) ----
# Emails are found by their Message-ID and moved, never deleted for good.

def _post(http: httpx.Client, url: str, token: str, body: Optional[dict] = None) -> httpx.Response:
    r = http.post(url, json=body or {}, headers={"Authorization": f"Bearer {token}"})
    if r.status_code in (401, 403):
        raise MailError("Bot Purge doesn't have cleanup permission for this mailbox. Reconnect it with cleanup allowed")
    if r.status_code >= 400:
        raise MailError(f"Mailbox error {r.status_code}: {r.text[:200]}")
    return r


def clean_gmail(token: str, message_ids: list[str], to: str = "trash", http: Optional[httpx.Client] = None, **_) -> int:
    http = http or httpx.Client(timeout=30)
    moved = 0
    for mid in message_ids:
        found = _get(http, GMAIL + "/messages", token, q=f"rfc822msgid:{mid.strip('<>')}", includeSpamTrash="false").json()
        for m in found.get("messages", []):
            if to == "trash":
                _post(http, f"{GMAIL}/messages/{m['id']}/trash", token)
            else:
                _post(http, f"{GMAIL}/messages/{m['id']}/modify", token, {"addLabelIds": ["SPAM"], "removeLabelIds": ["INBOX"]})
            moved += 1
    return moved


def clean_outlook(token: str, message_ids: list[str], to: str = "trash", http: Optional[httpx.Client] = None, **_) -> int:
    http = http or httpx.Client(timeout=30)
    dest = "deleteditems" if to == "trash" else "junkemail"
    moved = 0
    for mid in message_ids:
        mid = "<" + mid.strip("<>") + ">"
        found = _get(http, f"{GRAPH}/messages?" + urlencode({"$select": "id", "$filter": f"internetMessageId eq '{mid}'"}), token).json()
        for m in found.get("value", []):
            _post(http, f"{GRAPH}/messages/{m['id']}/move", token, {"destinationId": dest})
            moved += 1
    return moved


def clean_yahoo(token: str, message_ids: list[str], to: str = "trash", http: Optional[httpx.Client] = None,
                imap: Optional[Callable[[], imaplib.IMAP4]] = None) -> int:
    http = http or httpx.Client(timeout=30)
    dest = "Trash" if to == "trash" else "Bulk"
    box = _yahoo_box(token, http, imap)
    moved = 0
    try:
        if box.select("INBOX")[0] != "OK":
            return 0
        for mid in message_ids:
            _, data = box.uid("SEARCH", None, "HEADER", "Message-ID", "<" + mid.strip("<>") + ">")
            for uid in (data[0] or b"").split():
                if box.uid("MOVE", uid, dest)[0] == "OK":
                    moved += 1
        return moved
    finally:
        try:
            box.logout()
        except Exception:
            pass


CLEANERS: dict[str, Callable[..., int]] = {"gmail": clean_gmail, "outlook": clean_outlook, "yahoo": clean_yahoo}


FETCHERS: dict[str, Callable[..., list[bytes]]] = {"gmail": fetch_gmail, "outlook": fetch_outlook, "yahoo": fetch_yahoo}
