"""The agent reads your connected apps from the platform's own settings page.

For people whose data export doesn't include the list (TikTok's never does) and who don't want to type
it: the agent opens the app-permissions page in its window, signs in the way it always does (the saved
sign-in on the desktop app, or it hands the window to you), and reads the names off the page. What it
finds comes back as a checklist for you to confirm before anything is scored. If the platform has no
such page on the web, or the agent can't find it, it hands the window to you to open it.
"""
from __future__ import annotations

import re
from typing import Optional

from . import signin

APP_PAGES = {
    "x": "https://x.com/settings/connected_apps",
    "facebook": "https://www.facebook.com/settings?tab=applications",
    "instagram": "https://www.instagram.com/accounts/manage_access/",
    "linkedin": "https://www.linkedin.com/mypreferences/d/permitted-services",
    "tiktok": "https://www.tiktok.com/setting",      # no app-permissions page on the web: the agent asks you to open it
}
NO_WEB_PAGE = {"tiktok": "TikTok only shows app permissions in its phone app (Settings and privacy > Security > Manage app permissions). "
                         "If this window shows the list, leave it open and press Resume; otherwise press Resume and type the names in Bot Purge."}

# Words that are page furniture, never app names.
NOISE = re.compile(r"^(settings?|home|log ?in|sign ?in|sign ?up|privacy|security|apps?|apps and (web)?sites|connected apps|manage|remove|revoke|edit|log ?out|"
                   r"sign ?out|active|expired|removed|see all|show more|learn more|help|search|back|next|done|ok|cancel|close|"
                   r"permissions?|access|details|view|more|menu|notifications?|messages?|profile|explore|for you|following|"
                   r"account|password|email|phone|delete|save|apply|permitted services|your (apps|permissions))\W*$", re.I)

READ_JS = """() => {
  const root = document.querySelector('main') || document.body;
  const sel = 'h2,h3,h4,[role=heading],strong,b,li,[role=listitem],[role=row],article,[role=button],a';
  const seen = new Set(), likely = [], other = [];
  const HINT = /permission|access|connected|added|approved|expires|granted|installed|since/i;
  for (const el of root.querySelectorAll(sel)) {
    if (!el.offsetParent) continue;                                        // hidden
    const text = (el.innerText || el.textContent || '').split('\\n').map(s => s.trim()).filter(Boolean)[0] || '';
    if (text.length < 2 || text.length > 60 || seen.has(text.toLowerCase())) continue;
    if (/^https?:|^@|^\\d+$/.test(text)) continue;
    seen.add(text.toLowerCase());
    const box = el.closest('li,[role=listitem],[role=row],article,section') || el.parentElement;
    const around = box ? (box.innerText || '') : '';
    (HINT.test(around) && around.length < 600 ? likely : other).push(text);
  }
  return {likely: likely.slice(0, 60), other: other.slice(0, 120)};
}"""


def _clean(names: list[str]) -> list[str]:
    out, seen = [], set()
    for n in names:
        n = n.strip(" \t-•·:")
        if not n or NOISE.match(n) or n.lower() in seen:
            continue
        seen.add(n.lower())
        out.append(n)
    return out


def read_page(page) -> dict:
    """Names that look like connected apps on the current page, plus the other short texts as candidates."""
    found = page.evaluate(READ_JS)
    likely = _clean(found.get("likely", []))
    other = [n for n in _clean(found.get("other", [])) if n not in likely]
    return {"names": likely, "candidates": other}


def read_apps(page, platform: str, creds: Optional[dict] = None, cockpit=None, settle_ms: int = 2500) -> dict:
    """Open the platform's app-permissions page, sign in if needed, and read the list."""
    url = APP_PAGES.get(platform)
    if not url:
        raise ValueError(f"The agent doesn't know where {platform} lists connected apps")
    if cockpit:
        cockpit.say(f"Reading your connected apps on {platform.title() if platform != 'x' else 'X'}")
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(settle_ms)
    if signin.needs_sign_in(page):
        if creds:
            signin.sign_in(page, platform, creds, cockpit)
        elif cockpit:
            cockpit.hand_over(page, "Sign in here, then press Resume.")
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(settle_ms)
    if platform in NO_WEB_PAGE and cockpit:
        cockpit.hand_over(page, NO_WEB_PAGE[platform])
        page.wait_for_timeout(500)
    out = read_page(page)
    if not out["names"] and cockpit and platform not in NO_WEB_PAGE:
        cockpit.hand_over(page, "I can't find the list here. Open the page that shows your connected apps, then press Resume.")
        page.wait_for_timeout(500)
        out = read_page(page)
    out["url"] = page.url
    return out
