"""Step programs: what the agent does on a page, in order.

A program is a list of steps:
    {"op": "goto",   "url": "{profile_url}"}
    {"op": "click",  "text": "Following"}            # visible text / button name
    {"op": "click",  "role": "button", "name": "Unfollow"}
    {"op": "expect", "text": "Follow"}                # verify the result
    {"op": "wait",   "seconds": 2}

Built-in programs cover every platform x action the app offers. Customers can
also write their own steps in plain language; ``compile_steps`` turns lines like
"Tap 'Following'" or "Click Remove, then confirm Remove" into click steps.
"""
from __future__ import annotations

import re
from typing import Optional

BUILTIN: dict[tuple[str, str], list[dict]] = {
    ("instagram", "unfollow"): [
        {"op": "goto", "url": "{profile_url}"},
        {"op": "click", "text": "Following"},
        {"op": "click", "text": "Unfollow"},
        {"op": "expect", "text": "Follow"},
    ],
    ("instagram", "remove_follower"): [
        {"op": "goto", "url": "{own_followers_url}"},
        {"op": "fill", "placeholder": "Search", "value": "{handle}"},
        {"op": "click", "text": "Remove", "near": "{handle}"},
        {"op": "click", "text": "Remove"},
    ],
    ("tiktok", "unfollow"): [
        {"op": "goto", "url": "{profile_url}"},
        {"op": "click", "text": "Following"},
        {"op": "expect", "text": "Follow"},
    ],
    ("tiktok", "remove_follower"): [
        {"op": "goto", "url": "{profile_url}"},
        {"op": "click", "role": "button", "name": "More"},
        {"op": "click", "text": "Remove this follower"},
        {"op": "click", "text": "Remove"},
    ],
    ("facebook", "unfriend"): [
        {"op": "goto", "url": "{profile_url}"},
        {"op": "click", "text": "Friends"},
        {"op": "click", "text": "Unfriend"},
        {"op": "click", "text": "Confirm"},
    ],
    ("facebook", "unfollow"): [
        {"op": "goto", "url": "{profile_url}"},
        {"op": "click", "text": "Following"},
        {"op": "click", "text": "Unfollow"},
    ],
    ("facebook", "remove_follower"): [
        {"op": "goto", "url": "{profile_url}"},
        {"op": "click", "role": "button", "name": "More"},
        {"op": "click", "text": "Block"},
        {"op": "click", "text": "Confirm"},
    ],
    ("linkedin", "unfriend"): [
        {"op": "goto", "url": "{profile_url}"},
        {"op": "click", "text": "More"},
        {"op": "click", "text": "Remove connection"},
        {"op": "click", "text": "Remove"},
    ],
    ("linkedin", "unfollow"): [
        {"op": "goto", "url": "{profile_url}"},
        {"op": "click", "text": "More"},
        {"op": "click", "text": "Unfollow"},
    ],
    ("x", "unfollow"): [
        {"op": "goto", "url": "{profile_url}"},
        {"op": "click", "text": "Following"},
        {"op": "click", "text": "Unfollow"},
    ],
    ("x", "remove_follower"): [
        {"op": "goto", "url": "{own_followers_url}"},
        {"op": "click", "role": "button", "name": "More", "near": "{handle}"},
        {"op": "click", "text": "Remove this follower"},
        {"op": "click", "text": "Remove"},
    ],
}

