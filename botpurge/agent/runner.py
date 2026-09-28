"""Runs step programs in the customer's own browser, carefully.

Safety rails, all enforced here:
  * nothing runs without the customer's recorded consent for that platform;
  * human pace: a random pause between steps, and a longer one between accounts;
  * an hourly cap per platform (well under what the apps tolerate);
  * the moment a platform pushes back ("Action blocked", "Try again later",
    "We restrict certain activity", a CAPTCHA or identity check), the whole
    job pauses and the customer is told, so the agent never grinds an account
    into a restriction.

The browser is a dedicated profile on the customer's computer that they log
into themselves; Bot Purge never sees or stores a password.
"""
from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .programs import fill, program_for

BLOCK_SIGNS = re.compile(
    r"(action blocked|try again later|we restrict certain activity|we limit how often|temporarily (blocked|restricted|limited)|"
    r"unusual activity|suspicious activity|confirm it'?s you|verify (you'?re|that you are) (a )?human|captcha|too many requests|"
    r"you'?re doing that too (fast|much)|slow down|rate limit)", re.I)
LOGIN_SIGNS = re.compile(r"(log ?in|sign ?in)\s+(to|with)|forgot (your )?password|create new account", re.I)

HOURLY_CAP = {"instagram": 30, "tiktok": 30, "facebook": 25, "linkedin": 20, "x": 40}
REPORT_HOURLY_CAP = 10   # reports are slower on purpose: mass-reporting looks (and can be) abusive

CONSENT_TEXT = (
    "The done-for-you agent clicks Remove, Unfollow and Unfriend for you in your own browser. "
    "{platform} does not allow automated actions in its terms, and it can limit or suspend accounts it thinks are automated. "
    "Bot Purge keeps a human pace, stays under an hourly cap and stops the moment {platform} pushes back, but it cannot "
    "guarantee {platform} won't act on your account. You can always use Assisted or Guided removal instead, where you click."
)


@dataclass
class Pacing:
    step_min: float = 1.5
    step_max: float = 4.0
    item_min: float = 20.0
    item_max: float = 45.0
    sleep: Callable[[float], None] = time.sleep

    def step(self):
        self.sleep(random.uniform(self.step_min, self.step_max))

    def item(self):
        self.sleep(random.uniform(self.item_min, self.item_max))


@dataclass
class StepResult:
    ok: bool
    verified: bool = False
    blocked: bool = False
    needs_login: bool = False
    failed_step: Optional[int] = None
    error: str = ""
    log: list[str] = field(default_factory=list)


