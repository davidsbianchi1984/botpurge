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

# Live Guard moderation from a moderator account on platforms with no API.
LIVE_ACTIONS: dict[tuple[str, str], list[dict]] = {
    ("tiktok", "ban"): [{"op": "click", "text": "{author_name}"}, {"op": "click", "text": "Block"}, {"op": "click", "text": "Block"}],
    ("tiktok", "timeout"): [{"op": "click", "text": "{author_name}"}, {"op": "click", "text": "Mute"}, {"op": "click", "text": "{mute_label}"}],
    ("instagram", "ban"): [{"op": "click", "text": "{author_name}"}, {"op": "click", "text": "Block"}],
}

_QUOTED = re.compile(r"[\"'“‘]([^\"'”’]{1,60})[\"'”’]")
_VERB = re.compile(r"\b(?i:tap|click|press|choose|select|hit|confirm)\b\s+(?:(?i:on)\s+)?(?:(?i:the)\s+)?([A-Z][\w ]{1,40}?)"
                   r"(?=[,.;]|\s+(?:button|again|to|then|and|next|on|in|from|for|at|beside|under|below|if)\b|$)")
_OPEN = re.compile(r"\b(open|go to|visit)\b.*\b(profile|page)\b", re.I)


def compile_steps(lines: list[str]) -> list[dict]:
    """Turn written steps into a program. Quoted words and Capitalised button names become clicks."""
    program: list[dict] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if _OPEN.search(line) and not program:
            program.append({"op": "goto", "url": "{profile_url}"})
        targets = _QUOTED.findall(line)
        if not targets:
            targets = [m.group(1).strip() for m in _VERB.finditer(line)]
        for t in targets:
            if t.lower() in ("the", "a", "it"):
                continue
            program.append({"op": "click", "text": t})
        if not targets and re.search(r"\b(wait|pause)\b", line, re.I):
            program.append({"op": "wait", "seconds": 2})
    if not program or program[0]["op"] != "goto":
        program.insert(0, {"op": "goto", "url": "{profile_url}"})
    return program


def program_for(platform: str, action: str, custom_steps: Optional[list[str]] = None) -> list[dict]:
    if custom_steps:
        return compile_steps(custom_steps)
    prog = BUILTIN.get((platform, action))
    if not prog:
        raise ValueError(f"No agent program for {action} on {platform}")
    return [dict(s) for s in prog]


def fill(program: list[dict], values: dict) -> list[dict]:
    out = []
    for s in program:
        t = {}
        for k, v in s.items():
            t[k] = v.format(**values) if isinstance(v, str) else v
        out.append(t)
    return out