# Reporting (flagging) an account. Report wizards differ by platform and change often, so these use
# any-of labels and optional screens; the customer's own written steps override them.
REPORT_REASONS = {
    "spam": ["It's spam", "Spam", "Spam or scam", "It's a scam or spam", "Posting spam"],
    "fake": ["Fake account", "It's a fake account", "Fake profile", "This account is fake", "Inauthentic account"],
    "impersonation": ["It's pretending to be someone else", "Pretending to be someone", "Impersonation",
                      "Pretending to be me or someone I know", "They're pretending to be someone else"],
    "scam": ["Scam or fraud", "Fraud or scam", "It's a scam", "Scam", "Fraud"],
}
_MENU = ["Options", "More", "More options", "...", "…", "See options"]
_SUBMIT = ["Submit report", "Submit", "Report", "Next", "Done", "Close"]
REPORT: dict[str, list[dict]] = {
    "instagram": [{"op": "goto", "url": "{profile_url}"}, {"op": "click", "any": _MENU}, {"op": "click", "text": "Report"},
                  {"op": "click", "text": "Report account", "optional": True}, {"op": "select", "any_option": "{reason}"},
                  {"op": "click", "any": _SUBMIT, "optional": True}],
    "tiktok": [{"op": "goto", "url": "{profile_url}"}, {"op": "click", "any": ["Share", *_MENU]}, {"op": "click", "text": "Report"},
               {"op": "click", "text": "Report account", "optional": True}, {"op": "select", "any_option": "{reason}"},
               {"op": "click", "any": _SUBMIT, "optional": True}],
    "facebook": [{"op": "goto", "url": "{profile_url}"}, {"op": "click", "any": _MENU},
                 {"op": "click", "any": ["Find support or report", "Report profile", "Report"]},
                 {"op": "select", "any_option": "{reason}"}, {"op": "click", "any": _SUBMIT, "optional": True},
                 {"op": "click", "any": _SUBMIT, "optional": True}],
    "x": [{"op": "goto", "url": "{profile_url}"}, {"op": "click", "any": _MENU}, {"op": "click", "any": ["Report @{handle}", "Report"]},
          {"op": "select", "any_option": "{reason}"}, {"op": "click", "any": _SUBMIT, "optional": True},
          {"op": "click", "any": _SUBMIT, "optional": True}],
    "linkedin": [{"op": "goto", "url": "{profile_url}"}, {"op": "click", "any": ["More", *_MENU]},
                 {"op": "click", "any": ["Report / Block", "Report or block", "Report"]}, {"op": "click", "text": "Report", "optional": True},
                 {"op": "select", "any_option": "{reason}"}, {"op": "click", "any": _SUBMIT, "optional": True}],
}

_CONFIRM_BLOCK = {"op": "click", "any": ["Block", "Confirm", "Yes, block"], "optional": True}
BLOCK: dict[str, list[dict]] = {
    "instagram": [{"op": "goto", "url": "{profile_url}"}, {"op": "click", "any": _MENU}, {"op": "click", "text": "Block"},
                  {"op": "click", "any": ["Block {handle}", "Block"], "optional": True}, _CONFIRM_BLOCK],
    "tiktok": [{"op": "goto", "url": "{profile_url}"}, {"op": "click", "any": ["Share", *_MENU]}, {"op": "click", "text": "Block"},
               _CONFIRM_BLOCK],
    "facebook": [{"op": "goto", "url": "{profile_url}"}, {"op": "click", "any": _MENU}, {"op": "click", "text": "Block"},
                 {"op": "click", "any": ["Confirm", "Block"], "optional": True}],
    "x": [{"op": "goto", "url": "{profile_url}"}, {"op": "click", "any": _MENU}, {"op": "click", "any": ["Block @{handle}", "Block"]},
          _CONFIRM_BLOCK],
    "linkedin": [{"op": "goto", "url": "{profile_url}"}, {"op": "click", "any": ["More", *_MENU]},
                 {"op": "click", "any": ["Report / Block", "Report or block", "Block"]}, {"op": "click", "text": "Block", "optional": True},
                 _CONFIRM_BLOCK],
}

# Live Guard moderation from a moderator account on platforms with no API.
LIVE_ACTIONS: dict[tuple[str, str], list[dict]] = {
    ("tiktok", "ban"): [{"op": "click", "text": "{author_name}"}, {"op": "click", "text": "Block"}, {"op": "click", "text": "Block"}],
    ("tiktok", "timeout"): [{"op": "click", "text": "{author_name}"}, {"op": "click", "text": "Mute"}, {"op": "click", "text": "{mute_label}"}],
    ("instagram", "ban"): [{"op": "click", "text": "{author_name}"}, {"op": "click", "any": ["Remove", "Block"]},
                           {"op": "click", "any": ["Remove", "Block"], "optional": True}],
    ("facebook", "ban"): [{"op": "click", "text": "{author_name}"}, {"op": "click", "any": ["Remove from live video", "Block", "Remove"]},
                          {"op": "click", "any": ["Remove", "Block", "Confirm"], "optional": True}],
    ("kick", "ban"): [{"op": "click", "text": "{author_name}"}, {"op": "click", "any": ["Ban user", "Ban"]},
                      {"op": "click", "any": ["Ban", "Confirm"], "optional": True}],
    ("kick", "timeout"): [{"op": "click", "text": "{author_name}"}, {"op": "click", "any": ["Timeout", "Time out"]},
                          {"op": "click", "any": ["5 minutes", "Confirm"], "optional": True}],
}

