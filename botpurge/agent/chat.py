"""Talk to your agent: fine-tune its settings, directions and objectives in plain words.

"Go slower", "never remove @jenny_r", "always block too", "only 20 at a time",
"keep politics out of my live chat", "my goal is to clear out crypto scammers",
"on TikTok, to block someone: click "Share", then click "Block"".

Every change goes through a small set of tools that do only what the person could do by hand in
the app (no new powers), and the reply says exactly what changed. When the server has an
Anthropic API key the conversation is understood by Claude using those same tools; otherwise a
built-in phrase reader handles the common requests, so the desktop app works offline.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Optional

from .. import security as sec
from ..instructions import PLATFORM_ACTIONS
from .programs import compile_steps

PACES = {"gentle": (3.0, 7.0, 40.0, 90.0), "normal": (1.5, 4.0, 20.0, 45.0), "quick": (0.8, 2.0, 10.0, 20.0)}
DEFAULTS = {"pace": "normal", "max_per_run": 200, "default_actions": ["remove"], "objectives": [], "live": {}}
LIVE_TEACHABLE = {"tiktok": ("live_ban", "live_timeout", "live_delete"), "instagram": ("live_ban",),
                  "facebook": ("live_ban",), "kick": ("live_ban", "live_timeout")}
PLATFORMS = {"tiktok": "tiktok", "tik tok": "tiktok", "instagram": "instagram", "insta": "instagram", "ig": "instagram",
             "facebook": "facebook", "fb": "facebook", "linkedin": "linkedin", "x": "x", "twitter": "x", "kick": "kick"}
ACTION_WORDS = [(r"remove (a |the |my )?followers?|remove them as a follower", "remove_follower"), (r"unfollow", "unfollow"),
                (r"unfriend", "unfriend"), (r"\bban\b", "live_ban"), (r"\b(mute|time ?out)\b", "live_timeout"),
                (r"delete (a |the |their )?(comment|message)", "live_delete"), (r"\bblock\b", "block"), (r"\breport\b", "report")]
LIVE_KEYS = {"mode", "no_politics", "no_abuse", "blocked_phrases"}


def teachable(platform: str) -> tuple:
    return tuple(PLATFORM_ACTIONS.get(platform, ())) + LIVE_TEACHABLE.get(platform, ())


class Toolbox:
    """What the agent may change, all of it the person's own settings."""

    def __init__(self, svc, user_id: str):
        self.svc, self.uid = svc, user_id
        self.changes: list[str] = []

    # settings ---------------------------------------------------------------------
    def prefs(self) -> dict:
        r = self.svc.db.one("SELECT prefs_json FROM agent_prefs WHERE user_id=?", (self.uid,))
        return {**DEFAULTS, **(json.loads(r["prefs_json"]) if r else {})}

    def _save(self, p: dict) -> None:
        self.svc.db.x("INSERT OR REPLACE INTO agent_prefs VALUES (?,?,?)", (self.uid, json.dumps(p), sec.iso()))

    def set_pace(self, pace: str) -> str:
        if pace not in PACES:
            raise ValueError("pace is gentle, normal or quick")
        p = self.prefs(); p["pace"] = pace; self._save(p)
        return self._did(f"Pace set to {pace}" + (" (slower and more human-like)" if pace == "gentle" else ""))

    def set_max_per_run(self, n: int) -> str:
        n = max(1, min(int(n), 1000))
        p = self.prefs(); p["max_per_run"] = n; self._save(p)
        return self._did(f"At most {n} accounts per run")

    def set_default_actions(self, actions: list) -> str:
        acts = [a for a in ("remove", "block", "report") if a in set(actions)] or ["remove"]
        p = self.prefs(); p["default_actions"] = acts; self._save(p)
        return self._did("By default: " + ", ".join(acts))

    def add_objective(self, text: str) -> str:
        text = text.strip().rstrip(".")[:200]
        p = self.prefs()
        if text and text not in p["objectives"]:
            p["objectives"] = (p["objectives"] + [text])[-10:]
            self._save(p)
        return self._did(f"Objective noted: {text}")

    def clear_objectives(self) -> str:
        p = self.prefs(); p["objectives"] = []; self._save(p)
        return self._did("Objectives cleared")

    def set_live_rules(self, **rules) -> str:
        p = self.prefs()
        live = dict(p.get("live") or {})
        for k, v in rules.items():
            if k not in LIVE_KEYS or v is None:
                continue
            if k == "blocked_phrases":
                v = sorted({*live.get("blocked_phrases", []), *[str(x).strip().lower()[:60] for x in v if str(x).strip()]})[:100]
            if k == "mode" and v not in ("watch", "protect"):
                continue
            live[k] = v
        p["live"] = live
        self._save(p)
        said = []
        if "mode" in rules:
            said.append("Live Guard removes bots on its own" if live.get("mode") == "protect" else "Live Guard only watches and logs")
        if "no_politics" in rules:
            said.append("political talk kept out of your live chat" if live.get("no_politics") else "political talk allowed in your live chat")
        if "no_abuse" in rules:
            said.append("insults and personal attacks removed" if live.get("no_abuse") else "insults rule off")
        if rules.get("blocked_phrases"):
            said.append("blocked words: " + ", ".join(rules["blocked_phrases"]))
        return self._did("Live chat: " + "; ".join(said) + " (from your next live)")

    def set_rescan(self, cadence: str) -> str:
        if cadence == "daily":
            self.svc.plans.require(self.uid, "monitor")
        self.svc.personal.set_rescan(self.uid, cadence)
        return self._did(f"Rescans: {cadence}")

    def trust_account(self, name: str, platform: Optional[str] = None) -> str:
        name = name.lstrip("@").strip()
        rows = self.svc.db.q("SELECT DISTINCT platform, account_id FROM flags WHERE user_id=? AND (lower(handle)=? OR lower(account_id)=? OR lower(name)=?)"
                             + (" AND platform=?" if platform else ""),
                             (self.uid, name.lower(), name.lower(), name.lower(), *([platform] if platform else [])))
        if not rows:
            return f"I couldn't find @{name} in your scanned accounts, so nothing changed. Check the spelling, or scan first."
        for r in rows:
            self.svc.personal.feedback(self.uid, r["platform"], r["account_id"], False)
        return self._did(f"@{name} marked as a real person: never flagged or removed")

    def save_directions(self, platform: str, action: str, steps: list) -> str:
        if action not in teachable(platform):
            raise ValueError(f"{platform} doesn't have “{action.replace('_', ' ')}”")
        lines = [s.strip() for s in steps if s and s.strip()]
        ops = [s for s in compile_steps(lines) if s["op"] != "goto"]
        if not ops:
            raise ValueError("I couldn't turn that into steps. Name the buttons in quotes, like: click \"More\", then click \"Block\"")
        self.svc.instructions.submit(self.uid, platform, "web", action, lines)
        return self._did(f"Saved your steps to {action.replace('_', ' ')} on {platform.title()} ({len(lines)} steps). The agent follows them from now on")

    def show_settings(self) -> str:
        p = self.prefs()
        live = p.get("live") or {}
        parts = [f"pace {p['pace']}", f"up to {p['max_per_run']} per run", "default: " + ", ".join(p["default_actions"])]
        if p["objectives"]:
            parts.append("objectives: " + "; ".join(p["objectives"]))
        if live:
            parts.append("live chat: " + ", ".join(f"{k.replace('_', ' ')}={v}" for k, v in live.items()))
        own = [f"{s['platform']} {s['action'].replace('_', ' ')}" for s in self.svc.instructions.list(user_id=self.uid)
               if s.get("author_id") == self.uid]
        if own:
            parts.append("your own steps: " + ", ".join(own))
        return "Your agent: " + "; ".join(parts) + "."

    def _did(self, msg: str) -> str:
        self.changes.append(msg)
        return msg + "."