class Executor:
    """Executes one program on a Playwright page."""

    def __init__(self, pacing: Optional[Pacing] = None, planner: Optional[Callable[[str, dict], Optional[dict]]] = None,
                 timeout_ms: int = 8000):
        self.pacing = pacing or Pacing()
        self.planner = planner          # (visible_text, failed_step) -> replacement step or None
        self.timeout = timeout_ms

    def _locate(self, page, s: dict):
        if s.get("any"):
            # Any of several labels ("Options" / "More" / "…"): the first one on screen wins.
            for label in s["any"]:
                loc = self._locate(page, {k: v for k, v in s.items() if k != "any"} | {"text": label})
                if loc.count() and loc.is_visible():
                    return loc
            return self._locate(page, {k: v for k, v in s.items() if k != "any"} | {"text": s["any"][0]})
        scope = page
        if s.get("near"):
            row = page.locator("li, div[role=listitem], div[role=row], tr, article").filter(has_text=s["near"])
            if row.count():
                scope = row.first
        if s.get("role"):
            loc = scope.get_by_role(s["role"], name=s.get("name"), exact=False)
        elif s.get("placeholder"):
            loc = scope.get_by_placeholder(s["placeholder"])
        else:
            loc = scope.get_by_role("button", name=s["text"], exact=True)
            if not loc.count():
                loc = scope.get_by_text(s["text"], exact=True)
            if not loc.count():
                loc = scope.get_by_text(s["text"])
        return loc.first

    def _pushback(self, page) -> Optional[str]:
        try:
            body = page.locator("body").inner_text(timeout=2000)
        except Exception:
            return None
        m = BLOCK_SIGNS.search(body)
        if m:
            return f"blocked: {m.group(0)}"
        if LOGIN_SIGNS.search(body) and len(body) < 4000:
            return "login"
        return None

    # ---- keyboard ------------------------------------------------------------------
    KEY_NAMES = {"ctrl": "Control", "control": "Control", "cmd": "Meta", "command": "Meta", "win": "Meta", "meta": "Meta",
                 "opt": "Alt", "option": "Alt", "alt": "Alt", "shift": "Shift", "esc": "Escape", "escape": "Escape",
                 "enter": "Enter", "return": "Enter", "tab": "Tab", "space": "Space", "del": "Delete", "delete": "Delete",
                 "backspace": "Backspace", "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight",
                 "pageup": "PageUp", "pagedown": "PageDown", "home": "Home", "end": "End"}

    @classmethod
    def key_combo(cls, keys: str) -> str:
        """'ctrl + k' -> 'Control+k', 'Cmd+Shift+P' -> 'Meta+Shift+P', 'esc' -> 'Escape'."""
        parts = [p.strip() for p in re.split(r"\s*\+\s*", keys.strip()) if p.strip()]
        out = []
        for p in parts:
            low = p.lower()
            if low in cls.KEY_NAMES:
                out.append(cls.KEY_NAMES[low])
            elif re.fullmatch(r"f\d{1,2}", low):
                out.append(low.upper())
            else:
                out.append(p if len(p) > 1 else p.lower() if len(parts) > 1 else p)
        return "+".join(out)

    def _typing(self, loc_or_keyboard, text: str) -> None:
        """Type like a person: one key at a time with small uneven gaps."""
        for ch in text:
            loc_or_keyboard.type(ch, delay=random.uniform(40, 130))

    def run(self, page, program: list[dict]) -> StepResult:
        res = StepResult(ok=False)
        cur = page
        history = [page]
        # A step that accepts browser pop-ups ("Are you sure?") must be armed before the click that opens it.
        if any(s.get("op") == "dialog" for s in program):
            accept = next(s for s in program if s.get("op") == "dialog").get("action", "accept") == "accept"
            page.context.on("dialog", lambda d: d.accept() if accept else d.dismiss())
        for i, s in enumerate(program):
            try:
                op = s["op"]
                if op == "goto":
                    cur.goto(s["url"], wait_until="domcontentloaded")
                elif op == "wait":
                    self.pacing.sleep(float(s.get("seconds", 2)))
                elif op == "dialog":
                    pass  # armed above
                elif op == "press":
                    combo = self.key_combo(s["keys"])
                    if s.get("text") or s.get("role") or s.get("placeholder"):
                        self._locate(cur, s).press(combo)
                    else:
                        cur.keyboard.press(combo)
                elif op == "type":
                    if s.get("placeholder") or s.get("label"):
                        loc = cur.get_by_placeholder(s["placeholder"]) if s.get("placeholder") else cur.get_by_label(s["label"])
                        loc.first.click()
                    self._typing(cur.keyboard, s["value"])
                elif op == "scroll":
                    target = s.get("until")
                    for _ in range(int(s.get("max", 15))):
                        if target and cur.get_by_text(target).first.is_visible():
                            break
                        cur.mouse.wheel(0, 700 if s.get("direction", "down") == "down" else -700)
                        self.pacing.sleep(random.uniform(0.3, 0.9))
                    else:
                        if target:
                            raise RuntimeError(f"scrolled but never saw '{target}'")
                elif op == "switch":
                    pages = cur.context.pages
                    if s.get("to", "new") == "new":
                        deadline = time.time() + self.timeout / 1000
                        while len(cur.context.pages) <= len(history) and time.time() < deadline:
                            cur.wait_for_timeout(100)
                        pages = cur.context.pages
                        if len(pages) <= len(history):
                            raise RuntimeError("no new window opened")
                        cur = pages[-1]
                        history.append(cur)
                    else:
                        cur = history[0]
                    cur.bring_to_front()
                    cur.wait_for_load_state("domcontentloaded")
                elif op == "close":
                    closing = cur
                    history.remove(closing) if closing in history and len(history) > 1 else None
                    cur = history[-1]
                    closing.close()
                elif op in ("click", "fill", "expect", "hover", "select"):
                    if op == "select" and s.get("label"):
                        cur.get_by_label(s["label"]).first.select_option(label=s["option"])
                    else:
                        if op == "select":
                            choices = s.get("any") or [s["option"]]
                            loc = None
                            for opt in choices:
                                cand = cur.get_by_role("option", name=opt).or_(cur.get_by_role("radio", name=opt)) \
                                    .or_(cur.get_by_role("menuitem", name=opt))
                                if cand.count():
                                    loc = cand.first
                                    break
                            if loc is None:
                                loc = self._locate(cur, {"any": choices})
                        else:
                            loc = self._locate(cur, s)
                        try:
                            loc.wait_for(state="visible", timeout=self.timeout)
                        except Exception:
                            alt = self.planner(cur.locator("body").inner_text()[:6000], s) if self.planner else None
                            if not alt:
                                raise
                            res.log.append(f"step {i}: planner replaced {s} with {alt}")
                            s = alt
                            loc = self._locate(cur, s)
                            loc.wait_for(state="visible", timeout=self.timeout)
                        if op in ("click", "select"):
                            loc.click()
                        elif op == "hover":
                            loc.hover()
                        elif op == "fill":
                            loc.click()
                            loc.fill("")
                            self._typing(loc, s["value"])
                        else:
                            res.verified = True
                else:
                    raise ValueError(f"unknown step {op}")
                res.log.append(f"step {i}: {op} ok")
            except Exception as exc:
                if s.get("optional") and not self._pushback(cur):
                    res.log.append(f"step {i}: optional {op} skipped (not on this screen)")
                    continue
                why = self._pushback(cur)
                res.failed_step, res.error = i, (why or f"step {i} ({s.get('op')} {s.get('text') or s.get('name') or s.get('keys') or s.get('option') or ''}) failed: {str(exc)[:150]}")
                res.blocked, res.needs_login = bool(why and why.startswith("blocked")), why == "login"
                return res
            why = self._pushback(cur)
            if why:
                res.failed_step, res.error = i, why
                res.blocked, res.needs_login = why.startswith("blocked"), why == "login"
                return res
            if s["op"] not in ("wait", "dialog") and i < len(program) - 1:
                self.pacing.step()
        res.ok = True
        return res