# A quoted label; apostrophes inside words ("It's spam") don't end the quote.
_Q = r"[\"“‘']((?:[^\"”’']|(?<=\w)['’](?=\w)){1,60})[\"”’']"
_QUOTED = re.compile(_Q)
_VERB = re.compile(r"\b(?i:tap|click|press|choose|select|hit|confirm)\b\s+(?:(?i:on)\s+)?(?:(?i:the)\s+)?([A-Z][\w ]{1,40}?)"
                   r"(?=[,.;]|\s+(?:button|again|to|then|and|next|on|in|from|for|at|beside|under|below|if)\b|$)")
_OPEN = re.compile(r"\b(open|go to|visit)\b.*\b(profile|page)\b", re.I)
KEY_TOKEN = r"(?:ctrl|control|cmd|command|win|meta|opt|option|alt|shift|esc|escape|enter|return|tab|space|del|delete|backspace|up|down|left|right|pageup|pagedown|home|end|f\d{1,2}|[a-z0-9])"
KEY_NAMES = {"ctrl", "control", "cmd", "command", "win", "meta", "opt", "option", "alt", "shift", "esc", "escape", "enter",
             "return", "tab", "space", "del", "delete", "backspace", "up", "down", "left", "right", "pageup", "pagedown", "home", "end"}
_PRESS = re.compile(rf"\b(?:press|hit|use the shortcut|keyboard shortcut)\s+(?:the\s+)?({KEY_TOKEN}(?:\s*\+\s*{KEY_TOKEN})*)\b(?:\s+key)?", re.I)
_TYPE = re.compile(rf"\btype\s+{_Q}(?:\s+(?:in|into)\s+(?:the\s+)?(?:{_Q}|([A-Za-z][\w ]*?))(?:\s+(?:box|field|bar))?)?(?=[,.;]|$)", re.I)
_SCROLL = re.compile(rf"\bscroll\s+(down|up)(?:[^.;]*?\buntil\s+(?:you\s+)?(?:see|find)\s+(?:the\s+)?(?:{_Q}|([A-Z][\w ]*?)))?(?=[,.;]|\s+(?:then|and)\b|$)", re.I)
_HOVER = re.compile(rf"\bhover\s+(?:over|on)\s+(?:the\s+)?(?:{_Q}|([A-Za-z][\w ]*?))(?=[,.;]|\s+(?:then|and|to)\b|$)", re.I)
_SELECT = re.compile(rf"\b(?:select|choose|pick)\s+{_Q}(?:\s+(?:from|in)\s+(?:the\s+)?{_Q}?\s*(?:dropdown|list|menu)?)?", re.I)
_SWITCH_NEW = re.compile(r"\b(?:switch|go|move)\s+to\s+(?:the\s+)?(?:new|other|pop-?up)\s+(?:window|tab)", re.I)
_SWITCH_BACK = re.compile(r"\b(?:go|switch|return)\s+back\s+to\s+(?:the\s+)?(?:first|main|original|previous)\s+(?:window|tab)|\bswitch\s+back\b", re.I)
_CLOSE = re.compile(r"\bclose\s+(?:this|the)\s+(?:tab|window|pop-?up)", re.I)
_DIALOG = re.compile(r"\b(accept|ok|confirm|dismiss|cancel)\s+(?:the\s+)?(?:browser\s+)?(?:pop-?up|dialog|alert|prompt|confirmation)", re.I)
_CLAUSES = re.compile(r"\s*(?:,?\s+then\s+|;\s*|,\s+and\s+then\s+|\.\s+)", re.I)
_IF_SHOWN = re.compile(r"\b(if (it'?s |you see it|shown|asked|there|it appears)|when (asked|shown)|optionally)\b", re.I)