# ---- the built-in phrase reader (offline) --------------------------------------------

def _platform(t: str) -> Optional[str]:
    for word, p in PLATFORMS.items():
        if re.search(rf"\b{re.escape(word)}\b", t, re.I):
            return p
    return None


def _action(t: str) -> Optional[str]:
    for rx, a in ACTION_WORDS:
        if re.search(rx, t, re.I):
            return a
    return None


def _steps_text(t: str) -> Optional[str]:
    """The directions part of a message: from the first "click/tap/press/type..." on."""
    m = re.search(r"\b(open|click|tap|press|choose|select|type|hover|scroll)\b", t, re.I)
    return t[m.start():] if m else None


def interpret(tb: Toolbox, text: str, context: Optional[dict] = None) -> list[str]:
    t = text.strip()
    low = t.lower()
    ctx = context or {}
    out: list[str] = []

    def run(fn: Callable, *a, **k):
        try:
            out.append(fn(*a, **k))
        except Exception as exc:                         # say why, don't fail the conversation
            out.append(f"I couldn't do that: {getattr(exc, 'detail', None) or exc}")

    # Directions: "on TikTok, to block someone: click "Share", then click "Block""
    steps = _steps_text(t)
    if steps and re.search(r"[\"“'‘]", steps) and (re.search(r"\b(to|how to|when you|steps?)\b", low) or ctx.get("action")):
        platform = _platform(t.split(steps[:10])[0]) or _platform(t) or ctx.get("platform")
        action = _action(t.split(steps[:10])[0]) or ctx.get("action")
        if platform and action:
            lines = [s.strip(" .") + "." for s in re.split(r"\.\s+|\n+|,?\s+then\s+|;\s*", steps) if s.strip(" .")]
            run(tb.save_directions, platform, action, lines)
            return out
        out.append("Which platform and action are these steps for? For example: “On TikTok, to block: click \"Share\", then click \"Block\".”")
        return out

    if re.search(r"\b(slow(er)?( down)?|go slower|gentle|take it easy|more human)\b", low):
        run(tb.set_pace, "gentle")
    elif re.search(r"\b(faster|speed (it )?up|quick(er|ly)?|hurry)\b", low):
        run(tb.set_pace, "quick")
    elif re.search(r"\bnormal (pace|speed)\b", low):
        run(tb.set_pace, "normal")

    m = (re.search(r"\b(?:only|at most|no more than|max(?:imum)?(?: of)?|up to)\s+(?:do\s+|handle\s+|remove\s+)?(\d{1,4})\b", low)
         or re.search(r"\b(\d{1,4})\s+(?:accounts?\s+|people\s+)?(?:at a time|per run|a run)\b", low))
    if m and re.search(r"\b(at a time|per run|each (time|run)|accounts|a run|per day)\b", low):
        run(tb.set_max_per_run, int(m.group(1)))

    acts = set(tb.prefs()["default_actions"])
    changed = False
    for a in ("block", "report"):
        if re.search(rf"\b(always|also|by default|and)\s+(also\s+)?{a}\b|\b{a} (them )?too\b", low):
            acts.add(a); changed = True
        if re.search(rf"\b(don'?t|do not|never|stop|no need to)\s+(\w+\s+)?{a}", low):
            acts.discard(a); changed = True
    if re.search(r"\bjust remove\b|\bonly remove\b", low):
        acts, changed = {"remove"}, True
    if changed:
        run(tb.set_default_actions, sorted(acts))

    for m in re.finditer(r"(?:never|don'?t|do not)\s+(?:remove|block|flag|touch|report)\s+@?([\w.]{2,40})|\btrust\s+@?([\w.]{2,40})|@([\w.]{2,40}) is (?:a )?real", t, re.I):
        name = next(g for g in m.groups() if g)
        if name.lower() not in ("anyone", "people", "them", "real", "my", "friends", "anybody"):
            run(tb.trust_account, name, _platform(t))

    m = re.search(r"\b(daily|weekly|monthly|every (day|week|month))\b", low)
    if m and re.search(r"\b(scan|rescan|check)", low):
        run(tb.set_rescan, {"day": "daily", "week": "weekly", "month": "monthly"}.get(m.group(2) or "", m.group(1)))
    if re.search(r"\b(stop|turn off|no more) (re)?scans?\b", low):
        run(tb.set_rescan, "off")

    live: dict[str, Any] = {}
    if re.search(r"\b(no|keep|without|stop|ban|remove)\b[^.]*\bpolitic", low):
        live["no_politics"] = not re.search(r"\ballow\b[^.]*\bpolitic", low)
    elif re.search(r"\ballow\b[^.]*\bpolitic", low):
        live["no_politics"] = False
    if re.search(r"\b(insults?|abuse|harass\w*|name[- ]calling|personal attacks?)\b", low):
        live["no_abuse"] = not re.search(r"\ballow\b", low)
    words = re.findall(r"(?:block|ban|remove|filter)\s+(?:the\s+)?(?:words?|phrases?)\s+((?:[\"“'‘][^\"”'’]{1,60}[\"”'’][,\s]*(?:and\s+)?)+)", t, re.I)
    if words:
        live["blocked_phrases"] = re.findall(r"[\"“'‘]([^\"”'’]{1,60})[\"”'’]", words[0])
    if re.search(r"\b(automatically|on (its|your) own|without asking|protect mode)\b", low) and re.search(r"\blive|chat|stream", low):
        live["mode"] = "protect"
    elif re.search(r"\b(just|only) (watch|log)\b", low) and re.search(r"\blive|chat|stream", low):
        live["mode"] = "watch"
    if live:
        run(tb.set_live_rules, **live)

    m = re.search(r"\b(?:my (?:goal|objective|priority|aim) is(?: to)?|focus on|i (?:mostly |really )?(?:want|care about|need) to)\s+(.{4,200})", t, re.I)
    if m:
        run(tb.add_objective, m.group(1))
    if re.search(r"\b(clear|forget|reset) (my )?(goals|objectives)\b", low):
        run(tb.clear_objectives)

    if not out and re.search(r"\b(settings|what do you know|what are my|show me my|status)\b", low):
        out.append(tb.show_settings())
    if not out:
        out.append("I can change how I work for you. Try: “go slower”, “only 20 at a time”, “always block too”, "
                   "“never remove @jenny_r”, “scan every week”, “keep politics out of my live chat”, "
                   "“my goal is to clear out crypto scammers”, or give me directions: "
                   "“On TikTok, to block: click \"Share\", then click \"Block\".”")
    return out


