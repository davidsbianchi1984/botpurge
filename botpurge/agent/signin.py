"""Optional saved sign-in for the done-for-you agent.

People choose how the agent gets into their account:

* sign in themselves in the agent's window (the default, nothing saved), or
* save their username and password so the agent signs in on its own. This only exists in the
  desktop app: the details are sealed on that computer, typed straight into the platform's own
  login page, never sent anywhere else and never shown again. When the platform asks for a code,
  a puzzle or "was this you?", the agent hands the window to the person to finish.
"""
from __future__ import annotations

import json
import re
from typing import Optional

LOGIN_URLS = {"instagram": "https://www.instagram.com/accounts/login/", "tiktok": "https://www.tiktok.com/login/phone-or-email/email",
              "facebook": "https://www.facebook.com/login", "linkedin": "https://www.linkedin.com/login", "x": "https://x.com/i/flow/login"}
HOME_URLS = {"instagram": "https://www.instagram.com/", "tiktok": "https://www.tiktok.com/", "facebook": "https://www.facebook.com/",
             "linkedin": "https://www.linkedin.com/feed/", "x": "https://x.com/home", "kick": "https://kick.com/"}

USER_FIELD = ('input[autocomplete="username"], input[name="username"], input[name="email"], input[name="session_key"], '
              'input[name="text"], input[type="email"], input[type="text"]:not([role="combobox"]):not([type="search"])')
PASS_FIELD = 'input[type="password"]'
LOGIN_PATH = re.compile(r"/(login|signin|accounts/login|i/flow/login|checkpoint|challenge|two_step|2fa)", re.I)


def secret_name(platform: str) -> str:
    return f"agent_signin_{platform}"


def pack(username: str, password: str) -> str:
    return json.dumps({"username": username, "password": password})


def unpack(sealed_value: Optional[str]) -> Optional[dict]:
    try:
        d = json.loads(sealed_value or "")
        return d if d.get("username") and d.get("password") else None
    except (ValueError, AttributeError):
        return None


def masked(username: str) -> str:
    if "@" in username:
        name, _, dom = username.partition("@")
        return name[:2] + "•••@" + dom
    return username[:2] + "•••" + username[-1:] if len(username) > 3 else username[:1] + "•••"


def _visible(page, sel: str):
    loc = page.locator(sel)
    for i in range(min(loc.count(), 6)):
        if loc.nth(i).is_visible():
            return loc.nth(i)
    return None


SIGN_IN_BUTTON = re.compile(r"^\s*(log ?in|sign ?in)\s*$", re.I)


def needs_sign_in(page) -> bool:
    if LOGIN_PATH.search(page.url) or _visible(page, PASS_FIELD):
        return True
    for role in ("button", "link"):                  # a signed-out home page offers "Log in"
        loc = page.get_by_role(role, name=SIGN_IN_BUTTON)
        if any(loc.nth(i).is_visible() for i in range(min(loc.count(), 3))):
            return True
    return False


def sign_in(page, platform: str, creds: dict, cockpit=None, settle_ms: int = 4000) -> bool:
    """Type the saved details into the platform's login page. Returns True once signed in."""
    if cockpit:
        cockpit.say(f"Signing in to {platform.title()} with your saved sign-in")
        cockpit.acting(page, True)                   # the agent's own typing is not the person taking over
    try:
        page.goto(LOGIN_URLS[platform], wait_until="domcontentloaded")
        page.wait_for_timeout(1200)
        user = _visible(page, USER_FIELD)
        if user:
            user.fill(creds["username"])
            if not _visible(page, PASS_FIELD):       # two-step forms (X): name first, then the password page
                user.press("Enter")
                page.wait_for_timeout(1500)
        pw = _visible(page, PASS_FIELD)
        if pw:
            pw.fill(creds["password"])
            pw.press("Enter")
            page.wait_for_timeout(settle_ms)
    finally:
        if cockpit:
            cockpit.acting(page, False)
    if not needs_sign_in(page):
        return True
    # A code, a puzzle or a "was this you?" check: that's for the person, never the agent.
    if cockpit:
        cockpit.hand_over(page, "Finish signing in here (code, puzzle or confirmation), then press Resume.")
        return not needs_sign_in(page)
    return False


def ensure_signed_in(page, platform: str, creds: Optional[dict], cockpit=None) -> bool:
    """Before a run: if the account is signed out and a sign-in is saved, sign in."""
    if not creds:
        return True
    page.goto(HOME_URLS[platform], wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    return True if not needs_sign_in(page) else sign_in(page, platform, creds, cockpit)