def _clause_steps(c: str, first: bool) -> list[dict]:
    """Steps for one clause of a written instruction, in the order they appear."""
    out: list[dict] = []
    optional = bool(_IF_SHOWN.search(c))
    if _OPEN.search(c) and first:
        out.append({"op": "goto", "url": "{profile_url}"})
    if _SWITCH_NEW.search(c):
        return out + [{"op": "switch", "to": "new"}]
    if _SWITCH_BACK.search(c):
        return out + [{"op": "switch", "to": "main"}]
    if _CLOSE.search(c):
        return out + [{"op": "close"}]
    m = _DIALOG.search(c)
    if m:
        out.append({"op": "dialog", "action": "dismiss" if m.group(1).lower() in ("dismiss", "cancel") else "accept"})
        c = c[:m.start()] + c[m.end():]
    m = _PRESS.search(c)
    if m and (m.group(1).lower() in KEY_NAMES or "+" in m.group(1) or len(m.group(1)) == 1
              or re.fullmatch(r"f\d{1,2}", m.group(1), re.I)):     # "press Remove" is a button, "press Esc" a key
        return out + [{"op": "press", "keys": m.group(1)}]
    m = _TYPE.search(c)
    if m:
        target = m.group(2) or m.group(3)
        return out + [{"op": "type", "value": m.group(1), **({"placeholder": target.strip().title()} if target else {})}]
    m = _SCROLL.search(c)
    if m:
        step = {"op": "scroll", "direction": m.group(1).lower()}
        if m.group(2) or m.group(3):
            step["until"] = (m.group(2) or m.group(3)).strip()
        return out + [step]
    m = _HOVER.search(c)
    if m:
        return out + [{"op": "hover", "text": (m.group(1) or m.group(2)).strip()}]
    m = _SELECT.search(c)
    if m:
        step = {"op": "select", "option": m.group(1)}
        if m.group(2):
            step["label"] = m.group(2)
        return out + [step | ({"optional": True} if optional else {})]
    targets = _QUOTED.findall(c) or [x.strip() for x in _VERB.findall(c)]
    for t in targets:
        if t.lower() not in ("the", "a", "it"):
            out.append({"op": "click", "text": t} | ({"optional": True} if optional else {}))
    if not targets and re.search(r"\b(wait|pause)\b", c, re.I):
        out.append({"op": "wait", "seconds": 2})
    return out


def compile_steps(lines: list[str]) -> list[dict]:
    """Turn written steps into a program.

    Understands clicks on quoted or Capitalised buttons, keyboard shortcuts ("Press Ctrl+K",
    "hit Esc"), typing ("Type 'spam' in the Search box"), scrolling ("Scroll down until you see
    'Report'"), hovering, choosing from lists ("Select 'It's spam'"), new windows ("Switch to the
    new window", "Go back to the main window", "Close this tab"), browser pop-ups ("Accept the
    confirmation"), and optional screens ("Tap 'Next' if it's shown").
    """
    program: list[dict] = []
    for raw in lines:
        line = raw.strip().rstrip(".")
        if not line:
            continue
        for clause in [c for c in _CLAUSES.split(line) if c.strip()]:
            program += _clause_steps(clause, first=not program)
    if not program or program[0]["op"] != "goto":
        program.insert(0, {"op": "goto", "url": "{profile_url}"})
    return program


def program_for(platform: str, action: str, custom_steps: Optional[list[str]] = None) -> list[dict]:
    if custom_steps:
        return compile_steps(custom_steps)
    if action in ("report", "block"):
        table = REPORT if action == "report" else BLOCK
        if platform not in table:
            raise ValueError(f"No {action} program for {platform}")
        return [dict(s) for s in table[platform]]
    prog = BUILTIN.get((platform, action))
    if not prog:
        raise ValueError(f"No agent program for {action} on {platform}")
    return [dict(s) for s in prog]


def fill(program: list[dict], values: dict) -> list[dict]:
    out = []
    for s in program:
        t = {}
        for k, v in s.items():
            if k == "any_option":   # a report reason becomes the list of labels platforms use for it
                t["any"] = REPORT_REASONS.get(values.get("reason", "spam"), REPORT_REASONS["spam"])
                continue
            if isinstance(v, str):
                t[k] = v.format(**values)
            elif isinstance(v, list):
                t[k] = [x.format(**values) if isinstance(x, str) else x for x in v]
            else:
                t[k] = v
        out.append(t)
    return out