# ---- Claude (when the server has an API key) -----------------------------------------

TOOLS = [
    {"name": "set_pace", "description": "How fast the agent works on the platforms.",
     "input_schema": {"type": "object", "properties": {"pace": {"type": "string", "enum": list(PACES)}}, "required": ["pace"]}},
    {"name": "set_max_per_run", "description": "Most accounts the agent handles in one run.",
     "input_schema": {"type": "object", "properties": {"n": {"type": "integer", "minimum": 1, "maximum": 1000}}, "required": ["n"]}},
    {"name": "set_default_actions", "description": "What happens to a flagged account by default (remove is always included).",
     "input_schema": {"type": "object", "properties": {"actions": {"type": "array", "items": {"type": "string", "enum": ["remove", "block", "report"]}}},
                      "required": ["actions"]}},
    {"name": "trust_account", "description": "Mark an account as a real person so it's never flagged or removed.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "platform": {"type": "string"}}, "required": ["name"]}},
    {"name": "set_rescan", "description": "How often the person's networks are rescanned.",
     "input_schema": {"type": "object", "properties": {"cadence": {"type": "string", "enum": ["off", "daily", "weekly", "monthly"]}}, "required": ["cadence"]}},
    {"name": "set_live_rules", "description": "Defaults for Live Guard on the person's next lives. Rules apply the same to every side; never judge opinions.",
     "input_schema": {"type": "object", "properties": {"mode": {"type": "string", "enum": ["watch", "protect"]}, "no_politics": {"type": "boolean"},
                                                       "no_abuse": {"type": "boolean"}, "blocked_phrases": {"type": "array", "items": {"type": "string"}}}}},
    {"name": "add_objective", "description": "Remember what the person wants the agent to focus on.",
     "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "clear_objectives", "description": "Forget the person's objectives.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "save_directions", "description": "Save the person's own step-by-step directions for one action on one platform. "
                                               "Steps are short sentences naming buttons in quotes, e.g. 'Click \"More\".'. Never include passwords or links.",
     "input_schema": {"type": "object", "properties": {"platform": {"type": "string", "enum": ["tiktok", "instagram", "facebook", "linkedin", "x", "kick"]},
                                                       "action": {"type": "string", "enum": sorted({a for p in PLATFORMS.values() for a in teachable(p)})},
                                                       "steps": {"type": "array", "items": {"type": "string"}}}, "required": ["platform", "action", "steps"]}},
    {"name": "show_settings", "description": "Read the agent's current settings.", "input_schema": {"type": "object", "properties": {}}},
]
SYSTEM = ("You are the Bot Purge agent talking with the person you work for. You remove bots, fake accounts and scammers from their "
          "social networks and moderate their live chats. Help them fine-tune how you work, using only the tools. Confirm what you "
          "changed in one or two short sentences. If something is unclear, ask one short question. Never ask for passwords, codes or "
          "payment details. Judge accounts by behaviour, never by opinions.")


def claude_reply(tb: Toolbox, history: list[dict], text: str, http, api_key: str) -> str:
    model = os.environ.get("BOTPURGE_AGENT_MODEL", "claude-sonnet-5-5")
    msgs = [{"role": h["role"], "content": h["text"]} for h in history[-10:]] + [{"role": "user", "content": text}]
    system = SYSTEM + " Current settings: " + tb.show_settings()
    for _ in range(5):
        r = http.post("https://api.anthropic.com/v1/messages", timeout=60,
                      headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                      json={"model": model, "max_tokens": 800, "system": system, "tools": TOOLS, "messages": msgs})
        r.raise_for_status()
        data = r.json()
        msgs.append({"role": "assistant", "content": data["content"]})
        uses = [c for c in data["content"] if c.get("type") == "tool_use"]
        if not uses:
            return "".join(c.get("text", "") for c in data["content"] if c.get("type") == "text").strip()
        results = []
        for u in uses:
            try:
                out = getattr(tb, u["name"])(**(u.get("input") or {}))
                results.append({"type": "tool_result", "tool_use_id": u["id"], "content": out})
            except Exception as exc:
                results.append({"type": "tool_result", "tool_use_id": u["id"], "content": f"Error: {getattr(exc, 'detail', None) or exc}", "is_error": True})
        msgs.append({"role": "user", "content": results})
    return " ".join(tb.changes) or "Done."


def converse(svc, user_id: str, text: str, context: Optional[dict] = None, http=None) -> dict:
    tb = Toolbox(svc, user_id)
    history = [dict(r) for r in svc.db.q("SELECT role, text FROM agent_chat WHERE user_id=? ORDER BY rowid DESC LIMIT 10", (user_id,))][::-1]
    key = os.environ.get("ANTHROPIC_API_KEY")
    reply, engine = None, "built-in"
    if key and http is not None:
        try:
            reply, engine = claude_reply(tb, history, text, http, key), "claude"
        except Exception:
            reply = None                                   # fall back to the built-in reader
    if reply is None:
        reply = " ".join(interpret(tb, text, context))
    at = sec.iso()
    svc.db.x("INSERT INTO agent_chat(user_id, role, text, at) VALUES (?,?,?,?)", (user_id, "user", text[:2000], at))
    svc.db.x("INSERT INTO agent_chat(user_id, role, text, at) VALUES (?,?,?,?)", (user_id, "assistant", reply[:4000], at))
    return {"reply": reply, "changes": tb.changes, "engine": engine, "prefs": tb.prefs()}


def prefs_for(svc, user_id: str) -> dict:
    return Toolbox(svc, user_id).prefs()
