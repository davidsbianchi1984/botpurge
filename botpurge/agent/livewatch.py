"""Live Guard's moderator on TikTok, Instagram, Facebook and Kick live chats.

Those platforms have no moderation API, so the desktop agent does what a human
moderator does. It sits in the live, signed in as the moderator account the
streamer added, and reads each chat message as it appears. Every message goes
through the same Live Guard judge as Twitch and YouTube. When a scammer, spammer
or bot is flagged (and Protect mode is on), the agent removes them right there
in the chat, in a window the streamer can watch and take over (see cockpit.py).

Chat is read from the page itself. Each platform's chat markup is described in
``CHAT``; platforms change their markup, so when no messages can be found the
agent asks the person to click one chat message and learns where the chat is.
"""
from __future__ import annotations

import json
import time
from typing import Optional

from ..liveguard import ChatMessage
from .cockpit import Skipped
from .programs import LIVE_ACTIONS, fill

# Where each platform's chat lives on the live page: one message, and its author inside it.
CHAT = {
    "tiktok": {"item": '[data-e2e="chat-message"]', "author": '[data-e2e="message-owner-name"]'},
    "kick": {"item": "#chatroom-messages [data-index], .chat-entry", "author": ".chat-entry-username, button[title]"},
    "instagram": {"item": '[data-bp-chat], [role="log"] [role="listitem"]', "author": "a, span[dir=auto]"},
    "facebook": {"item": '[data-bp-chat], [role="log"] [role="article"]', "author": "a, span[dir=auto]"},
}

READER = r"""
(sel) => {
  if (window.__bpLive && window.__bpLive.sel.item === sel.item) return window.__bpLive.seenCount;
  const L = window.__bpLive = { sel, q: [], seen: new WeakSet(), seenCount: 0 };
  function read(el) {
    if (L.seen.has(el)) return;
    try { if (el.querySelector(sel.item)) return; } catch (e) {}      // a box holding many messages isn't one message
    L.seen.add(el); L.seenCount++;
    const a = sel.author ? el.querySelector(sel.author) : null;
    const author = (a ? a.innerText : "").trim().split("\n")[0].replace(/^@/, "");
    let text = (el.innerText || "").trim();
    if (author && text.startsWith(author)) text = text.slice(author.length);
    text = text.replace(/^[\s:：\-–]+/, "").trim();
    if (author && text) L.q.push({ author, text: text.slice(0, 500) });
  }
  const scan = () => { try { document.querySelectorAll(sel.item).forEach(read); } catch (e) {} };
  new MutationObserver(scan).observe(document.body, { childList: true, subtree: true });
  scan();
  return L.seenCount;
}
"""
DRAIN = "() => window.__bpLive ? { items: window.__bpLive.q.splice(0), seen: window.__bpLive.seenCount } : null"

# Point-and-learn: from one chat message the person clicks, work out the pattern every message shares.
LEARN = r"""
(el) => {
  const cls = e => (e.className && typeof e.className === "string") ? "." + e.className.trim().split(/\s+/).slice(0, 2).map(CSS.escape).join(".") : "";
  let node = el;
  while (node && node.parentElement && node.parentElement !== document.body) {
    const p = node.parentElement;
    const same = [...p.children].filter(c => c.tagName === node.tagName && cls(c) === cls(node));
    if (same.length >= 3) {
      const parent = p.id ? "#" + CSS.escape(p.id) : p.tagName.toLowerCase() + cls(p);
      const item = parent + " > " + node.tagName.toLowerCase() + cls(node);
      const first = [...node.querySelectorAll("*")].find(c => c.children.length === 0 && c.innerText && c.innerText.trim().length <= 40);
      const author = first ? first.tagName.toLowerCase() + cls(first) : "";
      return { item, author };
    }
    node = p;
  }
  return null;
}
"""


def watch(lg, user_id: str, sid: str, page, platform: str, executor=None, cockpit=None, poll: float = 1.0,
          max_seconds: Optional[float] = None, selectors: Optional[dict] = None, learned_store=None,
          find_timeout: float = 20.0) -> dict:
    """Moderate the live chat on ``page`` until the session stops (or ``max_seconds`` pass)."""
    sel = dict(selectors or CHAT.get(platform) or CHAT["instagram"])
    stats = {"read": 0, "removed": 0, "failed": 0}
    started = time.time()
    last_seen = time.time()
    gone: set = set()        # people already blocked or muted: their queued messages need no second click
    if cockpit:
        cockpit.say("Live Guard is watching the chat")
    while True:
        if max_seconds is not None and time.time() - started > max_seconds:
            break
        if lg.db.one("SELECT state FROM lg_sessions WHERE id=?", (sid,))["state"] != "running" or page.is_closed():
            break
        if cockpit:
            cockpit.checkpoint(page)                 # the streamer took over: wait
        seen = page.evaluate(READER, sel)             # (re)installs itself after the page reloads
        batch = page.evaluate(DRAIN) or {"items": [], "seen": seen}
        if batch["seen"]:
            last_seen = time.time()
        msgs = [ChatMessage(platform=platform, author_id=i["author"].lower(), author_name=i["author"], text=i["text"],
                            at=_now()) for i in batch["items"]]
        stats["read"] += len(msgs)
        for r in (lg.feed(user_id, sid, msgs) if msgs else []):
            if not r["act"]:
                continue
            if r["author_id"] in gone:
                lg.mark_applied(user_id, sid, r["event_id"])
                continue
            prog = LIVE_ACTIONS.get((platform, r["action"])) or (LIVE_ACTIONS.get((platform, "ban")) if r["action"] == "timeout" else None)
            if not prog or executor is None:
                continue                                  # e.g. "delete one message" where the platform has no such option
            if cockpit:
                cockpit.say(f"Removing {r['author_name']}: {r['reasons'][0] if r['reasons'] else 'bot'}")
            res = executor.run(page, fill(prog, {"author_name": r["author_name"], "mute_label": "5 minutes"}))
            lg.mark_applied(user_id, sid, r["event_id"], None if res.ok else (res.error or "failed"))
            stats["removed" if res.ok else "failed"] += 1
            if res.ok and r["action"] in ("ban", "timeout"):
                gone.add(r["author_id"])
            if cockpit:
                cockpit.say("Live Guard is watching the chat")
        # Can't find the chat? Ask the streamer to point at one message, once, and learn the pattern.
        if cockpit and not batch["seen"] and time.time() - last_seen > find_timeout:
            last_seen = time.time()
            page.evaluate("""(learn) => { window.__bpLearned = null; const f = (0, eval)('(' + learn + ')');
                window.addEventListener('click', e => { window.__bpLearned = f(e.target); }, { capture: true, once: true }); }""", LEARN)
            try:
                picked = cockpit.ask_help(page, {"text": "one chat message"})
            except Skipped:
                picked = None
            if picked is not None:
                new = page.evaluate("() => window.__bpLearned")
                if new and new.get("item"):
                    sel = new
                    if learned_store:
                        learned_store(json.dumps(new))
        page.wait_for_timeout(int(poll * 1000))
    return stats


def _now():
    from .. import security as sec

    return sec.now()