class AgentService:
    """Consent records, and running removal jobs in agent mode."""

    def __init__(self, db, removal, personal):
        self.db, self.removal, self.personal = db, removal, personal

    # consent ---------------------------------------------------------------------
    def consent_text(self, platform: str) -> str:
        return CONSENT_TEXT.format(platform=platform.title() if platform != "x" else "X")

    def give_consent(self, user_id: str, platform: str) -> dict:
        from .. import security as sec

        self.db.x("INSERT OR REPLACE INTO agent_consents VALUES (?,?,?,?)", (user_id, platform, sec.iso(), self.consent_text(platform)))
        return {"platform": platform, "consented": True}

    def withdraw_consent(self, user_id: str, platform: str) -> None:
        self.db.x("DELETE FROM agent_consents WHERE user_id=? AND platform=?", (user_id, platform))

    def has_consent(self, user_id: str, platform: str) -> bool:
        return bool(self.db.one("SELECT 1 FROM agent_consents WHERE user_id=? AND platform=?", (user_id, platform)))

    # running -------------------------------------------------------------------------
    def _done_this_hour(self, user_id: str, platform: str, reports: bool = False) -> int:
        from datetime import timedelta

        from .. import security as sec

        since = sec.iso(sec.now() - timedelta(hours=1))
        return self.db.one(
            "SELECT COUNT(*) n FROM removal_items i JOIN removal_jobs j ON j.id=i.job_id WHERE j.user_id=? AND i.platform=? "
            "AND i.mode='agent' AND i.status IN ('removed','blocked','pending','failed','reported') AND i.updated_at>=?"
            + (" AND i.action='report'" if reports else ""), (user_id, platform, since))["n"]

    def run_job(self, user_id: str, job_id: str, page, executor: Optional[Executor] = None,
                custom_steps: Optional[dict] = None, own_followers_url: str = "", max_items: int = 1000) -> dict:
        from .. import security as sec

        job = self.removal.job(user_id, job_id)
        if job["mode"] != "agent":
            raise ValueError("this job isn't a done-for-you job")
        if not self.has_consent(user_id, job["platform"]):
            raise PermissionError(f"Consent for the agent on {job['platform']} is needed first")
        ex = executor or Executor()
        cap = HOURLY_CAP.get(job["platform"], 20)
        done = 0
        for it in job["items"]:
            if done >= max_items:
                break
            if it["status"] != "queued":
                continue
            if self.db.one("SELECT state FROM removal_jobs WHERE id=?", (job_id,))["state"] != "running":
                break
            if self._done_this_hour(user_id, job["platform"]) >= cap:
                self._alert(user_id, f"Paused at the hourly limit for {job['platform']} to keep your account safe. It resumes on its own.")
                return {**self.removal.job(user_id, job_id), "paused_for": "hourly_cap"}
            prog = program_for(it["platform"], it["action"], (custom_steps or {}).get(it["action"]))
            if it["action"] == "report" and self._done_this_hour(user_id, job["platform"], reports=True) >= REPORT_HOURLY_CAP:
                self._alert(user_id, f"Paused at {REPORT_HOURLY_CAP} reports an hour on {job['platform']}. It resumes on its own.")
                return {**self.removal.job(user_id, job_id), "paused_for": "hourly_cap"}
            values = {"profile_url": it.get("profile_url") or "", "handle": it.get("handle") or it["account_id"],
                      "own_followers_url": own_followers_url or it.get("profile_url") or "", "reason": it.get("reason") or "spam"}
            res = ex.run(page, fill(prog, values))
            if res.ok:
                status, err = ("removed" if res.verified else "pending"), None
            else:
                status, err = "failed", res.error
            self.removal._finish_item(user_id, job_id, it, status, err, "agent")
            done += 1
            if res.blocked or res.needs_login:
                self.db.x("UPDATE removal_jobs SET state='paused', next_at=? WHERE id=?", (sec.iso(), job_id))
                self._alert(user_id, (f"{job['platform'].title()} pushed back ({res.error}). The agent stopped to protect your account. "
                                      "Wait a day before resuming, or finish with Assisted removal.") if res.blocked
                            else f"Log in to {job['platform'].title()} in the Bot Purge browser, then resume.")
                return {**self.removal.job(user_id, job_id), "stopped": res.error}
            ex.pacing.item()
        self.removal._maybe_done(job_id)
        return self.removal.job(user_id, job_id)

    def _alert(self, user_id: str, text: str) -> None:
        from .. import security as sec

        self.db.x("INSERT INTO alerts VALUES (?,?,?,?,?,?,?,?,0)", (sec.new_id("al_"), user_id, sec.iso(), "agent", text, None, None, None))
